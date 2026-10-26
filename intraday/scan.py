"""
intraday.scan — 全市场盘中扫描（每 15 分钟一个"当下最强/最弱"榜）

和"收盘评分榜"的区别
--------------------
    收盘评分榜：用日线算指标选股，一天一次，回答"明天该盯谁"
    盘中扫描榜：抓全市场实时快照，每 15 分钟一次，回答"此刻谁在动"

实现方式：把最新日线分片里的全部代码（约 5000 只）分批抓快照（40 只/请求，约 126 次），
按当日涨幅排序取前 N。耗时 20~40 秒，只读公开行情，不消耗 Tushare 额度。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

import config
from intraday.quotes import fetch_quotes

logger = logging.getLogger(__name__)

#: 行业映射的进程内缓存（一次扫描只读一次）
_INDUSTRY_CACHE: dict[str, str] | None = None

#: 扫描结果列
SCAN_COLUMNS = (
    "ts_code", "name", "price", "pct_chg", "turnover_rate",
    "amount_wan", "prev_close", "quote_time",
)


def universe_codes() -> list[str]:
    """取最新日线分片里的全部代码（即当日有行情的股票）。"""
    daily = Path(config.CACHE_DIR) / "daily"
    if not daily.is_dir():
        return []
    shards = sorted(daily.glob("*.csv"))
    if not shards:
        return []
    try:
        df = pd.read_csv(shards[-1], usecols=["ts_code"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取最新日线分片失败：%s", exc)
        return []
    codes = df["ts_code"].astype(str).unique().tolist()
    logger.info("盘中扫描范围：%s 的 %d 只股票", shards[-1].stem, len(codes))
    return codes


def scan_market(codes: list[str] | None = None, source: str | None = None) -> pd.DataFrame:
    """抓全市场快照并整理成 DataFrame（按涨幅降序）。"""
    # 优先东财列表（请求少、对海外机房宽容）；失败再回退逐只快照
    if not codes:
        try:
            em = scan_market_em()
            if not em.empty:
                return em
        except Exception as exc:  # noqa: BLE001
            logger.warning("东财列表不可用，回退逐只快照：%s", exc)

    codes = codes or universe_codes()
    if not codes:
        return pd.DataFrame(columns=list(SCAN_COLUMNS))

    quotes = fetch_quotes(codes, source=source)
    rows = []
    for q in quotes:
        if not (q.price > 0) or not (q.prev_close > 0):
            continue                       # 停牌/无报价
        rows.append({
            "ts_code": q.ts_code,
            "name": q.name,
            "price": q.price,
            "pct_chg": round(q.pct_chg, 2) if q.pct_chg == q.pct_chg else None,
            "turnover_rate": q.turnover_rate if q.turnover_rate == q.turnover_rate else None,
            "amount_wan": q.amount_wan if q.amount_wan == q.amount_wan else None,
            "prev_close": q.prev_close,
            "quote_time": q.stamp.strftime("%Y-%m-%d %H:%M:%S") if q.stamp else "",
        })
    df = pd.DataFrame(rows, columns=list(SCAN_COLUMNS))
    if df.empty:
        return df
    return df.sort_values("pct_chg", ascending=False, kind="mergesort").reset_index(drop=True)


# --------------------------------------------------------------------------
# 东财行情列表（一次 100 行，全市场约 56 页）——比逐只抓快照省 70% 的请求
# --------------------------------------------------------------------------
_EM_URL = ("http://push2.eastmoney.com/api/qt/clist/get?"
           "pn={pn}&pz=100&po={po}&np=1&fltt=2&invt=2&fid=f3"
           "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
           "&fields=f12,f14,f2,f3,f8,f6")


def _ts_code_from_em(code: str) -> str:
    c = str(code).zfill(6)
    if c.startswith(("60", "68", "90")):
        return f"{c}.SH"
    if c.startswith(("00", "30", "20")):
        return f"{c}.SZ"
    if c.startswith(("4", "8", "92")):
        return f"{c}.BJ"
    return f"{c}.SZ"


def scan_market_em(pages_top: int = 3, pages_bottom: int = 1, timeout: int = 15) -> pd.DataFrame:
    """用东财行情列表接口扫全市场（每页 100 只）。

    相比逐只抓腾讯快照（约 126 次请求），这里只要 ~56 次，且东财对海外机房更宽容。
    """
    import urllib.request as _req

    import time as _time

    rows: list[dict] = []
    failed_pages = 0
    # 东财已按涨幅排序：第 1 页就是涨幅榜前 100。
    # 只取"涨幅前 pages_top 页 + 跌幅前 pages_bottom 页"（4 次请求），
    # 不翻全市场 56 页——那样必被限流。
    plan = [(pn, 1) for pn in range(1, pages_top + 1)]
    plan += [(pn, 0) for pn in range(1, pages_bottom + 1)]
    for pn, po in plan:
        url = _EM_URL.format(pn=pn, po=po)
        payload = None
        # 单页最多试 2 次；页间 120ms 间隔，避免被断流
        for attempt in range(2):
            try:
                r = _req.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Referer": "https://quote.eastmoney.com/"})
                with _req.urlopen(r, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8", "replace"))
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 0:
                    _time.sleep(0.4)
                else:
                    failed_pages += 1
                    logger.debug("东财第 %d 页失败：%s", pn, exc)
        if payload is None:
            if failed_pages >= 3:      # 连续失败太多就收手，别死等
                logger.warning("东财列表连续失败 %d 页，提前结束（已取 %d 只）", failed_pages, len(rows))
                break
            continue
        failed_pages = 0
        _time.sleep(0.12)
        data = payload.get("data") or {}
        diff = data.get("diff") or []
        if not diff:
            break
        for x in diff:
            price = x.get("f2")
            if not isinstance(price, (int, float)) or price <= 0:
                continue
            pct = x.get("f3")
            turn = x.get("f8")
            amt = x.get("f6")
            rows.append({
                "ts_code": _ts_code_from_em(x.get("f12")),
                "name": str(x.get("f14") or ""),
                "price": float(price),
                "pct_chg": float(pct) if isinstance(pct, (int, float)) else None,
                "turnover_rate": float(turn) if isinstance(turn, (int, float)) else None,
                "amount_wan": round(float(amt) / 10000.0, 1) if isinstance(amt, (int, float)) else None,
                "prev_close": None,
                "quote_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        if len(diff) < 100:
            continue

    df = pd.DataFrame(rows, columns=list(SCAN_COLUMNS))
    if df.empty:
        return df
    logger.info("东财列表：%d 只（涨幅前 %d 页 + 跌幅前 %d 页）", len(df), pages_top, pages_bottom)
    return df.sort_values("pct_chg", ascending=False, kind="mergesort").reset_index(drop=True)


def top_movers(df: pd.DataFrame, top: int = 20, bottom: int = 10) -> pd.DataFrame:
    """涨幅前 top + 跌幅前 bottom（去重）。"""
    if df.empty:
        return df
    head = df.head(top)
    tail = df.tail(bottom)
    out = pd.concat([head, tail]).drop_duplicates(subset=["ts_code"], keep="first")
    return out.reset_index(drop=True)


def save_scan(df: pd.DataFrame, name: str = "") -> Path | None:
    """落盘到 output/reports/intraday_scan_YYYYMMDD_HHMMSS.csv（可指定 latest 名称）。"""
    if df is None or df.empty:
        return None
    path = Path(config.REPORT_DIR) / f"intraday_scan_{datetime.now():%Y%m%d_%H%M%S}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    latest = Path(config.REPORT_DIR) / (name or "intraday_scan_latest.csv")
    df.to_csv(latest, index=False, encoding="utf-8-sig")
    return path


def format_table(df: pd.DataFrame, limit: int = 30) -> str:
    """把扫描结果排成等宽文本（命令行用）。"""
    if df.empty:
        return "（无数据）"
    lines = [
        f"{'代码':<11}{'名称':<9}{'现价':>8}{'涨跌%':>8}{'换手%':>8}{'成交额(万)':>12}",
        "-" * 58,
    ]
    for row in df.head(limit).itertuples(index=False):
        lines.append(
            f"{row.ts_code:<11}{str(row.name)[:8]:<9}{_f(row.price):>8}"
            f"{_f(row.pct_chg, '+.2f'):>8}{_f(row.turnover_rate):>8}{_f(row.amount_wan, ',.0f'):>12}"
        )
    return "\n".join(lines)


def _f(value, spec: str = ".2f") -> str:
    try:
        return format(float(value), spec)
    except (TypeError, ValueError):
        return "-"


# --------------------------------------------------------------------------
# 行业优先：先排行业，再在强行业里给个股打分
# --------------------------------------------------------------------------
RANK_COLUMNS = (
    "rank", "industry", "industry_rank", "ts_code", "name", "score",
    "pct_chg", "turnover_rate", "amount_wan", "daily_score", "tier",
    "ma20_live", "dist_ma20_pct", "ma20_side",
    "vr_15m", "macd_15m", "kdj_15m",
    "bar_time", "bar_close", "bar_pct",
)


def load_industry() -> dict[str, str]:
    """ts_code -> 行业（来自 stock_basic 缓存，用 LocalCache API 读，进程内缓存）。"""
    global _INDUSTRY_CACHE
    if _INDUSTRY_CACHE is not None:
        return _INDUSTRY_CACHE
    try:
        from data.cache import LocalCache

        df = LocalCache().read("stock_basic")
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取行业失败：%s", exc)
        _INDUSTRY_CACHE = {}
        return _INDUSTRY_CACHE
    if df is None or df.empty or "industry" not in df.columns:
        _INDUSTRY_CACHE = {}
        return _INDUSTRY_CACHE
    out = {}
    for r in df.itertuples(index=False):
        ind = str(getattr(r, "industry", "") or "").strip()
        out[str(r.ts_code)] = ind if ind and ind.lower() != "nan" else "未分类"
    _INDUSTRY_CACHE = out
    logger.info("行业映射：%d 只股票，%d 个行业",
                len(out), len({v for v in out.values() if v != "未分类"}))
    return out


def load_recent_closes(days: int = 19) -> dict[str, list[float]]:
    """取每只股票最近 N 个交易日的收盘价（用于近似计算"实时 MA20"）。

    实时 MA20 = (最近 19 个收盘 + 现价) / 20 —— 纯本地计算，不需要分钟线。
    """
    daily = Path(config.CACHE_DIR) / "daily"
    if not daily.is_dir():
        return {}
    shards = sorted(daily.glob("*.csv"))[-(days + 1):]
    if not shards:
        return {}
    frames = []
    for shard in shards:
        try:
            frames.append(pd.read_csv(shard, usecols=["ts_code", "close"]))
        except Exception:  # noqa: BLE001
            continue
    if not frames:
        return {}
    df = pd.concat(frames, ignore_index=True)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    out: dict[str, list[float]] = {}
    for code, grp in df.groupby("ts_code", sort=False):
        vals = grp["close"].dropna().tolist()
        if len(vals) >= days:
            out[str(code)] = vals[-days:]
    logger.info("实时MA20：用了 %d 个分片，覆盖 %d 只（每只 %d 个收盘价）",
                len(shards), len(out), days)
    return out


def _live_ma20(price: float, closes: list[float] | None, days: int = 19) -> float | None:
    """(最近 19 个收盘 + 现价) / 20。数据不足返回 None。"""
    if not closes or len(closes) < days or not (price > 0):
        return None
    return (sum(closes[-days:]) + price) / (days + 1)


def load_daily_scores() -> dict[str, dict]:
    """最近一次日线榜的评分/档位（作为盘中评分的"底分"）。"""
    report_dir = Path(config.REPORT_DIR)
    if not report_dir.is_dir():
        return {}
    for path in sorted([p for p in report_dir.glob("*.csv")
                        if p.name.startswith(("screen_", "replay_"))],
                       key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
        try:
            head = pd.read_csv(path, nrows=1)
        except Exception:  # noqa: BLE001
            continue
        if "score" not in head.columns:
            continue
        try:
            df = pd.read_csv(path)
        except Exception:  # noqa: BLE001
            continue
        out = {}
        for r in df.itertuples(index=False):
            d = r._asdict()
            out[str(d.get("ts_code"))] = {
                "daily_score": float(d.get("score") or 0),
                "tier": str(d.get("tier") or ""),
            }
        logger.info("盘中评分底分：取自 %s（%d 只）", path.name, len(out))
        return out
    return {}


def _pct_rank(series: pd.Series) -> pd.Series:
    """横截面百分位（0~1），NaN 记为 0。"""
    return series.rank(pct=True, method="average").fillna(0.0)


def score_market(df: pd.DataFrame) -> pd.DataFrame:
    """给全市场快照打"盘中分"，并标注行业与行业排名。

    盘中分（满分 100）：
        涨幅     45  —— 当日涨幅在横截面中的百分位
        换手     20  —— 换手率百分位（资金活跃度）
        量能     15  —— 成交额百分位（真金白银）
        日线底分 10  —— 昨天日线评分 /100 × 10（结构质量）
        MA20 位置 10 —— 现价站在"实时 MA20"上方给满，跌破给 0
    """
    if df is None or df.empty:
        return df
    out = df.copy()
    industry = load_industry()
    daily = load_daily_scores()
    closes = load_recent_closes(19)

    out["industry"] = out["ts_code"].map(industry).fillna("未分类")
    out["daily_score"] = out["ts_code"].map(
        lambda c: (daily.get(c) or {}).get("daily_score", 0.0)).astype(float)
    out["tier"] = out["ts_code"].map(lambda c: (daily.get(c) or {}).get("tier", "")).fillna("")

    pct = pd.to_numeric(out["pct_chg"], errors="coerce")
    turn = pd.to_numeric(out["turnover_rate"], errors="coerce")
    amt = pd.to_numeric(out["amount_wan"], errors="coerce")
    price = pd.to_numeric(out["price"], errors="coerce")

    # ---- 实时 MA20（近 19 日收盘 + 现价）----
    ma20_live = [
        _live_ma20(p, closes.get(c))
        for c, p in zip(out["ts_code"].astype(str), price)
    ]
    out["ma20_live"] = [round(v, 3) if v else None for v in ma20_live]
    out["dist_ma20_pct"] = [
        round((p / m - 1) * 100, 2) if (m and p == p and p > 0) else None
        for p, m in zip(price, ma20_live)
    ]
    out["ma20_side"] = [
        ("上方" if (m and p == p and p >= m) else ("下方" if m else "—"))
        for p, m in zip(price, ma20_live)
    ]
    above = (out["ma20_side"] == "上方").astype(float)

    score = (
        _pct_rank(pct) * 45.0
        + _pct_rank(turn) * 20.0
        + _pct_rank(amt) * 15.0
        + (out["daily_score"].clip(0, 100) / 100.0) * 10.0
        + above * 10.0
    )
    out["score"] = score.round(1)

    # 行业强度：行业平均涨幅 + 上涨家数占比，合成 0~1 的强度值
    grp = out.groupby("industry")
    strength = pd.DataFrame({
        "avg_pct": grp["pct_chg"].mean(),
        "median_pct": grp["pct_chg"].median(),
        "up_ratio": grp["pct_chg"].apply(lambda s: float((s > 0).mean())),
        "count": grp["ts_code"].count(),
        "amount": grp["amount_wan"].sum(),
        "best": grp.apply(lambda g: g.loc[g["pct_chg"].idxmax(), "name"], include_groups=False),
    })
    # 只保留成分股 >= 5 的行业，避免一只票撑起一个行业
    strength = strength[strength["count"] >= 5].copy()
    strength["strength"] = (_pct_rank(strength["avg_pct"]) * 0.6
                            + _pct_rank(strength["up_ratio"]) * 0.4)
    strength = strength.sort_values(["strength", "avg_pct"], ascending=False)
    strength["industry_rank"] = range(1, len(strength) + 1)

    out["industry_rank"] = out["industry"].map(strength["industry_rank"]).fillna(999).astype(int)
    out["industry_name"] = out["industry"]
    out.attrs["industry_strength"] = strength.reset_index()
    return out


def pick_by_industry(scored: pd.DataFrame, top_industries: int = 5,
                     per_industry: int = 5, max_total: int = 20) -> pd.DataFrame:
    """先选强行业（前 N），再在行业内部按盘中分排序，每行业最多取 M 只。"""
    if scored is None or scored.empty:
        return scored
    picked = []
    for _, grp in scored.groupby("industry_rank"):
        if len(picked) >= top_industries:
            break
        sub = grp.sort_values("score", ascending=False).head(per_industry).copy()
        sub.attrs = {}                     # concat 时不能带 DataFrame 型 attrs
        picked.append(sub)
    if not picked:
        return scored.head(0)
    out = pd.concat(picked).sort_values(
        ["industry_rank", "score"], ascending=[True, False]).reset_index(drop=True)
    out = out.head(max_total).reset_index(drop=True)
    out["rank"] = range(1, len(out) + 1)
    return out


def enrich_picks(picks: pd.DataFrame) -> pd.DataFrame:
    """给选出的股票补 15 分钟指标（量比/MACD/KDJ）——只抓这几十只，秒级。"""
    if picks is None or picks.empty:
        return picks
    from intraday.kline import enrich

    info = enrich(picks["ts_code"].astype(str).tolist())
    out = picks.copy()
    for col, key in (("vr_15m", "vr_15m"), ("macd_15m", "macd_15m"),
                     ("kdj_15m", "kdj_15m"), ("dif_15m", "dif_15m"),
                     ("dea_15m", "dea_15m"), ("k_15m", "k_15m"), ("d_15m", "d_15m"),
                     ("bar_time", "bar_time"), ("bar_open", "bar_open"),
                     ("bar_close", "bar_close"), ("bar_pct", "bar_pct")):
        out[col] = out["ts_code"].astype(str).map(lambda c: (info.get(c) or {}).get(key))
    return out


def industry_table(scored: pd.DataFrame, top: int = 10) -> pd.DataFrame:
    """行业强度表（给界面/命令行看）。"""
    strength = scored.attrs.get("industry_strength")
    if strength is None:
        return pd.DataFrame()
    cols = ["industry_rank", "industry", "count", "avg_pct", "up_ratio", "best", "amount", "strength"]
    cols = [c for c in cols if c in strength.columns]
    return strength[cols].head(top).reset_index(drop=True)


__all__ = [
    "universe_codes", "scan_market", "scan_market_em", "top_movers", "save_scan", "format_table",
    "score_market", "pick_by_industry", "industry_table", "enrich_picks", "load_industry",
    "load_daily_scores", "SCAN_COLUMNS", "RANK_COLUMNS",
]
