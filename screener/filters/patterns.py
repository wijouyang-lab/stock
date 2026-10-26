"""
screener.filters.patterns — 双重底 / 双重顶筛选

口径（已固定）：**只认突破确认 + 当前仍在颈线正确一侧**
--------------------------------------------------------
1. 双重底只有在出现"收盘价突破颈线"之后才算数；两个底形成后一直没突破的，一律不选。
   双重顶镜像：只有"收盘价跌破颈线"之后才算数。
2. **当前有效性**（第 5 步新增，默认开启）：
     双重底要求 ``当前 close > db_neckline``；
     双重顶要求 ``当前 close < dt_neckline``；
   突破后又跌回（或反弹回）颈线另一侧的失效形态不计入。
   对应参数：``require_above_neckline`` / ``require_below_neckline``，默认值在
   ``config.PATTERN_REQUIRE_ABOVE_NECKLINE`` / ``PATTERN_REQUIRE_BELOW_NECKLINE``。

"最近 N 根内突破过"由指标层的派生列承担（``double_bottom_5d/10d/20d``），
筛选器只读这些 bool 列，因此不会重复做形态扫描。

默认 ``lookback=10``（最近 10 个交易日内突破过），可用参数覆盖。
"""

from __future__ import annotations

import pandas as pd

import config
from screener import Filter, register_filter


class _PatternFilter(Filter):
    """双重底/顶筛选的公共实现。"""

    pattern: str = ""          # "double_bottom" / "double_top"
    meta_col: str = ""         # 颈线列
    direction: str = ""        # "above"：底（close > 颈线）；"below"：顶（close < 颈线）

    def _column(self, columns: set[str]) -> str:
        lookback = int(self.params.get("lookback", 10) or 1)
        windowed = f"{self.pattern}_{lookback}d"
        if windowed in columns:
            return windowed
        if self.pattern in columns:
            return self.pattern
        raise ValueError(
            f"缺少指标列 {windowed} / {self.pattern}，请先运行 indicators.compute_panel"
        )

    def _require_neckline(self) -> bool:
        """是否要求"当前仍在颈线正确一侧"（参数名随方向而定）。"""
        key = f"require_{self.direction}_neckline"
        if key in self.params:
            return bool(self.params[key])
        # 未显式传参时取 config 默认值
        return bool(
            config.PATTERN_REQUIRE_ABOVE_NECKLINE
            if self.direction == "above"
            else config.PATTERN_REQUIRE_BELOW_NECKLINE
        )

    def mask(self, df: pd.DataFrame) -> pd.Series:
        col = self._column(set(df.columns))
        flags = df[col].fillna(False).astype(bool)

        detail: dict[str, object] = {
            "column_used": col,
            "lookback": int(self.params.get("lookback", 10) or 1),
            "confirmed_rows": int(flags.sum()),
            "strict_flag_rows": int(df[self.pattern].fillna(False).astype(bool).sum())
            if self.pattern in df.columns else None,
            "note": "只有收盘突破/跌破颈线后才为 True；未突破的形态不计入",
        }
        if self.meta_col in df.columns:
            detail["with_neckline_metadata"] = int(df[self.meta_col].notna().sum())

        # ---- 当前有效性：收盘价必须仍在颈线正确一侧 ----
        require = self._require_neckline()
        detail[f"require_{self.direction}_neckline"] = require
        if require:
            if self.meta_col not in df.columns or "close" not in df.columns:
                raise ValueError(
                    f"缺少 {self.meta_col} / close，无法校验颈线位置；"
                    f"若要跳过该校验请传 require_{self.direction}_neckline=False"
                )
            price = pd.to_numeric(df["close"], errors="coerce")
            neck = pd.to_numeric(df[self.meta_col], errors="coerce")
            keep = (price > neck) if self.direction == "above" else (price < neck)
            before = int(flags.sum())
            flags = flags & keep.fillna(False).astype(bool)
            detail["neckline_filtered_out"] = before - int(flags.sum())
            detail["neckline_missing"] = int(neck.isna().sum())
            detail["neckline_side"] = f"close {'>' if self.direction == 'above' else '<'} {self.meta_col}"

        self.detail = detail
        return flags


@register_filter
class DoubleBottomFilter(_PatternFilter):
    """双重底（突破确认 + 当前仍在颈线上方）。"""

    name = "double_bottom"
    description = (
        "双重底：摆动点 n=5、价差≤3%、间隔≥10日、收盘突破颈线确认，"
        "且当前收盘仍在颈线之上（require_above_neckline）"
    )
    default_params = {
        "lookback": 10,
        "require_above_neckline": config.PATTERN_REQUIRE_ABOVE_NECKLINE,
    }
    pattern = "double_bottom"
    meta_col = "db_neckline"
    direction = "above"


@register_filter
class DoubleTopFilter(_PatternFilter):
    """双重顶（破位确认 + 当前仍在颈线下方）。"""

    name = "double_top"
    description = (
        "双重顶：摆动点 n=5、价差≤3%、间隔≥10日、收盘跌破颈线确认，"
        "且当前收盘仍在颈线之下（require_below_neckline）"
    )
    default_params = {
        "lookback": 10,
        "require_below_neckline": config.PATTERN_REQUIRE_BELOW_NECKLINE,
    }
    pattern = "double_top"
    meta_col = "dt_neckline"
    direction = "below"


__all__ = ["DoubleBottomFilter", "DoubleTopFilter"]
