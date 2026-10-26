"""
screener.filters.volume — 换手率筛选

需求口径：**换手率 ≥ 3%，且 ≥ 全市场 80 分位**（两者取较严者）。

"全市场分位"的实现说明
----------------------
分位数在**当前传入的 DataFrame** 上计算。因此：

- 该规则应排在筛选链第一位（默认链就是这么排的），此时传入的就是全市场截面，
  分位数等于真实的"全市场 80 分位"；
- 如果排在其它规则之后，分母变成已经缩小过的池，分位数会被抬高（更严格），
  这通常不是想要的效果，funnel 里会记录 ``scope_rows`` 以便核对。

缺失值处理：换手率缺失（NaN）的股票一律不通过，并计入 ``missing`` 统计。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from screener import Filter, register_filter

@register_filter
class TurnoverFilter(Filter):
    """换手率 ≥ min_turnover 且 ≥ 全市场 percentile 分位。"""

    name = "turnover"
    description = "换手率 ≥ 3% 且 ≥ 全市场 80 分位"
    default_params = {
        "min_turnover": 3.0,     # 绝对下限（%）
        "percentile": 0.80,      # 全市场分位
        "column": "turnover",    # 指标列名（indicators.volume.turnover 产出）
    }

    def mask(self, df: pd.DataFrame) -> pd.Series:
        col = self.params["column"]
        if col not in df.columns:
            # 若只有接口给的 turnover_rate，也允许直接使用
            if "turnover_rate" in df.columns:
                col = "turnover_rate"
            else:
                raise ValueError(
                    f"缺少换手率列（{self.params['column']} / turnover_rate）；"
                    "请先运行 indicators.compute_panel 或检查 daily_basic 是否已入库"
                )

        values = pd.to_numeric(df[col], errors="coerce")
        valid = values.dropna()

        q = float(valid.quantile(self.params["percentile"])) if len(valid) else float("nan")
        floor = float(self.params["min_turnover"])
        threshold = max(floor, q) if np.isfinite(q) else floor

        self.detail = {
            "column": col,
            "scope_rows": int(len(df)),
            "valid_rows": int(len(valid)),
            "missing": int(len(df) - len(valid)),
            "percentile": float(self.params["percentile"]),
            "q_value": None if not np.isfinite(q) else round(q, 4),
            "floor": floor,
            "threshold": round(threshold, 4),
            "threshold_rule": "max(floor, q)",
            "median": None if not len(valid) else round(float(valid.median()), 4),
        }
        return values >= threshold


# --------------------------------------------------------------------------
# 量能：放量 / 缩量（基于 volume_ratio_5 = vol / 前 5 日均量，不含当日）
# --------------------------------------------------------------------------
class _VolumeRatioFilter(Filter):
    """量比类筛选的公共实现（放量 / 缩量）。

    两种阈值模式：

    - ``mode="absolute"``（默认）：固定阈值（放量 ≥1.5、缩量 ≤0.7）；
    - ``mode="percentile"``：横截面分位（放量 ≥ 全市场 80 分位、缩量 ≤ 全市场 20 分位）。
      **分位在原始全市场截面上计算**（``self.universe``，由引擎注入），
      不受漏斗上游过滤影响；未注入时回退到当前输入并在 funnel 里标注 ``reference="input"``。

    量比的分母（前 N 日均量，不含当日）由指标层 ``indicators.volume.volume_ratio`` 预算好，
    筛选器只读列——截面数据一行一只股票，算不了滚动均值。

    次新股（日线不足 ma_window+1 根）的 ratio 为 NaN：记入 funnel 的 ``missing``，
    判定为不命中，不抛异常、不阻断。
    """

    direction: str = ""      # "ge"（放量） / "le"（缩量）
    abs_key: str = ""        # min_ratio / max_ratio
    pctl_key: str = ""       # surge_percentile / shrink_percentile

    def _resolve_column(self, df: pd.DataFrame) -> str:
        window = int(self.params.get("ma_window", 5) or 5)
        for candidate in (f"volume_ratio_{window}", "volume_ratio_5"):
            if candidate in df.columns:
                return candidate
        raise ValueError(
            f"缺少量比列 volume_ratio_{window} / volume_ratio_5，"
            "请先运行 indicators.compute_panel（或检查 vol 列是否存在）"
        )

    def _threshold(self) -> float:
        return float(self.params[self.abs_key])

    # ---- 分位阈值：必须基于全市场 ----
    def _reference_ratio(self, df: pd.DataFrame, col: str) -> tuple[pd.Series, str]:
        """取"用于算分位"的量比序列，返回 (序列, 来源标记)。"""
        uni = self.universe
        if isinstance(uni, pd.DataFrame) and not uni.empty and col in uni.columns:
            ref = pd.to_numeric(uni[col], errors="coerce").dropna()
            if not ref.empty:
                return ref, "universe"
        return pd.to_numeric(df[col], errors="coerce").dropna(), "input"

    def _resolve_threshold(self, df: pd.DataFrame, col: str) -> tuple[float, str, dict]:
        """返回 (阈值, 比较方向, 审计信息)。"""
        mode = str(self.params.get("mode", "absolute")).lower()
        comp = ">=" if self.direction == "ge" else "<="
        if mode == "percentile":
            ref, source = self._reference_ratio(df, col)
            pct = float(self.params[self.pctl_key])
            threshold = float(ref.quantile(pct / 100.0)) if not ref.empty else float("nan")
            info = {
                "mode": "percentile",
                "percentile": pct,
                "threshold": None if not np.isfinite(threshold) else round(threshold, 4),
                "threshold_source": source,          # universe / input
                "reference_rows": int(len(ref)),
            }
            return threshold, comp, info

        threshold = float(self.params[self.abs_key])
        info = {"mode": "absolute", "threshold": threshold, "threshold_source": "config"}
        return threshold, comp, info

    def mask(self, df: pd.DataFrame) -> pd.Series:
        col = self._resolve_column(df)
        ratio = pd.to_numeric(df[col], errors="coerce")
        threshold, comp, info = self._resolve_threshold(df, col)

        flags = (ratio >= threshold) if comp == ">=" else (ratio <= threshold)
        flags = flags.fillna(False)

        self.detail = {
            "column_used": col,
            "ma_window": int(self.params.get("ma_window", 5) or 5),
            "comparison": comp,
            **info,
            "true_rows": int(flags.sum()),
            "valid_rows": int(ratio.notna().sum()),
            "missing": int(ratio.isna().sum()),
            "median_ratio": round(float(ratio.median()), 4) if ratio.notna().any() else None,
            "note": "量比分母=前 ma_window 日均量（不含当日）；percentile 模式的分位在全市场算",
        }
        return flags

    def apply(self, df: pd.DataFrame):
        """在基类基础上给结果补一列 ``threshold``，方便 CSV 直接看到判定阈值。"""
        out, stat = super().apply(df)
        if out is not None and not out.empty:
            thr = (stat.get("detail") or {}).get("threshold")
            out = out.assign(threshold=thr if thr is not None else float("nan"))
        return out, stat


@register_filter
class VolumeSurgeFilter(_VolumeRatioFilter):
    """当日放量：absolute 模式 ≥1.5；percentile 模式 ≥ 全市场 80 分位。"""

    name = "volume_surge"
    description = "当日放量：vol/前5日均量(不含当日) ≥1.5；或 ≥ 全市场80分位（mode=percentile）"
    default_params = {
        "mode": "absolute",
        "min_ratio": 1.5,
        "surge_percentile": 80,
        "ma_window": 5,
    }
    direction = "ge"
    abs_key = "min_ratio"
    pctl_key = "surge_percentile"


@register_filter
class VolumeShrinkFilter(_VolumeRatioFilter):
    """当日缩量：absolute 模式 ≤0.7；percentile 模式 ≤ 全市场 20 分位。"""

    name = "volume_shrink"
    description = "当日缩量：vol/前5日均量(不含当日) ≤0.7；或 ≤ 全市场20分位（mode=percentile）"
    default_params = {
        "mode": "absolute",
        "max_ratio": 0.7,
        "shrink_percentile": 20,
        "ma_window": 5,
    }
    direction = "le"
    abs_key = "max_ratio"
    pctl_key = "shrink_percentile"


__all__ = ["TurnoverFilter", "VolumeSurgeFilter", "VolumeShrinkFilter"]
