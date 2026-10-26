"""
data.integrity — 缓存完整性校验（**纯本地，零网络调用**）

校验对象
--------
`data/cache/daily/` 与 `data/cache/daily_basic/` 下每个分片 CSV：

1. **行数 ≥ min_rows**（默认 5000）：全市场单日应该有 5000+ 行，行数太少说明拉取被截断；
2. **trade_date 唯一**：分片内只允许出现 1 个 trade_date，且必须等于文件名；
3. **ts_code 不重复**：同一分片内每只股票只能出现一次（重复=重复拉取/拼接错误）；
4. **必需列存在**：至少要有 ts_code / trade_date，否则后续指标计算必错。

另外核对 `data/cache/_meta.json` 与实际文件是否一致：

- `meta_missing`      ：文件存在但 meta 里没有条目
- `orphan`            ：meta 里有条目但文件不存在（或已空）
- `rows_mismatch`     ：meta 记录的行数 ≠ 实际行数
- `size_mismatch`     ：meta 记录的字节数 ≠ 实际字节数

设计原则
--------
- **只读**：本模块绝不删除/修改任何分片。发现问题只报告，由人决定怎么处理。
- **不联网**：只读本地文件。
- **可独立运行**：``python -m data.integrity`` 直接打印报告。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import pandas as pd

from config import CACHE_DATASETS
from data.cache import LocalCache

logger = logging.getLogger(__name__)

#: 需要做"全市场行数下限"校验的数据集（按交易日分片的行情类）
DAILY_DATASETS: tuple[str, ...] = ("daily", "daily_basic", "adj_factor")

#: 分片必须包含的列
REQUIRED_COLUMNS: tuple[str, ...] = ("ts_code", "trade_date")

#: 默认行数下限：全市场单日 ≥ 5000 行
DEFAULT_MIN_ROWS = 5000


@dataclass
class Issue:
    """一条缓存问题。"""

    dataset: str
    shard: str
    kind: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "dataset": self.dataset,
            "shard": self.shard,
            "kind": self.kind,
            "detail": self.detail,
        }

    def __str__(self) -> str:
        return f"[{self.dataset}/{self.shard}] {self.kind}: {self.detail}"


@dataclass
class IntegrityReport:
    """一次完整性校验的结果。"""

    inventory: list[dict] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues

    def summary(self) -> str:
        n_issue = len(self.issues)
        return (
            f"检查 {self.checked} 个分片，问题 {n_issue} 个"
            + ("" if n_issue == 0 else "：" + "; ".join(str(i) for i in self.issues[:5])
               + (" …" if n_issue > 5 else ""))
        )

    def as_dict(self) -> dict:
        return {
            "checked": self.checked,
            "issues": [i.as_dict() for i in self.issues],
            "inventory": self.inventory,
        }


# --------------------------------------------------------------------------
# 单分片
# --------------------------------------------------------------------------
def _check_shard(
    cache: LocalCache,
    dataset: str,
    key: str,
    min_rows: int,
) -> tuple[dict, list[Issue]]:
    """校验一个分片，返回 (清单条目, 问题列表)。"""
    issues: list[Issue] = []
    path = cache.shard_path(dataset, key)
    info: dict = {
        "dataset": dataset,
        "shard": key,
        "file": path.name,
        "rows": 0,
        "ts_codes": 0,
        "trade_dates": 0,
        "size_bytes": path.stat().st_size if path.is_file() else 0,
    }

    try:
        # 只读两列：够做行数/唯一性校验，且比整表读取快数倍（500 个分片时差别明显）
        df = pd.read_csv(
            path,
            usecols=lambda c: c in REQUIRED_COLUMNS,
            dtype=str,
            keep_default_na=False,
            encoding="utf-8-sig",
        )
    except Exception as exc:  # noqa: BLE001
        issues.append(Issue(dataset, key, "unreadable", f"{type(exc).__name__}: {exc}"))
        return info, issues

    rows = int(len(df))
    info["rows"] = rows

    missing_cols = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing_cols:
        issues.append(Issue(dataset, key, "missing_columns", f"缺少列 {missing_cols}"))
        return info, issues

    if rows < min_rows:
        issues.append(Issue(dataset, key, "rows_below_min", f"{rows} 行 < {min_rows}"))

    dates = df["trade_date"].astype(str).str.strip()
    uniq_dates = sorted(dates.unique().tolist())
    info["trade_dates"] = len(uniq_dates)
    if len(uniq_dates) != 1:
        issues.append(
            Issue(dataset, key, "trade_date_not_unique",
                  f"分片内有 {len(uniq_dates)} 个 trade_date：{uniq_dates[:5]}")
        )
    elif uniq_dates[0] != key:
        issues.append(
            Issue(dataset, key, "trade_date_mismatch",
                  f"分片名 {key} 与内容 trade_date={uniq_dates[0]} 不一致")
        )

    codes = df["ts_code"].astype(str).str.strip()
    n_unique_codes = int(codes.nunique())
    info["ts_codes"] = n_unique_codes
    dup = rows - n_unique_codes
    if dup > 0:
        sample = codes[codes.duplicated()].unique()[:5].tolist()
        issues.append(
            Issue(dataset, key, "ts_code_duplicated",
                  f"{dup} 行重复，示例 {sample}")
        )

    return info, issues


# --------------------------------------------------------------------------
# _meta.json
# --------------------------------------------------------------------------
def check_meta(cache: LocalCache, datasets: Iterable[str] | None = None) -> list[Issue]:
    """核对 ``_meta.json`` 与实际文件（行数 / 字节数 / 存在性）。"""
    issues: list[Issue] = []
    datasets = list(datasets or CACHE_DATASETS)
    meta = cache.meta

    for dataset in datasets:
        entries: dict = meta.get(dataset, {}) or {}
        on_disk = set(cache.list_shards(dataset)) if CACHE_DATASETS[dataset] != "snapshot" else (
            {"snapshot"} if cache.exists(dataset) else set()
        )

        for key, entry in entries.items():
            path = cache.shard_path(dataset, key)
            if not path.is_file() or path.stat().st_size == 0:
                issues.append(Issue(dataset, key, "orphan",
                                    f"meta 有条目但文件不存在/为空：{path.name}"))
                continue
            size = path.stat().st_size
            if int(entry.get("size_bytes", -1)) != size:
                issues.append(Issue(dataset, key, "size_mismatch",
                                    f"meta={entry.get('size_bytes')} 实际={size}"))
            try:
                with open(path, "rb") as fh:      # 只数行，不解析 CSV（快）
                    actual_rows = max(0, sum(1 for _ in fh) - 1)
            except Exception as exc:  # noqa: BLE001
                issues.append(Issue(dataset, key, "unreadable", str(exc)))
                continue
            if int(entry.get("rows", -1)) != actual_rows:
                issues.append(Issue(dataset, key, "rows_mismatch",
                                    f"meta={entry.get('rows')} 实际={actual_rows}"))

        for key in on_disk - set(entries):
            issues.append(Issue(dataset, key, "meta_missing",
                                "文件存在但 _meta.json 里没有条目（例如手工拷贝进来的分片）"))

    return issues


# --------------------------------------------------------------------------
# 总入口
# --------------------------------------------------------------------------
def check_cache(
    cache: LocalCache | None = None,
    datasets: Iterable[str] = DAILY_DATASETS,
    min_rows: int = DEFAULT_MIN_ROWS,
    check_meta_file: bool = True,
) -> IntegrityReport:
    """跑一遍完整性校验，返回报告（只读，不修改任何文件）。"""
    cache = cache or LocalCache()
    report = IntegrityReport()

    for dataset in datasets:
        for key in cache.list_shards(dataset):
            info, issues = _check_shard(cache, dataset, key, min_rows)
            report.inventory.append(info)
            report.issues.extend(issues)
            report.checked += 1

    if check_meta_file:
        report.issues.extend(check_meta(cache))

    logger.info("缓存完整性：%s", report.summary())
    return report


def inventory_table(report: IntegrityReport) -> pd.DataFrame:
    """把清单转成便于打印的表（按数据集 + 日期排序）。"""
    if not report.inventory:
        return pd.DataFrame(columns=["dataset", "shard", "rows", "ts_codes", "size_bytes"])
    df = pd.DataFrame(report.inventory)
    return df.sort_values(["dataset", "shard"], kind="mergesort").reset_index(drop=True)


if __name__ == "__main__":  # pragma: no cover - 手工运行
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    rep = check_cache()
    tbl = inventory_table(rep)
    if not tbl.empty:
        print(tbl.to_string(index=False))
    print()
    if rep.ok:
        print(f"issues: []  （{rep.checked} 个分片全部通过）")
    else:
        print(f"issues（{len(rep.issues)} 条，未做任何删除）：")
        for issue in rep.issues:
            print(f"  - {issue}")
    raise SystemExit(0 if rep.ok else 1)
