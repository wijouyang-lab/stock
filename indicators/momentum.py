"""
indicators.momentum — 动量类指标：MACD(12/26/9)、KDJ(9/3/3)

口径说明（与国内软件对齐）：

MACD
    EMA12、EMA26 用 ``ewm(adjust=False)``（递推式，等价通达信 EMA）
    DIF = EMA12 - EMA26
    DEA = EMA(DIF, 9)
    MACD（柱） = ``config.MACD_HIST_MULTIPLIER`` × (DIF - DEA)，默认 2.0
    —— 国内软件（通达信/同花顺）的柱高就是 2×(DIF-DEA)，设为 1.0 则是国外口径。

KDJ
    RSV = (C - LLV(L,9)) / (HHV(H,9) - LLV(L,9)) × 100，分母为 0 时取 50
    K = SMA(RSV, 3, 1)、D = SMA(K, 3, 1)，首值均以 50 初始化（通达信口径）
    J = 3K - 2D
    前 n 根用 ``min_periods=1`` 的滚动窗口，属于"样本不足的近似值"，
    回测里通常丢弃前 n 根。

两个指标都只在"新增列"这一层面改动 DataFrame，行数与索引不变。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config import MACD_FAST, MACD_HIST_MULTIPLIER, MACD_SIGNAL, MACD_SLOW, KDJ_M1, KDJ_M2, KDJ_N
from indicators import register
from indicators._util import ema, numeric, safe_div, sma_seeded, with_columns


def macd(
    df: pd.DataFrame,
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL,
    hist_multiplier: float = MACD_HIST_MULTIPLIER,
) -> pd.DataFrame:
    """计算 MACD，新增列：``ema_fast`` / ``ema_slow`` / ``dif`` / ``dea`` / ``macd``。"""
    close = numeric(df, "close")

    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    dif = ema_fast - ema_slow
    dea = ema(dif, signal)

    return with_columns(
        df,
        {
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "dif": dif,
            "dea": dea,
            "macd": (dif - dea) * float(hist_multiplier),
        },
    )


def kdj(
    df: pd.DataFrame,
    n: int = KDJ_N,
    m1: int = KDJ_M1,
    m2: int = KDJ_M2,
    seed: float = 50.0,
) -> pd.DataFrame:
    """计算 KDJ，新增列：``rsv`` / ``k`` / ``d`` / ``j``。

    前 n 根用不完整窗口计算（``min_periods=1``），结果会在 ``kdj_warmup`` 列标记。
    """
    high = numeric(df, "high")
    low = numeric(df, "low")
    close = numeric(df, "close")

    llv = low.rolling(window=n, min_periods=1).min()
    hhv = high.rolling(window=n, min_periods=1).max()

    rng = hhv - llv
    rsv = safe_div((close - llv) * 100.0, rng)
    rsv = rsv.mask(rng.fillna(0) == 0, 50.0)          # 一字/停牌：分母为 0 → 中性 50
    rsv = rsv.fillna(50.0)

    k = sma_seeded(rsv, n=m1, m=1, seed=seed)
    d = sma_seeded(k, n=m2, m=1, seed=seed)
    j = 3.0 * k - 2.0 * d

    return with_columns(
        df,
        {
            "rsv": rsv,
            "k": k,
            "d": d,
            "j": j,
            # 标记"样本不足"的预热行：bars_seen < n
            "kdj_warmup": np.arange(len(df)) < max(0, n - 1),
        },
    )


@register("macd", "MACD(12/26/9)：DIF/DEA/柱", params={})
def _macd_indicator(df: pd.DataFrame, **params) -> pd.DataFrame:
    return macd(df, **params)


@register("kdj", "KDJ(9/3/3)：K/D/J", params={})
def _kdj_indicator(df: pd.DataFrame, **params) -> pd.DataFrame:
    return kdj(df, **params)


__all__ = ["macd", "kdj"]
