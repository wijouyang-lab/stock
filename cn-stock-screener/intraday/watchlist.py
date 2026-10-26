"""
intraday.watchlist — 从收盘扫描结果里读出"盯梢名单"

盯梢不重算全市场，只盯**最近一次收盘扫描入榜**的那些股票（默认前 30 只）。
名单来源是 ``output/reports/`` 下最新的、带 ``score`` 列的 CSV（即 ``--score`` 产物）。

为什么盯着名单而不是全市场
--------------------------
全市场重算需要读 750 个日线分片（~1 GB）+ 逐只算指标，耗时 5~6 分钟；
盘中每 15 分钟做一次既慢又无意义。盘中真正需要的是"我选的票现在怎么样了"，
所以只取几十只、只比价格与量能——秒级完成。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import pandas as pd

import config

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"(?:replay|screen)_(\d{8})_")


@dataclass
class WatchItem:
    """一只待盯梢的股票（含扫描日算出来的关键价位）。"""

    ts_code: str
    name: str = ""
    score: float = float("nan")
    tier: str = ""
    close: float = float("nan")        # 扫描日收盘（= 基准日价，等于当天原始价）
    ma20: float = float("nan")
    neckline: float = float("nan")     # 双底颈线
    volume: float = float("nan")       # 扫描日成交量（手）
    scan_date: str = ""

    def as_dict(self) -> dict:
        return {
            "ts_code": self.ts_code,
            "name": self.name,
            "score": self.score,
            "tier": self.tier,
            "close": self.close,
            "ma20": self.ma20,
            "neckline": self.neckline,
            "volume": self.volume,
            "scan_date": self.scan_date,
        }


@dataclass
class Watchlist:
    """一份盯梢名单。"""

    items: list[WatchItem] = field(default_factory=list)
    source: Path | None = None
    scan_date: str = ""
    warnings: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.items)

    @property
    def codes(self) -> list[str]:
        return [i.ts_code for i in self.items]


def _num(value) -> float:
    try:
        v = float(value)
        return v
    except (TypeError, ValueError):
        return float("nan")


def latest_scored_report(report_dir: Path | None = None) -> Path | None:
    """找最新的、带 ``score`` 列的扫描 CSV。"""
    d = Path(report_dir) if report_dir else config.REPORT_DIR
    if not d.is_dir():
        return None
    candidates = sorted(
        [p for p in d.glob("*.csv") if p.name.startswith(("replay_", "screen_"))],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates[:40]:
        try:
            head = pd.read_csv(path, nrows=1)
        except Exception:  # noqa: BLE001
            continue
        if "score" in head.columns:
            return path
    return None


def load_watchlist(
    top_n: int | None = None,
    report_dir: Path | None = None,
    report_file: Path | None = None,
) -> Watchlist:
    """读取盯梢名单（按 score 降序取前 N 只）。"""
    top_n = int(config.INTRADAY_WATCH_TOP_N if top_n is None else top_n)
    path = Path(report_file) if report_file else latest_scored_report(report_dir)
    wl = Watchlist(source=path)

    if path is None or not path.is_file():
        wl.warnings.append(
            "没有找到带评分的扫描结果；请先跑一次评分排行"
            "（exe 版：直接双击 cn-stock-screener.exe；"
            "源码版：python main.py --cli --local-only --score）"
        )
        return wl

    try:
        df = pd.read_csv(path)
    except Exception as exc:  # noqa: BLE001
        wl.warnings.append(f"读取榜单失败：{type(exc).__name__}: {exc}")
        return wl

    if "score" not in df.columns:
        wl.warnings.append(f"{path.name} 没有 score 列（不是 --score 产物）")
        return wl

    m = _DATE_RE.search(path.name)
    wl.scan_date = m.group(1) if m else ""
    df = df.sort_values(["score"], ascending=False, kind="mergesort").head(top_n)

    neck_col = "db_neckline" if "db_neckline" in df.columns else None
    for row in df.itertuples(index=False):
        d = row._asdict()
        wl.items.append(
            WatchItem(
                ts_code=str(d.get("ts_code", "")),
                name=str(d.get("name", "")),
                score=_num(d.get("score")),
                tier=str(d.get("tier", "") or ""),
                close=_num(d.get("close")),
                ma20=_num(d.get("ma20")),
                neckline=_num(d.get(neck_col)) if neck_col else float("nan"),
                volume=_num(d.get("vol")),
                scan_date=wl.scan_date,
            )
        )

    # 名单陈旧检查：扫描日距今超过 5 个自然日就提示
    if wl.scan_date:
        from datetime import date, datetime

        try:
            age = (date.today() - datetime.strptime(wl.scan_date, "%Y%m%d").date()).days
            if age > 5:
                wl.warnings.append(
                    f"榜单扫描日 {wl.scan_date} 距今 {age} 天，价格空间可能已偏移；"
                    "建议先重跑收盘扫描（--score）"
                )
        except ValueError:
            pass

    logger.info("盯梢名单：%d 只（来自 %s，扫描日 %s）", len(wl.items), path.name, wl.scan_date)
    return wl


def filter_codes(watchlist: Watchlist, codes: Iterable[str] | None = None) -> Watchlist:
    """按给定代码裁剪名单（便于手工指定盯哪几只）。"""
    if not codes:
        return watchlist
    wanted = {str(c).strip().upper() for c in codes if str(c).strip()}
    kept = [i for i in watchlist.items if i.ts_code.upper() in wanted]
    return Watchlist(items=kept, source=watchlist.source,
                     scan_date=watchlist.scan_date, warnings=list(watchlist.warnings))


__all__ = ["WatchItem", "Watchlist", "load_watchlist", "latest_scored_report", "filter_codes"]
