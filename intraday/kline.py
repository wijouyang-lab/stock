"""
intraday.kline — 15 分钟 K 线与盘中指标（量比 / MACD / KDJ）

数据来源
--------
东方财富公开 K 线接口（`push2his.eastmoney.com`，klt=15 即 15 分钟），
不需要 Token。**只对"行业优先选股"选出的那 20 只抓**。

频率控制（很重要）
------------------
该接口对短时高频请求会直接断开连接，所以这里做了三层保护：
    1. **本地缓存**：同一只股票 10 分钟内只抓一次（15 分钟 K 线本来就不会变）；
    2. **请求间隔**：每只之间 0.25 秒，避免突发；
    3. **重试**：失败后 1s / 2s 退避重试，最多 3 次；网络全挂时退回过期缓存。

为什么需要它
------------
腾讯/新浪的实时快照只给"当日累计成交量"，算不出量比、MACD、KDJ。
东财 K 线正好补上这块，指标计算直接复用 `indicators.momentum` 的同一套函数
（12/26/9 与 9/3/3），保证与日线口径一致。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd

import config

logger = logging.getLogger(__name__)

_URL = (
    "http://push2his.eastmoney.com/api/qt/stock/kline/get?"
    "secid={secid}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57&"
    "klt=15&fqt=1&end=20500101&lmt={limit}"
)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://quote.eastmoney.com/",
}
#: 缓存时长（秒）。15 分钟 K 线 10 分钟内不会变，够用且能挡掉重复请求。
CACHE_TTL = 600
#: 每只之间的间隔（秒）
REQUEST_GAP = 0.25


def _cache_path(ts_code: str) -> Path:
    return Path(config.CACHE_DIR) / "_kline15" / f"{ts_code}.json"


def to_secid(ts_code: str) -> str:
    """``600000.SH`` -> ``1.600000``；``000001.SZ`` -> ``0.000001``。"""
    code, _, suffix = str(ts_code).upper().partition(".")
    market = "1" if suffix in {"SH", "SS"} else "0"
    return f"{market}.{code}"


def _fetch_raw(ts_code: str, limit: int, timeout: int, retries: int = 3) -> list[str]:
    """抓原始 klines 列表（带重试与缓存）。"""
    cache = _cache_path(ts_code)
    try:
        if cache.is_file() and (time.time() - cache.stat().st_mtime) < CACHE_TTL:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if cached:
                return cached
    except Exception:  # noqa: BLE001
        pass

    url = _URL.format(secid=to_secid(ts_code), limit=int(limit))
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            klines = (payload.get("data") or {}).get("klines") or []
            if klines:
                try:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(json.dumps(klines), encoding="utf-8")
                except Exception:  # noqa: BLE001
                    pass
            return klines
        except Exception as exc:  # noqa: BLE001
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
            else:
                logger.debug("K线抓取失败 %s：%s", ts_code, exc)
    # 网络失败时退回过期缓存（总比没有强）
    try:
        if cache.is_file():
            return json.loads(cache.read_text(encoding="utf-8")) or []
    except Exception:  # noqa: BLE001
        pass
    return []


_SINA_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData?symbol={symbol}&scale=15&ma=no&datalen={limit}"
)


def _sina_symbol(ts_code: str) -> str:
    code, _, suffix = str(ts_code).upper().partition(".")
    return f"{suffix.lower()}{code}" if suffix else code.lower()


def _fetch_sina(ts_code: str, limit: int = 120, timeout: int = 8) -> list[dict]:
    """新浪 15 分钟 K 线（备用源）。返回 [{dt,open,close,high,low,vol}]。"""
    url = _SINA_URL.format(symbol=_sina_symbol(ts_code), limit=int(limit))
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            data = json.loads(raw) if raw.strip().startswith("[") else []
            rows = []
            for item in data:
                try:
                    rows.append({
                        "dt": datetime.strptime(str(item["day"])[:19], "%Y-%m-%d %H:%M:%S"),
                        "open": float(item["open"]),
                        "close": float(item["close"]),
                        "high": float(item["high"]),
                        "low": float(item["low"]),
                        "vol": float(item["volume"]),
                    })
                except (KeyError, TypeError, ValueError):
                    continue
            if rows:
                return rows
        except Exception as exc:  # noqa: BLE001
            if attempt == 0:
                time.sleep(0.8)
            else:
                logger.debug("新浪K线失败 %s：%s", ts_code, exc)
    return []


def fetch_15min(ts_code: str, limit: int = 120, timeout: int = 8) -> pd.DataFrame:
    """抓取 15 分钟 K 线，返回 DataFrame(dt, open, close, high, low, vol)。

    最后一根若仍在形成中（结束时间 > 现在）会被丢掉。
    """
    rows = []
    now = datetime.now()

    klines = _fetch_raw(ts_code, limit, timeout)
    if not klines:
        # 东财被限频时回退新浪
        rows = [r for r in _fetch_sina(ts_code, limit, timeout) if r["dt"] <= now]
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows).sort_values("dt").reset_index(drop=True)

    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 7:
            continue
        try:
            stamp = datetime.strptime(parts[0], "%Y-%m-%d %H:%M")
            if stamp > now:                 # 未收盘的 K 线
                continue
            rows.append({
                "dt": stamp,
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "vol": float(parts[5]),
                "amount": float(parts[6]),
            })
        except (ValueError, IndexError):
            continue
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("dt").reset_index(drop=True)


def indicators_15m(df: pd.DataFrame) -> dict:
    """基于 15 分钟 K 线算：量比、MACD、KDJ。数据不足返回 {}。"""
    if df is None or len(df) < 34:      # MACD(26) 至少要 34 根才有意义
        return {}
    from indicators.momentum import kdj, macd

    work = df.copy()
    work = macd(work)                   # 12/26/9
    work = kdj(work)                    # 9/3/3
    last = work.iloc[-1]

    vol = work["vol"].astype(float)
    base = vol.iloc[-6:-1].mean() if len(vol) >= 6 else float("nan")
    vr = float(vol.iloc[-1] / base) if (base == base and base > 0) else float("nan")

    dif, dea = float(last["dif"]), float(last["dea"])
    k, d = float(last["k"]), float(last["d"])

    def ok(*vals: float) -> bool:
        return all(v == v for v in vals)          # NaN != NaN

    has_macd, has_kdj = ok(dif, dea), ok(k, d)
    info = {
        "vr_15m": round(vr, 2) if ok(vr) else None,
        # 指标算不出来时给 "-"，不要误报成"空头/死叉"
        "macd_15m": ("多头" if dif > dea else "空头") if has_macd else "-",
        "dif_15m": round(dif, 3) if has_macd else None,
        "dea_15m": round(dea, 3) if has_macd else None,
        "kdj_15m": ("金叉" if k > d else "死叉") if has_kdj else "-",
        "k_15m": round(k, 1) if has_kdj else None,
        "d_15m": round(d, 1) if has_kdj else None,
        "bars_15m": int(len(work)),
    }
    # 本根 15 分钟 K 线的开/收/自身涨跌幅（给界面显示"这根线怎么走的"）
    try:
        bar_open = float(last["open"])
        bar_close = float(last["close"])
        info.update({
            "bar_time": last["dt"].strftime("%H:%M") if hasattr(last["dt"], "strftime") else "",
            "bar_open": round(bar_open, 2),
            "bar_close": round(bar_close, 2),
            "bar_pct": (round((bar_close / bar_open - 1) * 100, 2)
                        if bar_open > 0 else None),
        })
    except Exception:  # noqa: BLE001
        pass
    return info


def enrich(codes: list[str], limit: int = 120) -> dict[str, dict]:
    """批量抓 15 分钟 K 线并算指标（失败的单只跳过，不影响整体）。"""
    out: dict[str, dict] = {}
    failed: list[str] = []
    for i, code in enumerate(codes):
        try:
            info = indicators_15m(fetch_15min(code, limit=limit))
            if info:
                out[code] = info
            else:
                failed.append(code)
        except Exception as exc:  # noqa: BLE001
            failed.append(code)
            logger.debug("15分钟K线失败 %s：%s", code, exc)
        if i < len(codes) - 1:
            time.sleep(REQUEST_GAP)
    if len(out) < len(codes):
        logger.warning("15分钟指标：%d/%d 只成功（%d 只数据不足或抓取失败）",
                       len(out), len(codes), len(failed))
    else:
        logger.info("15分钟指标：%d/%d 只成功", len(out), len(codes))
    return out


__all__ = ["to_secid", "fetch_15min", "indicators_15m", "enrich", "CACHE_TTL"]
