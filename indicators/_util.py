"""
indicators._util — 指标实现共用的纯计算工具

硬性约束（与本项目其它部分一致）：
- **没有任何网络调用、没有任何文件 IO**，只依赖 numpy / pandas。
- 不修改入参 DataFrame，所有函数返回新对象或新 Series。

输入约定：
- 日线 DataFrame 至少包含列：`trade_date`（YYYYMMDD 字符串/int）、`open`、`high`、
  `low`、`close`、`vol`（可选 `amount`）。
- 索引必须是**唯一**的（`compute_panel` 用 `ignore_index=True` 拼接，天然满足），
  指标函数依赖索引对齐把结果写回原帧。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

#: 交易日字符串格式
DATE_FMT = "%Y%m%d"


# --------------------------------------------------------------------------
# 日期 / 排序
# --------------------------------------------------------------------------
def to_datetime(series: pd.Series) -> pd.Series:
    """YYYYMMDD（str/int）→ datetime64；无法解析的置 NaT。"""
    return pd.to_datetime(
        series.astype(str).str.slice(0, 8), format=DATE_FMT, errors="coerce"
    )


def sorted_view(df: pd.DataFrame, date_col: str = "trade_date") -> pd.DataFrame:
    """返回按 trade_date 升序排列的副本（保留原索引标签）。

    指标全部是"时序"计算，必须保证时间升序；调用方传入的顺序不做假设。
    用 mergesort 保证稳定排序（同日多条时保持原相对顺序）。
    """
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()
    if date_col not in df.columns:
        return df.copy()
    key = to_datetime(df[date_col])
    return df.assign(__sort_dt=key).sort_values("__sort_dt", kind="mergesort").drop(columns="__sort_dt")


def numeric(df: pd.DataFrame, col: str, default: float = np.nan) -> pd.Series:
    """取一列并转成 float；列不存在时返回全 default 的 Series。

    已经是数值类型时直接返回（``copy=False``），避免每个指标都把同一列重复转换一次
    ——实测这一步在 5000 只股票的面板上能省下不少时间。
    """
    if df is None or col not in df.columns:
        idx = df.index if df is not None else pd.RangeIndex(0)
        return pd.Series(default, index=idx, dtype="float64")
    series = df[col]
    if pd.api.types.is_numeric_dtype(series) and series.dtype == "float64":
        return series
    return pd.to_numeric(series, errors="coerce")


# --------------------------------------------------------------------------
# 均线 / 平滑
# --------------------------------------------------------------------------
def ema(series: pd.Series, span: int) -> pd.Series:
    """指数移动平均（与通达信/同花顺 EMA 一致：adjust=False）。"""
    return series.astype("float64").ewm(span=span, adjust=False).mean()


def sma_seeded(series: pd.Series, n: int, m: int = 1, seed: float = 50.0) -> pd.Series:
    """通达信 ``SMA(X, N, M) = (M*X + (N-M)*Y') / N``，首值用 seed。

    等价于 alpha = M/N 的 EWM，但在最前面补一个 seed 作为初值，
    这样第一根 K 线的结果就是 ``(M*X1 + (N-M)*seed)/N``（与国内软件一致）。
    默认 seed=50 对应 KDJ 的标准初始化。
    """
    s = series.astype("float64")
    if s.empty:
        return s.copy()
    arr = np.concatenate([[float(seed)], s.to_numpy()])
    smoothed = pd.Series(arr).ewm(alpha=float(m) / float(n), adjust=False).mean().to_numpy()[1:]
    return pd.Series(smoothed, index=s.index, dtype="float64")


def rolling_mean(series: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    """简单移动平均。``min_periods`` 默认等于 window（不足窗口即为 NaN，避免用残缺样本）。"""
    return series.astype("float64").rolling(
        window=window, min_periods=window if min_periods is None else min_periods
    ).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    """真实波幅 TR（供 ATR 等后续指标复用）。"""
    high = numeric(df, "high")
    low = numeric(df, "low")
    close_prev = numeric(df, "close").shift(1)
    a = high - low
    b = (high - close_prev).abs()
    c = (low - close_prev).abs()
    return pd.concat([a, b, c], axis=1).max(axis=1)


# --------------------------------------------------------------------------
# 布尔/窗口小工具
# --------------------------------------------------------------------------
def rolling_any(flags: pd.Series, window: int) -> pd.Series:
    """"最近 window 根内是否出现过 True"（含当前根）。window<=1 时原样返回。"""
    if window is None or window <= 1:
        return flags.fillna(False).astype(bool)
    return flags.fillna(False).astype(float).rolling(window, min_periods=1).max().astype(bool)


def safe_div(numerator: pd.Series, denominator: pd.Series, fill: float = np.nan) -> pd.Series:
    """逐元素除法，分母为 0/NaN 时返回 fill（不抛零除警告）。"""
    num = numerator.astype("float64")
    den = denominator.astype("float64")
    out = num / den.replace(0.0, np.nan)
    return out.fillna(fill) if not np.isnan(fill) else out


def where(cond: pd.Series, yes, no=np.nan) -> pd.Series:
    """pandas 版的 np.where，但保持索引与 dtype 可预测。"""
    return pd.Series(np.where(cond.fillna(False).to_numpy(), yes, no), index=cond.index)


def with_columns(df: pd.DataFrame, new: dict[str, object]) -> pd.DataFrame:
    """一次性把多列拼到 DataFrame 上（替代逐列 ``df[col] = ...``）。

    为什么需要它：pandas 每次 ``__setitem__`` 都要走一遍 BlockManager，单次约 0.2ms。
    一个指标动辄十几列，5000 只股票 × 9 个指标就是几十万次赋值——实测占了
    ``compute_panel`` 三分之二的时间。合并成一次 concat 后同样的计算快 3~5 倍。

    同名旧列会先被丢弃（等价于覆盖语义），避免 concat 产生重复列。
    """
    if not new:
        return df.copy()
    existing = [c for c in new if c in df.columns]
    base = df.drop(columns=existing) if existing else df
    add = pd.DataFrame({k: v for k, v in new.items()}, index=df.index)
    return pd.concat([base, add], axis=1)
