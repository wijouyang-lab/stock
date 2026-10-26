"""
trigger — 触发与调度

对外主要入口：

    from trigger import TradeCalendar, Scheduler, TriggerDecision

    sched = Scheduler()
    decision = sched.decide()          # 现在该不该跑？跑哪个模式？
    if decision.should_run:
        ...

职责划分：
    calendar.py   交易日判定（用 daily 探测，不依赖 trade_cal 权限）
    scheduler.py  触发时机决策（触发时间窗口、盘中保护、模式选择）

注意：真正的"定时"由 Windows 计划任务负责（scripts/register_task.bat），
本包只负责"被唤醒后，判断这次唤醒该不该干活"。
"""

from __future__ import annotations

from trigger.calendar import TradeCalendar, is_weekend, to_yyyymmdd
from trigger.scheduler import Scheduler, TriggerDecision

__all__ = ["TradeCalendar", "to_yyyymmdd", "is_weekend", "Scheduler", "TriggerDecision"]
