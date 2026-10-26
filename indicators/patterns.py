"""
indicators.patterns — 形态识别：双重底（double_bottom）、双重顶（double_top）

参数口径（按需求固定，可在 config.py 调整）
--------------------------------------------
- 摆动点（swing/pivot）窗口 ``n = 5``：某根 K 线的高点（低点）是前后各 5 根内的最大值（最小值），
  且严格高于（低于）相邻一根（避免平台整理时同一价位反复产出摆动点）。
- 两个同类摆动点价差 ≤ **3%**（分母取两者中的较小值/较大值，见下）。
- 两个摆动点间隔 ≥ **10 个交易日**（并设上限 250 个交易日，避免跨年配对）。
- 颈线（neckline）：
    双重底 = 两低点之间所有高点里的最高值；
    双重顶 = 两高点之间所有低点里的最低值。
  并要求颈线与两个摆动点有至少 1% 的距离，否则"形态"没有可操作空间（过滤噪音）。
- **只认突破确认**：形态必须出现"收盘价突破颈线"才算成立
    * 双重底：某根收盘价 > 颈线；
    * 双重顶：某根收盘价 < 颈线。
  只形成两个底、但收盘始终没越过颈线的，一律不算（``double_bottom=False``）。

关键实现约束：**不引入未来函数**
--------------------------------
1. 摆动点在 ``i + n`` 根之后才能确认，因此突破日 ``k`` 必须满足 ``k >= j + n``（j 为第二个摆动点），
   否则在 k 那天我们还不知道 j 是不是摆动点。
2. 突破前的"形态失效"检查只用 ``j+1 .. k-1`` 的数据：
   双重底若中途收盘跌破两个低点的下沿（容差 3%），形态作废；双重顶镜像处理。
3. 所有指标都是逐根向前看的因果计算，``--self-test`` 里用"截断历史重算"验证过。

新增列
------
``double_bottom`` / ``double_top``            ：突破当根为 True（其余 False）
``double_bottom_5d`` / ``_10d`` / ``_20d``    ：最近 N 根内是否发生过突破（含当根），供筛选器直接用
``db_low1`` / ``db_low2`` / ``db_neckline`` / ``db_breakout_date`` / ``db_gap``
``dt_high1`` / ``dt_high2`` / ``dt_neckline`` / ``dt_breakout_date`` / ``dt_gap``
    —— 元数据只在突破后 ``PATTERN_META_FFILL`` 根内向前填充，方便看盘时核对。
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from config import (
    PATTERN_BREAKOUT_LOOKBACKS,
    PATTERN_MAX_GAP,
    PATTERN_META_FFILL,
    PATTERN_MIN_GAP,
    PATTERN_NECKLINE_MIN_PCT,
    PATTERN_PRICE_TOL,
    SWING_N,
)
from indicators import register
from indicators._util import numeric, rolling_any, sorted_view, to_datetime, with_columns

logger = logging.getLogger(__name__)

#: 摆动点列名
PIVOT_HIGH = "_pivot_high"
PIVOT_LOW = "_pivot_low"


# --------------------------------------------------------------------------
# 摆动点
# --------------------------------------------------------------------------
def swing_points(
    df: pd.DataFrame,
    n: int = SWING_N,
) -> tuple[pd.Series, pd.Series]:
    """返回 (pivot_high, pivot_low) 两个 bool Series（索引与 df 一致）。

    第 ``i`` 根为摆动高点：``high[i]`` 是 ``[i-n, i+n]`` 窗口内的最大值，
    且严格大于 ``high[i-1]`` 与 ``high[i+1]``。窗口不完整（靠近两端）时为 False。
    """
    high = numeric(df, "high")
    low = numeric(df, "low")
    win = 2 * n + 1

    roll_max = high.rolling(win, center=True, min_periods=win).max()
    roll_min = low.rolling(win, center=True, min_periods=win).min()

    piv_high = (
        (high == roll_max)
        & (high > high.shift(1))
        & (high > high.shift(-1))
    )
    piv_low = (
        (low == roll_min)
        & (low < low.shift(1))
        & (low < low.shift(-1))
    )
    return piv_high.fillna(False).astype(bool), piv_low.fillna(False).astype(bool)


# --------------------------------------------------------------------------
# 形态扫描核心
# --------------------------------------------------------------------------
def _find_double_pattern(
    bars: pd.DataFrame,
    pivot_first: np.ndarray,
    pivot_second: np.ndarray,
    *,
    bottom: bool,
    n: int,
    tol: float,
    min_gap: int,
    max_gap: int,
    neckline_min_pct: float,
) -> list[dict[str, Any]]:
    """在单只股票的时间序列上扫描双重底/顶，返回"已确认突破"的形态列表。

    ``bottom=True`` 表示双重底（找两个低点，向上突破），False 表示双重顶。
    两个 pivot 数组：底形态传 (pivot_low, pivot_high)，顶形态传 (pivot_high, pivot_low)——
    第二组用于校验"中间确实出现了一个反向摆动点"。
    """
    close = numeric(bars, "close").to_numpy(dtype="float64")
    prices = bars["_p"].to_numpy(dtype="float64")          # 低点集合用 low，高点集合用 high
    opposite = bars["_q"].to_numpy(dtype="float64")        # 反向极值：底用 high，顶用 low
    dates = bars["trade_date"].astype(str).to_numpy()
    n_bars = len(bars)

    first_idx = np.flatnonzero(pivot_first)
    patterns: list[dict[str, Any]] = []

    for a_pos, i in enumerate(first_idx):
        for j in first_idx[a_pos + 1:]:
            gap = int(j - i)
            if gap < min_gap:
                continue
            if gap > max_gap:
                break                      # 摆动点升序，后续只会更远
            if j + n >= n_bars:
                break                      # 第二个点还没被确认，后面的点更不可能

            p_i = prices[i]
            p_j = prices[j]
            if not (np.isfinite(p_i) and np.isfinite(p_j)):
                continue

            # 价差 ≤ tol，分母取两价中点（对称口径：|p1-p2| / ((p1+p2)/2) ≤ 3%）
            mid_price = (abs(p_i) + abs(p_j)) / 2.0
            if mid_price <= 0:
                continue
            if abs(p_j - p_i) / mid_price > tol:
                continue

            # 中间必须有反向摆动点（底形态是反弹高点），否则只是趋势中的两点
            if not pivot_second[i + 1:j].any():
                continue

            mid = opposite[i + 1:j]
            if mid.size == 0:
                continue
            neckline = float(mid.max() if bottom else mid.min())

            # 颈线必须与两个摆动点拉开距离（否则"形态"没有可操作空间）
            if bottom:
                if neckline <= max(abs(p_i), abs(p_j)) * (1.0 + neckline_min_pct):
                    continue
            else:
                if neckline >= min(abs(p_i), abs(p_j)) * (1.0 - neckline_min_pct):
                    continue

            # 从第二点确认之后开始找第一根"收盘突破颈线"的 K 线
            start = int(j + n)
            if start >= n_bars:
                continue
            seg_close = close[start:]
            hit = np.flatnonzero(seg_close > neckline) if bottom else np.flatnonzero(seg_close < neckline)
            if hit.size == 0:
                continue
            k = int(start + hit[0])

            # 突破前若已破坏形态（收盘越过两摆动点外沿 3%），作废
            guard_from, guard_to = int(j + 1), k
            if guard_to > guard_from:
                seg = close[guard_from:guard_to]
                limit = min(p_i, p_j) * (1.0 - tol) if bottom else max(p_i, p_j) * (1.0 + tol)
                broken = (seg < limit).any() if bottom else (seg > limit).any()
                if broken:
                    continue

            patterns.append(
                {
                    "idx_first": int(i),
                    "idx_second": int(j),
                    "idx_breakout": k,
                    "date_first": dates[i],
                    "date_second": dates[j],
                    "date_breakout": dates[k],
                    "price_first": float(p_i),
                    "price_second": float(p_j),
                    "neckline": neckline,
                    "gap": gap,
                }
            )
    return patterns


def _pattern_frame(df: pd.DataFrame, price_col: str, opposite_col: str) -> pd.DataFrame:
    """构造"按时间升序 + 0..n-1 索引"的工作帧，并把原始索引标签存进 ``_orig_index``。"""
    bars = sorted_view(df).copy()
    bars["_orig_index"] = bars.index.to_numpy()
    bars = bars.reset_index(drop=True)
    bars["_p"] = numeric(bars, price_col)
    bars["_q"] = numeric(bars, opposite_col)
    return bars


def _stamp(
    out: pd.DataFrame,
    bars: pd.DataFrame,
    flag: pd.Series,
    meta: dict[str, pd.Series],
    prefix: str,
    lookbacks: tuple[int, ...],
    meta_ffill: int,
) -> pd.DataFrame:
    """把工作帧上的计算结果按原始索引一次性拼回 ``out``（单次 concat）。"""
    payload: dict[str, object] = {prefix: flag}
    for lb in lookbacks:
        payload[f"{prefix}_{lb}d"] = rolling_any(flag, lb)
    for col, series in meta.items():
        # 突破后向前填充若干根，方便在随后几天里核对形态参数
        payload[col] = series.ffill(limit=meta_ffill)

    orig_index = bars["_orig_index"].to_numpy()
    new = {
        col: pd.Series(series.to_numpy(), index=orig_index).reindex(out.index)
        for col, series in payload.items()
    }
    return with_columns(out, new)


def detect_double_bottom(
    df: pd.DataFrame,
    n: int = SWING_N,
    tol: float = PATTERN_PRICE_TOL,
    min_gap: int = PATTERN_MIN_GAP,
    max_gap: int = PATTERN_MAX_GAP,
    neckline_min_pct: float = PATTERN_NECKLINE_MIN_PCT,
    lookbacks: tuple[int, ...] = PATTERN_BREAKOUT_LOOKBACKS,
    meta_ffill: int = PATTERN_META_FFILL,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """双重底（只认收盘突破颈线）。返回 (新增列后的 df, 形态列表)。"""
    out = df.copy()
    bars = _pattern_frame(df, price_col="low", opposite_col="high")

    flag = pd.Series(False, index=bars.index)
    meta = {
        "db_low1": pd.Series(np.nan, index=bars.index),
        "db_low2": pd.Series(np.nan, index=bars.index),
        "db_neckline": pd.Series(np.nan, index=bars.index),
        "db_breakout_date": pd.Series(pd.NA, index=bars.index, dtype="object"),
        "db_gap": pd.Series(np.nan, index=bars.index),
    }

    patterns: list[dict[str, Any]] = []
    # 短路：至少需要 n(低点确认) + min_gap(间隔) + n(第二点确认) + 1(突破)
    if len(bars) >= 2 * n + min_gap + 1:
        piv_high, piv_low = swing_points(bars, n=n)
        patterns = _find_double_pattern(
            bars,
            pivot_first=piv_low.to_numpy(),
            pivot_second=piv_high.to_numpy(),
            bottom=True,
            n=n, tol=tol, min_gap=min_gap, max_gap=max_gap,
            neckline_min_pct=neckline_min_pct,
        )
        for p in patterns:
            k = p["idx_breakout"]
            flag.iloc[k] = True
            meta["db_low1"].iloc[k] = p["price_first"]
            meta["db_low2"].iloc[k] = p["price_second"]
            meta["db_neckline"].iloc[k] = p["neckline"]
            meta["db_breakout_date"].iloc[k] = p["date_breakout"]
            meta["db_gap"].iloc[k] = p["gap"]

    out = _stamp(out, bars, flag, meta, "double_bottom", lookbacks, meta_ffill)
    return out, patterns


def detect_double_top(
    df: pd.DataFrame,
    n: int = SWING_N,
    tol: float = PATTERN_PRICE_TOL,
    min_gap: int = PATTERN_MIN_GAP,
    max_gap: int = PATTERN_MAX_GAP,
    neckline_min_pct: float = PATTERN_NECKLINE_MIN_PCT,
    lookbacks: tuple[int, ...] = PATTERN_BREAKOUT_LOOKBACKS,
    meta_ffill: int = PATTERN_META_FFILL,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """双重顶（只认收盘跌破颈线）。返回 (新增列后的 df, 形态列表)。"""
    out = df.copy()
    bars = _pattern_frame(df, price_col="high", opposite_col="low")

    flag = pd.Series(False, index=bars.index)
    meta = {
        "dt_high1": pd.Series(np.nan, index=bars.index),
        "dt_high2": pd.Series(np.nan, index=bars.index),
        "dt_neckline": pd.Series(np.nan, index=bars.index),
        "dt_breakout_date": pd.Series(pd.NA, index=bars.index, dtype="object"),
        "dt_gap": pd.Series(np.nan, index=bars.index),
    }

    patterns: list[dict[str, Any]] = []
    if len(bars) >= 2 * n + min_gap + 1:
        piv_high, piv_low = swing_points(bars, n=n)
        patterns = _find_double_pattern(
            bars,
            pivot_first=piv_high.to_numpy(),
            pivot_second=piv_low.to_numpy(),
            bottom=False,
            n=n, tol=tol, min_gap=min_gap, max_gap=max_gap,
            neckline_min_pct=neckline_min_pct,
        )
        for p in patterns:
            k = p["idx_breakout"]
            flag.iloc[k] = True
            meta["dt_high1"].iloc[k] = p["price_first"]
            meta["dt_high2"].iloc[k] = p["price_second"]
            meta["dt_neckline"].iloc[k] = p["neckline"]
            meta["dt_breakout_date"].iloc[k] = p["date_breakout"]
            meta["dt_gap"].iloc[k] = p["gap"]

    out = _stamp(out, bars, flag, meta, "double_top", lookbacks, meta_ffill)
    return out, patterns


@register("double_bottom", "双重底（摆动点 n=5、价差≤3%、间隔≥10 日、收盘突破颈线确认）", params={})
def _double_bottom_indicator(df: pd.DataFrame, **params) -> pd.DataFrame:
    result, _ = detect_double_bottom(df, **params)
    return result


@register("double_top", "双重顶（摆动点 n=5、价差≤3%、间隔≥10 日、收盘跌破颈线确认）", params={})
def _double_top_indicator(df: pd.DataFrame, **params) -> pd.DataFrame:
    result, _ = detect_double_top(df, **params)
    return result


__all__ = ["swing_points", "detect_double_bottom", "detect_double_top"]
