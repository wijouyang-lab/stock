"""
indicators.volume — 量能类指标：换手率（turnover）、量比（volume_ratio = vol / MA5）

换手率口径
----------
优先直接用 ``daily_basic.turnover_rate``（Tushare 已经算好的百分数）；缺失时用
``vol / float_share`` 兜底。单位换算如下（Tushare 定义）：

    daily.vol          单位 = 手（1 手 = 100 股）
    daily_basic.float_share  单位 = 万股
    turnover_rate(%)   = vol × 100 / (float_share × 10000) × 100 = vol / float_share

即 ``turnover(%) = vol(手) / float_share(万股)``，不需要额外的常数。
``--self-test`` 里会用真实缓存交叉验证这个换算（算出来的与接口给的最大误差应接近 0）。

量比口径
--------
本项目按需求用 ``vol / MA5(vol)``，列名为 **``volume_ratio_5``**。
之所以不叫 ``volume_ratio``：``daily_basic`` 自带的 ``volume_ratio`` 是 Tushare 的
"量比"（当日每分钟均量 / 过去 5 日每分钟均量），两者定义不同，同名会互相覆盖。
本项目保留接口给的 ``volume_ratio`` 不动，另起 ``volume_ratio_5``，避免混淆。

新增列：``turnover``（%）、``ma5_vol``（前 5 日均量，不含当日）、``volume_ratio_5``。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from indicators import register
from indicators._util import numeric, rolling_mean, safe_div, with_columns


def turnover(df: pd.DataFrame, prefer_api_column: bool = True) -> pd.DataFrame:
    """换手率（%），新增列 ``turnover`` 与诊断列 ``turnover_source``。

    ``prefer_api_column=True`` 时优先用 ``turnover_rate``（接口口径），
    否则一律用 ``vol / float_share`` 计算（便于交叉验证两种口径是否一致）。
    """
    out = df.copy()
    computed = safe_div(numeric(df, "vol"), numeric(df, "float_share"))

    if prefer_api_column and "turnover_rate" in df.columns:
        api = numeric(df, "turnover_rate")
        # 接口值优先，缺失的用计算值补
        values = api.where(api.notna(), computed)
        source = np.where(
            api.notna(), "turnover_rate",
            np.where(computed.notna(), "vol/float_share", "missing"),
        )
    else:
        values = computed
        source = np.where(computed.notna(), "vol/float_share", "missing")

    return with_columns(out, {"turnover": values, "turnover_source": source})


def volume_ratio(
    df: pd.DataFrame,
    window: int = 5,
    min_periods: int | None = None,
) -> pd.DataFrame:
    """量比 = vol / **前 window 个交易日**均量（**不含当日**）。

    口径（硬性约定）：分母是 ``t-window … t-1`` 的算术平均，**绝不包含 t 日本身**。
    实现上先 ``shift(1)`` 再 ``rolling(window)``；若直接 ``rolling(window).mean()``，
    当日成交量会进入分母，放量会被系统性低估。

    例：``vol = [100,100,100,100,100,300]`` → 第 6 天 ratio = 300/100 = **3.0**；
    误含当日则得 300/140 ≈ 2.14。

    ``min_periods`` 默认等于 ``window``：至少要有 ``window+1`` 根日线才给值，
    次新股（历史不足）得到 NaN，由筛选器记为 missing 且不命中。
    新增列：``ma{window}_vol``（前 N 日均量）与 ``volume_ratio_{window}``。
    """
    vol = numeric(df, "vol")
    prev = vol.shift(1)
    ma = rolling_mean(prev, window, window if min_periods is None else min_periods)
    return with_columns(
        df,
        {
            f"ma{window}_vol": ma,
            f"volume_ratio_{window}": safe_div(vol, ma),
        },
    )


def ensure_turnover(df: pd.DataFrame) -> pd.DataFrame:
    """保证结果帧里有可用的 ``turnover`` 列（供筛选器使用）。

    - 已有 ``turnover`` 且非全空 → 原样返回；
    - 否则从 ``turnover_rate`` / ``vol / float_share`` 派生一列。
    """
    if df is None or df.empty:
        return df
    if "turnover" in df.columns and df["turnover"].notna().any():
        return df
    if "turnover_rate" in df.columns or "float_share" in df.columns:
        return turnover(df)
    return df


@register("turnover", "换手率（%）", params={})
def _turnover_indicator(df: pd.DataFrame, **params) -> pd.DataFrame:
    return turnover(df, **params)


@register("volume_ratio", "量比 = vol / 前 N 日均量（不含当日），列名 volume_ratio_5", params={})
def _volume_ratio_indicator(df: pd.DataFrame, **params) -> pd.DataFrame:
    return volume_ratio(df, **params)


__all__ = ["turnover", "volume_ratio", "ensure_turnover"]
