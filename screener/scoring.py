"""
screener.scoring — 评分与推荐分层（--score）

设计原则
--------
1. **opt-in**：不加 ``--score`` 时行为与之前完全一致（仍是布尔漏斗 + 按换手率排序）。
2. **不改规则**：评分只读指标列，不修改任何 filter 的判定；漏斗照旧。
3. **可解释**：总分由 7 个分项相加，每一项的得分单独输出成列（``pts_*``），
   CSV 里能直接看到"这 82 分是怎么来的"。
4. **满分 100**：权重表在 ``config.SCORE_WEIGHTS``，自检里有"权重累加 = 100"的断言。

分项与权重（默认）
------------------
| 分项 | 条件 | 满分 | 说明 |
| --- | --- | --- | --- |
| 趋势（共振） | `resonance_dwm` / 仅 `resonance_daily` | 30 / 12 | **二选一，不叠加** |
| 形态（双底） | 10 日内突破颈线 且 当前仍在颈线上方 | 20 | 与 double_bottom 规则同口径 |
| 量能（放量） | `volume_surge`（≥1.5 或 ≥80 分位，看配置） | 12 | 缩量不加分 |
| 动量 MACD | `dif > dea` | 10 | |
| 动量 KDJ | `k > d` | 8 | |
| 活跃度 | 换手率分档 ≥8% / ≥5% / ≥3% / ≥2% | 12 / 10 / 8 / 4 | 分档取最高档 |
| K 线 | 锤子线 | 8 | |

分档（``config.SCORE_TIERS``）
------------------------------
    核心 ≥85   观察 ≥75   候选 ≥65   低于 65 不入榜
另加一道 ``SCORE_MIN_HIT``（默认 2）：至少命中 2 个独立信号，避免"单项高分"误入榜。
档位名额由 ``config.TIER_CAPS`` 控制（核心 5 / 观察 10 / 候选 15）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 分项定义
# --------------------------------------------------------------------------
@dataclass
class ScoreComponent:
    """一个评分分项。"""

    key: str            # 列名前缀 / 配置键
    label: str          # 中文名（报告用）
    max_points: float
    kind: str = "flag"  # flag：布尔列给分；tier：按数值分档；either：二选一
    column: str = ""    # 读取的指标列
    detail: str = ""


#: 分项顺序（也是 CSV 里 pts_* 列的顺序）
COMPONENT_LABELS: dict[str, str] = {
    "resonance": "趋势共振",
    "double_bottom": "双重底",
    "volume_surge": "放量",
    "macd_bull": "MACD多头",
    "kdj_bull": "KDJ多头",
    "turnover": "活跃度",
    "hammer": "锤子线",
}


def _numeric(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce")


def _flag(df: pd.DataFrame, col: str) -> pd.Series:
    """布尔列（缺失/NaN 视为 False）。"""
    if col not in df.columns:
        return pd.Series(False, index=df.index)
    return df[col].fillna(False).astype(bool)


def _turnover_points(df: pd.DataFrame, tiers: Iterable[tuple[float, float]]) -> pd.Series:
    """换手率分档打分（取满足的最高档）。"""
    values = _numeric(df, "turnover")
    if "turnover" not in df.columns:
        values = _numeric(df, "turnover_rate")
    pts = pd.Series(0.0, index=df.index)
    for threshold, points in sorted(tiers, key=lambda t: t[0]):   # 从低到高叠加
        pts = pts.where(~(values >= threshold), float(points))
    return pts.where(values.notna(), 0.0)


# --------------------------------------------------------------------------
# 评分器
# --------------------------------------------------------------------------
@dataclass
class ScoreResult:
    """一次评分的统计摘要。"""

    total: int = 0
    scored: int = 0
    tiers: dict[str, int] = field(default_factory=dict)
    min_score: float = 0.0
    min_hit: int = 0
    components: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "scored": self.scored,
            "tiers": dict(self.tiers),
            "min_score": self.min_score,
            "min_hit": self.min_hit,
            "components": list(self.components),
        }


class Scorer:
    """把指标列映射成 0~100 的总分 + 分档。"""

    def __init__(
        self,
        weights: dict[str, float] | None = None,
        turnover_tiers: Iterable[tuple[float, float]] | None = None,
        tiers: dict[str, float] | None = None,
        tier_labels: dict[str, str] | None = None,
        min_hit: int | None = None,
        require_above_neckline: bool | None = None,
    ) -> None:
        self.weights = dict(weights or config.SCORE_WEIGHTS)
        self.turnover_tiers = list(turnover_tiers or config.TURNOVER_SCORE_TIERS)
        self.tiers = dict(tiers or config.SCORE_TIERS)
        self.labels = dict(tier_labels or config.SCORE_TIER_LABELS)
        self.min_hit = int(config.SCORE_MIN_HIT if min_hit is None else min_hit)
        self.require_above_neckline = (
            config.PATTERN_REQUIRE_ABOVE_NECKLINE
            if require_above_neckline is None
            else bool(require_above_neckline)
        )
        self.max_total = self._max_points()
        self.last: ScoreResult = ScoreResult(min_score=0.0, min_hit=self.min_hit)

    # ------------------------------------------------------------ 元信息
    def _max_points(self) -> float:
        """满分 = 各分项权重之和；趋势项取两组中的较大者（不叠加）。"""
        trend = max(self.weights.get("resonance_dwm", 0), self.weights.get("resonance_daily", 0))
        others = sum(
            self.weights.get(k, 0)
            for k in ("double_bottom", "volume_surge", "macd_bull", "kdj_bull", "turnover", "hammer")
        )
        return float(trend + others)

    def describe(self) -> list[dict[str, Any]]:
        """权重表（README / 报告用）。"""
        rows = [
            {"key": "resonance", "label": COMPONENT_LABELS["resonance"],
             "points": self.weights.get("resonance_dwm", 0),
             "rule": f"日周月共振 {self.weights.get('resonance_dwm', 0)} 分；"
                     f"仅日线共振 {self.weights.get('resonance_daily', 0)} 分（二选一）"},
            {"key": "double_bottom", "label": COMPONENT_LABELS["double_bottom"],
             "points": self.weights.get("double_bottom", 0), "rule": "双底突破确认且仍在颈线上方"},
            {"key": "volume_surge", "label": COMPONENT_LABELS["volume_surge"],
             "points": self.weights.get("volume_surge", 0), "rule": "当日放量"},
            {"key": "macd_bull", "label": COMPONENT_LABELS["macd_bull"],
             "points": self.weights.get("macd_bull", 0), "rule": "DIF > DEA"},
            {"key": "kdj_bull", "label": COMPONENT_LABELS["kdj_bull"],
             "points": self.weights.get("kdj_bull", 0), "rule": "K > D"},
            {"key": "turnover", "label": COMPONENT_LABELS["turnover"],
             "points": self.weights.get("turnover", 0),
             "rule": "分档：" + "、".join(f"≥{t:g}% → {p:g}" for t, p in self.turnover_tiers)},
            {"key": "hammer", "label": COMPONENT_LABELS["hammer"],
             "points": self.weights.get("hammer", 0), "rule": "锤子线"},
        ]
        return rows

    # -------------------------------------------------------------- 打分
    def _components(self, df: pd.DataFrame) -> dict[str, pd.Series]:
        """各分项的原始得分（0~满分）。"""
        w = self.weights
        out: dict[str, pd.Series] = {}

        # 趋势：dwm 与 daily 二选一（dwm 成立时必定 daily 也成立，取高者）
        dwm = _flag(df, "resonance_dwm")
        daily = _flag(df, "resonance_daily")
        trend = pd.Series(0.0, index=df.index)
        trend = trend.mask(daily, float(w.get("resonance_daily", 0)))
        trend = trend.mask(dwm, float(w.get("resonance_dwm", 0)))
        out["resonance"] = trend

        # 双底：与 double_bottom 规则同口径（突破确认 + 可选颈线位置）
        bottom = _flag(df, "double_bottom_10d") if "double_bottom_10d" in df.columns else _flag(df, "double_bottom")
        if self.require_above_neckline and "db_neckline" in df.columns and "close" in df.columns:
            bottom = bottom & (_numeric(df, "close") > _numeric(df, "db_neckline")).fillna(False)
        out["double_bottom"] = pd.Series(
            np.where(bottom, float(w.get("double_bottom", 0)), 0.0), index=df.index
        )

        # 放量：与 volume_surge 默认口径一致（≥1.5；若配置成 percentile 则 ≥ 全市场分位）
        surge_pts = 0.0
        if "volume_ratio_5" in df.columns:
            ratio = _numeric(df, "volume_ratio_5")
            mode = str(config.FILTER_PARAMS.get("volume_surge", {}).get("mode", "absolute")).lower()
            if mode == "percentile":
                pct = float(config.FILTER_PARAMS["volume_surge"].get("surge_percentile", 80))
                valid = ratio.dropna()
                threshold = float(valid.quantile(pct / 100.0)) if not valid.empty else np.nan
            else:
                threshold = float(config.FILTER_PARAMS.get("volume_surge", {}).get("min_ratio", 1.5))
            surge_pts = w.get("volume_surge", 0)
            out["volume_surge"] = pd.Series(
                np.where((ratio >= threshold).fillna(False), float(surge_pts), 0.0), index=df.index
            )
        else:
            out["volume_surge"] = pd.Series(0.0, index=df.index)

        out["macd_bull"] = pd.Series(
            np.where(_flag(df, "macd_bull") if "macd_bull" in df.columns
                     else (_numeric(df, "dif") > _numeric(df, "dea")).fillna(False),
                     float(w.get("macd_bull", 0)), 0.0), index=df.index)
        out["kdj_bull"] = pd.Series(
            np.where(_flag(df, "kdj_bull") if "kdj_bull" in df.columns
                     else (_numeric(df, "k") > _numeric(df, "d")).fillna(False),
                     float(w.get("kdj_bull", 0)), 0.0), index=df.index)
        out["turnover"] = _turnover_points(df, self.turnover_tiers) * (
            w.get("turnover", 0) / max(p for _, p in self.turnover_tiers)
        )
        out["hammer"] = pd.Series(
            np.where(_flag(df, "hammer"), float(w.get("hammer", 0)), 0.0), index=df.index
        )
        return out

    def score(self, df: pd.DataFrame) -> pd.DataFrame:
        """给面板加上 score / hit_count / tier 与各分项得分列。

        不修改入参，返回新的 DataFrame（列会追加在末尾）。
        """
        if df is None or df.empty:
            out = df.copy() if df is not None else pd.DataFrame()
            for col in ("score", "hit_count", "tier"):
                out[col] = [] if out.empty else pd.Series(dtype=object)
            return out

        out = df.copy()
        parts = self._components(out)

        for key, series in parts.items():
            out[f"pts_{key}"] = series.round(2)

        total = sum(parts[k] for k in parts)
        total = total.clip(upper=self.max_total)
        out["score"] = total.round(1).astype("float64")

        # 命中几个独立信号（趋势组算 1 个）
        hit = sum((parts[k] > 0).astype(int) for k in parts)
        out["hit_count"] = hit.astype(int)

        # 分档：分数门槛 + 至少命中 min_hit 个信号
        score = out["score"].to_numpy()
        core = float(self.tiers.get("core", 85))
        watch = float(self.tiers.get("watch", 75))
        cand = float(self.tiers.get("candidate", 65))
        tier = np.select([score >= core, score >= watch, score >= cand],
                         [self.labels.get("core", "核心"),
                          self.labels.get("watch", "观察"),
                          self.labels.get("candidate", "候选")],
                         default="")
        tier = np.where(out["hit_count"].to_numpy() >= self.min_hit, tier, "")
        out["tier"] = tier

        counts = {self.labels.get(k, k): int((out["tier"] == self.labels.get(k, k)).sum())
                  for k in ("core", "watch", "candidate")}
        self.last = ScoreResult(
            total=int(len(out)),
            scored=int((out["score"] > 0).sum()),
            tiers=counts,
            min_score=cand,
            min_hit=self.min_hit,
            components=self.describe(),
        )
        logger.info("评分完成：%d 行，分档 %s（满分 %.0f，最低入榜 %.0f，最少命中 %d 项）",
                    len(out), counts, self.max_total, cand, self.min_hit)
        return out


# --------------------------------------------------------------------------
# 档位裁剪
# --------------------------------------------------------------------------
def apply_tier_caps(
    df: pd.DataFrame,
    caps: dict[str, int] | None = None,
    labels: dict[str, str] | None = None,
    drop_unranked: bool = True,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """按档位限量，返回 (裁剪后的 df, 统计)。

    ``drop_unranked=True`` 时丢弃 ``tier == ""``（低于候选线或信号数不足）的行。
    """
    caps = dict(caps if caps is not None else config.TIER_CAPS)
    labels = dict(labels or config.SCORE_TIER_LABELS)
    if df is None or df.empty:
        return (df if df is not None else pd.DataFrame()), {"capped": 0, "dropped": 0, "per_tier": {}}

    work = df
    dropped = 0
    if drop_unranked and "tier" in work.columns:
        dropped = int((work["tier"].astype(str) == "").sum())
        work = work[work["tier"].astype(str) != ""]

    kept: list[pd.DataFrame] = []
    per_tier: dict[str, Any] = {}
    capped = 0
    for key in ("core", "watch", "candidate"):
        label = labels.get(key, key)
        sub = work[work["tier"].astype(str) == label]
        if sub.empty:
            per_tier[label] = {"available": 0, "kept": 0}
            continue
        sub = sub.sort_values(["score", "hit_count"], ascending=[False, False], kind="mergesort")
        cap = int(caps.get(key, 0) or 0)
        if cap and len(sub) > cap:
            capped += len(sub) - cap
            sub = sub.head(cap)
        per_tier[label] = {"available": int(len(work[work["tier"] == label])), "kept": int(len(sub))}
        kept.append(sub)

    out = pd.concat(kept, ignore_index=True) if kept else work.head(0)
    if not out.empty:
        out = out.sort_values(["score", "hit_count"], ascending=[False, False], kind="mergesort")
    return out, {"capped": capped, "dropped": dropped, "per_tier": per_tier}


__all__ = [
    "ScoreComponent",
    "ScoreResult",
    "Scorer",
    "COMPONENT_LABELS",
    "apply_tier_caps",
]
