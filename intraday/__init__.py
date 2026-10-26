"""
intraday — 盘中盯梢（免费行情快照，零大模型调用）

    from intraday import cmd_watch
    cmd_watch(top_n=30)

模块划分：
    quotes.py     实时快照抓取与解析（腾讯主 / 新浪备，无需 Token）
    watchlist.py  从最近一次 --score 榜单里取前 N 只作为盯梢范围
    rules.py      7 条确定性告警规则（跌破MA20/跌破颈线/急跌/拉升/涨跌停/放量）
    monitor.py    一轮检查 / 常驻循环 / 时段判断 / 告警落盘去重

设计约束（与项目其它部分一致）：
- **不调用任何大模型 API**，全部是阈值比较，不产生模型 token；
- 盘中**不重算全市场**，只盯名单里几十只，秒级完成；
- 依赖的联网主机仅 ``qt.gtimg.cn`` / ``hq.sinajs.cn``（自检会校验白名单）；
- 不在交易时段默认不跑（``--allow-outside-session`` 可强制一次）。
"""

from __future__ import annotations

from intraday.monitor import (
    alert_path,
    cmd_watch,
    in_session,
    next_session_hint,
    run_cycle,
    run_loop,
    sessions,
)
from intraday.quotes import Quote, fetch_quotes, from_symbol, to_symbol
from intraday.rules import Alert, evaluate, sort_alerts
from intraday.watchlist import Watchlist, WatchItem, load_watchlist

__all__ = [
    "Quote", "fetch_quotes", "to_symbol", "from_symbol",
    "WatchItem", "Watchlist", "load_watchlist",
    "Alert", "evaluate", "sort_alerts",
    "in_session", "next_session_hint", "sessions", "alert_path",
    "run_cycle", "run_loop", "cmd_watch",
]
