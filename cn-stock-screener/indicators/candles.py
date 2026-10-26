"""
indicators.candles — 单根 K 线形态：锤子线（hammer）、射击之星（shooting_star）

形态判定（全部用"相对比例"，与股价绝对高低无关）：

    rng   = high - low                （必须 > 0）
    body  = |close - open|
    lower = min(open, close) - low    （下影线）
    upper = high - max(open, close)   （上影线）

锤子线 hammer（出现在下跌途中，长下影、小实体、几乎无上影）
    body  ≤ 0.35 × rng
    lower ≥ 2.0 × max(body, 0.05 × rng)    ← 下影线至少是实体的两倍
    upper ≤ max(body, 0.10 × rng)          ← 上影线很短
    且（可选）收盘价位于 5 日均线之下 → 有"下跌后"的语境

射击之星 shooting_star（镜面：出现在上涨途中，长上影、小实体、几乎无下影）
    body  ≤ 0.35 × rng
    upper ≥ 2.0 × max(body, 0.05 × rng)
    lower ≤ max(body, 0.10 × rng)
    且（可选）收盘价位于 5 日均线之上

关于"是否要求趋势语境"：严格的技术分析定义要求形态出现在既有趋势之后，
否则任何一根长下影线都会被算成锤子。这里默认开启趋势过滤
（``config.CANDLE_TREND_FILTER``），可用参数 ``trend_filter=False`` 关闭。

新增列：``hammer`` / ``shooting_star``（bool）、``body_ratio`` / ``lower_shadow_ratio`` /
``upper_shadow_ratio``（诊断用，便于人工核对）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config import CANDLE_BODY_MAX_RATIO, CANDLE_SHADOW_MULT, CANDLE_TREND_FILTER
from indicators import register
from indicators._util import numeric, rolling_mean, with_columns

#: 趋势过滤用的均线周期
_TREND_MA = 5


def _shadow_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """计算实体/影线相对比例（诊断列）。"""
    o = numeric(df, "open")
    h = numeric(df, "high")
    low = numeric(df, "low")
    c = numeric(df, "close")

    rng = (h - low).astype("float64")
    body = (c - o).abs()
    lower = pd.concat([o, c], axis=1).min(axis=1) - low
    upper = h - pd.concat([o, c], axis=1).max(axis=1)

    valid = rng > 0
    rng_safe = rng.where(valid, np.nan)
    return pd.DataFrame(
        {
            "body_ratio": body / rng_safe,
            "lower_shadow_ratio": lower / rng_safe,
            "upper_shadow_ratio": upper / rng_safe,
            "_rng": rng,
            "_body": body,
            "_lower": lower,
            "_upper": upper,
            "_valid": valid,
        },
        index=df.index,
    )


def hammer(
    df: pd.DataFrame,
    body_max_ratio: float = CANDLE_BODY_MAX_RATIO,
    shadow_mult: float = CANDLE_SHADOW_MULT,
    trend_filter: bool = CANDLE_TREND_FILTER,
) -> pd.DataFrame:
    """锤子线，新增 bool 列 ``hammer``（False 表示不成立，避免 NaN 干扰筛选）。"""
    m = _shadow_metrics(df)

    body = m["_body"]
    lower = m["_lower"]
    upper = m["_upper"]
    rng = m["_rng"]

    cond = (
        m["_valid"]
        & (body <= body_max_ratio * rng)
        & (lower >= shadow_mult * pd.concat([body, 0.05 * rng], axis=1).max(axis=1))
        & (upper <= pd.concat([body, 0.10 * rng], axis=1).max(axis=1))
    )

    if trend_filter:
        close = numeric(df, "close")
        ma5 = rolling_mean(close, _TREND_MA, min_periods=_TREND_MA)
        cond = cond & (close < ma5.shift(1))

    return with_columns(
        df,
        {
            "hammer": cond.fillna(False).to_numpy(),
            "body_ratio": m["body_ratio"],
            "lower_shadow_ratio": m["lower_shadow_ratio"],
            "upper_shadow_ratio": m["upper_shadow_ratio"],
        },
    )


def shooting_star(
    df: pd.DataFrame,
    body_max_ratio: float = CANDLE_BODY_MAX_RATIO,
    shadow_mult: float = CANDLE_SHADOW_MULT,
    trend_filter: bool = CANDLE_TREND_FILTER,
) -> pd.DataFrame:
    """射击之星，新增 bool 列 ``shooting_star``。"""
    m = _shadow_metrics(df)

    body = m["_body"]
    lower = m["_lower"]
    upper = m["_upper"]
    rng = m["_rng"]

    cond = (
        m["_valid"]
        & (body <= body_max_ratio * rng)
        & (upper >= shadow_mult * pd.concat([body, 0.05 * rng], axis=1).max(axis=1))
        & (lower <= pd.concat([body, 0.10 * rng], axis=1).max(axis=1))
    )

    if trend_filter:
        close = numeric(df, "close")
        ma5 = rolling_mean(close, _TREND_MA, min_periods=_TREND_MA)
        cond = cond & (close > ma5.shift(1))

    return with_columns(df, {"shooting_star": cond.fillna(False).to_numpy()})


def candles(df: pd.DataFrame, **params) -> pd.DataFrame:
    """一次算完两种形态（避免重复计算指标列）。"""
    out = hammer(df, **params)
    shaped = shooting_star(df, **params)
    out["shooting_star"] = shaped["shooting_star"]
    return out


@register("hammer", "锤子线（长下影、小实体，下跌途中）", params={})
def _hammer_indicator(df: pd.DataFrame, **params) -> pd.DataFrame:
    return hammer(df, **params)


@register("shooting_star", "射击之星（长上影、小实体，上涨途中）", params={})
def _shooting_star_indicator(df: pd.DataFrame, **params) -> pd.DataFrame:
    return shooting_star(df, **params)


__all__ = ["hammer", "shooting_star", "candles"]
