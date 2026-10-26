"""
data.cache — 本地分片缓存

目录布局（全部位于项目内 data/cache/ 下）：

    data/cache/
      stock_basic.csv                 # 全市场股票列表（快照）
      daily/20250102.csv              # 日线，按 trade_date 分片
      daily_basic/20250102.csv        # 每日指标，按 trade_date 分片
      income/20240930.csv             # 利润表，按报告期分片
      balancesheet/20240930.csv
      cashflow/20240930.csv
      _meta.json                      # 每个数据集的写入时间、行数、来源

设计要点：
- 纯 CSV（utf-8-sig），不依赖 pyarrow，整包拷贝到任何机器都能直接读
- 分片键 = trade_date（行情类）或 period（财务类），避免重复拉取
- 写入走"临时文件 + 原子替换"，中途失败不会留下半个文件
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from config import CACHE_DATASETS, CACHE_DIR, CACHE_TTL

logger = logging.getLogger(__name__)

META_FILENAME = "_meta.json"
CSV_ENCODING = "utf-8-sig"


@dataclass
class ShardInfo:
    """单个缓存分片的元信息。"""

    dataset: str
    key: str
    path: Path
    rows: int = 0
    mtime: float = 0.0
    size_bytes: int = 0

    @property
    def modified_at(self) -> str:
        return (
            datetime.fromtimestamp(self.mtime).strftime("%Y-%m-%d %H:%M:%S")
            if self.mtime
            else "-"
        )


class LocalCache:
    """项目内本地缓存。所有路径均由 config 推导，与 CWD 无关。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root: Path = Path(root) if root is not None else CACHE_DIR
        self.root.mkdir(parents=True, exist_ok=True)
        self.meta_path: Path = self.root / META_FILENAME
        self._meta: dict[str, Any] = self._load_meta()

    # ------------------------------------------------------------- 元数据
    @property
    def meta(self) -> dict[str, dict[str, Any]]:
        """只读的元数据副本（dataset -> {shard_key: {rows, size_bytes, ...}}）。"""
        return {k: dict(v) for k, v in self._meta.items()}

    def _load_meta(self) -> dict[str, Any]:
        if self.meta_path.is_file():
            try:
                return json.loads(self.meta_path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("缓存元数据损坏，已忽略：%s", exc)
        return {}

    def _save_meta(self) -> None:
        try:
            self.meta_path.write_text(
                json.dumps(self._meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("缓存元数据写入失败：%s", exc)

    # --------------------------------------------------------------- 路径
    @staticmethod
    def normalize_key(key: str) -> str:
        return str(key).strip().replace("/", "-").replace("\\", "-")

    def dataset_dir(self, dataset: str) -> Path:
        if dataset not in CACHE_DATASETS:
            raise KeyError(f"未知数据集 {dataset}，已注册：{sorted(CACHE_DATASETS)}")
        return self.root if CACHE_DATASETS[dataset] == "snapshot" else self.root / dataset

    def shard_path(self, dataset: str, key: str = "snapshot") -> Path:
        d = self.dataset_dir(dataset)
        return d / f"{self.normalize_key(key)}.csv"

    # ------------------------------------------------------------ 读 / 写
    def exists(self, dataset: str, key: str = "snapshot") -> bool:
        p = self.shard_path(dataset, key)
        return p.is_file() and p.stat().st_size > 0

    def read(self, dataset: str, key: str = "snapshot") -> pd.DataFrame | None:
        p = self.shard_path(dataset, key)
        if not (p.is_file() and p.stat().st_size > 0):
            return None
        try:
            df = pd.read_csv(p, encoding=CSV_ENCODING, dtype=str, keep_default_na=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("缓存分片读取失败 %s：%s", p, exc)
            return None
        return df

    def write(
        self,
        dataset: str,
        key: str,
        df: pd.DataFrame,
        *,
        source: str = "tushare",
    ) -> Path:
        """原子写入一个分片，成功返回路径。"""
        p = self.shard_path(dataset, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        out = pd.DataFrame(df).copy()
        if "trade_date" in out.columns:
            out["trade_date"] = out["trade_date"].astype(str)
        if "end_date" in out.columns:
            out["end_date"] = out["end_date"].astype(str)

        fd, tmp_name = tempfile.mkstemp(prefix=f".{p.stem}.", suffix=".tmp", dir=str(p.parent))
        try:
            with open(fd, "w", encoding=CSV_ENCODING, newline="") as fh:
                out.to_csv(fh, index=False)
            Path(tmp_name).replace(p)
        except Exception:
            Path(tmp_name).unlink(missing_ok=True)
            raise

        stat = p.stat()
        self._meta.setdefault(dataset, {})[self.normalize_key(key)] = {
            "rows": int(len(out)),
            "mtime": stat.st_mtime,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "size_bytes": stat.st_size,
            "source": source,
        }
        self._save_meta()
        logger.debug("缓存写入 %s/%s：%d 行 -> %s", dataset, key, len(out), p.name)
        return p

    # ------------------------------------------------------------- 查询类
    def upsert(
        self,
        dataset: str,
        key: str,
        df: pd.DataFrame,
        on: str = "ts_code",
        source: str = "tushare",
    ) -> Path:
        """把新数据并入已有分片（按 on 列去重，新数据优先）。

        用于"按股票逐只拉取财务数据"的场景：同一个报告期分片被多次补充，
        最终仍是一个 period 分片文件。
        """
        old = self.read(dataset, key)
        if old is not None and not old.empty:
            combined = pd.concat([old, pd.DataFrame(df)], ignore_index=True)
            if on in combined.columns:
                combined = combined.drop_duplicates(subset=[on], keep="last")
        else:
            combined = pd.DataFrame(df)
        return self.write(dataset, key, combined, source=source)

    def is_fresh(self, dataset: str, key: str = "snapshot") -> bool:
        """缓存是否存在且未超过 TTL。"""
        p = self.shard_path(dataset, key)
        if not (p.is_file() and p.stat().st_size > 0):
            return False
        ttl = CACHE_TTL.get(dataset)
        if ttl is None:
            return True
        return (time.time() - p.stat().st_mtime) < ttl

    def list_shards(self, dataset: str) -> list[str]:
        d = self.dataset_dir(dataset)
        if not d.is_dir():
            return []
        return sorted(f.stem for f in d.glob("*.csv") if f.stat().st_size > 0)

    def shard_info(self, dataset: str, key: str = "snapshot") -> ShardInfo:
        p = self.shard_path(dataset, key)
        if not (p.is_file() and p.stat().st_size > 0):
            return ShardInfo(dataset=dataset, key=key, path=p)
        st = p.stat()
        meta_rows = (
            self._meta.get(dataset, {}).get(self.normalize_key(key), {}).get("rows", 0)
        )
        return ShardInfo(
            dataset=dataset,
            key=key,
            path=p,
            rows=int(meta_rows or 0),
            mtime=st.st_mtime,
            size_bytes=st.st_size,
        )

    def read_many(self, dataset: str, keys: Iterable[str]) -> pd.DataFrame:
        """读取多个分片并纵向拼接（缺分片自动跳过）。"""
        frames = []
        for k in keys:
            df = self.read(dataset, k)
            if df is not None and not df.empty:
                frames.append(df)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def read_all(self, dataset: str) -> pd.DataFrame:
        return self.read_many(dataset, self.list_shards(dataset))

    # --------------------------------------------------------------- 统计
    def summary(self) -> dict[str, Any]:
        """缓存概览，用于日志与 GUI 展示。"""
        out: dict[str, Any] = {"root": str(self.root), "datasets": {}}
        for ds in CACHE_DATASETS:
            if CACHE_DATASETS[ds] == "snapshot":
                info = self.shard_info(ds)
                out["datasets"][ds] = {
                    "shards": 1 if info.rows or info.mtime else 0,
                    "rows": info.rows,
                    "latest": info.key if info.mtime else None,
                    "modified_at": info.modified_at if info.mtime else None,
                    "size_bytes": info.size_bytes,
                }
                continue
            keys = self.list_shards(ds)
            rows_total = sum(
                int(self._meta.get(ds, {}).get(k, {}).get("rows", 0)) for k in keys
            )
            size_total = sum((self.dataset_dir(ds) / f"{k}.csv").stat().st_size for k in keys)
            out["datasets"][ds] = {
                "shards": len(keys),
                "rows": rows_total,
                "latest": keys[-1] if keys else None,
                "oldest": keys[0] if keys else None,
                "modified_at": (
                    self.shard_info(ds, keys[-1]).modified_at if keys else None
                ),
                "size_bytes": size_total,
            }
        return out

    def forget(self, dataset: str, key: str | None = None) -> int:
        """只从 ``_meta.json`` 移除条目（**不删任何文件**）。

        用于"删文件的同时收拾元数据"（例如自检写的临时分片），以及事后清理
        指向已不存在文件的孤儿条目。返回移除的条目数。
        """
        if key is None:
            removed = len(self._meta.get(dataset, {}) or {})
            self._meta.pop(dataset, None)
        else:
            entries = self._meta.get(dataset, {}) or {}
            removed = 1 if entries.pop(self.normalize_key(key), None) is not None else 0
            if not entries:
                self._meta.pop(dataset, None)
        if removed:
            self._save_meta()
        return removed

    def clear(self, dataset: str | None = None) -> int:
        """删除缓存（dataset=None 时清空全部）。返回删除的分片数。"""
        n = 0
        names = list(CACHE_DATASETS) if dataset is None else [dataset]
        for ds in names:
            if CACHE_DATASETS[ds] == "snapshot":
                p = self.shard_path(ds)
                if p.is_file():
                    p.unlink()
                    n += 1
            else:
                d = self.dataset_dir(ds)
                if d.is_dir():
                    n += len(list(d.glob("*.csv")))
                    shutil.rmtree(d, ignore_errors=True)
            self._meta.pop(ds, None)
        self._save_meta()
        return n
