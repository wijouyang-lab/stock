"""
indicators.adjust — 前复权（qfq）计算，纯本地

公式（必须严格遵守）
-------------------
    qfq(price, t) = raw(price, t) × adj(t) / adj(basis_date)

- 只复权 **open / high / low / close / pre_close**；
  vol / amount / turnover_rate / total_mv 等一律不动（成交量金额本身不受除权影响）。
- 基准日 ``basis_date``：
    * 常规扫描 = 本次 panel 的最后一天；
    * ``--replay D`` = D（**绝不能用最新日做基准**，否则引入未来函数）。
- 硬约束：任何参与计算的 adj_factor，其 ``trade_date`` 必须 ``<= basis_date``，否则抛异常。

缺失处理
--------
- 个股某日 adj 缺失 → 用 ``<= 该日`` 最近一个交易日的前值（ffill）；
- 整段都没有 adj → ``ratio = 1``，标记 ``adj_missing=True``，不阻断计算（进 funnel 诊断）；
- ``basis_date`` 当天没有 adj（停牌）→ 用 ``<= basis_date`` 最近一个有值的。

只读原始数据
------------
本模块只在内存里算复权价，**不写回任何缓存**：磁盘上永远只存原始价 + adj_factor。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

#: 需要复权的价格列（顺序无关，缺失列自动跳过）
PRICE_COLS: tuple[str, ...] = ("open", "high", "low", "close", "pre_close")

#: 复权相关的新增列
ADJ_COLS: tuple[str, ...] = ("adj_factor", "adj_basis", "adj_ratio", "adj_missing")

#: adj_factor 长表的标准列
ADJ_LONG_COLS: tuple[str, ...] = ("ts_code", "trade_date", "adj_factor")


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------
def assert_no_future(adj_long: pd.DataFrame, basis_date: str) -> None:
    """硬约束：adj_factor 里不允许出现 ``trade_date > basis_date`` 的行。"""
    if adj_long is None or adj_long.empty or "trade_date" not in adj_long.columns:
        return
    basis = str(basis_date)
    future = adj_long.loc[adj_long["trade_date"].astype(str) > basis, "trade_date"]
    if not future.empty:
        worst = str(future.max())
        raise ValueError(
            f"复权因子包含基准日 {basis} 之后的数据（最大 {worst}），"
            "会引入未来函数，已拒绝计算。请检查 load_adj_long(basis_date=...) 的调用。"
        )


def empty_adj_long() -> pd.DataFrame:
    return pd.DataFrame(columns=list(ADJ_LONG_COLS), dtype="object")


# --------------------------------------------------------------------------
# 核心
# --------------------------------------------------------------------------
def build_basis(adj_long: pd.DataFrame, basis_date: str) -> pd.Series:
    """每只股票在 ``basis_date``（或之前最近一个）的 adj_factor。"""
    if adj_long is None or adj_long.empty:
        return pd.Series(dtype="float64")
    usable = adj_long[adj_long["trade_date"].astype(str) <= str(basis_date)]
    if usable.empty:
        return pd.Series(dtype="float64")
    usable = usable.sort_values(["ts_code", "trade_date"], kind="mergesort")
    return usable.groupby("ts_code")["adj_factor"].last()


def apply_qfq(
    daily: pd.DataFrame,
    adj_long: pd.DataFrame,
    basis_date: str,
    price_cols: Sequence[str] = PRICE_COLS,
) -> pd.DataFrame:
    """把日线面板前复权到 ``basis_date``。

    返回新的 DataFrame：价格列已替换为前复权价，并新增
    ``adj_factor`` / ``adj_basis`` / ``adj_ratio`` / ``adj_missing`` 四个诊断列。
    入参不会被修改。
    """
    basis_date = str(basis_date)
    if daily is None or daily.empty:
        return pd.DataFrame() if daily is None else daily.copy()

    assert_no_future(adj_long, basis_date)

    work = daily.copy()
    work["ts_code"] = work["ts_code"].astype(str)
    work["trade_date"] = work["trade_date"].astype(str)
    work = work.sort_values(["ts_code", "trade_date"], kind="mergesort")

    if adj_long is None or adj_long.empty:
        # 没有任何复权因子：ratio=1，全部标记缺失
        work["adj_factor"] = np.nan
        work["adj_basis"] = np.nan
        work["adj_ratio"] = 1.0
        work["adj_missing"] = True
        return work.reset_index(drop=True)

    adj = adj_long.loc[:, list(ADJ_LONG_COLS)].copy()
    adj["ts_code"] = adj["ts_code"].astype(str)
    adj["trade_date"] = adj["trade_date"].astype(str)
    adj = adj.sort_values(["ts_code", "trade_date"], kind="mergesort").drop_duplicates(
        subset=["ts_code", "trade_date"], keep="last"
    )

    merged = work.merge(adj, on=["ts_code", "trade_date"], how="left")
    # 个股某日缺失 → 用该日之前最近一个值
    merged["adj_factor"] = merged.groupby("ts_code", sort=False)["adj_factor"].ffill()

    basis = build_basis(adj, basis_date)
    merged["adj_basis"] = merged["ts_code"].map(basis)

    ratio = merged["adj_factor"] / merged["adj_basis"]
    merged["adj_missing"] = ratio.isna()
    merged["adj_ratio"] = ratio.fillna(1.0).astype("float64")

    for col in price_cols:
        if col in merged.columns:
            merged[col] = pd.to_numeric(merged[col], errors="coerce") * merged["adj_ratio"]

    return merged.reset_index(drop=True)


# --------------------------------------------------------------------------
# 除权日诊断
# --------------------------------------------------------------------------
def detect_adjust_events(
    adj_long: pd.DataFrame,
    basis_date: str,
    codes: Iterable[str] | None = None,
) -> pd.DataFrame:
    """找出 ``adj(t) != adj(t-1)`` 的日期（即除权除息/送转发生日）。

    只做诊断，不参与筛选。
    """
    cols = ["ts_code", "date", "adj_before", "adj_after", "ratio"]
    if adj_long is None or adj_long.empty:
        return pd.DataFrame(columns=cols)

    work = adj_long[adj_long["trade_date"].astype(str) <= str(basis_date)].copy()
    if codes is not None:
        work = work[work["ts_code"].isin(set(map(str, codes)))]
    if work.empty:
        return pd.DataFrame(columns=cols)

    work = work.sort_values(["ts_code", "trade_date"], kind="mergesort")
    work["adj_before"] = work.groupby("ts_code", sort=False)["adj_factor"].shift(1)
    events = work[work["adj_before"].notna() & (work["adj_factor"] != work["adj_before"])]
    if events.empty:
        return pd.DataFrame(columns=cols)

    out = pd.DataFrame(
        {
            "ts_code": events["ts_code"].to_numpy(),
            "date": events["trade_date"].to_numpy(),
            "adj_before": events["adj_before"].to_numpy(),
            "adj_after": events["adj_factor"].to_numpy(),
            "ratio": (events["adj_factor"] / events["adj_before"]).to_numpy(),
        }
    )
    return out.reset_index(drop=True)


def write_adjust_events(
    adj_long: pd.DataFrame,
    basis_date: str,
    log_dir: Path,
    codes: Iterable[str] | None = None,
) -> tuple[Path | None, int]:
    """把除权日诊断写到 ``output/logs/adjust_events_{basis_date}.csv``。

    返回 (文件路径, 行数)；没有除权事件时返回 (None, 0)。
    """
    events = detect_adjust_events(adj_long, basis_date, codes=codes)
    if events.empty:
        logger.info("除权日诊断：%s 之前没有检测到除权事件", basis_date)
        return None, 0

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"adjust_events_{basis_date}.csv"
    events.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info("除权日诊断：%d 条事件 -> %s", len(events), path.name)
    return path, len(events)


__all__ = [
    "PRICE_COLS",
    "ADJ_COLS",
    "ADJ_LONG_COLS",
    "assert_no_future",
    "empty_adj_long",
    "build_basis",
    "apply_qfq",
    "detect_adjust_events",
    "write_adjust_events",
]
