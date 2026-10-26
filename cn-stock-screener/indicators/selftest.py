"""
indicators.selftest — 指标与筛选的本地自检

设计原则：
- 全部用**合成数据**验证数学正确性（可复现、不依赖网络与行情）；
- 涉及真实缓存的检查（换手率换算、零调用断言）在缓存缺失时标记 SKIP，不算失败；
- 返回结构化结果，供 ``main.py --self-test`` 统一渲染，不自己打印。

核心断言（需求第 9 条）：
    指标计算期间 Tushare 调用计数必须为 0
    实现方式：``reset_call_count()`` → 在 ``local_only()`` 上下文里跑完整本地流水线
    → ``get_attempt_count()`` 仍为 0。因为 ``TushareClient.call`` 每次进来都会先给
    计数器 +1，所以只要有任何代码路径尝试联网，这个断言就会失败。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)


@dataclass
class Check:
    """一条自检结果。"""

    name: str
    ok: bool
    detail: str = ""
    skip: bool = False
    warn_only: bool = False


# --------------------------------------------------------------------------
# 合成数据
# --------------------------------------------------------------------------
def make_bars(
    closes: list[float] | np.ndarray,
    start: str = "2025-01-02",
    vol: float = 1000.0,
    pad: float = 0.002,
) -> pd.DataFrame:
    """由收盘价序列造一份日线（工作日序列，OHLC 由收盘价按比例派生）。

    ``pad`` 控制上下影线幅度，默认 0.2%，便于形态类测试精确控制。
    """
    closes = np.asarray(closes, dtype="float64")
    dates = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame(
        {
            "ts_code": "TEST.SZ",
            "trade_date": [d.strftime("%Y%m%d") for d in dates],
            "open": np.r_[closes[0], closes[:-1]],
            "high": closes * (1.0 + pad),
            "low": closes * (1.0 - pad),
            "close": closes,
            "vol": vol,
            "amount": closes * vol,
        }
    )


def _path(anchors: list[tuple[int, float]], length: int) -> np.ndarray:
    """按 (位置, 价格) 锚点线性插值出收盘价路径。"""
    xs = np.array([a[0] for a in anchors], dtype="float64")
    ys = np.array([a[1] for a in anchors], dtype="float64")
    return np.interp(np.arange(length), xs, ys)


def synthetic_double_bottom(breakout: bool = True, gap: int = 20, tol: float = 0.03) -> pd.DataFrame:
    """构造双重底：低点 10 与低点 2（价差可配），中间反弹到 90，可选随后突破颈线。"""
    length = 70
    second_low_idx = 10 + gap
    tip = 89.0 if not breakout else 96.0
    anchors = [
        (0, 100.0),
        (10, 80.0),                      # 第一个低点
        (10 + gap // 2, 90.0),           # 中间反弹（颈线）
        (second_low_idx, 80.0 * (1 + tol * 0.5)),  # 第二个低点（价差在容差内）
        (second_low_idx + 8, 84.0),
        (length - 1, tip),               # 突破 / 不突破
    ]
    return make_bars(_path(anchors, length))


def synthetic_double_top(breakdown: bool = True) -> pd.DataFrame:
    """构造双重顶：高点 100 与 100*(1-tol/2)，中间回落到 85，可选随后跌破颈线。"""
    length = 70
    anchors = [
        (0, 80.0),
        (10, 100.0),                     # 第一个高点
        (20, 85.0),                      # 中间回落（颈线）
        (30, 99.0),                      # 第二个高点
        (38, 90.0),
        (length - 1, 70.0 if breakdown else 86.0),
    ]
    return make_bars(_path(anchors, length))


# --------------------------------------------------------------------------
# 各项检查
# --------------------------------------------------------------------------
def _check_resample() -> Check:
    from indicators.resample import to_monthly, to_weekly

    # 从周一开始的连续 10 个工作日 = 恰好两周（每根周线 5 天）
    df = make_bars(list(np.linspace(10, 20, 10)), start="2025-01-06")
    weekly = to_weekly(df)
    if len(weekly) != 2:
        return Check("resample 周线根数", False,
                     f"期望 2 根（周一开盘的 10 个工作日），实际 {len(weekly)} 根")

    first = df.iloc[:5]
    w = weekly.iloc[0]
    expected = {
        "open": float(first["open"].iloc[0]),
        "close": float(first["close"].iloc[-1]),
        "high": float(first["high"].max()),
        "low": float(first["low"].min()),
        "vol": float(first["vol"].sum()),
    }
    ok = all(abs(float(w[k]) - v) < 1e-9 for k, v in expected.items())
    ok = ok and str(w["trade_date"]) == str(first["trade_date"].iloc[-1]) and int(w["bar_count"]) == 5
    detail = f"首周 O/H/L/C/vol={w['open']:.2f}/{w['high']:.2f}/{w['low']:.2f}/{w['close']:.2f}/{w['vol']:.0f}"

    monthly = to_monthly(make_bars(list(np.linspace(10, 20, 60)), start="2025-01-06"))
    ok = ok and len(monthly) == 3      # 60 个工作日跨 1~3 月，各一根
    return Check("resample 周线/月线聚合", ok, f"{detail}；月线 {len(monthly)} 根")


def _check_macd() -> Check:
    from indicators.momentum import macd

    flat = macd(make_bars([10.0] * 60))
    flat_ok = bool(np.allclose(flat["dif"].to_numpy(), 0.0, atol=1e-12))

    rising = macd(make_bars(list(np.linspace(10, 20, 60))))
    rise_ok = bool(rising["dif"].iloc[-1] > 0 and rising["dif"].iloc[-1] > rising["dea"].iloc[-1])

    falling = macd(make_bars(list(np.linspace(20, 10, 60))))
    fall_ok = bool(falling["dif"].iloc[-1] < 0 and falling["dif"].iloc[-1] < falling["dea"].iloc[-1])

    hist_ok = bool(
        np.allclose(
            rising["macd"].to_numpy(),
            (rising["dif"] - rising["dea"]).to_numpy() * 2.0,
            atol=1e-12,
        )
    )
    return Check(
        "MACD 数学（常数/上涨/下跌/柱值）",
        flat_ok and rise_ok and fall_ok and hist_ok,
        f"flat_dif≈0={flat_ok} 上涨dif>dea={rise_ok} 下跌dif<dea={fall_ok} 柱=2×(DIF-DEA)={hist_ok}",
    )


def _check_kdj() -> Check:
    from indicators.momentum import kdj

    df = kdj(make_bars(list(np.linspace(10, 20, 40))))
    k, d = df["k"], df["d"]
    in_range = bool(((k >= 0) & (k <= 100)).all() and ((d >= 0) & (d <= 100)).all())
    j_ok = bool(np.allclose(df["j"], 3 * k - 2 * d))
    # 首根：K 应等于 (2*50 + rsv)/3，且不越界
    seed_ok = bool(abs(df["k"].iloc[0] - (2 * 50 + df["rsv"].iloc[0]) / 3) < 1e-9)
    return Check(
        "KDJ(9/3/3) 取值与初始化",
        in_range and j_ok and seed_ok,
        f"K∈[0,100]={in_range} J=3K-2D={j_ok} 首根按50初始化={seed_ok}",
    )


def _check_candles() -> Check:
    from indicators.candles import hammer, shooting_star

    # 锤子线：开 10.2 收 10.3（小实体），下影到 9.0，上影到 10.35
    base = make_bars([10.0] * 30)
    base.loc[base.index[-1], ["open", "close", "high", "low"]] = [10.2, 10.3, 10.35, 9.0]
    # 让前 5 根均价高于当前收盘，满足"下跌途中"
    base.loc[base.index[-6:-1], "close"] = 11.0
    ham = hammer(base, trend_filter=True)

    star = make_bars([10.0] * 30)
    star.loc[star.index[-1], ["open", "close", "high", "low"]] = [10.3, 10.2, 11.5, 10.15]
    star.loc[star.index[-6:-1], "close"] = 9.0
    st = shooting_star(star, trend_filter=True)

    plain = hammer(make_bars([10.0] * 30), trend_filter=False)
    plain_false = not bool(plain["hammer"].iloc[-1])

    ok = bool(ham["hammer"].iloc[-1]) and bool(st["shooting_star"].iloc[-1]) and plain_false
    return Check(
        "锤子线 / 射击之星识别",
        ok,
        f"hammer={bool(ham['hammer'].iloc[-1])} shooting_star={bool(st['shooting_star'].iloc[-1])} "
        f"普通K线不误判={plain_false}",
    )


def _check_double_bottom() -> Check:
    from indicators.patterns import detect_double_bottom

    with_break = detect_double_bottom(synthetic_double_bottom(breakout=True))[0]
    no_break = detect_double_bottom(synthetic_double_bottom(breakout=False))[0]
    tight = detect_double_bottom(synthetic_double_bottom(breakout=True, gap=6))[0]

    hit = bool(with_break["double_bottom"].any())
    blocked = not bool(no_break["double_bottom"].any())
    gap_ok = not bool(tight["double_bottom"].any())
    window_ok = bool(with_break["double_bottom_10d"].any())
    neckline = float(with_break.loc[with_break["double_bottom"], "db_neckline"].iloc[0]) if hit else float("nan")

    return Check(
        "双重底：只认突破确认",
        hit and blocked and gap_ok and window_ok,
        f"突破→命中={hit}(颈线≈{neckline:.2f}) 未突破→不命中={blocked} "
        f"间隔不足→不命中={gap_ok} 10日窗口列={window_ok}",
    )


def _check_double_top() -> Check:
    from indicators.patterns import detect_double_top

    with_break = detect_double_top(synthetic_double_top(breakdown=True))[0]
    no_break = detect_double_top(synthetic_double_top(breakdown=False))[0]

    hit = bool(with_break["double_top"].any())
    blocked = not bool(no_break["double_top"].any())
    return Check(
        "双重顶：只认破位确认",
        hit and blocked,
        f"跌破→命中={hit} 未跌破→不命中={blocked}",
    )


def _check_swings() -> Check:
    from indicators.patterns import swing_points

    length = 41
    anchors = [(0, 10.0), (20, 20.0), (40, 10.0)]     # 中间一个高点
    df = make_bars(_path(anchors, length))
    piv_h, piv_l = swing_points(df, n=5)
    peak = int(np.argmax(df["high"].to_numpy()))
    ok = bool(piv_h.iloc[peak]) and not bool(piv_h.iloc[[0, -1]].any())
    return Check("摆动点 n=5（窗口中心极值）", ok, f"峰值位置 {peak} 被识别为摆动高点={bool(piv_h.iloc[peak])}")


def _check_resonance() -> Check:
    """共振：列齐全、无未来函数（截断重算后同一日期取值一致）。"""
    from indicators.resonance import resonance

    rng = np.random.default_rng(7)
    closes = 50 * np.cumprod(1 + rng.normal(0.0006, 0.012, 900))
    df = make_bars(closes.tolist(), start="2022-01-03")

    full = resonance(df)
    cut_idx = 800
    cut = resonance(df.iloc[: cut_idx + 1])

    cols_ok = all(
        c in full.columns
        for c in ("resonance_daily", "resonance_weekly", "resonance_monthly", "resonance_dwm",
                  "weekly_ma20", "monthly_ma20", "weekly_dif", "monthly_dif")
    )
    same = all(
        bool(full[c].iloc[cut_idx]) == bool(cut[c].iloc[cut_idx])
        for c in ("resonance_daily", "resonance_weekly", "resonance_monthly", "resonance_dwm")
    )
    ma_same = bool(
        np.allclose(
            [full["weekly_ma20"].iloc[cut_idx], full["monthly_ma20"].iloc[cut_idx]],
            [cut["weekly_ma20"].iloc[cut_idx], cut["monthly_ma20"].iloc[cut_idx]],
            equal_nan=True,
        )
    )
    any_true = bool(full["resonance_weekly"].any() and full["resonance_monthly"].any())
    return Check(
        "日周月共振：列齐全 + 无未来函数",
        cols_ok and same and ma_same and any_true,
        f"列齐全={cols_ok} 截断重算一致={same and ma_same} 周/月线出现过共振={any_true}",
    )


def _qfq_fixture(
    n_codes: int = 50,
    n_days: int = 900,
    seed: int = 11,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """构造"50 只股票 × 900 个交易日"的合成面板 + 复权因子长表。

    每只股票在中途有 2 次"除权"（adj 阶跃跳变），用来验证
    ``qfq(t)/raw(t) == adj(t)/adj(basis)`` 与截断重算一致性。
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2022-01-03", periods=n_days)
    trade_dates = np.array([d.strftime("%Y%m%d") for d in dates])

    panel_parts: list[pd.DataFrame] = []
    adj_parts: list[pd.DataFrame] = []
    for ci in range(n_codes):
        code = f"{600000 + ci}.SH"
        base = 8.0 + ci * 0.5
        close = base * np.cumprod(1.0 + rng.normal(0.0004, 0.012, n_days))

        # 两次除权：第 1 次 ×1.6，第 2 次再 ×1.25
        adj = np.ones(n_days)
        j1 = int(rng.integers(120, 380))
        j2 = int(rng.integers(520, 780))
        adj[j1:] *= 1.6
        adj[j2:] *= 1.25

        panel_parts.append(
            pd.DataFrame(
                {
                    "ts_code": code,
                    "trade_date": trade_dates,
                    "open": close * 0.998,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "pre_close": np.r_[close[0], close[:-1]],
                    "vol": 1000.0,
                    "amount": close * 1000.0,
                }
            )
        )
        adj_parts.append(
            pd.DataFrame({"ts_code": code, "trade_date": trade_dates, "adj_factor": adj})
        )

    return pd.concat(panel_parts, ignore_index=True), pd.concat(adj_parts, ignore_index=True)


def _check_qfq_formula() -> Check:
    """断言 qfq(t)/raw(t) == adj(t)/adj(basis_date)，误差 < 1e-9。"""
    from indicators.adjust import apply_qfq

    panel, adj = _qfq_fixture()
    basis = sorted(panel["trade_date"].unique())[800]
    upto = panel[panel["trade_date"] <= basis]
    adj_upto = adj[adj["trade_date"] <= basis]

    out = apply_qfq(upto, adj_upto, basis)

    raw = upto.set_index(["ts_code", "trade_date"])["close"]
    got = out.set_index(["ts_code", "trade_date"])["close"] / raw
    adj_idx = adj_upto.set_index(["ts_code", "trade_date"])["adj_factor"]
    base_per_code = (
        adj_upto.sort_values("trade_date").groupby("ts_code")["adj_factor"].last()
    )
    expected = adj_idx / adj_idx.index.get_level_values("ts_code").map(base_per_code)

    err = float((got - expected).abs().max())
    n_codes = int(panel["ts_code"].nunique())

    # vol/amount 不允许被复权
    vol_same = bool(np.allclose(out["vol"].to_numpy(), upto["vol"].to_numpy()))
    amt_same = bool(np.allclose(out["amount"].to_numpy(), upto["amount"].to_numpy()))

    # 基准日当天 ratio 必须为 1（qfq == raw）
    at_basis = out[out["trade_date"] == basis]
    basis_ratio_ok = bool(np.allclose(at_basis["adj_ratio"].to_numpy(), 1.0))

    ok = err < 1e-9 and vol_same and amt_same and basis_ratio_ok
    return Check(
        "前复权公式 qfq/raw = adj/adj(basis)",
        ok,
        f"{n_codes} 只 × {len(upto)} 行，最大误差={err:.2e}，"
        f"vol/amount 未复权={vol_same and amt_same}，基准日 ratio=1={basis_ratio_ok}",
    )


def _check_qfq_no_lookahead() -> Check:
    """截断到第 800 根重算：同一天复权后 close 必须与全量计算一致。"""
    from indicators.adjust import apply_qfq

    panel, adj = _qfq_fixture()
    basis = sorted(panel["trade_date"].unique())[800]

    # 全量价格面板（含 basis 之后的 99 根），但因子只给到 basis
    full = apply_qfq(panel, adj[adj["trade_date"] <= basis], basis)
    cut = apply_qfq(panel[panel["trade_date"] <= basis], adj[adj["trade_date"] <= basis], basis)

    a = full[full["trade_date"] == basis].set_index("ts_code")["close"].sort_index()
    b = cut[cut["trade_date"] == basis].set_index("ts_code")["close"].sort_index()
    same = bool(np.allclose(a.to_numpy(), b.to_numpy()))
    max_diff = float(np.abs(a.to_numpy() - b.to_numpy()).max())

    return Check(
        "前复权截断重算一致（无未来函数）",
        same,
        f"basis={basis}，{len(a)} 只股票在基准日的 qfq close 最大差异={max_diff:.2e}",
    )


def _check_qfq_future_guard() -> Check:
    """hard constraint：adj_factor 的 trade_date 必须 <= basis_date，否则抛异常。"""
    from indicators.adjust import apply_qfq

    panel, adj = _qfq_fixture(n_codes=3, n_days=120)
    basis = sorted(panel["trade_date"].unique())[100]
    raised = False
    try:
        apply_qfq(panel, adj, basis)          # 因子含 basis 之后的数据 → 必须抛
    except ValueError:
        raised = True
    return Check(
        "复权未来函数防护（因子 > basis 即抛）",
        raised,
        f"传入含 {basis} 之后因子的长表 -> {'已拒绝' if raised else '未拒绝（危险）'}",
    )


def _check_turnover_units() -> Check:
    """用真实缓存交叉验证 turnover = vol / float_share（Tushare 单位换算）。"""
    from data.cache import LocalCache

    cache = LocalCache()
    dates = cache.list_shards("daily_basic")
    if not dates:
        return Check("换手率口径交叉验证", True, "本地无 daily_basic 缓存，跳过", skip=True)

    trade_date = dates[-1]
    db = cache.read("daily_basic", trade_date)
    dy = cache.read("daily", trade_date)
    if db is None or dy is None or db.empty or dy.empty:
        return Check("换手率口径交叉验证", True, f"{trade_date} 分片不可读，跳过", skip=True)

    merged = db.merge(dy[["ts_code", "vol"]], on="ts_code", how="inner")
    vol = pd.to_numeric(merged["vol"], errors="coerce")
    fs = pd.to_numeric(merged["float_share"], errors="coerce")
    api = pd.to_numeric(merged["turnover_rate"], errors="coerce")
    calc = vol / fs
    diff = (calc - api).abs()
    valid = diff.notna()
    if not valid.any():
        return Check("换手率口径交叉验证", True, "样本不足，跳过", skip=True)

    max_diff = float(diff[valid].max())
    same = max_diff < 0.05          # 允许接口四舍五入带来的极小误差
    return Check(
        "换手率口径 vol/float_share 与接口一致",
        same,
        f"{trade_date} n={int(valid.sum())} 最大偏差={max_diff:.4f} 个百分点",
    )


def _check_filters() -> Check:
    from config import FILTER_PARAMS
    from screener import FILTER_REGISTRY

    expected = set(FILTER_PARAMS)
    registered = set(FILTER_REGISTRY)
    missing = expected - registered
    ok = not missing
    return Check(
        "筛选项注册齐全",
        ok,
        f"已注册 {len(registered)} 条：" + ",".join(sorted(registered))
        + (f"；缺失 {sorted(missing)}" if missing else ""),
    )


def _check_turnover_filter() -> Check:
    from screener.filters.volume import TurnoverFilter

    df = pd.DataFrame(
        {
            "ts_code": [f"{i:06d}.SZ" for i in range(10)],
            "turnover": [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 20.0],
        }
    )
    flt = TurnoverFilter(min_turnover=3.0, percentile=0.8)
    out, stat = flt.apply(df)
    q80 = float(df["turnover"].quantile(0.8))
    threshold = max(3.0, q80)
    ok = (
        stat["detail"]["threshold"] == round(threshold, 4)
        and set(out["ts_code"]) == set(df.loc[df["turnover"] >= threshold, "ts_code"])
        and "q_value" in stat["detail"]
    )
    return Check(
        "换手率筛选（≥3% 且 ≥80分位）",
        ok,
        f"q80={q80:.2f} 阈值={stat['detail']['threshold']} 通过={len(out)} 只（funnel 含阈值明细）",
    )


def _check_local_pipeline_no_calls() -> Check:
    """核心断言：本地流水线（读缓存→指标→筛选）期间 Tushare 调用计数必须为 0。"""
    from data.loader import DataLoader
    from data.tushare_client import get_attempt_count, local_only, reset_call_count
    from indicators import compute_panel
    from indicators.volume import ensure_turnover
    from screener import ScreenerEngine
    from screener.filters import default_chain

    base_cols = {
        "ts_code", "trade_date", "open", "high", "low", "close", "pre_close",
        "change", "pct_chg", "vol", "amount",
    }
    loader = DataLoader()
    dates = loader.local_trade_dates()
    if not dates:
        return Check("指标计算期间 Tushare 调用计数=0", True, "本地无日线缓存，跳过", skip=True)

    trade_date = dates[-1]
    reset_call_count()
    with local_only():
        # 与 main.screen_local 完全一致的路径：截面 + 历史 → 前复权 → 指标 → 合并 → 筛选
        universe = loader.load_universe(trade_date)
        sample = universe["ts_code"].astype(str).head(200).tolist() if not universe.empty else []
        # 自检只看最近 120 根，既覆盖全部指标，又不让自检变慢
        panel = loader.load_history_panel(trade_date, codes=sample, max_bars=120)
        adj_long = loader.load_adj_long(basis_date=trade_date, codes=sample)
        if not adj_long.empty:
            from indicators.adjust import apply_qfq

            panel = apply_qfq(panel, adj_long, basis_date=trade_date)
        computed = compute_panel(panel)
        rows = len(computed)
        if not computed.empty:
            feats = [c for c in computed.columns if c not in base_cols]
            snapshot = computed.loc[computed["trade_date"] == trade_date, ["ts_code"] + feats]
            merged = ensure_turnover(
                universe.merge(snapshot, on="ts_code", how="left", suffixes=("", "_ind"))
            )
            out = ScreenerEngine(filters=default_chain()).run(merged, trade_date=trade_date)
            turnover_ok = "turnover" not in out.matched.columns or out.matched["turnover"].notna().all()
        else:
            turnover_ok = True
    attempts = get_attempt_count()

    ok = attempts == 0 and turnover_ok
    return Check(
        "指标计算期间 Tushare 调用计数=0",
        ok,
        f"{trade_date}：{len(sample)} 只股票 / {rows} 行指标 / 复权因子 {len(adj_long)} 行，"
        f"API 调用尝试={attempts}"
        + ("" if ok else " ← 有代码在本地计算里偷偷联网了"),
    )


def _check_local_only_guard() -> Check:
    from data.tushare_client import (
        NetworkBlockedError,
        TushareClient,
        get_blocked_count,
        local_only,
    )

    client = TushareClient(token="dummy-token-for-guard-test")
    blocked = False
    with local_only():
        try:
            client.daily(trade_date="20260101")
        except NetworkBlockedError:
            blocked = True
        except Exception as exc:  # noqa: BLE001
            return Check("--local-only 网络调用即抛异常", False, f"抛了非预期异常：{type(exc).__name__}: {exc}")

    inc_ok = get_blocked_count() >= 1
    return Check(
        "--local-only 网络调用即抛异常",
        blocked and inc_ok,
        f"NetworkBlockedError={blocked} 拦截计数={get_blocked_count()}",
    )


def _check_neckline_params() -> Check:
    """形态口径：默认要求"当前仍在颈线正确一侧"，可用参数关闭。"""
    import config
    from screener.filters.patterns import DoubleBottomFilter, DoubleTopFilter

    base = {
        "ts_code": ["AAA", "BBB", "CCC"],
        "close": [12.0, 9.0, 11.0],          # 颈线都设 10.0
        "db_neckline": [10.0, 10.0, 10.0],
        "dt_neckline": [10.0, 10.0, 10.0],
        "double_bottom_10d": [True, True, True],
        "double_bottom": [True, True, True],
        "double_top_10d": [True, True, True],
        "double_top": [True, True, True],
    }
    df = pd.DataFrame(base)

    on_b, stat_b = DoubleBottomFilter().apply(df)                          # 默认：close > 颈线
    off_b, _ = DoubleBottomFilter(require_above_neckline=False).apply(df)  # 关闭：只看突破
    on_t, stat_t = DoubleTopFilter().apply(df)                             # 默认：close < 颈线
    off_t, _ = DoubleTopFilter(require_below_neckline=False).apply(df)

    defaults_ok = (
        config.PATTERN_REQUIRE_ABOVE_NECKLINE is True
        and config.PATTERN_REQUIRE_BELOW_NECKLINE is True
    )
    ok = (
        sorted(on_b["ts_code"]) == ["AAA", "CCC"]      # 12>10, 11>10 保留；9<10 剔除
        and len(off_b) == 3                            # 关闭后 3 只全保留
        and sorted(on_t["ts_code"]) == ["BBB"]         # 9<10 保留；12>10, 11>10 剔除
        and len(off_t) == 3
        and defaults_ok
        and stat_b["detail"].get("neckline_filtered_out") == 1
        and stat_t["detail"].get("neckline_filtered_out") == 2
    )
    return Check(
        "形态颈线口径（require_above/below_neckline）",
        ok,
        f"默认开启={defaults_ok}；双底保留 {len(on_b)}/3（关闭后 {len(off_b)}），"
        f"双顶保留 {len(on_t)}/3（关闭后 {len(off_t)}）；"
        f"funnel 记录剔除数 {stat_b['detail'].get('neckline_filtered_out')}"
        f"/{stat_t['detail'].get('neckline_filtered_out')}",
    )


def _check_volume_ratio_excludes_today() -> Check:
    """量比公式边界：分母必须是"前 5 日"，不含当日。

    vol = [100,100,100,100,100,300] → 当日 ratio 必须 = 3.0；
    若误把当日算进分母（rolling(5).mean()）会得 300/140 ≈ 2.14。
    """
    from indicators.volume import volume_ratio

    vol = [100.0, 100.0, 100.0, 100.0, 100.0, 300.0]
    df = make_bars([10.0] * len(vol))
    df["vol"] = vol
    out = volume_ratio(df)
    ratio = float(out["volume_ratio_5"].iloc[-1])
    ma5 = float(out["ma5_vol"].iloc[-1])
    include_today = 300.0 / (sum(vol[-5:]) / 5.0)      # 错误口径的对照值

    ok = (
        abs(ratio - 3.0) < 1e-9
        and abs(ma5 - 100.0) < 1e-9
        and abs(ratio - include_today) > 0.5            # 必须与"含当日"口径明显不同
    )
    return Check(
        "量比分母=前5日均量（不含当日）",
        ok,
        f"vol={[int(v) for v in vol]} → ratio={ratio:.4f}（应为 3.0），"
        f"ma5_vol={ma5:.1f}；含当日口径会是 {include_today:.4f}",
    )


def _check_volume_filters_mutual_exclusion() -> Check:
    """放量与缩量互斥：1000 组随机量能序列，两者不能同时命中。"""
    from indicators.volume import volume_ratio
    from screener.filters.volume import VolumeShrinkFilter, VolumeSurgeFilter

    rng = np.random.default_rng(20261021)
    surge = VolumeSurgeFilter()
    shrink = VolumeShrinkFilter()
    both_true = 0
    checked = 0
    for _ in range(1000):
        vols = rng.integers(1, 5000, size=7).astype(float)
        df = make_bars([10.0] * 7)
        df["vol"] = vols
        ratio = float(volume_ratio(df)["volume_ratio_5"].iloc[-1])
        row = pd.DataFrame(
            {"ts_code": ["X"], "vol": [vols[-1]], "volume_ratio_5": [ratio]}
        )
        s = bool(surge.mask(row).iloc[0])
        k = bool(shrink.mask(row).iloc[0])
        both_true += int(s and k)
        checked += 1

    return Check(
        "放量/缩量互斥（1000 组随机序列）",
        both_true == 0,
        f"检查 {checked} 组，同时命中 {both_true} 组（应为 0）",
    )


def _check_volume_filter_missing_history() -> Check:
    """次新股（不足 ma_window+1 根日线）：ratio=NaN、不命中、记 missing、不抛异常。"""
    from indicators.volume import volume_ratio
    from screener.filters.volume import VolumeShrinkFilter, VolumeSurgeFilter

    # 只有 5 根日线（需要 6 根才有值）
    df = make_bars([10.0] * 5)
    df["vol"] = [100.0] * 5
    out = volume_ratio(df)
    all_nan = bool(out["volume_ratio_5"].isna().all())

    row = pd.DataFrame({"ts_code": ["NEW"], "vol": [100.0], "volume_ratio_5": [np.nan]})
    raised = False
    try:
        surge_out, surge_stat = VolumeSurgeFilter().apply(row)
        shrink_out, shrink_stat = VolumeShrinkFilter().apply(row)
    except Exception as exc:  # noqa: BLE001
        raised = True
        surge_out = shrink_out = pd.DataFrame()
        surge_stat = shrink_stat = {"detail": {}}

    ok = (
        all_nan
        and not raised
        and len(surge_out) == 0
        and len(shrink_out) == 0
        and surge_stat["detail"].get("missing") == 1
        and shrink_stat["detail"].get("missing") == 1
    )
    return Check(
        "次新股量比缺历史 → NaN/missing/不命中",
        ok,
        f"5 根日线 ratio 全 NaN={all_nan}；异常={raised}；"
        f"surge 命中 {len(surge_out)} / missing={surge_stat['detail'].get('missing')}，"
        f"shrink 命中 {len(shrink_out)} / missing={shrink_stat['detail'].get('missing')}",
    )


def _volume_mode_frame(n: int = 1000, seed: int = 7) -> pd.DataFrame:
    """构造一个"全市场量比"截面（均匀分布在 0.1~2.0）。"""
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            "ts_code": [f"T{i:04d}" for i in range(n)],
            "vol": 1000.0,
            "volume_ratio_5": np.round(rng.uniform(0.1, 2.0, n), 4),
        }
    )


def _check_volume_mode_absolute_unchanged() -> Check:
    """absolute（默认）模式的判定必须与"固定阈值"完全一致，不受新模式影响。"""
    from screener.filters.volume import VolumeShrinkFilter, VolumeSurgeFilter

    df = _volume_mode_frame()
    surge, shrink = VolumeSurgeFilter(), VolumeShrinkFilter()
    s_out, s_stat = surge.apply(df)
    k_out, k_stat = shrink.apply(df)

    expect_surge = set(df.loc[df["volume_ratio_5"] >= 1.5, "ts_code"])
    expect_shrink = set(df.loc[df["volume_ratio_5"] <= 0.7, "ts_code"])

    ok = (
        set(s_out["ts_code"]) == expect_surge
        and set(k_out["ts_code"]) == expect_shrink
        and surge.params.get("mode") == "absolute"
        and shrink.params.get("mode") == "absolute"
        and s_stat["detail"]["mode"] == "absolute"
        and k_stat["detail"]["mode"] == "absolute"
        and s_stat["detail"]["threshold"] == 1.5
        and k_stat["detail"]["threshold"] == 0.7
    )
    return Check(
        "量能 absolute 模式默认且判定不变",
        ok,
        f"surge {len(s_out)} 只（阈值 {s_stat['detail']['threshold']}）、"
        f"shrink {len(k_out)} 只（阈值 {k_stat['detail']['threshold']}），"
        f"均与固定阈值判定一致；默认 mode=absolute",
    )


def _check_volume_percentile_rate() -> Check:
    """percentile 模式：80 分位放量 / 20 分位缩量 → 全市场命中率约 20%（±1.5%）。"""
    from screener.filters.volume import VolumeShrinkFilter, VolumeSurgeFilter

    df = _volume_mode_frame(1000, seed=21)
    surge = VolumeSurgeFilter(mode="percentile")
    surge.bind_universe(df)
    s_out, s_stat = surge.apply(df)

    shrink = VolumeShrinkFilter(mode="percentile")
    shrink.bind_universe(df)
    k_out, k_stat = shrink.apply(df)

    s_rate = len(s_out) / len(df)
    k_rate = len(k_out) / len(df)
    ok = (
        0.185 <= s_rate <= 0.215
        and 0.185 <= k_rate <= 0.215
        and s_stat["detail"]["percentile"] == 80
        and k_stat["detail"]["percentile"] == 20
        and s_stat["detail"]["threshold_source"] == "universe"
    )
    return Check(
        "量能 percentile 模式命中率≈20%",
        ok,
        f"surge(p80, thr={s_stat['detail']['threshold']}) 命中 {len(s_out)} 只 = {s_rate:.2%}；"
        f"shrink(p20, thr={k_stat['detail']['threshold']}) 命中 {len(k_out)} 只 = {k_rate:.2%}",
    )


def _check_volume_percentile_uses_universe() -> Check:
    """分位阈值必须来自全市场 universe，而不是漏斗上游传来的子集。"""
    from screener.filters.volume import VolumeSurgeFilter

    uni = _volume_mode_frame(1000, seed=31)
    # 故意取量比最低的 10 只当"上游子集"，其自身 80 分位远低于全市场 80 分位
    subset = uni.nsmallest(10, "volume_ratio_5").copy()
    uni_q80 = float(uni["volume_ratio_5"].quantile(0.8))
    sub_q80 = float(subset["volume_ratio_5"].quantile(0.8))

    bound = VolumeSurgeFilter(mode="percentile")
    bound.bind_universe(uni)
    bound.mask(subset)
    thr = bound.detail["threshold"]
    source = bound.detail["threshold_source"]

    # 未注入 universe 时回退到输入，并在 detail 里标明
    fallback = VolumeSurgeFilter(mode="percentile")
    fallback.mask(subset)
    fb_thr = fallback.detail["threshold"]
    fb_source = fallback.detail["threshold_source"]

    ok = (
        abs(float(thr) - uni_q80) < 1e-3          # 取的是全市场 80 分位
        and abs(uni_q80 - sub_q80) > 1e-3         # 两者确实不同
        and source == "universe"
        and fb_source == "input"                  # 回退路径可识别
        and abs(float(fb_thr) - sub_q80) < 1e-3
    )
    return Check(
        "量能 percentile 分位取自全市场（非上游子集）",
        ok,
        f"全市场 q80={uni_q80:.4f}，子集(10只) q80={sub_q80:.4f}；"
        f"注入后取 {thr}（source={source}），未注入回退 {fb_thr}（source={fb_source}）",
    )


def _check_volume_mode_isolation() -> Check:
    """absolute 与 percentile 互不干扰：切换模式不影响 absolute 的判定。"""
    from screener.filters.volume import VolumeSurgeFilter

    df = _volume_mode_frame(500, seed=41)
    abs_first, _ = VolumeSurgeFilter().apply(df)
    pct = VolumeSurgeFilter(mode="percentile")
    pct.bind_universe(df)
    pct_out, _ = pct.apply(df)
    abs_again, _ = VolumeSurgeFilter().apply(df)

    fixed = set(df.loc[df["volume_ratio_5"] >= 1.5, "ts_code"])
    ok = (
        set(abs_first["ts_code"]) == fixed          # 第一次 absolute
        and set(abs_again["ts_code"]) == fixed      # 夹着 percentile 跑完再跑 absolute，结果一致
        and set(pct_out["ts_code"]) != fixed        # percentile 确实不同
    )
    return Check(
        "量能 absolute/percentile 互不干扰",
        ok,
        f"absolute 前后两次各命中 {len(abs_first)}/{len(abs_again)} 只且都等于固定阈值判定；"
        f"percentile 命中 {len(pct_out)} 只（不同）",
    )


def _score_row(**kwargs) -> pd.DataFrame:
    """构造一行评分用的合成数据（未给的列用中性值）。"""
    base = {
        "ts_code": ["X"], "close": [11.0], "turnover": [0.0],
        "resonance_dwm": [False], "resonance_daily": [False],
        "double_bottom_10d": [False], "db_neckline": [10.0],
        "volume_ratio_5": [1.0], "dif": [0.0], "dea": [1.0],
        "k": [0.0], "d": [1.0], "hammer": [False],
    }
    base.update({k: [v] for k, v in kwargs.items()})
    return pd.DataFrame(base)


def _check_score_weights_sum() -> Check:
    """评分权重：满分必须 = 100，且趋势项 dwm/daily 二选一不叠加。"""
    from screener.scoring import Scorer

    s = Scorer()
    both = _score_row(resonance_dwm=True, resonance_daily=True)
    out = s.score(both)
    trend_pts = float(out["pts_resonance"].iloc[0])
    w = s.weights
    expected = (
        w["resonance_dwm"] + w["double_bottom"] + w["volume_surge"]
        + w["macd_bull"] + w["kdj_bull"] + w["turnover"] + w["hammer"]
    )
    ok = (
        abs(s.max_total - 100.0) < 1e-9
        and abs(expected - 100.0) < 1e-9
        and abs(trend_pts - w["resonance_dwm"]) < 1e-9      # 不是 30+12=42
        and len(s.describe()) == 7
    )
    return Check(
        "评分权重满分=100 且趋势二选一",
        ok,
        f"max_total={s.max_total:.0f}，累加口径={expected:.0f}，"
        f"dwm+daily 同时成立时趋势只加 {trend_pts:.0f} 分（不是 42）",
    )


def _check_score_perfect_and_empty() -> Check:
    """满分构造 → 100/核心/命中7；全无信号 → 0/空档/命中0。"""
    from screener.scoring import Scorer

    s = Scorer()
    full = _score_row(
        turnover=9.0, resonance_dwm=True, resonance_daily=True,
        double_bottom_10d=True, db_neckline=10.0, volume_ratio_5=2.0,
        dif=1.0, dea=0.0, k=60.0, d=50.0, hammer=True,
    )
    empty = _score_row()
    of = s.score(full).iloc[0]
    oe = s.score(empty).iloc[0]
    ok = (
        abs(float(of["score"]) - 100.0) < 1e-9
        and str(of["tier"]) == "核心"
        and int(of["hit_count"]) == 7
        and abs(float(oe["score"])) < 1e-9
        and str(oe["tier"]) == ""
        and int(oe["hit_count"]) == 0
    )
    return Check(
        "评分：满分行=100/核心，空行=0/无档",
        ok,
        f"满分行 score={of['score']:.0f} tier={of['tier']} 命中={of['hit_count']}；"
        f"空行 score={oe['score']:.0f} tier={oe['tier']!r} 命中={oe['hit_count']}",
    )


def _check_score_tier_boundary() -> Check:
    """分档边界：85→核心、75→观察、65→候选、64→不入榜（含 >= 语义）。"""
    from screener.scoring import Scorer

    def tier_for(points: float) -> str:
        s = Scorer(weights={
            "resonance_dwm": points, "resonance_daily": 0.0, "double_bottom": 0.0,
            "volume_surge": 0.0, "macd_bull": 0.0, "kdj_bull": 0.0,
            "turnover": 0.0, "hammer": 0.0,
        }, min_hit=0)
        return str(s.score(_score_row(resonance_dwm=True))["tier"].iloc[0])

    got = {p: tier_for(p) for p in (85.0, 75.0, 65.0, 64.0)}
    ok = got == {85.0: "核心", 75.0: "观察", 65.0: "候选", 64.0: ""}
    return Check("评分分档边界（85/75/65）", ok, f"{got}")


def _check_score_min_hit_gate() -> Check:
    """min_hit 门槛：分数够但信号数不足时不入榜。"""
    from screener.scoring import Scorer

    weights = {
        "resonance_dwm": 90.0, "resonance_daily": 0.0, "double_bottom": 0.0,
        "volume_surge": 0.0, "macd_bull": 0.0, "kdj_bull": 0.0,
        "turnover": 0.0, "hammer": 0.0,
    }
    row = _score_row(resonance_dwm=True)          # 90 分，但只命中 1 个信号
    strict = Scorer(weights=weights, min_hit=2).score(row)["tier"].iloc[0]
    loose = Scorer(weights=weights, min_hit=1).score(row)["tier"].iloc[0]
    ok = str(strict) == "" and str(loose) == "核心"
    return Check(
        "评分 min_hit 门槛",
        ok,
        f"90 分但只命中 1 项：min_hit=2 → {strict!r}（不入榜），min_hit=1 → {loose}",
    )


def _check_tier_caps() -> Check:
    """档位名额：核心超限时按分数截断。"""
    from screener.scoring import apply_tier_caps

    df = pd.DataFrame({
        "ts_code": [f"C{i}" for i in range(10)],
        "score": [99, 98, 97, 96, 95, 94, 93, 92, 91, 90],
        "hit_count": [5] * 10,
        "tier": ["核心"] * 10,
    })
    capped, stats = apply_tier_caps(df, caps={"core": 3, "watch": 0, "candidate": 0})
    ok = len(capped) == 3 and list(capped["score"]) == [99, 98, 97] and stats["capped"] == 7
    return Check(
        "档位名额截断",
        ok,
        f"核心 10 只 + 上限 3 → 保留 {len(capped)} 只（{list(capped['score'])}），截断 {stats['capped']} 只",
    )


def _tencent_fixture() -> str:
    """构造一条 88 字段的腾讯行情响应（各字段位置与真实接口一致）。

    真实接口已逐位核对：[47]=涨停价、[48]=跌停价（≈ 昨收 ×1.1 / ×0.9）、
    [30]=时间戳、[38]=换手率。这里只填测试关心的字段，其余留空。
    """
    f = [""] * 88
    f[0], f[1], f[2] = "1", "浦发银行", "600000"
    f[3], f[4], f[5], f[6] = "9.69", "9.62", "9.69", "367947"     # 现价/昨收/今开/量(手)
    f[30], f[31], f[32], f[33], f[34] = "20261023115438", "0.07", "0.73", "9.73", "9.52"
    f[37], f[38] = "35452", "0.11"                                 # 成交额(万)/换手率%
    f[47], f[48] = "10.58", "8.66"                                 # 涨停/跌停
    return 'v_sh600000="' + "~".join(f) + '";'


def _check_quote_parse() -> Check:
    """行情解析：字段映射正确（用"关系断言"防止索引写错）。"""
    from intraday.quotes import from_symbol, parse_sina, parse_tencent, to_symbol

    q = parse_tencent(_tencent_fixture())[0]
    pct_ok = abs(q.pct_chg - 0.73) < 0.02
    limit_ok = (
        abs(q.limit_up - q.prev_close * 1.1) < 0.02
        and abs(q.limit_down - q.prev_close * 0.9) < 0.02
    )
    sina = (
        'var hq_str_sz000001="平安银行,11.20,11.10,11.15,11.25,11.05,11.15,11.16,'
        '12345678,137654321.000,100,11.15,200,11.14,300,11.13,400,11.12,500,11.11,'
        '600,11.16,700,11.17,800,11.18,900,11.19,1000,11.20,0,0,0,0,0,0,'
        '2026-10-23,11:54:38,00,";'
    )
    q2 = parse_sina(sina)[0]
    sym_ok = to_symbol("600000.SH") == "sh600000" and from_symbol("sz000001") == "000001.SZ"
    ok = (
        q.ts_code == "600000.SH" and q.name == "浦发银行"
        and abs(q.price - 9.69) < 1e-9 and abs(q.prev_close - 9.62) < 1e-9
        and pct_ok and limit_ok
        and q.stamp is not None and q.stamp.strftime("%H:%M:%S") == "11:54:38"
        and abs(q.volume_lots - 367947) < 1e-6
        and abs(q2.price - 11.15) < 1e-9 and abs(q2.volume_lots - 123456.78) < 0.01
        and sym_ok
    )
    return Check(
        "盘中行情解析（腾讯/新浪/代码转换）",
        ok,
        f"腾讯 {q.ts_code} {q.name} {q.price} 昨收 {q.prev_close} 涨跌 {q.pct_chg:+.2f}% "
        f"涨停 {q.limit_up} 跌停 {q.limit_down}（=昨收±10%：{limit_ok}）；"
        f"新浪 {q2.ts_code} {q2.price} 量 {q2.volume_lots:.0f} 手",
    )


def _check_intraday_rules() -> Check:
    """盘中规则：跌破 MA20/颈线、急跌、拉升、放量各自触发，正常行情不误报。"""
    from intraday.quotes import Quote
    from intraday.rules import evaluate
    from intraday.watchlist import WatchItem

    item = WatchItem(ts_code="300006.SZ", name="莱美药业", score=92, tier="核心",
                     close=10.0, ma20=10.0, neckline=9.5, volume=1000.0, scan_date="20261014")
    rules = dict(config.INTRADAY_RULES)

    crash = Quote(ts_code="300006.SZ", name="莱美药业", price=9.0, prev_close=10.0,
                  volume_lots=500.0)
    calm = Quote(ts_code="300006.SZ", name="莱美药业", price=10.4, prev_close=10.3,
                 volume_lots=100.0)
    strong = Quote(ts_code="300006.SZ", name="莱美药业", price=10.2, prev_close=9.3,
                   volume_lots=1500.0, limit_up=10.23, limit_down=8.37)

    a_crash = {a.rule for a in evaluate(item, crash, rules)}
    a_calm = {a.rule for a in evaluate(item, calm, rules)}
    a_strong = {a.rule for a in evaluate(item, strong, rules)}

    ok = (
        {"break_ma20", "break_neckline", "drop"} <= a_crash
        and a_calm == set()
        and {"surge", "volume_burst"} <= a_strong
    )
    return Check(
        "盘中告警规则（跌破/急跌/拉升/放量）",
        ok,
        f"急跌 -10% → {sorted(a_crash)}；平稳 → {sorted(a_calm)}（应为空）；"
        f"放量+拉升 → {sorted(a_strong)}",
    )


def _check_intraday_dedupe_and_session() -> Check:
    """告警去重（同一股票同一规则每天一次）与交易时段判断。"""
    import shutil
    from datetime import datetime

    from intraday import monitor as mon
    from intraday.rules import Alert

    orig_dir = config.ALERT_DIR
    tmp_dir = config.OUTPUT_DIR / "_selftest_alerts"
    ok_dedupe = False
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        config.ALERT_DIR = tmp_dir
        alert = Alert(ts_code="000001.SZ", name="平安银行", rule="drop", level="risk",
                      message="测试", price=10.0, pct_chg=-6.0)
        mon._append_alerts([alert], "20261014")
        mon._append_alerts([alert], "20261014")          # 重复写一次
        seen = mon._load_seen(mon.alert_path())
        import pandas as pd

        rows = len(pd.read_csv(mon.alert_path()))
        ok_dedupe = rows == 2 and ("000001.SZ", "drop") in seen   # 文件两行（未去重由调用方过滤）
    finally:
        config.ALERT_DIR = orig_dir
        shutil.rmtree(tmp_dir, ignore_errors=True)

    sess = {
        "10:00": mon.in_session(datetime(2026, 10, 23, 10, 0)),
        "12:00": mon.in_session(datetime(2026, 10, 23, 12, 0)),
        "14:00": mon.in_session(datetime(2026, 10, 23, 14, 0)),
        "15:30": mon.in_session(datetime(2026, 10, 23, 15, 30)),
        "周六10:00": mon.in_session(datetime(2026, 10, 24, 10, 0)),
    }
    ok_session = (sess["10:00"] and not sess["12:00"] and sess["14:00"]
                  and not sess["15:30"] and not sess["周六10:00"])
    return Check(
        "盯梢时段判断 + 告警落盘/去重基础",
        ok_dedupe and ok_session,
        f"时段 {sess}；告警写入后 _load_seen 可识别={ok_dedupe}",
    )


def _check_no_llm_and_network_whitelist() -> Check:
    """硬规则：代码里没有任何大模型 SDK，且所有 URL 主机都在白名单内。"""
    import re as _re
    from pathlib import Path

    root = Path(config.BASE_DIR)
    llm_kw = ("openai", "anthropic", "langchain", "transformers", "llama_index",
              "google.generativeai", "cohere", "mistralai", "groq", "ollama", "vllm",
              "tiktoken", "sentence_transformers", "torch", "dashscope", "zhipuai", "qianfan")
    llm_hits: list[str] = []
    url_hosts: dict[str, list[str]] = {}
    for p in root.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip().lower()
            if s.startswith(("import ", "from ")):
                for kw in llm_kw:
                    if _re.search(rf"\b{_re.escape(kw)}\b", s):
                        llm_hits.append(f"{p.relative_to(root)}:{i}")
        for m in _re.finditer(r"https?://([^/\"'\s:]+)", text):
            url_hosts.setdefault(m.group(1).lower(), []).append(str(p.relative_to(root)))
    bad = sorted(h for h in url_hosts if h not in config.ALLOWED_NETWORK_HOSTS)
    ok = not llm_hits and not bad and config.LLM_FREE is True
    return Check(
        "零大模型依赖 + 联网主机白名单",
        ok,
        f"大模型 SDK 命中 {len(llm_hits)} 处；URL 主机 {sorted(url_hosts)}；"
        f"越界主机 {bad or '无'}"
        + (f"；越界文件 {url_hosts[bad[0]][:2]}" if bad else ""),
    )


CHECKS: list[Callable[[], Check]] = [
    _check_resample,
    _check_macd,
    _check_kdj,
    _check_candles,
    _check_swings,
    _check_double_bottom,
    _check_double_top,
    _check_resonance,
    _check_qfq_formula,
    _check_qfq_no_lookahead,
    _check_qfq_future_guard,
    _check_turnover_units,
    _check_filters,
    _check_turnover_filter,
    _check_neckline_params,
    _check_volume_ratio_excludes_today,
    _check_volume_filters_mutual_exclusion,
    _check_volume_filter_missing_history,
    _check_volume_mode_absolute_unchanged,
    _check_volume_percentile_rate,
    _check_volume_percentile_uses_universe,
    _check_volume_mode_isolation,
    _check_score_weights_sum,
    _check_score_perfect_and_empty,
    _check_score_tier_boundary,
    _check_score_min_hit_gate,
    _check_tier_caps,
    _check_quote_parse,
    _check_intraday_rules,
    _check_intraday_dedupe_and_session,
    _check_no_llm_and_network_whitelist,
    _check_local_only_guard,
    _check_local_pipeline_no_calls,
]


def run_checks() -> list[Check]:
    """执行全部检查，任何一条抛异常都会被转成失败项（不影响其它检查）。"""
    results: list[Check] = []
    for fn in CHECKS:
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001
            logger.exception("自检项 %s 失败", fn.__name__)
            results.append(Check(fn.__name__, False, f"{type(exc).__name__}: {exc}"))
    return results


if __name__ == "__main__":     # pragma: no cover - 手工运行
    logging.basicConfig(level=logging.WARNING)
    rows = run_checks()
    for c in rows:
        icon = "SKIP" if c.skip else ("PASS" if c.ok else "FAIL")
        print(f"[{icon}] {c.name:<44} {c.detail}")
    bad = [c for c in rows if not c.ok]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} 通过")
    raise SystemExit(1 if bad else 0)
