"""
intraday.rules — 盘中告警规则（全部是确定性阈值判断，零模型参与）

规则一览
--------
| 规则 | 条件 | 级别 |
| --- | --- | --- |
| `break_ma20`     | 现价 < 扫描日的 MA20 | risk |
| `break_neckline` | 现价 < 双底颈线（形态失效） | risk |
| `drop`           | 当日涨跌幅 ≤ -5% | risk |
| `surge`          | 当日涨跌幅 ≥ +7% | opp |
| `near_limit_up`  | 现价距涨停 ≤ 2% | opp |
| `near_limit_down`| 现价距跌停 ≤ 2% | risk |
| `volume_burst`   | 当日成交量 / 扫描日成交量 ≥ 1.0 | info |

阈值全部来自 ``config.INTRADAY_RULES``，可以逐条关掉（置 False 或 0）。

注意：``ma20`` / ``neckline`` 是**扫描日（基准日）价格空间**里的值。
基准日当天 qfq == 原始价，所以价格可直接比较；但若个股在扫描日之后除权，
原始价会向下跳空，可能误触 `break_ma20`。收盘扫描每天 22:30 自动刷新名单，
所以这个偏移最多持续一个交易日（README 里已注明）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import config
from intraday.quotes import Quote
from intraday.watchlist import WatchItem

#: 规则中文名（打印/CSV 用）
RULE_LABELS: dict[str, str] = {
    "break_ma20": "跌破MA20",
    "break_neckline": "跌破颈线",
    "drop": "急跌",
    "surge": "拉升",
    "near_limit_up": "逼近涨停",
    "near_limit_down": "逼近跌停",
    "volume_burst": "放量",
}

#: 级别权重（越大越靠前展示）
LEVEL_ORDER: dict[str, int] = {"risk": 3, "opp": 2, "info": 1}
LEVEL_LABELS: dict[str, str] = {"risk": "风险", "opp": "机会", "info": "提示"}


@dataclass
class Alert:
    """一条盘中告警。"""

    ts_code: str
    name: str
    rule: str
    level: str
    message: str
    price: float
    pct_chg: float
    score: float = float("nan")
    tier: str = ""
    fired_at: str = ""

    def __post_init__(self) -> None:
        if not self.fired_at:
            self.fired_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @property
    def label(self) -> str:
        return RULE_LABELS.get(self.rule, self.rule)

    def as_row(self, trade_date: str = "") -> dict[str, Any]:
        return {
            "fired_at": self.fired_at,
            "trade_date": trade_date,
            "ts_code": self.ts_code,
            "name": self.name,
            "rule": self.rule,
            "rule_label": self.label,
            "level": self.level,
            "level_label": LEVEL_LABELS.get(self.level, self.level),
            "message": self.message,
            "price": round(self.price, 3) if self.price == self.price else None,
            "pct_chg": round(self.pct_chg, 2) if self.pct_chg == self.pct_chg else None,
            "score": self.score if self.score == self.score else None,
            "tier": self.tier,
        }


def _enabled(value: Any) -> bool:
    """规则开关：False / 0 / None 视为关闭。"""
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and value == 0:
        return False
    return True


def _finite(*values: float) -> bool:
    return all(v == v for v in values)     # NaN != NaN


def evaluate(
    item: WatchItem,
    quote: Quote,
    rules: dict[str, Any] | None = None,
) -> list[Alert]:
    """对一只股票评估全部规则，返回命中的告警列表（可能为空）。"""
    cfg = dict(config.INTRADAY_RULES if rules is None else rules)
    price = float(quote.price)
    pct = float(quote.pct_chg)
    out: list[Alert] = []

    if not _finite(price) or price <= 0:
        return out

    def add(rule: str, level: str, message: str) -> None:
        out.append(
            Alert(
                ts_code=item.ts_code,
                name=quote.name or item.name,
                rule=rule,
                level=level,
                message=message,
                price=price,
                pct_chg=pct,
                score=item.score,
                tier=item.tier,
            )
        )

    if _enabled(cfg.get("break_ma20")) and _finite(item.ma20) and item.ma20 > 0:
        if price < item.ma20:
            gap = (price / item.ma20 - 1.0) * 100.0
            add("break_ma20", "risk", f"现价 {price:.2f} 跌破 MA20 {item.ma20:.2f}（{gap:+.2f}%）")

    if _enabled(cfg.get("break_neckline")) and _finite(item.neckline) and item.neckline > 0:
        if price < item.neckline:
            gap = (price / item.neckline - 1.0) * 100.0
            add("break_neckline", "risk", f"现价 {price:.2f} 跌破颈线 {item.neckline:.2f}（{gap:+.2f}%）→ 双底形态失效")

    drop = cfg.get("drop_pct")
    if _enabled(drop) and _finite(pct) and pct <= float(drop):
        add("drop", "risk", f"当日跌幅 {pct:+.2f}%（阈值 {float(drop):+.1f}%）")

    surge = cfg.get("surge_pct")
    if _enabled(surge) and _finite(pct) and pct >= float(surge):
        add("surge", "opp", f"当日涨幅 {pct:+.2f}%（阈值 {float(surge):+.1f}%）")

    near = cfg.get("near_limit_pct")
    if _enabled(near) and float(near) > 0:
        tol = float(near) / 100.0
        if _finite(quote.limit_up) and quote.limit_up > 0 and price >= quote.limit_up * (1 - tol):
            add("near_limit_up", "opp", f"现价 {price:.2f} 逼近涨停 {quote.limit_up:.2f}")
        if _finite(quote.limit_down) and quote.limit_down > 0 and price <= quote.limit_down * (1 + tol):
            add("near_limit_down", "risk", f"现价 {price:.2f} 逼近跌停 {quote.limit_down:.2f}")

    ratio_thr = cfg.get("volume_burst_ratio")
    if _enabled(ratio_thr) and float(ratio_thr) > 0:
        base = item.volume
        if _finite(base, quote.volume_lots) and base > 0 and quote.volume_lots > 0:
            ratio = quote.volume_lots / base
            if ratio >= float(ratio_thr):
                add("volume_burst", "info",
                    f"当日成交 {quote.volume_lots:.0f} 手 / 扫描日 {base:.0f} 手 = {ratio:.2f} 倍")

    return out


def sort_alerts(alerts: list[Alert]) -> list[Alert]:
    """按级别（风险优先）再按涨跌幅排序。"""
    return sorted(
        alerts,
        key=lambda a: (-LEVEL_ORDER.get(a.level, 0), a.pct_chg if a.pct_chg == a.pct_chg else 0.0),
    )


__all__ = ["Alert", "RULE_LABELS", "LEVEL_LABELS", "evaluate", "sort_alerts"]
