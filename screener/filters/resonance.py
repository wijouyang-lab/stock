"""
screener.filters.resonance — 日 / 周 / 月共振筛选

两条规则：

- ``resonance_daily``：日线 收盘 > MA20 且 DIF > DEA
- ``resonance_dwm``  ：日线 + 周线 + 月线 三个周期**同时**满足上述条件

历史不足的处理
--------------
周线 MA20 需要约 100 个交易日、月线 MA20 需要约 420 个交易日。历史不够时
对应指标列是 NaN，规则会把该股票判为不通过（而不是报错），并在 funnel 的
``detail`` 里分别统计"周线数据不足 / 月线数据不足"的只数——这样看到
"5002 -> 0" 时能立刻判断是"没有共振"还是"历史太短"。
"""

from __future__ import annotations

import pandas as pd

from screener import Filter, register_filter


@register_filter
class ResonanceDailyFilter(Filter):
    """日线共振：收盘 > MA20 且 DIF > DEA。"""

    name = "resonance_daily"
    description = "日线共振：收盘 > MA20 且 DIF > DEA"
    default_params = {"column": "resonance_daily"}

    def mask(self, df: pd.DataFrame) -> pd.Series:
        col = self.params["column"]
        if col not in df.columns:
            raise ValueError(f"缺少指标列 {col}，请先运行 indicators.compute_panel")

        flags = df[col].fillna(False).astype(bool)
        ma_missing = int(df["ma20"].isna().sum()) if "ma20" in df.columns else 0
        self.detail = {
            "true_rows": int(flags.sum()),
            "ma20_missing": ma_missing,
            "note": "ma20_missing>0 表示历史不足 20 根，这些股票不可能通过",
        }
        return flags


@register_filter
class ResonanceDwmFilter(Filter):
    """日周月共振：收盘 > MA20 且 DIF > DEA（三周期同时成立）。"""

    name = "resonance_dwm"
    description = "日周月共振：收盘>MA20 且 DIF>DEA（三周期同时）"
    default_params = {"column": "resonance_dwm"}

    def mask(self, df: pd.DataFrame) -> pd.Series:
        col = self.params["column"]
        if col not in df.columns:
            raise ValueError(f"缺少指标列 {col}，请先运行 indicators.compute_panel")

        flags = df[col].fillna(False).astype(bool)

        def _missing(column: str) -> int:
            return int(df[column].isna().sum()) if column in df.columns else int(len(df))

        self.detail = {
            "true_rows": int(flags.sum()),
            "daily_ok": int(df["resonance_daily"].fillna(False).astype(bool).sum())
            if "resonance_daily" in df.columns else None,
            "weekly_ok": int(df["resonance_weekly"].fillna(False).astype(bool).sum())
            if "resonance_weekly" in df.columns else None,
            "monthly_ok": int(df["resonance_monthly"].fillna(False).astype(bool).sum())
            if "resonance_monthly" in df.columns else None,
            "weekly_history_missing": _missing("weekly_ma20"),
            "monthly_history_missing": _missing("monthly_ma20"),
            "note": "history_missing 很大说明本地日线历史不足以形成周/月线 MA20",
        }
        return flags


__all__ = ["ResonanceDailyFilter", "ResonanceDwmFilter"]
