"""
indicators.resample — 日线 → 周线 / 月线重采样

用途：日周月共振（``indicators.resonance``）需要周线与月线的收盘价与 MACD。

关键设计（避免未来函数）：
- 重采样标签用 pandas 的周期末：
  * 周线 ``W-FRI``：标签是该周最后一个**名义**周五（即使周五休市，标签仍是周五）；
  * 月线 ``ME``：标签是月末。
- **但标签不等于"该周期的最后一个交易日"**。所以每个 bar 额外保留
  ``trade_date`` = 该周期内**最后一个真实交易日**，以及 ``period_end`` = 重采样标签。
  下游用 ``merge_asof(on=period_end, direction="backward")`` 对齐回日线：
  只有"周期已经结束"的 bar 才会被日线用到，因此不会引入未来数据。
- ``bar_count`` = 该周期内包含的日线根数，便于识别"数据起点处的残缺周期"。

本模块**不注册**为指标：它改变行数（日线 → 周/月线），不满足
"同长度、新增列"的指标契约，属于纯工具函数，供 resonance 等模块调用。
"""

from __future__ import annotations

import logging

import pandas as pd

from config import MONTHLY_RULE, WEEKLY_RULE
from indicators._util import to_datetime

logger = logging.getLogger(__name__)

# WEEKLY_RULE / MONTHLY_RULE 统一在 config.py 定义（单一事实来源），此处仅 re-export。

#: 各周期的聚合方式
_AGG: Final[dict[str, str]] = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "vol": "sum",
    "amount": "sum",
    "trade_date": "last",   # 周期内最后一个真实交易日
}


def resample_bars(
    df: pd.DataFrame,
    rule: str,
    date_col: str = "trade_date",
    include_bar_count: bool = True,
) -> pd.DataFrame:
    """把单只股票的日线重采样成任意周期（rule 为 pandas 频率别名）。

    参数
    ----
    df               : 日线（需含 date_col/open/high/low/close/vol，amount 可选）
    rule             : ``"W-FRI"`` 周线 / ``"ME"`` 月线 / 任意 pandas 别名
    date_col         : 日期列名，默认 ``trade_date``
    include_bar_count: 是否输出 ``bar_count``（周期内日线根数）

    返回
    ----
    DataFrame，索引为 RangeIndex，列：
        trade_date（周期内最后一个交易日，YYYYMMDD 字符串）
        period_end（重采样标签，datetime64）
        open / high / low / close / vol / amount / bar_count
    空输入返回空表（列结构保持一致）。
    """
    cols = ["trade_date", "period_end", "open", "high", "low", "close", "vol", "amount"]
    if include_bar_count:
        cols.append("bar_count")
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)

    work = df.copy()
    work["__dt"] = to_datetime(work[date_col])
    work = work.dropna(subset=["__dt"]).sort_values("__dt", kind="mergesort")
    if work.empty:
        return pd.DataFrame(columns=cols)

    agg = {k: v for k, v in _AGG.items() if k in work.columns}

    g = work.set_index("__dt")
    out = g.resample(rule).agg(agg)
    if include_bar_count:
        # 用非空 close 计数近似周期内日线根数（停牌日不会有日线行）
        out["bar_count"] = g["close"].resample(rule).count()

    # 丢弃"整周期无数据"的空 bar（周末/节假日造成的空洞）
    out = out.dropna(subset=["close"]).reset_index()

    # 重命名重采样标签列
    out = out.rename(columns={"__dt": "period_end"})
    if "trade_date" in out.columns:
        out["trade_date"] = out["trade_date"].astype(str).str.slice(0, 8)
    else:  # 理论上不会发生，保底
        out["trade_date"] = out["period_end"].dt.strftime("%Y%m%d")

    # 补齐可能缺失的列，保证下游列结构稳定
    for c in ("open", "high", "low", "close", "vol", "amount"):
        if c not in out.columns:
            out[c] = float("nan")

    out = out.reset_index(drop=True)
    ordered = cols + [c for c in out.columns if c not in cols]
    return out[ordered]


def to_weekly(df: pd.DataFrame, date_col: str = "trade_date") -> pd.DataFrame:
    """日线 → 周线（``W-FRI``）。"""
    return resample_bars(df, WEEKLY_RULE, date_col=date_col)


def to_monthly(df: pd.DataFrame, date_col: str = "trade_date") -> pd.DataFrame:
    """日线 → 月线（``ME``，月末）。"""
    return resample_bars(df, MONTHLY_RULE, date_col=date_col)


def resample_panel(
    panel: pd.DataFrame,
    rule: str,
    code_col: str = "ts_code",
    date_col: str = "trade_date",
) -> pd.DataFrame:
    """对"多股票面板"逐只重采样并拼回，结果多一列 ``ts_code``。"""
    if panel is None or panel.empty:
        return pd.DataFrame()
    if code_col not in panel.columns:
        return resample_bars(panel, rule, date_col=date_col)

    frames: list[pd.DataFrame] = []
    for code, grp in panel.groupby(code_col, sort=False):
        bars = resample_bars(grp, rule, date_col=date_col)
        if bars.empty:
            continue
        bars.insert(0, code_col, code)
        frames.append(bars)
    if not frames:
        return pd.DataFrame(columns=[code_col, "trade_date", "period_end", "open", "high",
                                     "low", "close", "vol", "amount", "bar_count"])
    return pd.concat(frames, ignore_index=True)


__all__ = [
    "WEEKLY_RULE",
    "MONTHLY_RULE",
    "resample_bars",
    "to_weekly",
    "to_monthly",
    "resample_panel",
]
