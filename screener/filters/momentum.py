"""
screener.filters.momentum — 动量类筛选：MACD 多头 / KDJ 多头

- ``macd_bull``：DIF > DEA（MACD 金叉状态，不一定当天金叉）
- ``kdj_bull`` ：K > D（KDJ 多头排列）

这两个是"状态"判断而不是"交叉事件"判断：截面数据里没有上一根，
所以不做金叉检测。需要"当天金叉"的话，应在指标层再加一列
``macd_cross_up``（dif>dea 且前一根 dif<=dea），再写一个新规则。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from screener import Filter, register_filter


def _numeric(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        raise ValueError(f"缺少指标列 {col}，请先运行 indicators.compute_panel")
    return pd.to_numeric(df[col], errors="coerce")


@register_filter
class MacdBullFilter(Filter):
    """MACD 多头：DIF > DEA。"""

    name = "macd_bull"
    description = "MACD 多头：DIF > DEA"
    default_params = {"min_hist": 0.0}

    def mask(self, df: pd.DataFrame) -> pd.Series:
        dif = _numeric(df, "dif")
        dea = _numeric(df, "dea")
        flags = (dif > dea).fillna(False)
        if float(self.params.get("min_hist", 0.0)) > 0 and "macd" in df.columns:
            flags &= _numeric(df, "macd") >= float(self.params["min_hist"])
        self.detail = {
            "true_rows": int(flags.sum()),
            "dif_missing": int(dif.isna().sum()),
            "note": "dif_missing>0 多为次新股或历史不足",
        }
        return flags


@register_filter
class KdjBullFilter(Filter):
    """KDJ 多头：K > D（可选 j 上限过滤超买）。"""

    name = "kdj_bull"
    description = "KDJ 多头：K > D"
    default_params = {"max_j": np.nan}

    def mask(self, df: pd.DataFrame) -> pd.Series:
        k = _numeric(df, "k")
        d = _numeric(df, "d")
        flags = (k > d).fillna(False)

        max_j = self.params.get("max_j", np.nan)
        if max_j is not None and not (isinstance(max_j, float) and np.isnan(max_j)):
            j = _numeric(df, "j")
            flags &= (j <= float(max_j)).fillna(False)

        self.detail = {
            "true_rows": int(flags.sum()),
            "k_missing": int(k.isna().sum()),
            "max_j": None if max_j is None or (isinstance(max_j, float) and np.isnan(max_j)) else float(max_j),
        }
        return flags


__all__ = ["MacdBullFilter", "KdjBullFilter"]
