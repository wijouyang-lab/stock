"""
screener — 筛选逻辑与规则引擎

约定：

1. 规则（Filter）是一个对象，包含：
       name         : 规则名（唯一，注册表键）
       description  : 一句话说明
       default_params / params : 默认参数与生效参数
       mask(df)     : 返回 bool Series（True = 保留）
       apply(df)    : 由基类实现，产出 (结果 df, 统计)，统计会进 funnel

2. 用 `@register_filter` 注册；`ScreenerEngine.run(universe)` 按注册顺序（即传入顺序）
   串联执行，每一步都记录"输入行数 -> 输出行数 -> 剔除数"，产出可审计的漏斗。

3. 入参 `universe` 是"目标交易日截面 + 计算好的指标列"（每只股票一行），
   由 `main.py` 的本地流水线装配：先对**多日历史面板**算指标，再取目标交易日的截面。

4. 结果统一经 `ScreenerEngine.finalize()` 排序、截断，再由 `main.py` 输出
   CSV（明细）+ JSON（汇总）+ funnel JSON（逐规则漏斗）。

5. 已实现的规则在 `screener/filters/` 子包里（导入即注册）：
       turnover / resonance_daily / resonance_dwm
       double_bottom / double_top / hammer / shooting_star
       macd_bull / kdj_bull
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable

import pandas as pd

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 规则基类 / 注册表
# --------------------------------------------------------------------------
class Filter:
    """筛选规则基类。

    子类只需实现 ``mask(df) -> pd.Series[bool]``（True = 保留），并声明：
      - ``name``           规则名（唯一，注册表键）
      - ``description``    一句话说明
      - ``default_params`` 默认参数（与命令行/配置合并，后者优先）

    审计信息：子类在 ``mask()`` 里可以往 ``self.detail`` 写运行时算出来的中间量
    （例如换手率的 80 分位阈值），会被 ``apply()`` 收进 funnel 统计并写入 funnel JSON。
    """

    name: str = "base"
    description: str = ""
    default_params: dict[str, Any] = {}

    def __init__(self, enabled: bool = True, **params: Any) -> None:
        self.enabled = enabled
        self.params: dict[str, Any] = {**self.default_params, **params}
        self.detail: dict[str, Any] = {}
        #: 引擎在开跑前注入的**原始全市场截面**（未经过任何上游规则过滤）。
        #: 只有需要"横截面分位"的规则会用到它；默认 None 表示没注入。
        self.universe: pd.DataFrame | None = None

    def bind_universe(self, universe: pd.DataFrame) -> None:
        """由 ``ScreenerEngine`` 在筛选开始前调用，注入全市场截面。

        为什么要这个钩子：filter 的 ``mask(df)`` 只拿到**上游过滤后**的子集
        （例如 turnover 之后的 1001 只）。若在该子集上算分位数，阈值会随漏斗位置漂移。
        需要"全市场分位"的规则（如量比的 percentile 模式）必须用**原始 universe**。

        这是一个 opt-in 钩子：默认实现只是保存引用，不影响任何既有规则的 ``mask`` 语义。
        """
        self.universe = universe

    def mask(self, df: pd.DataFrame) -> pd.Series:  # pragma: no cover - 抽象
        raise NotImplementedError

    def apply(self, df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
        """应用规则，返回 (结果 df, 统计)。"""
        self.detail = {}
        stat: dict[str, Any] = {
            "rule": self.name,
            "description": self.description,
            "enabled": bool(self.enabled),
            "params": dict(self.params),
            "in": 0 if df is None else int(len(df)),
            "out": 0 if df is None else int(len(df)),
            "dropped": 0,
            "detail": {},
        }
        if not self.enabled or df is None or df.empty:
            return df, stat

        m = self.mask(df).reindex(df.index).fillna(False).astype(bool)
        out = df[m]
        stat.update(
            {
                "out": int(len(out)),
                "dropped": int(len(df) - len(out)),
                "detail": dict(self.detail),
            }
        )
        logger.info(
            "规则 %-18s %5d -> %5d (剔除 %d) %s",
            self.name, stat["in"], stat["out"], stat["dropped"],
            self.detail or "",
        )
        return out, stat


#: 规则注册表
FILTER_REGISTRY: dict[str, type[Filter]] = {}


def register_filter(cls: type[Filter]) -> type[Filter]:
    """注册筛选规则的类装饰器。"""
    FILTER_REGISTRY[cls.name] = cls
    return cls


def list_filters() -> list[str]:
    return list(FILTER_REGISTRY)


def build_filters(specs: Iterable[dict[str, Any]] | None = None) -> list[Filter]:
    """按配置构造规则实例列表（第二阶段从 config.SCREEN_PARAMS 读取）。"""
    specs = list(specs or [])
    out: list[Filter] = []
    for spec in specs:
        name = spec.get("name", "")
        cls = FILTER_REGISTRY.get(name)
        if cls is None:
            logger.warning("未注册的筛选规则：%s（已跳过）", name)
            continue
        out.append(cls(**{k: v for k, v in spec.items() if k != "name"}))
    return out


# --------------------------------------------------------------------------
# 结果 / 引擎
# --------------------------------------------------------------------------
@dataclass
class ScreenResult:
    """一次筛选的完整结果。"""

    trade_date: str = ""
    mode: str = "quote"
    universe_size: int = 0
    matched: pd.DataFrame = field(default_factory=pd.DataFrame)
    funnel: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    @property
    def matched_count(self) -> int:
        return int(len(self.matched))

    def describe(self) -> str:
        return (
            f"筛选结果：{self.matched_count} 只（股票池 {self.universe_size} 只，"
            f"{len(self.funnel)} 条规则）"
        )


class ScreenerEngine:
    """规则引擎：把规则串联跑一遍，产出 ScreenResult。"""

    def __init__(self, filters: Iterable[Filter] | None = None) -> None:
        self.filters: list[Filter] = list(filters or [])

    def run(
        self,
        universe: pd.DataFrame,
        trade_date: str = "",
        mode: str = "quote",
        sort_by: tuple[str, ...] | None = None,
    ) -> ScreenResult:
        res = ScreenResult(trade_date=trade_date, mode=mode, universe_size=0 if universe is None else len(universe))
        if universe is None or universe.empty:
            res.warnings.append("股票池为空，未执行任何规则")
            return res

        df = universe
        # 注入原始全市场截面（在链开始前、对每条规则各注入一次）：
        # 分位/排名类规则必须基于全市场，不能基于上游子集。
        for flt in self.filters:
            try:
                flt.bind_universe(universe)
            except Exception as exc:  # noqa: BLE001 - 自定义规则可能没有该钩子
                logger.debug("规则 %s 不支持 bind_universe：%s", flt.name, exc)
        for flt in self.filters:
            try:
                df, stat = flt.apply(df)
                res.funnel.append(stat)
            except Exception as exc:  # noqa: BLE001 - 单条规则出错不应中断整体
                logger.exception("规则 %s 执行失败：%s", flt.name, exc)
                res.warnings.append(f"规则 {flt.name} 执行失败：{exc}")

        res.matched = self.finalize(df, sort_by=sort_by) if sort_by else self.finalize(df)
        logger.info(res.describe())
        return res

    @staticmethod
    def finalize(df: pd.DataFrame, max_rows: int = 0, sort_by: tuple[str, ...] = ("turnover", "ts_code")) -> pd.DataFrame:
        """收尾：排序 + 截断 + 重置索引。

        默认按换手率降序（最活跃的排前面），换手率不存在时退化为按代码升序，
        保证同样的输入每次输出顺序一致（便于 diff 两次结果）。
        """
        if df is None or df.empty:
            return pd.DataFrame() if df is None else df
        out = df.copy()
        keys = [c for c in sort_by if c in out.columns]
        if keys:
            ascending = [c == "ts_code" for c in keys]
            try:
                out = out.sort_values(keys, ascending=ascending, kind="mergesort")
            except Exception:  # noqa: BLE001 - 排序失败不影响结果正确性
                pass
        if max_rows and len(out) > max_rows:
            out = out.head(max_rows)
        return out.reset_index(drop=True)


def screen(universe: pd.DataFrame, **kw: Any) -> ScreenResult:
    """便捷函数：用默认配置跑一次筛选（第一阶段无规则 → 原样返回股票池）。"""
    return ScreenerEngine(filters=build_filters()).run(universe, **kw)


__all__ = [
    "Filter",
    "FILTER_REGISTRY",
    "register_filter",
    "list_filters",
    "build_filters",
    "ScreenResult",
    "ScreenerEngine",
    "screen",
]


# --------------------------------------------------------------------------
# 导入内置规则子包 → 触发 @register_filter 注册
# 放在文件末尾：此刻 Filter / register_filter 已定义，子模块 `from screener import ...` 才可用。
# --------------------------------------------------------------------------
from screener import filters  # noqa: E402,F401
