"""
screener.filters —— 技术指标筛选规则集（导入即注册）

每个模块对应一组指标，规则用 ``@register_filter`` 注册到 ``screener.FILTER_REGISTRY``：

    volume.py       turnover         换手率 ≥ 3% 且 ≥ 全市场 80 分位
                    volume_surge     当日放量：vol / 前5日均量(不含当日) ≥ 1.5
                    volume_shrink    当日缩量：vol / 前5日均量(不含当日) ≤ 0.7
    resonance.py    resonance_daily  日线：收盘>MA20 且 DIF>DEA
                    resonance_dwm    日/周/月三周期同时满足
    patterns.py     double_bottom    双重底突破确认（默认看最近 10 根内是否突破）
                    double_top       双重顶跌破确认
    candles.py      hammer           锤子线（目标交易日当根）
                    shooting_star    射击之星
    momentum.py     macd_bull        DIF > DEA
                    kdj_bull         K > D

设计注意
--------
- 规则之间是 **AND** 串联（漏斗），顺序有意义：
  ``turnover`` 必须排第一，因为它算"全市场 80 分位"时分母是当前传入的池，
  放到其它规则后面会把分位数算窄。
- 所有规则只读 DataFrame，不联网、不写文件。
- 规则失败不会中断整体：``ScreenerEngine`` 会捕获异常、记录 warning 并继续。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from config import DEFAULT_FILTER_CHAIN, FILTER_PARAMS
from screener import Filter, build_filters
from screener.filters import candles, momentum, patterns, resonance, volume  # noqa: F401
from screener.filters.candles import HammerFilter, ShootingStarFilter
from screener.filters.momentum import KdjBullFilter, MacdBullFilter
from screener.filters.patterns import DoubleBottomFilter, DoubleTopFilter
from screener.filters.resonance import ResonanceDailyFilter, ResonanceDwmFilter
from screener.filters.volume import TurnoverFilter, VolumeShrinkFilter, VolumeSurgeFilter

logger = logging.getLogger(__name__)

#: 默认筛选链（与 config.DEFAULT_FILTER_CHAIN 对齐，这里显式列出便于阅读）
DEFAULT_CHAIN: tuple[str, ...] = tuple(DEFAULT_FILTER_CHAIN)


def default_chain(chain: Iterable[str | dict] | None = None) -> list[Filter]:
    """按名称构造默认筛选链（参数取自 ``config.FILTER_PARAMS``）。

    支持两种写法混合：

    - 字符串：只用配置里的默认参数；
    - 字典：``{"name": "volume_surge", "mode": "percentile"}``，字典里的键覆盖配置。

    用法::

        filters = default_chain()                       # turnover → resonance_dwm → double_bottom
        filters = default_chain(["turnover", "hammer"]) # 自定义顺序
        filters = default_chain([
            "turnover",
            {"name": "volume_surge", "mode": "percentile"},   # 临时切成分位模式
        ])
    """
    items = list(chain or DEFAULT_CHAIN)
    specs: list[dict] = []
    for item in items:
        if isinstance(item, dict):
            name = str(item.get("name", ""))
            override = {k: v for k, v in item.items() if k != "name"}
        else:
            name = str(item)
            override = {}
        specs.append({"name": name, **FILTER_PARAMS.get(name, {}), **override})
    return build_filters(specs)


__all__ = [
    "DEFAULT_CHAIN",
    "default_chain",
    "TurnoverFilter",
    "VolumeSurgeFilter",
    "VolumeShrinkFilter",
    "ResonanceDailyFilter",
    "ResonanceDwmFilter",
    "DoubleBottomFilter",
    "DoubleTopFilter",
    "HammerFilter",
    "ShootingStarFilter",
    "MacdBullFilter",
    "KdjBullFilter",
]
