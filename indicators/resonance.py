"""
indicators.resonance — 日 / 周 / 月 三周期共振

口径：某周期"共振成立" = **收盘价 > MA20** 且 **DIF > DEA**

- MA20 为该周期收盘价的 20 期简单均线（周线 20 周、月线 20 月）；
- DIF/DEA 为该周期上的 MACD(12/26/9)，与 ``indicators.momentum.macd`` 完全同一实现；
- 三周期同时成立才是"日周月共振"（``resonance_dwm``）。

无未来函数的关键：周线/月线用 ``merge_asof(direction="backward")`` 对齐回日线，
匹配键是**周期末标签**（``period_end``，周五 / 月末）。因此：

- 周五收盘那天，本周/本月的 bar 已经收完，可以用；
- 周三看盘时，本周 bar 还没收完（标签是本周五 > 当天），自动退回到**上一个已收完**的周期。

历史长度要求（不足时对应列为 NaN、共振为 False，并在 ``resonance_bars`` 里给出实际根数）：

| 周期 | MA20 | MACD(26/9) ≈ | 大致需要 |
| --- | --- | --- | --- |
| 日线 | 20 根 | 35 根 | 2 个月 |
| 周线 | 20 周 | 35 周 | 约 2 年 |
| 月线 | 20 月 | 35 月 | 约 3 年 |

新增列
------
``resonance_daily`` / ``resonance_weekly`` / ``resonance_monthly`` / ``resonance_dwm``（bool）
``ma20`` / ``weekly_ma20`` / ``monthly_ma20``
``weekly_close`` / ``monthly_close``
``weekly_dif`` / ``weekly_dea`` / ``monthly_dif`` / ``monthly_dea``
``weekly_bars`` / ``monthly_bars``（该周期 bar 内包含的日线根数）
``resonance_bars``（本只股票可用于计算的日线根数）
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from config import (
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    MONTHLY_RULE,
    RESONANCE_MA_WINDOW,
    WEEKLY_RULE,
)
from indicators import register
from indicators._util import numeric, rolling_mean, sorted_view, to_datetime, with_columns
from indicators.momentum import macd
from indicators.resample import resample_bars

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 单周期
# --------------------------------------------------------------------------
def period_flags(
    bars: pd.DataFrame,
    ma_window: int = RESONANCE_MA_WINDOW,
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL,
) -> pd.DataFrame:
    """在任意周期的 bar 上算 MA20 / MACD / 共振标记。

    输入需要列：``trade_date`` / ``close``（周线月线由 resample 产出，含 ``period_end``）。
    返回列：period_end, trade_date, close, ma, dif, dea, ok, bars
    """
    b = bars.reset_index(drop=True).copy()
    if b.empty:
        return pd.DataFrame(columns=["period_end", "trade_date", "close", "ma", "dif", "dea", "ok", "bars"])

    b["ma"] = rolling_mean(numeric(b, "close"), ma_window)
    b = macd(b, fast=fast, slow=slow, signal=signal)
    b["ok"] = (numeric(b, "close") > b["ma"]) & (numeric(b, "dif") > numeric(b, "dea"))

    if "period_end" not in b.columns:
        b["period_end"] = to_datetime(b["trade_date"])
    if "bar_count" not in b.columns:
        b["bars"] = 1
    else:
        b["bars"] = b["bar_count"]

    return b[["period_end", "trade_date", "close", "ma", "dif", "dea", "ok", "bars"]]


def _align_to_daily(daily_dt: pd.Series, periodic: pd.DataFrame, prefix: str) -> pd.DataFrame:
    """把周期指标按"已收完的最近一个周期"对齐到每一个日线交易日。

    ``merge_asof(direction="backward")``：日线日期 d 只用 ``period_end <= d`` 的周期 bar，
    因此不会看到"还没收完"的当周/当月数据（无未来函数）。
    """
    fields = {
        "close": f"{prefix}_close",
        "ma": f"{prefix}_ma20",
        "dif": f"{prefix}_dif",
        "dea": f"{prefix}_dea",
        "ok": f"{prefix}_ok",
        "bars": f"{prefix}_bars",
    }
    index = daily_dt.index
    if periodic is None or periodic.empty:
        return pd.DataFrame({v: pd.Series(np.nan, index=index) for v in fields.values()})

    right = (
        periodic.dropna(subset=["period_end"])
        .sort_values("period_end", kind="mergesort")
        .drop_duplicates(subset=["period_end"], keep="last")
    )
    if right.empty:
        return pd.DataFrame({v: pd.Series(np.nan, index=index) for v in fields.values()})

    left = pd.DataFrame(
        {"__dt": daily_dt.to_numpy(), "__pos": np.arange(len(daily_dt))}
    ).sort_values("__dt", kind="mergesort")

    merged = pd.merge_asof(
        left, right, left_on="__dt", right_on="period_end", direction="backward"
    )
    merged = merged.sort_values("__pos", kind="mergesort").reset_index(drop=True)

    data = {}
    for src, dst in fields.items():
        col = merged[src] if src in merged.columns else pd.Series(np.nan, index=merged.index)
        data[dst] = col.to_numpy()
    return pd.DataFrame(data, index=index)


# --------------------------------------------------------------------------
# 主函数
# --------------------------------------------------------------------------
def resonance(
    df: pd.DataFrame,
    ma_window: int = RESONANCE_MA_WINDOW,
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL,
    weekly_rule: str = WEEKLY_RULE,
    monthly_rule: str = MONTHLY_RULE,
) -> pd.DataFrame:
    """计算日/周/月共振，新增列见模块 docstring。"""
    out = df.copy()
    if df is None or df.empty:
        for col in ("resonance_daily", "resonance_weekly", "resonance_monthly", "resonance_dwm"):
            out[col] = pd.Series(dtype=bool)
        return out

    work = sorted_view(df).copy()
    work["__dt"] = to_datetime(work["trade_date"])
    order = work.index            # 原索引标签，已按时间升序
    n_bars = len(work)

    # --- 日线 ---
    daily = period_flags(
        work.assign(period_end=work["__dt"]), ma_window=ma_window,
        fast=fast, slow=slow, signal=signal,
    )
    daily_ok = pd.Series(daily["ok"].to_numpy(), index=order).fillna(False).astype(bool)
    ma20 = pd.Series(daily["ma"].to_numpy(), index=order)

    # --- 周线 / 月线 ---
    # 短路：单个周期最多只能有 n_bars 根 bar，少于 ma_window 根时 MA20 必然是 NaN、
    # 该周期必定不成立。此时跳过 resample + MACD（省掉大量 pandas 调用），
    # 结果与完整计算完全一致（都是 False + 明细列 NaN）。
    if n_bars >= ma_window:
        weekly_bars = resample_bars(work, weekly_rule)
        weekly = period_flags(weekly_bars, ma_window=ma_window, fast=fast, slow=slow, signal=signal)
        monthly_bars = resample_bars(work, monthly_rule)
        monthly = period_flags(monthly_bars, ma_window=ma_window, fast=fast, slow=slow, signal=signal)
    else:
        weekly = pd.DataFrame()
        monthly = pd.DataFrame()

    weekly_aligned = _align_to_daily(work["__dt"], weekly, "weekly")
    monthly_aligned = _align_to_daily(work["__dt"], monthly, "monthly")

    weekly_ok = weekly_aligned["weekly_ok"].fillna(False).astype(bool)
    monthly_ok = monthly_aligned["monthly_ok"].fillna(False).astype(bool)

    payload: dict[str, object] = {
        "resonance_daily": daily_ok,
        "resonance_weekly": weekly_ok,
        "resonance_monthly": monthly_ok,
        "resonance_dwm": daily_ok & weekly_ok & monthly_ok,
        "ma20": ma20,
        "resonance_bars": pd.Series(np.arange(1, n_bars + 1), index=order),
    }
    for col in weekly_aligned.columns:
        payload[col] = weekly_aligned[col]
    for col in monthly_aligned.columns:
        payload[col] = monthly_aligned[col]

    aligned = {k: (v.reindex(out.index) if isinstance(v, pd.Series) else v) for k, v in payload.items()}
    return with_columns(out, aligned)


@register("resonance", "日周月共振（收盘>MA20 且 DIF>DEA，三周期同时成立）", params={})
def _resonance_indicator(df: pd.DataFrame, **params) -> pd.DataFrame:
    return resonance(df, **params)


__all__ = ["period_flags", "resonance"]
