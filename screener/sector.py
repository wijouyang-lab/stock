"""
screener.sector — 行业（板块）强度：日线口径

和盘中榜的区别（很重要）
------------------------
15 分钟榜的"行业强度"用的是**当日平均涨幅**——那是脉冲。
日线用同一把尺子等于追涨，所以在日线侧换成三个更慢、更稳的维度：

    1. 行业 5 日等权涨幅      权重 50%   —— 中期趋势
    2. 行业内站上 MA20 的比例  权重 30%   —— 广度（是不是整个板块在走）
    3. 成交额占全市场比重的变化 权重 20%   —— 资金增量（不是存量）

三个维度各自取横截面百分位后加权，得到 0~1 的强度值，再排名。

对外提供
--------
``analyze(df, trade_date)``   算行业强度 + 落历史 + 算出排名趋势
``attach(df, sector)``        给个股打标记：行业排名 / 行业内排名 / 是否逆势
``sector_first(df, sector)``  只保留前 N 行业（每行业上限 M 只），逆势股另册
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import config

logger = logging.getLogger(__name__)

#: 行业历史文件（累积多天后才能算"排名连续上升"）
def history_path() -> Path:
    return Path(config.OUTPUT_DIR) / "industry" / "history.csv"


# --------------------------------------------------------------------------
# 取数
# --------------------------------------------------------------------------
def load_recent_closes(days: int = 21) -> dict[str, list[float]]:
    """从日线缓存读每只股票最近 N 个交易日的收盘价（用于算区间涨幅）。"""
    daily = Path(config.CACHE_DIR) / "daily"
    if not daily.is_dir():
        return {}
    shards = sorted(daily.glob("*.csv"))[-(days + 1):]
    if not shards:
        return {}
    frames = []
    for shard in shards:
        try:
            frames.append(pd.read_csv(shard, usecols=["ts_code", "close"]))
        except Exception:  # noqa: BLE001
            continue
    if not frames:
        return {}
    df = pd.concat(frames, ignore_index=True)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    out: dict[str, list[float]] = {}
    for code, grp in df.groupby("ts_code", sort=False):
        vals = grp["close"].dropna().tolist()
        if len(vals) >= 6:
            out[str(code)] = vals[-days:]
    return out


def _pct_rank(series: pd.Series) -> pd.Series:
    return series.rank(pct=True, method="average").fillna(0.0)


# --------------------------------------------------------------------------
# 行业强度
# --------------------------------------------------------------------------
def analyze(
    df: pd.DataFrame,
    trade_date: str,
    min_members: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """算行业强度。返回 (带行业列的个股表, 行业强度表)。"""
    min_members = int(config.SECTOR_MIN_MEMBERS if min_members is None else min_members)
    if df is None or df.empty or "industry" not in df.columns:
        return df, pd.DataFrame()

    work = df.copy()
    closes = load_recent_closes(21)

    def ret_n(code: str, n: int) -> float:
        vals = closes.get(str(code))
        if not vals or len(vals) <= n:
            return float("nan")
        prev, last = vals[-1 - n], vals[-1]
        if prev <= 0:
            return float("nan")
        return (last / prev - 1.0) * 100.0

    work["ret5"] = [ret_n(c, 5) for c in work["ts_code"]]
    close = pd.to_numeric(work.get("close"), errors="coerce")
    ma20 = pd.to_numeric(work.get("ma20"), errors="coerce")
    work["above_ma20"] = (close > ma20).astype(float)
    amount = pd.to_numeric(work.get("amount"), errors="coerce")

    grp = work.groupby("industry")
    sector = pd.DataFrame({
        "members": grp["ts_code"].count(),
        "ret5": grp["ret5"].mean(),
        "breadth": grp["above_ma20"].mean(),
    })
    total_amount = amount.sum()
    if total_amount and total_amount > 0:
        sector["amount_share"] = grp["amount"].apply(
            lambda s: pd.to_numeric(s, errors="coerce").sum() / total_amount)
    else:
        sector["amount_share"] = 0.0

    # 资金增量：和 5 日前的占比比。没有历史就先按 0 处理（不影响首日）
    prev = _load_history()
    if not prev.empty and "amount_share" in prev.columns:
        last_day = prev["trade_date"].max()
        base = prev[prev["trade_date"] == last_day].set_index("industry")["amount_share"]
        sector["amount_share_chg"] = sector["amount_share"] - sector.index.map(base).astype(float)
    else:
        sector["amount_share_chg"] = 0.0
    sector["amount_share_chg"] = sector["amount_share_chg"].fillna(0.0)

    # 只保留成分够多的行业
    sector = sector[sector["members"] >= min_members].copy()
    if sector.empty:
        return work, sector

    sector["strength"] = (
        _pct_rank(sector["ret5"]) * 0.5
        + _pct_rank(sector["breadth"]) * 0.3
        + _pct_rank(sector["amount_share_chg"]) * 0.2
    )
    sector = sector.sort_values("strength", ascending=False)
    sector["rank"] = range(1, len(sector) + 1)

    # 排名趋势：比前一交易日上升了几天
    prev_rank = {}
    if not prev.empty and "rank" in prev.columns:
        last_day = prev["trade_date"].max()
        prev_rank = prev[prev["trade_date"] == last_day].set_index("industry")["rank"].to_dict()
    sector["rank_prev"] = [
        int(prev_rank.get(ind, 0)) or pd.NA for ind in sector.index
    ]
    trend = []
    for ind, row in sector.iterrows():
        pr = prev_rank.get(ind)
        if pr is None:
            trend.append(0)
        else:
            trend.append(1 if row["rank"] < pr else 0)   # 名次前进记 1
    sector["rank_up"] = trend

    # 连续上升天数（需要历史里累积的记录）
    streak = []
    for ind in sector.index:
        streak.append(_up_streak(prev, ind, int(sector.loc[ind, "rank"])))
    sector["rank_up_days"] = streak

    sector = sector.reset_index()
    _save_history(sector, trade_date)
    logger.info("行业强度：%d 个行业（前 3：%s）", len(sector),
                "、".join(f"{r.industry}({r.rank})" for r in sector.head(3).itertuples(index=False)))
    return work, sector


def _load_history() -> pd.DataFrame:
    path = history_path()
    if not path.is_file():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:  # noqa: BLE001
        return pd.DataFrame()


def _save_history(sector: pd.DataFrame, trade_date: str) -> None:
    """按交易日追加/覆盖行业强度记录（同一天重复跑只保留最后一次）。"""
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [c for c in ("trade_date", "industry", "rank", "strength", "ret5",
                        "breadth", "amount_share") if c in sector.columns]
    rows = sector[cols].copy()
    rows.insert(0, "trade_date", trade_date)

    old = _load_history()
    if not old.empty:
        old = old[old["trade_date"].astype(str) != str(trade_date)]
        rows = pd.concat([old, rows], ignore_index=True)
    rows.to_csv(path, index=False, encoding="utf-8-sig")


def _up_streak(history: pd.DataFrame, industry: str, today_rank: int) -> int:
    """连续"名次前进"的天数（今天算第一天）。"""
    if history.empty or "rank" not in history.columns:
        return 0
    sub = history[history["industry"] == industry].sort_values("trade_date")
    if sub.empty:
        return 0
    ranks = [int(x) for x in sub["rank"].tolist()] + [today_rank]
    streak = 0
    for i in range(len(ranks) - 1, 0, -1):
        if ranks[i] < ranks[i - 1]:
            streak += 1
        else:
            break
    return streak


# --------------------------------------------------------------------------
# 给个股打标记
# --------------------------------------------------------------------------
def attach(df: pd.DataFrame, sector: pd.DataFrame) -> pd.DataFrame:
    """加三列：industry_rank（行业排名）、rank_in_industry（行业内排名）、contrarian（逆势）。"""
    if df is None or df.empty or sector is None or sector.empty:
        return df
    out = df.copy()
    rank_map = dict(zip(sector["industry"], sector["rank"]))
    streak_map = dict(zip(sector["industry"], sector["rank_up_days"]))
    out["industry_rank"] = out["industry"].map(rank_map).fillna(999).astype(int)
    out["industry_up_days"] = out["industry"].map(streak_map).fillna(0).astype(int)

    # 行业内排名：同行业按 score 降序
    if "score" in out.columns:
        out["rank_in_industry"] = (
            out.groupby("industry")["score"].rank(ascending=False, method="min").fillna(0).astype(int)
        )
    else:
        out["rank_in_industry"] = 0

    thr = int(config.SECTOR_CONTRARIAN_RANK)
    tier = out.get("tier", pd.Series("", index=out.index)).astype(str)
    out["contrarian"] = (out["industry_rank"] > thr) & tier.isin(["核心", "观察"])
    return out


def sector_first(
    df: pd.DataFrame,
    top_n: int | None = None,
    per_industry: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """先选前 N 个行业，再在行业内按分数取前 M 只。逆势股单独返回。

    返回 (主榜, 逆势另册)。主榜带 sector_bucket="主榜"，另册带 "逆势"。
    """
    top_n = int(config.SECTOR_TOP_N if top_n is None else top_n)
    per_industry = int(config.SECTOR_PER_INDUSTRY if per_industry is None else per_industry)
    if df is None or df.empty or "industry_rank" not in df.columns:
        return df, df.head(0)

    ranked = df.sort_values(["score"], ascending=False) if "score" in df.columns else df
    strong = ranked[ranked["industry_rank"] <= top_n]
    main = (strong.groupby("industry", group_keys=False)
                  .head(per_industry)
                  .sort_values(["industry_rank", "score"], ascending=[True, False]))
    main = main.copy()
    main["sector_bucket"] = "主榜"

    contra = df[(df.get("contrarian") == True)]  # noqa: E712
    contra = contra[~contra.index.isin(main.index)].copy()
    contra["sector_bucket"] = "逆势"
    return main, contra


__all__ = ["analyze", "attach", "sector_first", "history_path", "load_recent_closes"]
