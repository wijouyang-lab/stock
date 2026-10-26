"""
trigger.scheduler — 触发时机决策

数据更新节奏（决定了触发时间）：

    交易日 15:00~17:00   日线行情入库
    交易日 17:00~19:00   daily_basic（PE/PB/换手率/市值）更新
    披露当晚 21:00~23:00 三大报表更新
    18:00~22:00          A股清算期间数据逐步稳定

因此本项目设定：

    22:30  主触发  mode=full   全市场扫描（含财务数据）
    17:30  辅助触发 mode=quote  纯行情扫描（不含财务数据）

并且**禁止盘中触发**（09:15~15:05 一律拒绝执行）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from typing import Any

from config import (
    MARKET_CLOSE_TIME,
    MARKET_OPEN_TIME,
    MODE_FULL,
    MODE_QUOTE,
    SCHEDULE_FULL_TIME,
    SCHEDULE_QUOTE_TIME,
)
from trigger.calendar import TradeCalendar

logger = logging.getLogger(__name__)


def _parse_hhmm(value: str) -> dtime:
    h, m = value.strip().split(":")[:2]
    return dtime(int(h), int(m))


MARKET_OPEN = _parse_hhmm(MARKET_OPEN_TIME)
MARKET_CLOSE = _parse_hhmm(MARKET_CLOSE_TIME)
FULL_TIME = _parse_hhmm(SCHEDULE_FULL_TIME)
QUOTE_TIME = _parse_hhmm(SCHEDULE_QUOTE_TIME)

#: 财务数据开始可用的时间（披露当晚 21:00 起）
FINANCIAL_READY = dtime(21, 0)
#: 日线行情开始可用的时间（15:00 起）
DAILY_READY = dtime(15, 0)
#: daily_basic 开始可用的时间（17:00 起）
DAILY_BASIC_READY = dtime(17, 0)


@dataclass
class TriggerDecision:
    """一次触发决策的结果。"""

    should_run: bool
    mode: str = MODE_QUOTE
    trade_date: str = ""
    reason: str = ""
    in_market_hours: bool = False
    is_trading_day: bool = False
    now: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        verdict = "执行" if self.should_run else "跳过"
        return (
            f"[{verdict}] mode={self.mode} trade_date={self.trade_date} "
            f"交易日={self.is_trading_day} 盘中={self.in_market_hours} | {self.reason}"
        )


class Scheduler:
    """决定"现在该不该跑、跑哪个模式"。"""

    def __init__(self, calendar: TradeCalendar | None = None) -> None:
        self.calendar = calendar or TradeCalendar()

    # ------------------------------------------------------------ 时段判断
    @staticmethod
    def in_market_hours(now: datetime | None = None) -> bool:
        """是否处于禁止触发的盘中时段（09:15~15:05）。"""
        t = (now or datetime.now()).time()
        return MARKET_OPEN <= t <= MARKET_CLOSE

    @staticmethod
    def mode_for_now(now: datetime | None = None) -> str:
        """按当前时间推断应使用的模式（>=21:00 用 full，否则 quote）。"""
        t = (now or datetime.now()).time()
        return MODE_FULL if t >= FINANCIAL_READY else MODE_QUOTE

    def decide(
        self,
        mode: str | None = None,
        now: datetime | None = None,
        enforce_window: bool = False,
        probe_calendar: bool = True,
    ) -> TriggerDecision:
        """给出触发决策。

        参数
        ----
        mode            : 指定模式（None = 按当前时间自动判断）
        now             : 注入"当前时间"便于测试
        enforce_window  : True 时严格执行"交易日 + 非盘中 + 数据已入库"三重判断
                          （计划任务场景）；手工运行时通常传 False
        probe_calendar  : 是否允许联网探测交易日
        """
        now = now or datetime.now()
        cal = self.calendar
        is_trading = cal.is_trading_day(now, probe=probe_calendar)
        latest = cal.latest_trade_date(ref=now, probe=probe_calendar)
        in_market = self.in_market_hours(now)
        chosen = mode or self.mode_for_now(now)

        d = TriggerDecision(
            should_run=True,
            mode=chosen,
            trade_date=latest,
            is_trading_day=is_trading,
            in_market_hours=in_market,
        )

        if not enforce_window:
            d.reason = "手工运行，跳过时间窗口检查"
            if in_market:
                d.warnings.append(
                    "当前处于盘中时段（09:15~15:05）：日线尚未入库，"
                    "本次结果很可能基于上一个交易日的数据"
                )
            return d

        # ---- 以下为计划任务场景的严格判断 ----
        if not is_trading:
            d.should_run = False
            d.reason = f"{now:%Y-%m-%d} 非交易日，跳过"
            return d

        if in_market:
            d.should_run = False
            d.reason = (
                f"当前 {now:%H:%M} 处于盘中时段（{MARKET_OPEN_TIME}~{MARKET_CLOSE_TIME}），"
                "禁止触发"
            )
            return d

        t = now.time()
        if chosen == MODE_FULL:
            if t < FINANCIAL_READY:
                d.should_run = False
                d.reason = (
                    f"当前 {now:%H:%M} 早于财务数据可用的 21:00，"
                    "full 模式会拿到不完整的财务数据；请改在 22:30 之后运行"
                )
                return d
            d.reason = "交易日 22:30 主触发：全市场扫描（含财务数据）"
        else:
            if t < DAILY_BASIC_READY:
                d.should_run = False
                d.reason = (
                    f"当前 {now:%H:%M} 早于 daily_basic 入库的 17:00，纯行情扫描数据不完整"
                )
                return d
            d.reason = "交易日 17:30 辅助触发：纯行情扫描（不含财务数据）"
            d.warnings.append("quote 模式不含财务数据，如需完整扫描请等待 22:30 主触发")
        return d

    def explain(self) -> dict[str, Any]:
        """当前调度状态说明（GUI / --self-test 展示用）。"""
        now = datetime.now()
        d = self.decide(now=now, enforce_window=False)
        return {
            "now": d.now,
            "is_trading_day": d.is_trading_day,
            "in_market_hours": d.in_market_hours,
            "suggested_mode": d.mode,
            "latest_trade_date": d.trade_date,
            "full_time": SCHEDULE_FULL_TIME,
            "quote_time": SCHEDULE_QUOTE_TIME,
            "market_hours_blocked": f"{MARKET_OPEN_TIME}~{MARKET_CLOSE_TIME}",
            "calendar": self.calendar.stats(),
        }


__all__ = ["Scheduler", "TriggerDecision", "MARKET_OPEN", "MARKET_CLOSE"]
