"""
screener.filters.candles — 单根 K 线形态筛选：锤子线 / 射击之星

说明：这两个形态是**单根 K 线**事件，规则只看目标交易日那一根
（指标层 ``hammer`` / ``shooting_star`` 列）。若要看"最近 3 根内出现过"，
可以给指标层的列套 windowed 版本（当前未实现，按需再加）。

两个规则各自独立注册，默认都不在默认筛选链里——它们更适合用来观察/组合，
当作硬性门槛容易把结果打到 0。
"""

from __future__ import annotations

import pandas as pd

from screener import Filter, register_filter


class _CandleFilter(Filter):
    """K 线形态筛选的公共实现。"""

    column: str = ""

    def mask(self, df: pd.DataFrame) -> pd.Series:
        if self.column not in df.columns:
            raise ValueError(
                f"缺少指标列 {self.column}，请先运行 indicators.compute_panel"
            )
        flags = df[self.column].fillna(False).astype(bool)
        detail = {"column": self.column, "true_rows": int(flags.sum())}
        for diag in ("body_ratio", "lower_shadow_ratio", "upper_shadow_ratio"):
            if diag in df.columns:
                detail[f"median_{diag}"] = round(float(pd.to_numeric(df[diag], errors="coerce").median()), 4)
        self.detail = detail
        return flags


@register_filter
class HammerFilter(_CandleFilter):
    """锤子线：长下影、小实体，出现在下跌途中。"""

    name = "hammer"
    description = "锤子线（长下影≥2×实体、实体≤35%全幅、出现在 5 日均线下方）"
    default_params = {}
    column = "hammer"


@register_filter
class ShootingStarFilter(_CandleFilter):
    """射击之星：长上影、小实体，出现在上涨途中。"""

    name = "shooting_star"
    description = "射击之星（长上影≥2×实体、实体≤35%全幅、出现在 5 日均线上方）"
    default_params = {}
    column = "shooting_star"


__all__ = ["HammerFilter", "ShootingStarFilter"]
