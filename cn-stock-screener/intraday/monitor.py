"""
intraday.monitor — 盘中盯梢主循环

两种用法
--------
    python main.py --watch                  # 检查一次（供计划任务每 15 分钟调用）
    python main.py --watch --loop           # 常驻循环，直到收盘（默认 300 秒一轮）

每轮做的事（全部确定性、秒级、零模型调用）：

1. 读最近一次 ``--score`` 榜单，取前 N 只（默认 30）；
2. 一次 HTTP 批量拉这几十只的实时快照（腾讯，失败自动回退新浪）；
3. 按 ``intraday/rules.py`` 的阈值评估 7 条规则；
4. 新告警追加到 ``output/alerts/intraday_YYYYMMDD.csv``（同一 (股票, 规则) 每天只报一次）；
5. 控制台打印「持仓/名单快照 + 触发的告警」。

为什么不重算全市场
------------------
全市场重算要读 750 个日线分片（~1 GB）并逐只算指标，5~6 分钟一次；
盘中盯梢只需要"我选的票现在什么价、比关键价位高还是低"，所以只抓几十只、
只做价格/量能比较——这才是 15 分钟级刷新该有的形态。
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

import config
from intraday.quotes import Quote, fetch_quotes
from intraday.rules import Alert, LEVEL_LABELS, evaluate, sort_alerts
from intraday.watchlist import Watchlist, filter_codes, load_watchlist

logger = logging.getLogger(__name__)

THIN = "-" * 96


# --------------------------------------------------------------------------
# 时段
# --------------------------------------------------------------------------
def _parse_hhmm(text: str) -> dtime:
    h, m = str(text).split(":")[:2]
    return dtime(int(h), int(m))


def sessions() -> list[tuple[dtime, dtime]]:
    return [(_parse_hhmm(a), _parse_hhmm(b)) for a, b in config.INTRADAY_SESSIONS]


def sessions_text() -> str:
    """``09:30~11:30 / 13:00~15:00``（打印用）。"""
    return " / ".join(f"{a}~{b}" for a, b in config.INTRADAY_SESSIONS)


#: 盯梢颗粒度：对齐 A 股 15 分钟 K 线的收盘时刻，而不是"打开软件后每 15 分钟"
BAR_MINUTES = 15


def bar_times() -> list[dtime]:
    """一个交易日内全部 15 分钟 K 线的收盘时刻。

    09:30 开盘 → 第一根 15 分钟线在 **09:45** 收盘，最后在 11:30 / 15:00 收盘。
    即：09:45, 10:00, ..., 11:30, 13:15, 13:30, ..., 15:00（共 16 根）。
    """
    out: list[dtime] = []
    for start, end in sessions():
        cursor = datetime.combine(date.today(), start) + timedelta(minutes=BAR_MINUTES)
        stop = datetime.combine(date.today(), end)
        while cursor <= stop:
            out.append(cursor.time())
            cursor += timedelta(minutes=BAR_MINUTES)
    return sorted(out)


def next_bar_time(now: datetime | None = None) -> datetime | None:
    """下一条 15 分钟 K 线的收盘时刻（跳过周末；没有则返回 None）。"""
    now = now or datetime.now()
    times = bar_times()
    for offset in range(0, 8):                  # 最多往后找一周
        day = now.date() + timedelta(days=offset)
        if day.weekday() >= 5:                  # 周末
            continue
        for bt in times:
            cand = datetime.combine(day, bt)
            if cand > now:
                return cand
    return None


def next_bar_text(now: datetime | None = None) -> str:
    """``2026-10-23 13:15`` 或 ``2026-10-26 09:45（下周一）``。"""
    nxt = next_bar_time(now)
    if nxt is None:
        return "无"
    now = now or datetime.now()
    tag = "" if nxt.date() == now.date() else "（非今日）"
    return f"{nxt:%Y-%m-%d %H:%M}{tag}"


def in_session(now: datetime | None = None) -> bool:
    """是否处于盯梢时段（默认 09:30~11:30 / 13:00~15:00）。"""
    now = now or datetime.now()
    if now.weekday() >= 5:          # 周末
        return False
    t = now.time()
    return any(start <= t <= end for start, end in sessions())


def next_session_hint(now: datetime | None = None) -> str:
    """给一个"下次什么时候盯"的提示。"""
    now = now or datetime.now()
    t = now.time()
    for start, end in sessions():
        if t < start:
            return f"下一次盯梢时段 {start.strftime('%H:%M')}~{end.strftime('%H:%M')}"
    return "今日盯梢时段已结束"


# --------------------------------------------------------------------------
# 告警落盘
# --------------------------------------------------------------------------
def alert_path(day: str | None = None) -> Path:
    day = day or date.today().strftime("%Y%m%d")
    return Path(config.ALERT_DIR) / f"intraday_{day}.csv"


def latest_quote_path() -> Path:
    """最近一次盯梢的行情快照（GUI 用它回填"现价/实时涨跌"）。"""
    return Path(config.REPORT_DIR) / "intraday_latest.csv"


def write_latest_quotes(watchlist: Watchlist, qmap: dict[str, Quote]) -> Path | None:
    """把本轮实时行情写成 CSV，供界面显示"现在什么价"。

    告警是**事件**（去重后只报一次），行情是**状态**（每轮都刷新）——
    分开存，界面才有肉眼可见的变化。
    """
    rows = []
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for item in watchlist.items:
        q = qmap.get(item.ts_code)
        if q is None:
            continue
        pct = q.pct_chg
        dist = ((q.price / item.ma20 - 1) * 100
                if item.ma20 and item.ma20 == item.ma20 and item.ma20 else None)
        rows.append({
            "ts_code": item.ts_code,
            "name": q.name or item.name,
            "score": item.score,
            "tier": item.tier,
            "price": q.price,
            "pct_chg": round(pct, 2) if pct == pct else None,
            "prev_close": q.prev_close,
            "turnover_rate": q.turnover_rate,
            "ma20": item.ma20,
            "neckline": item.neckline,
            "dist_ma20_pct": round(dist, 2) if dist is not None else None,
            "quote_time": q.stamp.strftime("%Y-%m-%d %H:%M:%S") if q.stamp else "",
            "fetched_at": now_text,
        })
    if not rows:
        return None
    path = latest_quote_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    return path


def _load_seen(path: Path) -> set[tuple[str, str]]:
    if not path.is_file():
        return set()
    try:
        df = pd.read_csv(path)
    except Exception:  # noqa: BLE001
        return set()
    if "ts_code" not in df.columns or "rule" not in df.columns:
        return set()
    return set(zip(df["ts_code"].astype(str), df["rule"].astype(str)))


def _append_alerts(alerts: Sequence[Alert], trade_date: str = "") -> Path | None:
    if not alerts:
        return None
    path = alert_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([a.as_row(trade_date) for a in alerts])
    header = not path.exists()
    df.to_csv(path, mode="a", header=header, index=False, encoding="utf-8-sig")
    return path


# --------------------------------------------------------------------------
# 一轮
# --------------------------------------------------------------------------
def run_cycle(
    top_n: int | None = None,
    codes: Sequence[str] | None = None,
    source: str | None = None,
    dedupe: bool | None = None,
    write: bool = True,
    verbose: bool = True,
) -> dict[str, Any]:
    """跑一轮盯梢，返回统计（便于自检/调用方断言）。"""
    result: dict[str, Any] = {
        "ok": False, "watch": 0, "quoted": 0, "alerts": 0, "new": 0,
        "path": None, "error": None, "quotes": [], "fired": [],
    }

    wl: Watchlist = load_watchlist(top_n=top_n)
    if codes:
        wl = filter_codes(wl, codes)
    if verbose:
        for w in wl.warnings:
            print(f"  ! {w}")
    if not wl.items:
        result["error"] = "名单为空"
        return result
    result["watch"] = len(wl.items)

    try:
        quotes = fetch_quotes(wl.codes, source=source)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        logger.error("行情获取失败：%s", exc)
        return result

    qmap: dict[str, Quote] = {q.ts_code: q for q in quotes}
    result["quoted"] = len(qmap)
    result["quotes"] = quotes
    # 每轮都刷新行情快照（界面据此更新"现价/实时涨跌"）
    try:
        result["snapshot"] = str(write_latest_quotes(wl, qmap) or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("写行情快照失败：%s", exc)

    fired: list[Alert] = []
    stale: list[str] = []
    for item in wl.items:
        q = qmap.get(item.ts_code)
        if q is None:
            stale.append(item.ts_code)
            continue
        fired.extend(evaluate(item, q))
    fired = sort_alerts(fired)
    result["alerts"] = len(fired)

    use_dedupe = config.INTRADAY_DEDUPE if dedupe is None else bool(dedupe)
    seen = _load_seen(alert_path()) if use_dedupe else set()
    fresh = [a for a in fired if (a.ts_code, a.rule) not in seen]
    result["new"] = len(fresh)

    if write and fresh:
        result["path"] = _append_alerts(fresh, wl.scan_date)

    if verbose:
        print(THIN)
        stamp = max((q.stamp for q in quotes if q.stamp), default=None)
        print(
            f"盘中盯梢 {datetime.now():%Y-%m-%d %H:%M:%S} | 名单 {len(wl.items)} 只"
            f" | 行情时间 {stamp.strftime('%H:%M:%S') if stamp else '未知'}"
            f" | 扫描日 {wl.scan_date or '-'}"
        )
        print(f"名单来源：{wl.source.name if wl.source else '-'}")
        if stale:
            print(f"  ! {len(stale)} 只无行情（停牌/代码异常）：{', '.join(stale[:5])}")
        print(THIN)
        print(f"{'代码':<11}{'名称':<9}{'现价':>8}{'涨跌%':>8}{'评分':>6}{'档位':<6}{'距MA20':>9}{'距颈线':>9}")
        for item in wl.items:
            q = qmap.get(item.ts_code)
            if q is None:
                print(f"{item.ts_code:<11}{item.name[:8]:<9}{'停牌/无行情':>8}")
                continue
            def _pct(a: float, b: float) -> str:
                return f"{(a / b - 1) * 100:+.2f}%" if (b and b == b) else "-"
            print(
                f"{item.ts_code:<11}{item.name[:8]:<9}{q.price:>8.2f}"
                f"{q.pct_chg:>+8.2f}{item.score:>6.0f}{item.tier:<6}"
                f"{_pct(q.price, item.ma20):>9}{_pct(q.price, item.neckline):>9}"
            )
        print(THIN)
        if fired:
            print(f"触发 {len(fired)} 条（其中新增 {len(fresh)} 条）")
            for a in fired:
                flag = "" if (a.ts_code, a.rule) in seen else " [新]"
                print(f"  [{LEVEL_LABELS.get(a.level, a.level)}] {a.ts_code} {a.name[:6]:<7}"
                      f" {a.label:<8} {a.message}{flag}")
        else:
            print("无告警：名单内价格均未触及阈值")
        if result["path"]:
            print(f"  新告警已追加：{result['path']}")
        print(THIN)

    result["fired"] = fired
    result["ok"] = True
    return result


# --------------------------------------------------------------------------
# 循环
# --------------------------------------------------------------------------
def run_loop(
    interval: int | None = None,
    top_n: int | None = None,
    codes: Sequence[str] | None = None,
    source: str | None = None,
    force: bool = False,
) -> int:
    """常驻循环，直到收盘或跨出盯梢时段。"""
    interval = int(interval or config.INTRADAY_INTERVAL_SECONDS)
    rounds = 0
    while True:
        now = datetime.now()
        if not force and not in_session(now):
            if rounds == 0:
                print(f"[跳过] 当前 {now:%H:%M} 不在盯梢时段"
                      f"（{sessions_text()}）。{next_session_hint(now)}")
                return 0
            print(f"[收工] {now:%H:%M} 已离开盯梢时段，共 {rounds} 轮")
            return 0

        res = run_cycle(top_n=top_n, codes=codes, source=source)
        rounds += 1
        if not res["ok"]:
            logger.warning("第 %d 轮失败：%s", rounds, res["error"])

        now = datetime.now()
        if not force and not in_session(now):
            print(f"[收工] {now:%H:%M} 已离开盯梢时段，共 {rounds} 轮")
            return 0
        print(f"--- {interval} 秒后进入下一轮 ---")
        time.sleep(interval)


def cmd_watch(
    top_n: int | None = None,
    codes: Sequence[str] | None = None,
    source: str | None = None,
    loop: bool = False,
    interval: int | None = None,
    allow_outside_session: bool = False,
) -> int:
    """``--watch`` 入口。返回进程退出码。"""
    if not config.INTRADAY_ENABLED:
        print("[跳过] 配置里 INTRADAY_ENABLED = False")
        return 0

    if not allow_outside_session and not in_session():
        print(f"[跳过] 当前 {datetime.now():%H:%M} 不在盯梢时段"
              f"（{sessions_text()}）。{next_session_hint()}")
        print("       想强制跑一次可加 --allow-outside-session")
        return 0

    if loop:
        try:
            return run_loop(interval=interval, top_n=top_n, codes=codes, source=source)
        except KeyboardInterrupt:
            print("\n[中断] 已停止盯梢")
            return 0

    res = run_cycle(top_n=top_n, codes=codes, source=source)
    if not res["ok"]:
        print(f"[错误] {res['error']}")
        return 3
    return 0


__all__ = [
    "in_session", "next_session_hint", "alert_path", "latest_quote_path",
    "write_latest_quotes", "bar_times", "next_bar_time", "next_bar_text",
    "run_cycle", "run_loop", "cmd_watch", "sessions",
]
