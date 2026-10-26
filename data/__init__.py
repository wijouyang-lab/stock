"""
data — Tushare 数据层（完全自包含）

对外主要入口：

    from data import DataLoader, LocalCache, TushareClient, SyncResult

    loader = DataLoader()
    result = loader.sync(mode="quote")      # 只拉行情
    result = loader.sync(mode="full")       # 行情 + 三大报表
    print(result.describe())

模块划分：
    tushare_client.py  Tushare 调用封装（权限白名单 + 限流 + 重试）
    cache.py           本地分片缓存（按 trade_date / period 分片）
    loader.py          数据编排（股票列表 / 日线 / 每日指标 / 三大报表 / 股票池）
"""

from __future__ import annotations

from data.cache import LocalCache, ShardInfo
from data.loader import (
    FINANCIAL_DATASETS,
    DataLoader,
    SyncResult,
    recent_periods,
)
from data.tushare_client import (
    DependencyMissingError,
    ForbiddenApiError,
    PermissionDeniedError,
    RateLimiter,
    RateLimitError,
    TokenMissingError,
    TushareClient,
    TushareError,
    make_client,
)

__all__ = [
    "LocalCache",
    "ShardInfo",
    "DataLoader",
    "SyncResult",
    "FINANCIAL_DATASETS",
    "recent_periods",
    "TushareClient",
    "TushareError",
    "TokenMissingError",
    "PermissionDeniedError",
    "RateLimitError",
    "ForbiddenApiError",
    "DependencyMissingError",
    "RateLimiter",
    "make_client",
]
