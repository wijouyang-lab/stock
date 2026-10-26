"""
screener.signals — 日线买卖信号（**T+1 感知**，每个交易日只评估一次）

为什么要单独做
--------------
评分榜告诉你"哪些股票值得盯"，但不告诉你"什么时候进、什么时候出"。
盘中每 15 分钟发一次买卖信号会变成噪音，而且**A 股是 T+1**——今天买的明天才能卖，
盘中发"卖出"根本没有可执行性。

规则（全部基于**日线收盘**，一天最多一个信号）
------------------------------------------
买入（空仓 → 持仓）
    `double_bottom_10d` 为真（双底突破确认）
    且 `close > ma20`
    且 `volume_ratio_5 >= 1.5`（放量）
卖出（持仓 → 空仓）
    `close < db_neckline`（跌破颈线，形态失效）
    或 `close < ma20`（趋势走坏）

T+1 约束
--------
- 买入当天：即使触发卖出条件，也只标记 `T+1锁定`，不改状态；
- 次一交易日起才允许真正卖出。

去重
----
状态文件里记 `last_eval`（最后评估的交易日）。同一天重复运行（刷新界面、多跑几次）
不会重复发信号、也不会重复累加持仓天数。

状态持久化
----------
`output/signals/signals.json`：持仓、成本、买入日、已持天数、信号历史。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

import config

logger = logging.getLogger(__name__)

#: 信号取值
SIG_BUY = "买入"
SIG_SELL = "卖出"
SIG_HOLD = "持有"
SIG_LOCKED = "T+1锁定"
SIG_FLAT = "空仓"


@dataclass
class Position:
    """一笔持仓（只记自己的信号系统，不涉及真实账户）。"""

    ts_code: str
    name: str = ""
    entry_date: str = ""
    entry_price: float = float("nan")
    entry_score: float = float("nan")
    days_held: int = 0
    last_eval: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class SignalState:
    """全部持仓 + 运行元信息。"""

    positions: dict[str, dict] = field(default_factory=dict)
    last_eval: str = ""
    history: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "SignalState":
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("信号状态文件损坏，重新开始：%s", exc)
            return cls()
        return cls(
            positions=dict(raw.get("positions") or {}),
            last_eval=str(raw.get("last_eval") or ""),
            history=list(raw.get("history") or []),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "positions": self.positions,
            "last_eval": self.last_eval,
            "history": self.history[-500:],          # 只留最近 500 条
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def state_path() -> Path:
    return Path(config.OUTPUT_DIR) / "signals" / "signals.json"


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    try:
        return bool(value)
    except Exception:  # noqa: BLE001
        return False


def update(
    snapshot: pd.DataFrame,
    trade_date: str,
    path: Path | None = None,
    force: bool = False,
    max_buys: int | None = None,
) -> tuple[pd.DataFrame, list[dict]]:
    """按当日快照更新信号，返回 (带信号列的 DataFrame, 本次产生的事件列表)。

    ``snapshot`` 需要列：ts_code, close, ma20, db_neckline, double_bottom_10d,
    volume_ratio_5（可选 name / score）。

    每个交易日**最多** ``max_buys`` 笔买入（按评分从高到低取），
    避免某天几十只同时满足条件、信号刷屏。
    """
    path = Path(path) if path else state_path()
    state = SignalState.load(path)
    already = (state.last_eval == trade_date) and not force
    limit = int(config.SIGNAL_MAX_BUYS_PER_DAY if max_buys is None else max_buys)
    out = snapshot.copy()
    events: list[dict] = []

    # ---- 先算"今天谁有资格买"，按评分取前 limit 只 ----
    allowed: set[str] = set()
    if not already:
        cand: list[tuple[float, str]] = []
        for row in snapshot.itertuples(index=False):
            d = row._asdict()
            code = str(d.get("ts_code", ""))
            close, ma20, vr = _num(d.get("close")), _num(d.get("ma20")), _num(d.get("volume_ratio_5"))
            if code in state.positions:
                continue                       # 已持仓的不再占买入名额
            if (_truthy(d.get("double_bottom_10d")) and close == close and ma20 == ma20
                    and close > ma20 and vr == vr and vr >= config.SIGNAL_VOLUME_RATIO):
                cand.append((_num(d.get("score")) if _num(d.get("score")) == _num(d.get("score")) else 0.0, code))
        cand.sort(reverse=True)
        allowed = {code for _, code in cand[:max(0, limit)]}

    signals: list[str] = []
    notes: list[str] = []
    days: list[int] = []
    entries: list[str] = []

    for row in snapshot.itertuples(index=False):
        d = row._asdict()
        code = str(d.get("ts_code", ""))
        close = _num(d.get("close"))
        ma20 = _num(d.get("ma20"))
        neck = _num(d.get("db_neckline"))
        vr = _num(d.get("volume_ratio_5"))
        breakout = _truthy(d.get("double_bottom_10d"))
        pos = state.positions.get(code)

        buy_ok = (
            breakout and close == close and ma20 == ma20
            and close > ma20 and vr == vr and vr >= config.SIGNAL_VOLUME_RATIO
            and (already or code in allowed)          # 受每日买入名额限制
        )
        sell_reason = ""
        if pos:
            if neck == neck and close < neck:
                sell_reason = "跌破颈线"
            elif ma20 == ma20 and close < ma20:
                sell_reason = "跌破MA20"

        if pos is None:
            if buy_ok and not already:
                state.positions[code] = Position(
                    ts_code=code,
                    name=str(d.get("name", "")),
                    entry_date=trade_date,
                    entry_price=close,
                    entry_score=_num(d.get("score")),
                    days_held=0,
                    last_eval=trade_date,
                ).as_dict()
                signals.append(SIG_BUY)
                notes.append(f"双底突破+站上MA20+量比{vr:.1f}")
                days.append(0)
                entries.append(trade_date)
                events.append({"date": trade_date, "ts_code": code,
                               "name": str(d.get("name", "")), "signal": SIG_BUY,
                               "price": close, "note": notes[-1]})
            else:
                signals.append(SIG_FLAT)
                notes.append("" if already else "未满足买入条件")
                days.append(0)
                entries.append("")
        else:
            entry_date = str(pos.get("entry_date") or "")
            if not already:
                pos["days_held"] = int(pos.get("days_held") or 0) + 1
                pos["last_eval"] = trade_date
                state.positions[code] = pos

            if sell_reason and entry_date == trade_date:
                # T+1：今天买的，今天不能卖
                signals.append(SIG_LOCKED)
                notes.append(f"{sell_reason}，但今日买入（T+1），最早明日卖出")
                days.append(int(pos.get("days_held") or 0))
                entries.append(entry_date)
            elif sell_reason and not already:
                del state.positions[code]
                signals.append(SIG_SELL)
                notes.append(sell_reason)
                days.append(int(pos.get("days_held") or 0))
                entries.append(entry_date)
                events.append({"date": trade_date, "ts_code": code,
                               "name": str(d.get("name", "")), "signal": SIG_SELL,
                               "price": close, "note": sell_reason})
            else:
                signals.append(SIG_HOLD)
                notes.append("持有中")
                days.append(int(pos.get("days_held") or 0))
                entries.append(entry_date)

    out["signal"] = signals
    out["signal_note"] = notes
    out["hold_days"] = days
    out["entry_date"] = entries
    out["t1_blocked"] = [s == SIG_LOCKED for s in signals]

    if not already:
        state.last_eval = trade_date
        state.history.extend(events)
        state.save(path)
        logger.info("信号：%d 笔事件（持仓 %d 只），交易日 %s",
                    len(events), len(state.positions), trade_date)
    return out, events


def open_positions(path: Path | None = None) -> dict[str, dict]:
    return SignalState.load(Path(path) if path else state_path()).positions


__all__ = [
    "SIG_BUY", "SIG_SELL", "SIG_HOLD", "SIG_LOCKED", "SIG_FLAT",
    "Position", "SignalState", "state_path", "update", "open_positions",
]
