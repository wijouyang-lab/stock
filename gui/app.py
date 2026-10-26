"""
gui.app — 桌面窗口（tkinter，仅用标准库，无额外依赖）

定位：给不习惯命令行的使用者一个可视化入口（打包成 exe 时的默认界面）。

- 顶部：项目根目录、Token 状态
- 按钮：更新数据（纯行情 / 含财务）、**评分排行**、自检、刷新、打开结果
- 中部：**评分排行结果表格**（双击某行可在下方日志里看细节）
- 底部：实时运行日志
- 所有产物仍落在项目文件夹内

实现要点
--------
1. 所有"跑任务"的动作都通过 **子进程** 调用本程序自己的 CLI 入口
   （冻结后 ``sys.executable`` 就是 exe 本身）。这样：
   - 窗口模式（无控制台）下不会因为 ``print()`` 没有 stdout 而崩溃；
   - GUI 与 CLI 复用同一套逻辑，不存在第二套实现。
2. 子进程输出逐行读入队列，再由 Tk 主线程渲染，跨线程不直接碰控件。
"""

from __future__ import annotations

import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

from config import (
    BASE_DIR,
    CACHE_DIR,
    LOG_DIR,
    OUTPUT_DIR,
    REPORT_DIR,
    get_token,
    mask_token,
    token_source,
)
from intraday.monitor import next_bar_time

APP_TITLE = "cn-stock-screener — A股技术指标筛选器"

#: 自动刷新对齐 A 股 15 分钟 K 线收盘（09:45/10:00/.../11:30、13:15/.../15:00），
#: 而不是"打开软件后每 15 分钟"。边界由 intraday.monitor.next_bar_time 计算。
#: 子进程不弹黑窗
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class QueueHandler(logging.Handler):
    """把日志推进队列，交给 Tk 主线程渲染。"""

    def __init__(self, q: "queue.Queue[str]") -> None:
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put(self.format(record))
        except Exception:  # noqa: BLE001
            pass


class ScreenerApp:
    """主窗口。"""

    #: 盘中榜列（行业优先选股结果）
    SCAN_COLUMNS = (
        ("rank", "#", 30, "center"),
        ("industry", "行业", 78, "w"),
        ("ts_code", "代码", 82, "center"),
        ("name", "名称", 84, "w"),
        ("score", "盘中分", 52, "center"),
        ("price", "现价", 54, "e"),
        ("bar_close", "K线收", 54, "e"),
        ("bar_pct", "15分%", 54, "e"),
        ("pct_chg", "今日%", 54, "e"),
        ("dist_ma20", "距MA20", 62, "e"),
        ("vr_15m", "量比", 48, "e"),
        ("macd_15m", "MACD", 50, "center"),
        ("kdj_15m", "KDJ", 46, "center"),
    )

    #: 结果表格列（key, 标题, 宽度, 对齐）
    COLUMNS = (
        ("rank", "#", 34, "center"),
        ("state", "状态", 56, "center"),
        ("signal", "信号", 62, "center"),
        ("score", "评分", 48, "center"),
        ("tier", "档位", 48, "center"),
        ("ts_code", "代码", 88, "center"),
        ("name", "名称", 100, "w"),
        ("industry", "行业", 100, "w"),
        ("close", "收盘", 58, "e"),
        ("pct_chg", "涨跌%", 58, "e"),
        ("price", "现价", 58, "e"),
        ("live_pct", "实时%", 58, "e"),
        ("dist_ma20", "距MA20", 66, "e"),
        ("turnover", "换手%", 54, "e"),
        ("vr", "量比", 48, "e"),
        ("macd", "MACD", 54, "center"),
        ("kdj", "KDJ", 50, "center"),
        ("hit_count", "命中", 42, "center"),
    )
    #: 列名 -> 下标（回填实时行情时用）
    _IDX = {key: i for i, (key, *_rest) in enumerate(COLUMNS)}
    #: 盘中状态排序权重（越小越靠前）
    STATE_RANK = {"冲高": 0, "正常": 1, "预警": 2, "失效": 3, "—": 4}

    def __init__(self, root: tk.Tk, base_dir: Path, offline: bool = False) -> None:
        self.root = root
        self.base_dir = base_dir
        self.offline = offline
        self.queue: "queue.Queue[str]" = queue.Queue()
        self.running = False
        self._proc: subprocess.Popen | None = None
        self._what = ""
        self._start_ts: float | None = None
        # 自动刷新（对齐 15 分钟 K 线收盘）
        self.auto_var = tk.BooleanVar(value=True)
        self._next_bar = next_bar_time()
        self._last_watch = ""
        self._last_rank = ""
        self._live_stamp = ""
        self._base_rows: list[dict] = []
        self._has_score = False
        self._prev_state: dict[str, str] = {}
        self.auto_scan_var = tk.BooleanVar(value=True)

        root.title(APP_TITLE)
        root.geometry("1060x760")
        root.minsize(900, 620)

        self._build_widgets()
        self._refresh_status()
        self._load_latest_results(quiet=True)
        self.root.after(150, self._drain_queue)
        # 开屏自动刷新：先把窗口画出来，再开始算
        self.root.after(1200, self._auto_start)
        self.root.after(1000, self._countdown_tick)

    # ------------------------------------------------------------ 界面构建
    def _build_widgets(self) -> None:
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text=APP_TITLE, font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        ttk.Label(top, text=f"项目根目录：{self.base_dir}",
                  foreground="#555").pack(anchor="w", pady=(4, 0))
        self.token_var = tk.StringVar()
        ttk.Label(top, textvariable=self.token_var, foreground="#555").pack(anchor="w")

        btns = ttk.Frame(self.root, padding=(10, 0))
        btns.pack(fill="x")
        self.btn_rank = ttk.Button(btns, text="★ 评分排行（约 5 分钟）",
                                   command=self._run_ranking)
        self.btn_quote = ttk.Button(
            btns, text="更新数据（纯行情）",
            command=lambda: self._run_steps(
                [{"args": ["--cli", "--mode", "quote"], "label": "同步行情"}], "同步行情"))
        self.btn_full = ttk.Button(
            btns, text="更新数据（含财务）",
            command=lambda: self._run_steps(
                [{"args": ["--cli", "--mode", "full"], "label": "完整同步"}], "完整同步"))
        self.btn_watch = ttk.Button(btns, text="立即盯梢", command=self._run_watch)
        self.btn_test = ttk.Button(btns, text="自检", command=self._run_selftest)
        self.btn_refresh = ttk.Button(btns, text="刷新", command=self._refresh_status)
        self.btn_open = ttk.Button(btns, text="打开结果文件夹", command=self._open_reports)
        for b in (self.btn_rank, self.btn_watch, self.btn_quote, self.btn_full,
                  self.btn_test, self.btn_refresh, self.btn_open):
            b.pack(side="left", padx=(0, 6), pady=6)
        ttk.Checkbutton(btns, text="自动刷新", variable=self.auto_var).pack(
            side="left", padx=(10, 0), pady=6
        )

        # ---- 两个榜单分开显示 ----
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10)

        # 标签页 1：日线榜（每日收盘选股 + 买卖信号）
        tab_daily = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(tab_daily, text="  日线榜 · 每日收盘选股  ")
        self.tree = ttk.Treeview(
            tab_daily, columns=[c[0] for c in self.COLUMNS], show="headings", height=10
        )
        for key, title, width, anchor in self.COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=width - 10, anchor=anchor,
                             stretch=(key == "industry"))
        vsb = ttk.Scrollbar(tab_daily, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("core", background="#fff2cc")
        self.tree.tag_configure("watch", background="#e8f2ff")
        self.tree.tag_configure("risk", background="#ffe0e0")     # 盘中跌幅大 / 失效
        self.tree.tag_configure("opp", background="#e0f7e0")      # 盘中大涨 / 冲高

        # 标签页 2：盘中榜（每 15 分钟全市场扫描）
        tab_intra = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(tab_intra, text="  盘中榜 · 每 15 分钟全市场  ")

        bar = ttk.Frame(tab_intra)
        bar.pack(fill="x", pady=(0, 4))
        self.btn_scan = ttk.Button(bar, text="扫描全市场（约 30 秒）", command=self._run_scan)
        self.btn_scan.pack(side="left")
        ttk.Checkbutton(bar, text="每根 15 分钟 K 线自动扫描",
                        variable=self.auto_scan_var).pack(side="left", padx=(10, 0))
        self.scan_info = tk.StringVar(value="尚未扫描")
        ttk.Label(bar, textvariable=self.scan_info, foreground="#555").pack(side="left", padx=(12, 0))

        self.tree_scan = ttk.Treeview(
            tab_intra, columns=[c[0] for c in self.SCAN_COLUMNS], show="headings", height=10
        )
        for key, title, width, anchor in self.SCAN_COLUMNS:
            self.tree_scan.heading(key, text=title)
            self.tree_scan.column(key, width=width, minwidth=width - 10, anchor=anchor,
                                  stretch=(key == "name"))
        vsb2 = ttk.Scrollbar(tab_intra, orient="vertical", command=self.tree_scan.yview)
        self.tree_scan.configure(yscrollcommand=vsb2.set)
        self.tree_scan.pack(side="left", fill="both", expand=True)
        vsb2.pack(side="right", fill="y")
        self.tree_scan.tag_configure("up", background="#e0f7e0")
        self.tree_scan.tag_configure("down", background="#ffe0e0")

        # ---- 日志 ----
        bottom = ttk.LabelFrame(self.root, text="运行日志", padding=6)
        bottom.pack(fill="both", padx=10, pady=(6, 10))
        self.log_text = tk.Text(bottom, height=11, wrap="word", font=("Consolas", 9))
        scroll = ttk.Scrollbar(bottom, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status_var, relief="sunken",
                  anchor="w", padding=4).pack(fill="x", side="bottom")

    # -------------------------------------------------------------- 状态栏
    def _refresh_status(self) -> None:
        token = get_token()
        state = "已配置" if token else "未配置"
        self.token_var.set(
            f"Tushare Token：{mask_token()}  [{state}]  来源：{token_source()}"
            f"    |    结果目录：{REPORT_DIR}"
        )

    # ---------------------------------------------------------------- 日志
    def _log(self, text: str) -> None:
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")

    def _drain_queue(self) -> None:
        try:
            while True:
                self._log(self.queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(150, self._drain_queue)

    def _set_running(self, running: bool, what: str = "") -> None:
        self.running = running
        state = "disabled" if running else "normal"
        for b in (self.btn_rank, self.btn_watch, self.btn_quote, self.btn_full,
                  self.btn_test, self.btn_refresh, self.btn_open):
            b.configure(state=state)
        if running:
            self._what = what
            self._start_ts = time.monotonic()
            self._tick()
        else:
            self._next_bar = next_bar_time()
            self.status_var.set(self._idle_status())

    # ---------------------------------------------------------- 自动刷新
    def _idle_status(self) -> str:
        if not self.auto_var.get():
            return "就绪 ｜ 自动刷新已关闭"
        if self._next_bar is None:
            return "就绪 ｜ 自动刷新（无交易时段）"
        left = max(0, int((self._next_bar - datetime.now()).total_seconds()))
        m, s = divmod(left, 60)
        same_day = self._next_bar.date() == datetime.now().date()
        target = f"{self._next_bar:%H:%M}" if same_day else f"{self._next_bar:%m-%d %H:%M}"
        text = f"就绪 ｜ 下一条 15 分钟 K 线 {target} 收盘，剩余 {m:02d}:{s:02d}"
        if self._live_stamp:
            text += f" ｜ 行情 {self._live_stamp[11:19]}"
        if self._last_watch:
            text += f" ｜ {self._last_watch}"
        return text

    def _data_changed(self, report: Path | None) -> bool:
        """缓存里是否有比现有结果更新的数据。"""
        if report is None:
            return True
        shard_dir = CACHE_DIR / "daily"
        shards = sorted(shard_dir.glob("*.csv")) if shard_dir.is_dir() else []
        if not shards:
            return True
        latest = shards[-1]
        m = re.search(r"_(\d{8})_", report.name)
        rep_date = m.group(1) if m else ""
        if rep_date != latest.stem:                 # 有了更新交易日的分片
            return True
        try:
            return latest.stat().st_mtime > report.stat().st_mtime
        except OSError:
            return True

    def _needs_refresh(self) -> bool:
        report = self._latest_report()
        if report is None:
            return True
        try:
            if time.time() - report.stat().st_mtime < 60:   # 刚算完，别重复跑
                return False
        except OSError:
            pass
        return self._data_changed(report)

    def _auto_start(self) -> None:
        """开屏：日线有新数据 → 重算榜单；否则 → 先跑一次盘中盯梢（行情总是新的）。"""
        if not self.auto_var.get():
            return
        if self._needs_refresh():
            self._log("[自动] 启动：检测到日线数据有更新，先刷新榜单…")
            self._run_ranking(auto=True)
        else:
            self._log("[自动] 启动：榜单已是最新（日线未变），先跑一次盘中盯梢看当下行情…")
            self._run_watch("启动盯梢")

    def _countdown_tick(self) -> None:
        """到 15 分钟 K 线收盘：**必跑盯梢**（行情在变），
        日线数据有更新时才追加榜单重算（日线一天一根，盘中重算结果不会变）。"""
        try:
            now = datetime.now()
            if self._next_bar is None:
                self._next_bar = next_bar_time(now)
            if (not self.running and self.auto_var.get()
                    and self._next_bar is not None and now >= self._next_bar):
                bar_label = f"{self._next_bar:%H:%M}"
                self._next_bar = next_bar_time(now)
                steps = [{
                    "args": ["--watch"],
                    "label": f"{bar_label} K线收盘 · 盯梢",
                }]
                if self._needs_refresh():
                    steps.append({
                        "args": ["--cli", "--score"],
                        "label": "榜单刷新（日线已更新）",
                        "fallback": ["--cli", "--local-only", "--score"],
                    })
                    note = "盯梢 + 重算榜单（日线数据已更新）"
                else:
                    note = "盯梢（行情实时变化）；榜单不变——日线要收盘后才入库"
                self._log(f"[自动] {now:%H:%M:%S} {bar_label} 的 15 分钟 K 线收盘 → {note}")
                self._run_steps(steps, f"{bar_label} K线", reload_results=True)
            if not self.running:
                self.status_var.set(self._idle_status())
        finally:
            self.root.after(1000, self._countdown_tick)

    def _tick(self) -> None:
        """运行中每秒刷新"已用时长"——避免长时间计算看起来像卡死。"""
        if not self.running or self._start_ts is None:
            return
        el = int(time.monotonic() - self._start_ts)
        self.status_var.set(
            f"正在执行：{self._what} … 已用 {el // 60} 分 {el % 60:02d} 秒"
            f"（计算期间日志会安静几分钟，属正常）"
        )
        self.root.after(1000, self._tick)

    # -------------------------------------------------------------- CLI 调用
    def _cmd(self, extra: list[str]) -> list[str]:
        """冻结时 sys.executable 就是 exe；源码时用 python main.py。"""
        if getattr(sys, "frozen", False):
            return [sys.executable, *extra]
        return [sys.executable, str(self.base_dir / "main.py"), *extra]

    def _run_steps(self, steps: list[dict], what: str, reload_results: bool = False,
                   reload_scan: bool = False) -> None:
        """依次跑一串子进程任务（每步可选 fallback），全程日志可见。"""
        if self.running:
            return
        self._set_running(True, what)
        self._log("=" * 70)
        threading.Thread(
            target=self._worker_steps,
            args=(steps, what, reload_results, reload_scan),
            daemon=True,
        ).start()

    def _spawn(self, extra: list[str]) -> tuple[int, list[str]]:
        """跑一次子进程，把输出逐行送进日志队列，返回 (退出码, 输出行)。"""
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"        # 子进程逐行输出，日志不会憋到最后才出来
        lines: list[str] = []
        proc = subprocess.Popen(
            self._cmd(extra),
            cwd=str(self.base_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=_CREATE_NO_WINDOW,
        )
        self._proc = proc
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    lines.append(line)
                    self.queue.put(line)
        finally:
            code = proc.wait()
            self._proc = None
        return code, lines

    def _worker_steps(self, steps: list[dict], what: str, reload_results: bool, reload_scan: bool = False) -> None:
        code = 0
        try:
            for step in steps:
                label = step["label"]
                self.queue.put(f"[{datetime_now()}] {label} …")
                code, lines = self._spawn(step["args"])
                fallback = step.get("fallback")
                if code != 0 and fallback:
                    self.queue.put(f"[提示] {label} 失败（退出码 {code}），改用本地缓存重试…")
                    code, lines = self._spawn(fallback)
                self._summarize(label, lines, code)
        except Exception as exc:  # noqa: BLE001
            self.queue.put(f"[错误] {type(exc).__name__}: {exc}")
            code = -1
        finally:
            self.queue.put(f"[{datetime_now()}] 结束：{what}，退出码 {code}")
            self.root.after(0, lambda: self._after_run(reload_results, reload_scan))

    def _summarize(self, label: str, lines: list[str], code: int) -> None:
        """从子进程输出里提炼一句结论（盯梢结果 / 失败原因）。"""
        text = "\n".join(lines)
        if "盘中盯梢" in text or "盯梢名单" in text:
            m = re.search(r"名单\s*(\d+)\s*只", text)
            n = re.search(r"其中新增\s*(\d+)\s*条", text)
            total = re.search(r"触发\s*(\d+)\s*条", text)
            self._last_watch = (
                f"{label}：名单 {m.group(1) if m else '?'} 只，"
                f"命中 {total.group(1) if total else '0'} 条，新增 {n.group(1) if n else '0'} 条"
            )
            self.queue.put(f"[盯梢] {self._last_watch}")
            if (n.group(1) if n else "0") == "0":
                self.queue.put("        （新增 0 条 = 这些告警今天已经报过，去重不重复写；"
                               "最新价看表格「现价/实时%」两列）")
            self.root.after(0, self._apply_live_quotes)
        elif "筛选命中" in text:
            m = re.search(r"筛选命中\s*(\d+)\s*只", text)
            if m:
                self._last_rank = f"{label}：命中 {m.group(1)} 只"
                self.queue.put(f"[榜单] {self._last_rank}")
        elif code != 0:
            self.queue.put(f"[错误] {label} 退出码 {code}")

    def _after_run(self, reload_results: bool = False, reload_scan: bool = False) -> None:
        self._set_running(False)
        self._refresh_status()
        if reload_results:
            self._load_latest_results()
        if reload_scan:
            self._load_scan()

    # ------------------------------------------------------------ 具体动作
    def _run_ranking(self, auto: bool = False) -> None:
        """一键：先联网更新到最新交易日，失败则退回本地缓存重算。"""
        self._run_steps(
            [{
                "args": ["--cli", "--score"],
                "label": "榜单刷新（先更新日线数据）",
                "fallback": ["--cli", "--local-only", "--score"],
            }],
            "自动刷新" if auto else "评分排行",
            reload_results=True,
        )

    def _run_watch(self, label: str = "盘中盯梢") -> None:
        """跑一次盯梢：抓实时报价，比 MA20/颈线/涨跌停/量能。"""
        self._run_steps([{"args": ["--watch"], "label": label}], label)

    def _run_scan(self, auto: bool = False) -> None:
        """全市场盘中扫描（约 5000 只，30 秒左右）。"""
        label = "全市场盘中扫描" if not auto else "K线收盘 · 全市场扫描"
        self._run_steps([{"args": ["--scan"], "label": label}], label,
                        reload_scan=True)

    def _load_scan(self, quiet: bool = False) -> None:
        """读取最近一次扫描结果（intraday_scan_latest.csv）。"""
        path = REPORT_DIR / "intraday_rank_latest.csv"
        self.tree_scan.delete(*self.tree_scan.get_children())
        if not path.is_file():
            if not quiet:
                self._log("[提示] 还没有盘中扫描结果，点「扫描全市场」或等下一根 15 分钟 K 线。")
            return
        try:
            import pandas as pd

            df = pd.read_csv(path)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[错误] 读取扫描结果失败：{exc}")
            return
        for i, row in enumerate(df.itertuples(index=False), 1):
            d = row._asdict()
            try:
                pct = float(d.get("pct_chg"))
            except (TypeError, ValueError):
                pct = 0.0
            tag = "up" if pct >= 0 else "down"
            self.tree_scan.insert("", "end", values=(
                i, str(d.get("industry", ""))[:7], str(d.get("ts_code", "")),
                str(d.get("name", ""))[:9], _fmt(d.get("score"), "{:.1f}"),
                _fmt(d.get("price"), "{:.2f}"),
                _fmt(d.get("bar_close"), "{:.2f}"),
                _fmt(d.get("bar_pct"), "{:+.2f}"),
                _fmt(d.get("pct_chg"), "{:+.2f}"),
                _fmt(d.get("dist_ma20_pct"), "{:+.2f}"),
                _fmt(d.get("vr_15m"), "{:.2f}"),
                str(d.get("macd_15m") or "-"), str(d.get("kdj_15m") or "-"),
            ), tags=(tag,))
        ind_path = REPORT_DIR / "intraday_industry_latest.csv"
        summary = ""
        if ind_path.is_file():
            try:
                import pandas as _pd
                ind = _pd.read_csv(ind_path).head(5)
                summary = " ｜ ".join(
                    f"{r.industry}+{float(r.avg_pct):.1f}%"
                    for r in ind.itertuples(index=False))
            except Exception:
                pass
        self.scan_info.set(f"入选 {len(df)} 只（前 5 行业各取最多 5 只）")
        if summary:
            self._log(f"[行业强度] {summary}")
        if not quiet:
            self._log(f"[盘中榜] 已加载 {len(df)} 只（行业优先选股）")

    def _run_selftest(self) -> None:
        extra = ["--self-test"] + (["--offline"] if self.offline else [])
        self._run_steps([{"args": extra, "label": "自检"}], "自检")

    def _open_reports(self) -> None:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(REPORT_DIR))  # noqa: S606 - Windows 专用
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打开失败", str(exc))

    # ------------------------------------------------------------ 结果加载
    def _latest_report(self) -> Path | None:
        if not REPORT_DIR.is_dir():
            return None
        files = sorted(
            [p for p in REPORT_DIR.glob("*.csv") if p.name.startswith(("replay_", "screen_"))],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return files[0] if files else None

    def _load_latest_results(self, quiet: bool = False) -> None:
        path = self._latest_report()
        self.tree.delete(*self.tree.get_children())
        if path is None:
            if not quiet:
                self._log("[提示] 还没有结果文件，先点「★ 评分排行」跑一次。")
            return
        try:
            import pandas as pd

            df = pd.read_csv(path)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[错误] 读取 {path.name} 失败：{exc}")
            return

        has_score = "score" in df.columns
        if has_score:
            df = df.sort_values("score", ascending=False, kind="mergesort")
        top = df.head(300)

        # 缓存基础行；渲染时叠加实时行情并按"盘中状态"排序
        self._has_score = has_score
        self._base_rows = []
        for row in top.itertuples(index=False):
            d = row._asdict()
            self._base_rows.append({
                "ts_code": str(d.get("ts_code", "")),
                "name": str(d.get("name", "")),
                "industry": str(d.get("industry", "") or "-"),
                "score": float(d.get("score") or 0) if has_score else 0.0,
                "tier": str(d.get("tier") or "") or "-",
                "signal": str(d.get("signal") or "—"),
                "signal_note": str(d.get("signal_note") or ""),
                "hold_days": d.get("hold_days"),
                "close": d.get("close"),
                "pct_chg": d.get("pct_chg"),
                "turnover": d.get("turnover"),
                "vr": d.get("volume_ratio_5"),
                "hit": int(d.get("hit_count") or 0) if has_score else 0,
                "macd": _side(d.get("dif"), d.get("dea"), "多头", "空头"),
                "kdj": _side(d.get("k"), d.get("d"), "金叉", "死叉"),
            })
        self._render_rows()
        if not quiet:
            self._log(f"[完成] 已加载 {path.name}，共 {len(df)} 行（表格显示前 {len(top)} 行）")

    # -------------------------------------------------------- 实时行情回填
    def _read_live(self) -> dict[str, dict]:
        """读最近一次盯梢落盘的行情快照。"""
        path = REPORT_DIR / "intraday_latest.csv"
        if not path.is_file():
            return {}
        try:
            import pandas as pd

            df = pd.read_csv(path)
        except Exception:  # noqa: BLE001
            return {}
        return {str(r["ts_code"]): r for r in df.to_dict("records")}

    @staticmethod
    def _state_of(q: dict) -> str:
        """盘中状态：跌回关键位下方=失效，贴线=预警，大涨=冲高，其余=正常。"""
        try:
            price = float(q.get("price"))
            ma20 = float(q.get("ma20") or 0)
            neck_raw = q.get("neckline")
            neck = float(neck_raw) if neck_raw not in (None, "") else 0.0
            pct = float(q.get("pct_chg") or 0)
        except (TypeError, ValueError):
            return "—"
        if ma20 and price < ma20:
            return "失效"
        if neck and price < neck:
            return "失效"
        if pct >= 7:
            return "冲高"
        if ma20 and abs(price / ma20 - 1) <= 0.02:
            return "预警"
        return "正常"

    def _render_rows(self) -> None:
        """叠加实时行情 + 按状态排序 + 记录状态迁移（名单不变，状态会变）。"""
        live = self._read_live()
        rows = []
        for base in self._base_rows:
            r = dict(base)
            q = live.get(r["ts_code"])
            r["price"] = q.get("price") if q else None
            r["live_pct"] = q.get("pct_chg") if q else None
            r["dist"] = q.get("dist_ma20_pct") if q else None
            r["state"] = self._state_of(q) if q else "—"
            rows.append(r)
        rows.sort(key=lambda r: (
            self.STATE_RANK.get(r["state"], 9),
            -(r["live_pct"] if isinstance(r["live_pct"], (int, float)) else -999.0),
            -r["score"],
        ))

        self.tree.delete(*self.tree.get_children())
        state_tag = {"失效": "risk", "冲高": "opp", "预警": "watch", "核心": "core"}
        signal_tag = {"买入": "opp", "卖出": "risk", "T+1锁定": "watch"}
        now_state: dict[str, str] = {}
        for i, r in enumerate(rows, 1):
            tag = signal_tag.get(r.get("signal", ""), "") or state_tag.get(r["state"], "")
            if not tag and r["tier"] == "核心":
                tag = "core"
            self.tree.insert("", "end", values=(
                i, r["state"], r.get("signal", "—"),
                f"{r['score']:.0f}" if self._has_score else "-",
                r["tier"], r["ts_code"], r["name"][:10], r["industry"],
                _fmt(r["close"], "{:.2f}"), _fmt(r["pct_chg"], "{:+.2f}"),
                _fmt(r["price"], "{:.2f}"), _fmt(r["live_pct"], "{:+.2f}"),
                _fmt(r["dist"], "{:+.2f}"),
                _fmt(r["turnover"], "{:.2f}"), _fmt(r["vr"], "{:.2f}"),
                r.get("macd", "-"), r.get("kdj", "-"),
                r["hit"] if self._has_score else "-",
            ), tags=(tag,) if tag else ())
            now_state[r["ts_code"]] = r["state"]

        changes = []
        for r in rows:
            prev = self._prev_state.get(r["ts_code"])
            if prev and prev != r["state"]:
                changes.append(f"{r['name'][:6]}({r['ts_code']}) {prev}→{r['state']}")
        if changes:
            self._log(f"[状态] {len(changes)} 只状态变化：" + "；".join(changes))
        self._prev_state = now_state

        for q in live.values():
            stamp = str(q.get("quote_time") or "")
            if stamp:
                self._live_stamp = stamp
                break
        if self._live_stamp:
            self.status_var.set(self._idle_status())

    def _apply_live_quotes(self) -> None:
        self._render_rows()


def _side(a, b, up: str, down: str) -> str:
    """两个指标值比较出方向；任一为 NaN/缺失时返回 "-"（不猜）。"""
    try:
        va, vb = float(a), float(b)
    except (TypeError, ValueError):
        return "-"
    if va != va or vb != vb:
        return "-"
    return up if va > vb else down


def _fmt(value, spec: str) -> str:
    """格式化数值；None / NaN / 非数字一律显示 "-"。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "-"
    if num != num:                 # NaN
        return "-"
    try:
        return spec.format(num)
    except (TypeError, ValueError):
        return "-"


def datetime_now() -> str:
    from datetime import datetime

    return datetime.now().strftime("%H:%M:%S")


def launch(base_dir: Path | None = None, offline: bool = False) -> int:
    """启动桌面窗口。返回进程退出码。"""
    base_dir = base_dir or BASE_DIR
    try:
        root = tk.Tk()
    except tk.TclError as exc:          # 无图形环境（如计划任务里误开）
        try:
            print(f"[GUI] 当前环境没有可用的显示设备：{exc}")
            print("请改用 cn-stock-screener.exe --cli")
        except Exception:  # noqa: BLE001 - 窗口模式下 stdout 可能为 None
            pass
        return 4
    ScreenerApp(root, base_dir, offline=offline)
    root.mainloop()
    return 0
