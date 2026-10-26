"""
intraday.quotes — 盘中实时行情快照（免费源，不需要任何 Token）

为什么不用 Tushare
------------------
Tushare 的 `daily` 是日线，收盘后才入库；分钟线/实时行情（`stk_mins`/`rt_k`）
在 `config.FORBIDDEN_APIS` 里被明确禁用（2000 积分也拿不到）。
所以盘中盯梢改用**免费公开快照接口**：

    tencent  http://qt.gtimg.cn/q=sh600000,sz000001     （主，字段最全）
    sina     http://hq.sinajs.cn/list=sh600000          （备，字段较少）

两者都是 GET 文本、GBK 编码、无需 Key、不产生任何模型调用。
**只抓取已在榜单里的几十只股票**，不做全市场扫描。

字段对照（腾讯，`v_sh600000="..."` 内以 `~` 分隔）
--------------------------------------------------
    [1] 名称       [2] 代码        [3] 现价      [4] 昨收      [5] 今开
    [6] 成交量(手) [30] 时间戳     [31] 涨跌     [32] 涨跌幅%   [33] 最高  [34] 最低
    [37] 成交额(万) [38] 换手率%   [45] 总市值(亿) [47] 涨停价  [48] 跌停价

（以上索引已用真实响应逐位核对，见 README 10.x 与自检 ``_check_quote_parse``。）
"""

from __future__ import annotations

import logging
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from config import (
    INTRADAY_BATCH_SIZE,
    INTRADAY_QUOTE_SOURCE,
    INTRADAY_QUOTE_TIMEOUT,
)

logger = logging.getLogger(__name__)

#: 允许的行情源
SOURCES: tuple[str, ...] = ("tencent", "sina")

#: 请求头（两个源都要求一个像样的 UA；新浪还需要 Referer）
_HEADERS: dict[str, dict[str, str]] = {
    "tencent": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://gu.qq.com/",
    },
    "sina": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://finance.sina.com.cn",
    },
}

_URLS: dict[str, str] = {
    "tencent": "http://qt.gtimg.cn/q={codes}",
    "sina": "http://hq.sinajs.cn/list={codes}",
}


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class Quote:
    """一只股票的盘中快照。"""

    ts_code: str
    name: str
    price: float
    prev_close: float
    open: float = float("nan")
    high: float = float("nan")
    low: float = float("nan")
    volume_lots: float = float("nan")     # 成交量（手）
    amount_wan: float = float("nan")      # 成交额（万元）
    turnover_rate: float = float("nan")   # 换手率 %
    limit_up: float = float("nan")
    limit_down: float = float("nan")
    stamp: datetime | None = None         # 行情时间

    @property
    def pct_chg(self) -> float:
        """当日涨跌幅（%），由现价与昨收计算，不依赖接口给的字段。"""
        if not self.prev_close or self.prev_close != self.prev_close:
            return float("nan")
        return (self.price / self.prev_close - 1.0) * 100.0

    def as_dict(self) -> dict:
        return {
            "ts_code": self.ts_code,
            "name": self.name,
            "price": self.price,
            "prev_close": self.prev_close,
            "pct_chg": round(self.pct_chg, 2) if self.pct_chg == self.pct_chg else None,
            "volume_lots": self.volume_lots,
            "amount_wan": self.amount_wan,
            "turnover_rate": self.turnover_rate,
            "limit_up": self.limit_up,
            "limit_down": self.limit_down,
            "stamp": self.stamp.strftime("%Y-%m-%d %H:%M:%S") if self.stamp else None,
        }


# --------------------------------------------------------------------------
# 代码转换
# --------------------------------------------------------------------------
def to_symbol(ts_code: str) -> str:
    """``600000.SH`` → ``sh600000``（腾讯/新浪都用这个写法）。"""
    code, _, suffix = str(ts_code).strip().upper().partition(".")
    if not suffix:
        return code.lower()
    return f"{suffix.lower()}{code}"


def from_symbol(symbol: str) -> str:
    """``sh600000`` → ``600000.SH``。"""
    s = str(symbol).strip().lower()
    m = re.match(r"^(sh|sz|bj)(\d{6})$", s)
    if not m:
        return s.upper()
    return f"{m.group(2)}.{m.group(1).upper()}"


def _f(value: str, default: float = float("nan")) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_stamp(text: str) -> datetime | None:
    """``20261023115438`` → datetime；解析失败返回 None。"""
    s = str(text).strip()
    if len(s) < 14 or not s.isdigit():
        return None
    try:
        return datetime.strptime(s[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------
def parse_tencent(text: str) -> list[Quote]:
    """解析腾讯 ``v_sh600000="..."`` 响应。"""
    quotes: list[Quote] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if "=" not in line or '"' not in line:
            continue
        var, _, raw = line.partition("=")
        symbol = var.strip()[2:] if var.strip().lower().startswith("v_") else var.strip()
        body = raw.strip().strip(";").strip('"')
        f = body.split("~")
        if len(f) < 49:
            logger.debug("腾讯行情字段不足，跳过：%s", symbol)
            continue
        quotes.append(
            Quote(
                ts_code=from_symbol(symbol),
                name=f[1].strip(),
                price=_f(f[3]),
                prev_close=_f(f[4]),
                open=_f(f[5]),
                high=_f(f[33]),
                low=_f(f[34]),
                volume_lots=_f(f[6]),
                amount_wan=_f(f[37]),
                turnover_rate=_f(f[38]),
                limit_up=_f(f[47]),
                limit_down=_f(f[48]),
                stamp=_parse_stamp(f[30]),
            )
        )
    return quotes


def parse_sina(text: str) -> list[Quote]:
    """解析新浪 ``var hq_str_sh600000="..."`` 响应（无涨跌停/换手率字段）。"""
    quotes: list[Quote] = []
    for line in (text or "").splitlines():
        m = re.search(r'hq_str_([a-z]{2}\d{6})="([^"]*)"', line)
        if not m:
            continue
        symbol, body = m.group(1), m.group(2)
        f = body.split(",")
        if len(f) < 32:
            logger.debug("新浪行情字段不足，跳过：%s", symbol)
            continue
        stamp = _parse_stamp(f[30] + f[31]) if f[30] and f[31] else None
        quotes.append(
            Quote(
                ts_code=from_symbol(symbol),
                name=f[0].strip(),
                price=_f(f[3]),
                prev_close=_f(f[2]),
                open=_f(f[1]),
                high=_f(f[4]),
                low=_f(f[5]),
                volume_lots=_f(f[8]) / 100.0,     # 新浪给的是「股」，换算成「手」
                amount_wan=_f(f[9]) / 10000.0,    # 元 → 万元
                turnover_rate=float("nan"),
                limit_up=float("nan"),
                limit_down=float("nan"),
                stamp=stamp,
            )
        )
    return quotes


_PARSERS = {"tencent": parse_tencent, "sina": parse_sina}


# --------------------------------------------------------------------------
# 抓取
# --------------------------------------------------------------------------
def _http_get(url: str, headers: dict[str, str], timeout: int) -> str:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("gbk", errors="replace")


def fetch_quotes(
    codes: Sequence[str],
    source: str | None = None,
    timeout: int | None = None,
) -> list[Quote]:
    """批量抓取行情快照（自动分批、失败回退到另一个源）。

    入参 ``codes`` 是 Tushare 风格代码（``600000.SH``），返回 ``Quote`` 列表。
    网络失败时抛 ``RuntimeError``（由调用方决定是否继续下一轮）。
    """
    codes = [str(c).strip() for c in codes if str(c).strip()]
    if not codes:
        return []

    src = (source or INTRADAY_QUOTE_SOURCE or "tencent").lower()
    if src not in SOURCES:
        logger.warning("未知行情源 %s，回退 tencent", src)
        src = "tencent"
    timeout = int(timeout or INTRADAY_QUOTE_TIMEOUT)
    batch = max(1, int(INTRADAY_BATCH_SIZE))

    out: list[Quote] = []
    errors: list[str] = []
    for i in range(0, len(codes), batch):
        chunk = codes[i : i + batch]
        params = ",".join(to_symbol(c) for c in chunk)
        for attempt_src in (src, *[s for s in SOURCES if s != src]):
            url = _URLS[attempt_src].format(codes=params)
            try:
                text = _http_get(url, _HEADERS[attempt_src], timeout)
                got = _PARSERS[attempt_src](text)
                if got:
                    out.extend(got)
                    break
                errors.append(f"{attempt_src}: 返回空/解析失败")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{attempt_src}: {type(exc).__name__}: {exc}")
                logger.debug("行情源 %s 失败：%s", attempt_src, exc)
    if not out:
        raise RuntimeError("行情获取失败：" + "; ".join(errors[:3]))
    return out
