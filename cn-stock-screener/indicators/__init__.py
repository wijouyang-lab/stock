"""
indicators — 技术指标计算（纯本地计算，零网络、零文件 IO）

约定：

1. 每个指标是一个函数：`f(df: pd.DataFrame, **params) -> pd.DataFrame`
   输入 df 至少包含列：ts_code, trade_date, open, high, low, close, vol, amount
   输出是"新增若干列"的 DataFrame（同长度、同索引），列名统一小写蛇形命名。
   不允许修改输入 df（内部自行 copy）。

2. 用 `@register("名字", ...)` 注册，`compute_all()` 按注册顺序计算。
   注册发生在子模块被导入时，包的 `__init__` 末尾会统一导入所有子模块。

3. 已实现的模块（对应需求逐条实现）：

   | 文件 | 内容 | 是否注册为指标 |
   | --- | --- | --- |
   | `_util.py` | 排序/EMA/SMA/滚动窗口等纯计算工具 | — |
   | `resample.py` | 日线 → 周线(W-FRI) / 月线(ME) | 否（改变行数） |
   | `candles.py` | hammer / shooting_star | 是 |
   | `patterns.py` | double_bottom / double_top（只认突破确认） | 是 |
   | `momentum.py` | macd(12/26/9) / kdj(9/3/3) | 是 |
   | `volume.py` | turnover / volume_ratio(vol/MA5) | 是 |
   | `resonance.py` | 日周月共振（收盘>MA20 且 DIF>DEA） | 是 |
   | `selftest.py` | 指标级自检（合成数据验证 + 零调用断言） | — |

4. 列命名规范：
       ma5 / ma10 / ma20 / ma60            ema_fast / ema_slow
       dif / dea / macd                    rsv / k / d / j
       turnover / ma5_vol / volume_ratio_5
       hammer / shooting_star
       double_bottom / double_bottom_10d / db_neckline ...
       resonance_daily / resonance_weekly / resonance_monthly / resonance_dwm

5. `compute_panel(panel)` 面向"多股票面板"：先按 ts_code 分组，再逐组计算，
   最后拼回一张表（供 screener 直接使用）。
   注意：面板必须是**多日历史**（每只股票 ≥ 数十根 K 线），只有单日截面算不出 MACD/MA20。

6. `resample.py` 刻意不注册：它把日线变成周线/月线（行数变化），
   不满足"同长度、新增列"的契约，属于工具函数，供 resonance 调用。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import pandas as pd

logger = logging.getLogger(__name__)

#: 指标函数签名
IndicatorFunc = Callable[..., pd.DataFrame]


@dataclass
class IndicatorSpec:
    """一个指标的定义。"""

    name: str
    func: IndicatorFunc
    description: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    requires: tuple[str, ...] = ("open", "high", "low", "close", "vol")

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        return self.func(df, **self.params)


#: 指标注册表（第二阶段填充）
REGISTRY: dict[str, IndicatorSpec] = {}


def register(
    name: str,
    description: str = "",
    params: dict[str, Any] | None = None,
    requires: Iterable[str] | None = None,
) -> Callable[[IndicatorFunc], IndicatorFunc]:
    """注册指标的装饰器。

    用法::

        @register("ma", "简单移动平均", params={"periods": (5, 20)})
        def add_ma(df, periods=(5, 20)):
            ...
            return df
    """

    def deco(func: IndicatorFunc) -> IndicatorFunc:
        doc = (func.__doc__ or "").strip().splitlines()
        desc = description or (doc[0].strip() if doc else "")
        REGISTRY[name] = IndicatorSpec(
            name=name,
            func=func,
            description=desc,
            params=dict(params or {}),
            requires=tuple(requires) if requires else ("open", "high", "low", "close", "vol"),
        )
        return func

    return deco


def list_indicators() -> list[str]:
    """已注册指标名列表。"""
    return list(REGISTRY)


def describe() -> pd.DataFrame:
    """指标清单（用于 --self-test / GUI 展示）。"""
    return pd.DataFrame(
        [
            {
                "name": s.name,
                "description": s.description,
                "params": s.params,
                "requires": ",".join(s.requires),
            }
            for s in REGISTRY.values()
        ]
    )


def _check_columns(df: pd.DataFrame, spec: IndicatorSpec) -> None:
    missing = [c for c in spec.requires if c not in df.columns]
    if missing:
        raise ValueError(f"指标 {spec.name} 缺少必需列：{missing}")


def compute_all(df: pd.DataFrame, names: Iterable[str] | None = None) -> pd.DataFrame:
    """对单只股票（或单组）的日线计算全部（或指定）指标。

    第一阶段 REGISTRY 为空，因此这里等价于原样返回 df 的拷贝。
    """
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()
    out = df.copy()
    for name in (names or list(REGISTRY)):
        spec = REGISTRY.get(name)
        if spec is None:
            logger.warning("未注册的指标：%s", name)
            continue
        _check_columns(out, spec)
        out = spec.apply(out)
    return out


def compute_panel(
    panel: pd.DataFrame,
    names: Iterable[str] | None = None,
    code_col: str = "ts_code",
    last_trade_date: str | None = None,
) -> pd.DataFrame:
    """多股票面板计算：按 code_col 分组 -> compute_all -> 拼回。

    ``last_trade_date`` 不为空时，每只股票只保留该交易日一行（边算边丢）。
    筛选只需要"当天那一行"，保留 750 天历史会让内存翻倍（实测峰值 5.1 GB ——> 约 2 GB），
    所以 ``screen_local`` 走这条路径；默认 ``None`` 保持原行为（返回全历史）。
    """
    if panel is None or panel.empty:
        return pd.DataFrame() if panel is None else panel.copy()
    if code_col not in panel.columns:
        return compute_all(panel, names)
    drop_history = last_trade_date is not None and "trade_date" in panel.columns

    frames = []
    total = 0
    try:
        total = int(panel[code_col].nunique())
    except Exception:  # noqa: BLE001
        total = 0
    # 每 10% 打一条进度：全市场要算几分钟，没有进度会让人以为卡死
    step = max(1, total // 10) if total else 500
    t0 = time.time()
    for i, (code, grp) in enumerate(panel.groupby(code_col, sort=False), 1):
        g = grp.sort_values("trade_date") if "trade_date" in grp.columns else grp
        computed = compute_all(g, names)
        if drop_history:
            computed = computed.loc[computed["trade_date"] == last_trade_date]
            if computed.empty:          # 该股当天没有 bar（停牌等）
                continue
        computed[code_col] = code
        frames.append(computed)
        logger.debug("指标计算完成：%s (%d 行)", code, len(computed))
        if total and (i % step == 0 or i == total):
            logger.info("指标计算进度：%d/%d 只（%.0f%%），已用 %.0fs",
                        i, total, i * 100.0 / total, time.time() - t0)
    return pd.concat(frames, ignore_index=True) if frames else panel.iloc[0:0].copy()


__all__ = [
    "IndicatorSpec",
    "IndicatorFunc",
    "REGISTRY",
    "register",
    "list_indicators",
    "describe",
    "compute_all",
    "compute_panel",
]


# --------------------------------------------------------------------------
# 导入所有指标子模块 → 触发 @register 注册
#
# 必须放在本文件末尾（register/REGISTRY 已定义之后），
# 子模块里的 `from indicators import register` 才能拿到可用的注册函数。
# --------------------------------------------------------------------------
from indicators import _util  # noqa: E402,F401
from indicators import candles, momentum, patterns, resample, resonance, volume  # noqa: E402,F401

#: 需要多日历史才能算出有意义数值的指标（用于历史不足时提示）
HISTORY_SENSITIVE: tuple[str, ...] = ("macd", "resonance", "double_bottom", "double_top")
