"""
trigger.calendar — 交易日判定与最近交易日探测

权限约束：权限清单里没有 trade_cal，因此**默认不使用交易日历接口**。
这里采用"用 daily 接口探测"的方式判断交易日，有两条路径：

1. **区间探测（首选，1 次调用）**：`daily(start_date=..., end_date=..., limit=6000)` 返回按
   日期倒序的行情，取 `max(trade_date)` 就是最近的交易日。省额度，且不怕某一天偶发返回空表。
2. **逐日探测（回退）**：`daily(trade_date=YYYYMMDD)` 有数据 = 交易日，返回空 = 非交易日。

探测结果缓存在 data/cache/_trade_dates.json。为避免上游偶发返回空表把交易日
误判成休市并永久记住，"非交易日"结论只对距今 10 天以上的历史日期落缓存
（见 NEGATIVE_CACHE_LAG_DAYS）。离线（无 Token）时退化为"周一~周五视为交易日"的粗判。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from config import CACHE_DIR

logger = logging.getLogger(__name__)

CALENDAR_CACHE_FILE: Path = CACHE_DIR / "_trade_dates.json"

#: 允许向前回看的最大自然日数（覆盖春节/国庆等长假期）
MAX_LOOKBACK_DAYS = 30

#: "非交易日"结论的最小缓存年龄（天）。近期日期不缓存否定结论，
#: 避免上游偶发返回空表时把交易日误判为休市并永久记住。
NEGATIVE_CACHE_LAG_DAYS = 10


def _to_date(value: str | date | datetime | None) -> date:
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip().replace("-", "").replace("/", "")
    return datetime.strptime(s[:8], "%Y%m%d").date()


def to_yyyymmdd(value: str | date | datetime) -> str:
    """统一格式化为 Tushare 需要的 YYYYMMDD。"""
    return _to_date(value).strftime("%Y%m%d")


def is_weekend(value: str | date | datetime) -> bool:
    return _to_date(value).weekday() >= 5


class TradeCalendar:
    """交易日判定（带项目内持久缓存）。"""

    def __init__(self, cache_file: Path | None = None, client: Any | None = None) -> None:
        self.cache_file: Path = Path(cache_file) if cache_file else CALENDAR_CACHE_FILE
        self.client = client
        self._data: dict[str, bool] = self._load()

    # ------------------------------------------------------------ 持久缓存
    def _load(self) -> dict[str, bool]:
        if self.cache_file.is_file():
            try:
                raw = json.loads(self.cache_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return {str(k): bool(v) for k, v in raw.items()}
            except Exception as exc:  # noqa: BLE001
                logger.warning("交易日缓存损坏，已忽略：%s", exc)
        return {}

    def _save(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("交易日缓存写入失败：%s", exc)

    # ---------------------------------------------------------------- 判定
    def known(self, value: str | date | datetime) -> bool | None:
        """返回已知判定结果；未探测过返回 None。"""
        return self._data.get(to_yyyymmdd(value))

    def remember(self, value: str | date | datetime, is_trading: bool) -> None:
        self._data[to_yyyymmdd(value)] = bool(is_trading)
        self._save()

    def is_trading_day(self, value: str | date | datetime, probe: bool = True) -> bool:
        """判断是否为交易日。

        probe=False 时不联网，仅用缓存 + 周末规则（离线自检用）。

        缓存策略（防"毒化"）：
        - 探测到"是交易日"→ 立即缓存；
        - 探测到"不是交易日"，只有距今天超过 NEGATIVE_CACHE_LAG_DAYS 天的历史日期才缓存。
          近期日期不缓存，避免上游偶发返回空表时把交易日误判为休市并永久记住。
        """
        d = _to_date(value)
        if d.weekday() >= 5:          # 周末一定不是交易日，无需探测
            return False
        cached = self._data.get(to_yyyymmdd(d))
        if cached is not None:
            return cached
        if not probe or self.client is None:
            return True               # 离线退化：工作日视为交易日

        df = self.client.daily(trade_date=to_yyyymmdd(d))
        is_trading = not (df is None or len(df) == 0)
        if is_trading or d < date.today() - timedelta(days=NEGATIVE_CACHE_LAG_DAYS):
            self.remember(d, is_trading)
        logger.debug("探测交易日 %s -> %s", to_yyyymmdd(d), is_trading)
        return is_trading

    def probe_latest_by_range(
        self,
        ref: str | date | datetime | None = None,
        spans: tuple[int, ...] = (60, 400, 1500),
    ) -> str | None:
        """用一次区间查询直接定位最近的交易日（推荐路径）。

        比逐日探测更省额度，也不受"某一天偶发返回空表"影响。
        返回 YYYYMMDD；全部区间都为空时返回 None（调用方回退到逐日探测）。
        """
        if self.client is None:
            return None
        end = to_yyyymmdd(_to_date(ref))
        for span in spans:
            start = to_yyyymmdd(_to_date(ref) - timedelta(days=span))
            try:
                df = self.client.daily_range(start_date=start, end_date=end)
            except Exception as exc:  # noqa: BLE001 - 区间探测失败则退回逐日探测
                logger.warning("区间探测交易日失败（%s~%s）：%s", start, end, exc)
                return None
            if df is not None and not df.empty and "trade_date" in df.columns:
                latest = str(df["trade_date"].max())
                self.remember(latest, True)
                logger.info("区间探测到最近交易日：%s（区间 %s~%s）", latest, start, end)
                return latest
            logger.warning("区间 %s~%s 无日线数据，扩大回看范围", start, end)
        return None

    def latest_trade_date(
        self,
        ref: str | date | datetime | None = None,
        probe: bool = True,
        max_lookback: int = MAX_LOOKBACK_DAYS,
        prefer_range_probe: bool = True,
    ) -> str:
        """从 ref（默认今天）向前找最近的一个交易日，返回 YYYYMMDD。

        注意：当天是否为交易日需要等 daily 入库（15:00~17:00）后才能探测到，
        所以盘前运行时该方法会自然回退到上一个交易日。
        """
        ref = ref or date.today()
        if probe and self.client is not None and prefer_range_probe:
            latest = self.probe_latest_by_range(ref=ref)
            if latest:
                return latest

        d = _to_date(ref)
        for _ in range(max_lookback):
            if self.is_trading_day(d, probe=probe):
                return to_yyyymmdd(d)
            d -= timedelta(days=1)
        logger.warning("向前 %d 天未找到交易日，回退为 ref 当天", max_lookback)
        return to_yyyymmdd(ref)

    def recent_trade_dates(
        self,
        end: str | date | datetime | None = None,
        count: int = 5,
        probe: bool = True,
    ) -> list[str]:
        """返回截至 end（含）的最近 count 个交易日，升序排列。"""
        out: list[str] = []
        d = _to_date(end) if end else _to_date(self.latest_trade_date(probe=probe))
        guard = 0
        while len(out) < count and guard < count * 4 + MAX_LOOKBACK_DAYS:
            if self.is_trading_day(d, probe=probe):
                out.append(to_yyyymmdd(d))
            d -= timedelta(days=1)
            guard += 1
        return sorted(out)

    def stats(self) -> dict[str, Any]:
        return {
            "file": str(self.cache_file),
            "known_days": len(self._data),
            "trading_days": sum(1 for v in self._data.values() if v),
        }
