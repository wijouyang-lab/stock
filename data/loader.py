"""
data.loader — 数据编排层

把"Tushare 接口 + 本地缓存 + 交易日判定"组合成一次完整的数据同步：

    sync(mode="full")   → stock_basic + daily + daily_basic + income/balancesheet/cashflow
    sync(mode="quote")  → stock_basic + daily + daily_basic（不碰财务数据）

产出：
    - data/cache/ 下的分片文件
    - 内存中的"全市场面板" DataFrame（后续交给 indicators / screener）
    - SyncResult 摘要（供日志、GUI、自检使用）

所有路径来自 config，与 CWD 无关。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import pandas as pd

from config import (
    DAILY_BACKFILL_DAYS,
    DEFAULT_INIT_DAYS,
    UNIVERSE_EXCLUDE_BJ,
    UNIVERSE_EXCLUDE_DELISTED,
    UNIVERSE_EXCLUDE_ST,
    UNIVERSE_MIN_AMOUNT,
    UNIVERSE_MIN_LIST_DAYS,
    UNIVERSE_MIN_TOTAL_MV,
)
from data.cache import LocalCache
from data.tushare_client import TushareClient, TushareError
from trigger.calendar import TradeCalendar, to_yyyymmdd

logger = logging.getLogger(__name__)

FINANCIAL_DATASETS = ("income", "balancesheet", "cashflow")


# --------------------------------------------------------------------------
# 结果容器
# --------------------------------------------------------------------------
@dataclass
class SyncResult:
    """一次数据同步的结果摘要。"""

    mode: str = "quote"
    trade_date: str = ""
    period: str | None = None
    universe_size: int = 0
    rows_daily: int = 0
    rows_daily_basic: int = 0
    rows_adj_factor: int = 0
    rows_stock_basic: int = 0
    days_requested: int = 0                               # 计划补齐的交易日数
    walk_api_calls: int = 0                               # "边探边拉"阶段的 API 调用数
    holiday_probes: int = 0                               # 其中因休市返回空表的探测次数
    rows_financials: dict[str, int] = field(default_factory=dict)
    financial_mode: str = "none"                          # bulk | per_stock | cache | none
    fetched: list[str] = field(default_factory=list)      # 实际联网拉取的分片
    from_cache: list[str] = field(default_factory=list)   # 命中本地缓存的分片
    api_calls: int = 0
    warnings: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    skipped: bool = False
    skip_reason: str = ""

    def counts(self) -> dict[str, int]:
        """按数据集统计"新拉 / 命中缓存"的分片数（回补报告用）。"""
        out: dict[str, int] = {}
        for prefix in ("daily", "daily_basic", "adj_factor"):
            out[prefix] = sum(1 for x in self.fetched if x.split(":")[0] == prefix)
            out[f"{prefix}_cached"] = sum(1 for x in self.from_cache if x.split(":")[0] == prefix)
        return out

    def describe(self) -> str:
        if self.skipped:
            return f"已跳过：{self.skip_reason}"
        parts = [
            f"mode={self.mode}",
            f"trade_date={self.trade_date}",
            f"股票池={self.universe_size} 只",
            f"日线={self.rows_daily} 行",
            f"每日指标={self.rows_daily_basic} 行",
            f"复权因子={self.rows_adj_factor} 行",
        ]
        if self.period:
            fin = "/".join(f"{k}:{v}" for k, v in self.rows_financials.items()) or "-"
            parts.append(f"财务期={self.period}({fin}, {self.financial_mode})")
        parts.append(f"API调用={self.api_calls}")
        parts.append(f"命中缓存={len(self.from_cache)}")
        parts.append(f"耗时={self.elapsed:.1f}s")
        return " | ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "trade_date": self.trade_date,
            "period": self.period,
            "financial_mode": self.financial_mode,
            "universe_size": self.universe_size,
            "days_requested": self.days_requested,
            "walk_api_calls": self.walk_api_calls,
            "holiday_probes": self.holiday_probes,
            "rows": {
                "stock_basic": self.rows_stock_basic,
                "daily": self.rows_daily,
                "daily_basic": self.rows_daily_basic,
                "adj_factor": self.rows_adj_factor,
                "financials": dict(self.rows_financials),
            },
            "counts": self.counts(),
            "fetched": list(self.fetched),
            "from_cache": list(self.from_cache),
            "api_calls": self.api_calls,
            "warnings": list(self.warnings),
            "elapsed": round(self.elapsed, 2),
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
        }


# --------------------------------------------------------------------------
# 报告期工具
# --------------------------------------------------------------------------
QUARTER_ENDS = ("0331", "0630", "0930", "1231")


def recent_periods(ref: date | None = None, count: int = 4, lag_days: int = 20) -> list[str]:
    """返回最近 count 个"已过披露滞后"的报告期，从新到旧。

    A 股定期报告披露截止：一季报 4/30、半年报 8/31、三季报 10/31、年报 4/30。
    这里用 lag_days 做保守滞后，避免拉到还没披露完的报告期（空表）。
    """
    ref = ref or date.today()
    ref = ref - timedelta(days=lag_days)
    periods: list[str] = []
    year = ref.year
    while len(periods) < count and year >= ref.year - 5:
        for q in reversed(QUARTER_ENDS):
            p = f"{year}{q}"
            if datetime.strptime(p, "%Y%m%d").date() <= ref:
                periods.append(p)
                if len(periods) >= count:
                    break
        year -= 1
    return periods


# --------------------------------------------------------------------------
# 加载器
# --------------------------------------------------------------------------
class DataLoader:
    """面向本项目的数据加载器。"""

    def __init__(
        self,
        client: TushareClient | None = None,
        cache: LocalCache | None = None,
        calendar: TradeCalendar | None = None,
    ) -> None:
        self.cache = cache or LocalCache()
        self.client = client or TushareClient()
        self.calendar = calendar or TradeCalendar(client=self.client)
        #: 最近一次 load_history_panel 实际用到的交易日（诊断用）
        self.last_history_dates: list[str] = []
        #: 最近一次 _collect_trade_dates 的统计（调用数 / 休市探测数）
        self.last_walk_stats: dict[str, int] = {}

    # ------------------------------------------------------------ 股票列表
    def load_stock_basic(self, force: bool = False) -> pd.DataFrame:
        """全市场股票列表（含行业分类）。"""
        if not force and self.cache.is_fresh("stock_basic"):
            df = self.cache.read("stock_basic")
            if df is not None and not df.empty:
                logger.info("股票列表：命中缓存 %d 行", len(df))
                return df

        df = self.client.stock_basic(list_status="L")
        if df is None or df.empty:
            raise TushareError("stock_basic 返回空表，请检查 Token 权限")
        self.cache.write("stock_basic", "snapshot", df)
        logger.info("股票列表：拉取 %d 行并已缓存", len(df))
        return df

    # ---------------------------------------------------------------- 日线
    def load_daily(self, trade_date: str, force: bool = False, quiet: bool = False) -> pd.DataFrame:
        """某交易日的全市场日线（按 trade_date 分片缓存）。

        ``quiet=True`` 时不对空表打 WARNING（回补时逐日向前探路会大量命中休市日）。
        """
        trade_date = to_yyyymmdd(trade_date)
        if not force and self.cache.is_fresh("daily", trade_date):
            df = self.cache.read("daily", trade_date)
            if df is not None and not df.empty:
                logger.info("日线 %s：命中缓存 %d 行", trade_date, len(df))
                return df

        df = self.client.daily(trade_date=trade_date)
        if df is None or df.empty:
            if quiet:
                logger.debug("日线 %s：空表（休市或未入库）", trade_date)
            else:
                logger.warning("日线 %s：接口返回空表（非交易日或当日行情尚未入库）", trade_date)
            return pd.DataFrame()
        self.cache.write("daily", trade_date, df)
        logger.info("日线 %s：拉取 %d 行并已缓存", trade_date, len(df))
        return df

    # ------------------------------------------------------------ 复权因子
    def load_adj_factor(
        self,
        trade_date: str,
        force: bool = False,
        quiet: bool = False,
    ) -> pd.DataFrame:
        """某交易日的全市场复权因子（按 trade_date 分片缓存）。

        只存原始值（ts_code / trade_date / adj_factor），**绝不缓存复权后的价格**。
        """
        trade_date = to_yyyymmdd(trade_date)
        if not force and self.cache.is_fresh("adj_factor", trade_date):
            df = self.cache.read("adj_factor", trade_date)
            if df is not None and not df.empty:
                logger.debug("复权因子 %s：命中缓存 %d 行", trade_date, len(df))
                return df

        df = self.client.adj_factor(trade_date=trade_date)
        if df is None or df.empty:
            if quiet:
                logger.debug("复权因子 %s：空表", trade_date)
            else:
                logger.warning("复权因子 %s：接口返回空表", trade_date)
            return pd.DataFrame()
        self.cache.write("adj_factor", trade_date, df)
        logger.info("复权因子 %s：拉取 %d 行并已缓存", trade_date, len(df))
        return df

    def load_adj_long(
        self,
        basis_date: str,
        codes: Iterable[str] | None = None,
        use_derived_cache: bool = True,
    ) -> pd.DataFrame:
        """把 ``<= basis_date`` 的所有 adj_factor 分片**一次性**拼成长表。

        返回列：ts_code / trade_date / adj_factor（trade_date 为 YYYYMMDD 字符串）。
        硬约束：返回行里绝不允许出现 ``trade_date > basis_date``（见断言）。

        效率：分片只读一遍并缓存派生结果（``data/cache/adj_factor/_long.pkl``），
        分片集合变化（新增交易日）时才重建，避免每次运行都扫 500 个文件。
        """
        basis_date = to_yyyymmdd(basis_date)
        dates = [d for d in self.cache.list_shards("adj_factor") if d <= basis_date]
        if not dates:
            logger.warning("没有 <= %s 的复权因子分片", basis_date)
            return pd.DataFrame(columns=["ts_code", "trade_date", "adj_factor"])

        derived = self.cache.dataset_dir("adj_factor") / "_long.pkl"
        signature = (len(dates), dates[0], dates[-1])

        long: pd.DataFrame | None = None
        if use_derived_cache and derived.is_file():
            try:
                import pickle

                with open(derived, "rb") as fh:
                    payload = pickle.load(fh)
                if payload.get("signature") == signature:
                    long = payload["data"]
                    logger.debug("复权因子长表：命中派生缓存（%d 行）", len(long))
            except Exception as exc:  # noqa: BLE001 - 派生缓存坏了就重建
                logger.debug("派生缓存不可用，重建：%s", exc)

        if long is None:
            frames = [self.cache.read("adj_factor", d) for d in dates]
            frames = [f for f in frames if f is not None and not f.empty]
            if not frames:
                return pd.DataFrame(columns=["ts_code", "trade_date", "adj_factor"])
            long = pd.concat(frames, ignore_index=True)
            long["ts_code"] = long["ts_code"].astype(str)
            long["trade_date"] = long["trade_date"].astype(str)
            long["adj_factor"] = pd.to_numeric(long["adj_factor"], errors="coerce")
            long = long[["ts_code", "trade_date", "adj_factor"]]
            logger.info("复权因子长表：由 %d 个分片拼装，共 %d 行", len(dates), len(long))
            if use_derived_cache:
                try:
                    import pickle

                    tmp = derived.with_suffix(".pkl.tmp")
                    with open(tmp, "wb") as fh:
                        pickle.dump({"signature": signature, "data": long}, fh,
                                    protocol=pickle.HIGHEST_PROTOCOL)
                    tmp.replace(derived)
                    logger.debug("复权因子长表：派生缓存已写入 %s", derived.name)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("派生缓存写入失败（不影响计算）：%s", exc)

        if codes is not None:
            wanted = set(map(str, codes))
            long = long[long["ts_code"].isin(wanted)]

        long = long[long["trade_date"] <= basis_date]
        if not long.empty and (long["trade_date"] > basis_date).any():   # 双保险
            raise ValueError(f"复权因子包含 {basis_date} 之后的数据，违反无未来函数约束")
        return long.sort_values(["ts_code", "trade_date"], kind="mergesort").reset_index(drop=True)

    def _collect_trade_dates(self, end: str, count: int, quiet: bool = True) -> list[str]:
        """从 end 向前收集 count 个交易日，**边探边拉**（探路用的 daily 结果直接入缓存）。

        为什么不用 ``calendar.recent_trade_dates``：它会为每个候选日单独探测一次
        （1 次调用），紧接着 pull 时再拉一次同样的数据（第 2 次调用），回补时白白翻倍。
        这里直接用"拉 daily"当探针，空表即休市，不重复调用。
        """
        from datetime import date as _date, timedelta

        out: list[str] = []
        cursor = datetime.strptime(to_yyyymmdd(end), "%Y%m%d").date()
        guard = 0
        calls = 0
        holiday_probes = 0
        max_guard = int(count * 1.75) + 60        # 覆盖周末 + 法定假期
        while len(out) < count and guard < max_guard:
            if cursor.weekday() < 5:
                key = to_yyyymmdd(cursor)
                known = self.calendar.known(cursor)
                if known is False:
                    pass                           # 已知休市，直接跳过（不耗调用）
                else:
                    before = len(self.client.call_log)
                    df = self.load_daily(key, quiet=quiet)
                    calls += len(self.client.call_log) - before
                    if df is None or df.empty:
                        holiday_probes += 1
                        if cursor < _date.today() - timedelta(days=10):
                            self.calendar.remember(cursor, False)
                    else:
                        out.append(key)
                        self.calendar.remember(cursor, True)
            cursor -= timedelta(days=1)
            guard += 1
        self.last_walk_stats = {
            "api_calls": calls,
            "trading_days": len(out),
            "holiday_probes": holiday_probes,
            "calendar_days_scanned": guard,
        }
        logger.info(
            "回补交易日收集完成：%d 个（%s ~ %s），扫描 %d 个自然日，"
            "API 调用 %d 次（其中休市探测 %d 次）",
            len(out), out[-1] if out else "-", out[0] if out else "-",
            guard, calls, holiday_probes,
        )
        return sorted(out)

    # ------------------------------------------------------------ 每日指标
    def load_daily_basic(self, trade_date: str, force: bool = False, quiet: bool = False) -> pd.DataFrame:
        """某交易日的每日指标（PE/PB/换手率/市值）。"""
        trade_date = to_yyyymmdd(trade_date)
        if not force and self.cache.is_fresh("daily_basic", trade_date):
            df = self.cache.read("daily_basic", trade_date)
            if df is not None and not df.empty:
                logger.info("每日指标 %s：命中缓存 %d 行", trade_date, len(df))
                return df

        df = self.client.daily_basic(trade_date=trade_date)
        if df is None or df.empty:
            if quiet:
                logger.debug("每日指标 %s：空表", trade_date)
            else:
                logger.warning("每日指标 %s：接口返回空表（17:00~19:00 后才入库）", trade_date)
            return pd.DataFrame()
        self.cache.write("daily_basic", trade_date, df)
        logger.info("每日指标 %s：拉取 %d 行并已缓存", trade_date, len(df))
        return df

    # -------------------------------------------------------------- 财务表
    def load_financial(
        self,
        dataset: str,
        period: str,
        force: bool = False,
        ts_code: str | None = None,
    ) -> pd.DataFrame:
        """三大报表之一，按报告期分片缓存。

        ts_code 为空 = 整表按报告期拉取（一次调用拿全市场，最省额度）；
        部分账号/接口版本会强制要求 ts_code（报"必填参数, ts_code"），
        此时改用逐只拉取（见 load_financials_per_stock）。
        """
        if dataset not in FINANCIAL_DATASETS:
            raise KeyError(f"不支持的数据集 {dataset}，仅支持 {FINANCIAL_DATASETS}")
        period = to_yyyymmdd(period)
        if ts_code is None and not force and self.cache.is_fresh(dataset, period):
            df = self.cache.read(dataset, period)
            if df is not None and not df.empty:
                logger.info("%s %s：命中缓存 %d 行", dataset, period, len(df))
                return df

        fn = getattr(self.client, dataset)
        kwargs: dict[str, Any] = {"period": period}
        if ts_code:
            kwargs["ts_code"] = ts_code
        df = fn(**kwargs)
        if df is None or df.empty:
            logger.debug("%s %s%s：接口返回空表", dataset, period,
                         f" {ts_code}" if ts_code else "")
            return pd.DataFrame()

        if ts_code:
            self.cache.upsert(dataset, period, df, on="ts_code")
        else:
            self.cache.write(dataset, period, df)
        logger.info("%s %s%s：拉取 %d 行并已缓存", dataset, period,
                    f" {ts_code}" if ts_code else "", len(df))
        return df

    def load_financials_per_stock(
        self,
        codes: Iterable[str],
        period: str,
        force: bool = False,
        per_code_limit: int = 0,
    ) -> dict[str, pd.DataFrame]:
        """逐只股票拉取三大报表（整表按报告期不可用时的回退方案）。

        2000 积分限流 200 次/分钟，每只股票 3 次调用：
        per_code_limit 只股票约需 per_code_limit * 3 / 200 分钟。
        因此默认关闭，由 --financial-limit 显式开启。
        """
        period = to_yyyymmdd(period)
        codes = list(codes)
        if per_code_limit:
            codes = codes[:per_code_limit]
        frames: dict[str, list[pd.DataFrame]] = {ds: [] for ds in FINANCIAL_DATASETS}
        total = len(codes) * len(FINANCIAL_DATASETS)
        done = 0
        for code in codes:
            for ds in FINANCIAL_DATASETS:
                done += 1
                try:
                    df = self.load_financial(ds, period, ts_code=code)
                    if not df.empty:
                        frames[ds].append(df)
                except Exception as exc:  # noqa: BLE001 - 单只失败不影响整体
                    logger.warning("%s %s %s 拉取失败：%s", ds, period, code, exc)
                if done % 60 == 0:
                    logger.info("逐只拉取财务数据进度：%d/%d", done, total)
        return {
            ds: (pd.concat(v, ignore_index=True) if v else pd.DataFrame())
            for ds, v in frames.items()
        }

    def load_financials(
        self,
        period: str | None = None,
        force: bool = False,
        per_stock_codes: Iterable[str] | None = None,
        per_stock_limit: int = 0,
    ) -> tuple[str | None, dict[str, pd.DataFrame], str]:
        """一次拉取三大报表，自动从近到远挑选有数据的报告期。

        策略：
          1) 先尝试"整表按报告期"（1 次调用拿全市场，共 3 次调用）；
          2) 若接口强制要求 ts_code，且调用方给了 per_stock_limit > 0，退化为逐只拉取。

        返回 (报告期, {dataset: DataFrame}, 取数模式)，模式取值 bulk / per_stock / none。
        """
        codes = list(per_stock_codes or [])
        candidates = [to_yyyymmdd(period)] if period else recent_periods(count=4)
        last_error = ""

        for p in candidates:
            frames: dict[str, pd.DataFrame] = {}
            bulk_ok = False
            from_cache = True
            for ds in FINANCIAL_DATASETS:
                # 命中缓存时不需要区分 bulk/per_stock，统一标成 cache（缓存里已经是合并结果）
                from_cache = from_cache and self.cache.is_fresh(ds, p)
                try:
                    frames[ds] = self.load_financial(ds, p, force=force)
                    bulk_ok = bulk_ok or not frames[ds].empty
                except TushareError as exc:  # 财务数据缺失不应中断主流程
                    frames[ds] = pd.DataFrame()
                    last_error = str(exc)
                    logger.warning("%s %s 整表拉取不可用：%s", ds, p, exc)
            if bulk_ok:
                return p, frames, ("cache" if from_cache else "bulk")

            if per_stock_limit and codes:
                logger.info("改用逐只拉取财务数据：报告期=%s，股票数=%d", p, per_stock_limit)
                frames = self.load_financials_per_stock(
                    codes, p, force=force, per_code_limit=per_stock_limit
                )
                if any(not f.empty for f in frames.values()):
                    return p, frames, "per_stock"

            logger.warning("报告期 %s 三大报表均为空，尝试更早的报告期", p)

        if last_error:
            logger.warning("财务数据整体不可用：%s", last_error)
        return (
            candidates[0] if candidates else None,
            {ds: pd.DataFrame() for ds in FINANCIAL_DATASETS},
            "none",
        )

    # -------------------------------------------------------------- 股票池
    @staticmethod
    def _apply_universe_filters(df: pd.DataFrame, trade_date: str) -> pd.DataFrame:
        """第一阶段粗筛：只用 stock_basic + daily + daily_basic 可得的字段。"""
        if df.empty:
            return df
        n0 = len(df)
        out = df.copy()

        if UNIVERSE_EXCLUDE_DELISTED and "list_status" in out.columns:
            out = out[out["list_status"].astype(str).str.upper() == "L"]

        if UNIVERSE_EXCLUDE_ST and "name" in out.columns:
            # 注意：ST 与"退"必须合并成同一个 mask 一次性过滤，
            # 否则第二个 mask 的索引会对不上已经缩小后的 DataFrame（pandas 会告警并按索引重排）。
            name = out["name"].astype(str)
            keep = ~name.str.contains("ST", case=False, na=False) & ~name.str.contains("退", na=False)
            out = out[keep]

        if UNIVERSE_EXCLUDE_BJ and "ts_code" in out.columns:
            board = out["ts_code"].astype(str).str[:3]
            out = out[~board.isin({"8", "4", "920"})]
            out = out[~out["ts_code"].astype(str).str.endswith(".BJ")]

        if UNIVERSE_MIN_LIST_DAYS > 0 and "list_date" in out.columns:
            cutoff = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=UNIVERSE_MIN_LIST_DAYS))
            ld = pd.to_datetime(out["list_date"], format="%Y%m%d", errors="coerce")
            out = out[ld.isna() | (ld <= cutoff)]

        if UNIVERSE_MIN_AMOUNT > 0 and "amount" in out.columns:
            amt = pd.to_numeric(out["amount"], errors="coerce")
            out = out[amt.isna() | (amt >= UNIVERSE_MIN_AMOUNT)]

        if UNIVERSE_MIN_TOTAL_MV > 0 and "total_mv" in out.columns:
            mv = pd.to_numeric(out["total_mv"], errors="coerce")
            out = out[mv.isna() | (mv >= UNIVERSE_MIN_TOTAL_MV)]

        logger.info("股票池粗筛：%d -> %d 只", n0, len(out))
        return out.reset_index(drop=True)

    def build_universe(
        self,
        stock_basic: pd.DataFrame,
        daily: pd.DataFrame,
        daily_basic: pd.DataFrame | None = None,
        trade_date: str = "",
    ) -> pd.DataFrame:
        """把三张表按 ts_code 合并成全市场面板。"""
        if stock_basic is None or stock_basic.empty:
            return pd.DataFrame()

        basic = stock_basic.copy()
        basic["ts_code"] = basic["ts_code"].astype(str)

        if daily is not None and not daily.empty:
            d = daily.copy()
            d["ts_code"] = d["ts_code"].astype(str)
            basic = basic.merge(d, on="ts_code", how="left", suffixes=("", "_daily"))
            # 当日有行情的才算"在市可交易"
            if "close" in basic.columns:
                basic = basic[basic["close"].notna()]
        else:
            logger.warning("日线为空，股票池将只包含股票列表")

        if daily_basic is not None and not daily_basic.empty:
            db = daily_basic.copy()
            db["ts_code"] = db["ts_code"].astype(str)
            # daily_basic 自带的 trade_date 与 daily 重复，先丢掉；close 会以 close_db 形式保留
            db = db.drop(columns=[c for c in ("trade_date",) if c in db.columns])
            basic = basic.merge(db, on="ts_code", how="left", suffixes=("", "_db"))

        return self._apply_universe_filters(basic, trade_date or to_yyyymmdd(date.today()))

    def load_universe(self, trade_date: str, force: bool = False) -> pd.DataFrame:
        """从本地缓存构建股票池（不联网）。"""
        basic = self.cache.read("stock_basic")
        daily = self.cache.read("daily", trade_date)
        dbasic = self.cache.read("daily_basic", trade_date)
        if basic is None or basic.empty:
            return pd.DataFrame()
        return self.build_universe(
            basic,
            daily if daily is not None else pd.DataFrame(),
            dbasic if dbasic is not None else pd.DataFrame(),
            trade_date,
        )

    # ------------------------------------------------------ 本地历史（零网络）
    def local_trade_dates(self, end_date: str | None = None) -> list[str]:
        """缓存里已有的日线分片日期（升序），可限定 ``<= end_date``。

        这是"本地能回放到哪一天"的唯一依据，不触发任何网络请求。
        """
        dates = self.cache.list_shards("daily")
        if end_date:
            cutoff = to_yyyymmdd(end_date)
            dates = [d for d in dates if d <= cutoff]
        return sorted(dates)

    def load_history_panel(
        self,
        trade_date: str,
        codes: Iterable[str] | None = None,
        max_bars: int | None = None,
    ) -> pd.DataFrame:
        """从本地缓存拼出"多日历史面板"（每只股票多行），**不联网**。

        参数
        ----
        trade_date : 截止交易日（含）
        codes      : 只保留这些代码（None = 全部）
        max_bars   : 最多取最近 N 个日线分片（None = 全部本地分片）

        返回按 (ts_code, trade_date) 升序排列的 DataFrame，数值列已转成 float。
        """
        dates = self.local_trade_dates(trade_date)
        if not dates:
            logger.warning("本地没有任何日线分片（<= %s）", trade_date)
            return pd.DataFrame()
        if max_bars and len(dates) > max_bars:
            dates = dates[-max_bars:]

        frames = [self.cache.read("daily", d) for d in dates]
        frames = [f for f in frames if f is not None and not f.empty]
        if not frames:
            return pd.DataFrame()

        panel = pd.concat(frames, ignore_index=True)
        panel["ts_code"] = panel["ts_code"].astype(str)
        panel["trade_date"] = panel["trade_date"].astype(str)

        if codes is not None:
            wanted = set(map(str, codes))
            panel = panel[panel["ts_code"].isin(wanted)]

        numeric_cols = [
            "open", "high", "low", "close", "pre_close",
            "change", "pct_chg", "vol", "amount",
        ]
        for col in numeric_cols:
            if col in panel.columns:
                panel[col] = pd.to_numeric(panel[col], errors="coerce")

        panel = panel.sort_values(["ts_code", "trade_date"], kind="mergesort").reset_index(drop=True)
        self.last_history_dates = dates
        logger.info(
            "本地历史面板：%d 个交易日（%s~%s）× %d 只股票 = %d 行",
            len(dates), dates[0], dates[-1], panel["ts_code"].nunique(), len(panel),
        )
        return panel

    # ---------------------------------------------------------------- 总控
    def _prefer_liquid_codes(self, codes: list[str], trade_date: str) -> list[str]:
        """按当日总市值从大到小重排股票代码。

        逐只拉取财务数据时通常只取前 N 只，按市值降序可以保证先拿到大票，
        而不是按代码顺序随机取到一批北交所小票。
        """
        if not codes:
            return codes
        db = self.cache.read("daily_basic", trade_date)
        if db is None or db.empty or "total_mv" not in db.columns:
            return codes
        try:
            mv = pd.to_numeric(db["total_mv"], errors="coerce")
            ordered = (
                db.assign(_mv=mv)
                .dropna(subset=["_mv"])
                .sort_values("_mv", ascending=False)["ts_code"]
                .astype(str)
                .tolist()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("按市值排序失败，改用原始顺序：%s", exc)
            return codes
        known = set(codes)
        head = [c for c in ordered if c in known]
        tail = [c for c in codes if c not in set(head)]
        return head + tail

    def sync(
        self,
        mode: str = "quote",
        trade_date: str | None = None,
        force: bool = False,
        include_financials: bool | None = None,
        init_days: int = DEFAULT_INIT_DAYS,
        backfill: bool = False,
        ignore_calendar: bool = False,
        financial_limit: int = 0,
    ) -> SyncResult:
        """执行一次数据同步。

        参数
        ----
        mode              : "quote" | "full"
        trade_date        : 指定交易日（YYYYMMDD），默认自动探测最近交易日
        force             : 忽略缓存强制重拉
        include_financials: 是否拉财务数据，默认 = (mode == "full")
        init_days         : 首次运行时补齐最近 N 个交易日的行情
        backfill          : 是否回补 DAILY_BACKFILL_DAYS 天历史日线
        ignore_calendar   : True 时跳过交易日判断（调试用）
        financial_limit   : >0 时，若整表按报告期拉取不可用，则逐只拉取前 N 只股票
        """
        t0 = datetime.now()
        res = SyncResult(mode=mode)
        if include_financials is None:
            include_financials = mode == "full"

        # 1) 交易日判定
        if trade_date:
            target = to_yyyymmdd(trade_date)
        else:
            target = self.calendar.latest_trade_date(probe=not ignore_calendar)

        if not ignore_calendar and not self.calendar.is_trading_day(target, probe=True):
            res.skipped = True
            res.skip_reason = f"{target} 非交易日，已跳过（无需拉取数据）"
            res.trade_date = target
            logger.info(res.skip_reason)
            res.elapsed = (datetime.now() - t0).total_seconds()
            return res

        res.trade_date = target

        # 2) 股票列表
        basic = self.load_stock_basic(force=force)
        res.rows_stock_basic = len(basic)
        if any(c["api"] == "stock_basic" for c in self.client.call_log):
            res.fetched.append("stock_basic")
        else:
            res.from_cache.append("stock_basic")

        # 3) 确定要补齐的交易日列表
        if backfill:
            # 边探边拉：探路用的 daily 结果直接入缓存，不额外消耗调用
            self.last_walk_stats = {"api_calls": 0, "trading_days": 0, "holiday_probes": 0}
            days = self._collect_trade_dates(
                target, count=max(init_days, DAILY_BACKFILL_DAYS), quiet=True
            )
            res.walk_api_calls = int(self.last_walk_stats.get("api_calls", 0))
            res.holiday_probes = int(self.last_walk_stats.get("holiday_probes", 0))
        else:
            days = self.calendar.recent_trade_dates(end=target, count=init_days, probe=True)

        # 注意：这里**不**把 500 天的 DataFrame 全留在内存里拼接（会占 5~6 GB）。
        # 只需要行数统计，逐日累加即可；真正要用数据时再从缓存按需读取。
        rows_daily_total = 0
        rows_dbasic_total = 0
        rows_adj_total = 0
        for d in days:
            before = len(self.client.call_log)
            df = self.load_daily(d, force=force)
            bucket = res.fetched if len(self.client.call_log) > before else res.from_cache
            bucket.append(f"daily:{d}")
            rows_daily_total += len(df)

            before = len(self.client.call_log)
            db = self.load_daily_basic(d, force=force, quiet=backfill)
            bucket = res.fetched if len(self.client.call_log) > before else res.from_cache
            bucket.append(f"daily_basic:{d}")
            rows_dbasic_total += len(db)

            # 复权因子：与前两者同频，按交易日整表拉取（1 次调用/日）
            before = len(self.client.call_log)
            adj = self.load_adj_factor(d, force=force, quiet=backfill)
            bucket = res.fetched if len(self.client.call_log) > before else res.from_cache
            bucket.append(f"adj_factor:{d}")
            rows_adj_total += len(adj)

        res.rows_daily = rows_daily_total
        res.rows_daily_basic = rows_dbasic_total
        res.rows_adj_factor = rows_adj_total
        res.days_requested = len(days)

        # 4) 财务数据（仅 full 模式）
        if include_financials:
            codes = basic["ts_code"].astype(str).tolist() if "ts_code" in basic else []
            codes = self._prefer_liquid_codes(codes, target)
            period, frames, mode = self.load_financials(
                force=force,
                per_stock_codes=codes,
                per_stock_limit=financial_limit,
            )
            res.period = period
            res.financial_mode = mode
            res.rows_financials = {k: len(v) for k, v in frames.items()}
            empty = [k for k, v in frames.items() if v.empty]
            if empty:
                res.warnings.append(
                    f"财务表为空：{', '.join(empty)}"
                    "（该账号/接口版本要求按 ts_code 逐只查询；"
                    "如需拉取请加 --financial-limit N，例如 --financial-limit 300）"
                )

        # 5) 构建股票池（用目标交易日的切片）
        daily_today = self.cache.read("daily", target)
        dbasic_today = self.cache.read("daily_basic", target)
        universe = self.build_universe(
            basic,
            daily_today if daily_today is not None else pd.DataFrame(),
            dbasic_today if dbasic_today is not None else pd.DataFrame(),
            target,
        )
        res.universe_size = len(universe)

        res.api_calls = len(self.client.call_log)
        res.elapsed = (datetime.now() - t0).total_seconds()
        logger.info("数据同步完成：%s", res.describe())
        return res
