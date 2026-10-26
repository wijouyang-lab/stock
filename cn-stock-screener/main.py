#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
cn-stock-screener — A股技术指标筛选器（入口）

用法
----
    python main.py --cli                 # 拉数据 + 写缓存 + 指标 + 筛选（推荐先跑这个）
    python main.py --cli --mode full     # 含三大报表（22:30 主触发用的模式）
    python main.py --cli --mode quote    # 纯行情（17:30 辅助触发用的模式）
    python main.py --cli --date 20250102 # 指定交易日
    python main.py --cli --force         # 忽略缓存强制重拉
    python main.py --cli --backfill      # 回补最近 30 个交易日的日线
    python main.py --scheduled --mode full   # 计划任务入口（严格执行时间窗口判断）

    # ---- 纯本地：不联网、不拉数据，只读 data/cache/ ----
    python main.py --local-only                    # 用本地最新分片算指标 + 筛选
    python main.py --replay 20261015               # 回放某个交易日（零网络）
    python main.py --local-only --filters turnover # 只跑某几条规则
    python main.py --replay 20261015 --filters none --history-bars 30

    python main.py --self-test           # 基础自检（全绿说明环境 OK）
    python main.py --self-test --offline # 离线自检（不校验 Token / 不联网）
    python main.py --paths               # 打印所有解析后的绝对路径（验证自包含）
    python main.py --config              # 打印配置摘要与缓存概览
    python main.py --gui                 # 打开桌面窗口（tkinter，无额外依赖）

本地计算的硬保证
----------------
`--local-only` / `--replay` 期间，data.tushare_client 处于 local-only 状态，
任何 API 调用都会抛 NetworkBlockedError；同时全局调用计数器可用来断言
"指标与筛选计算期间 Tushare 调用次数 = 0"（--self-test 里就是这么断言的）。

约定
----
- 所有路径由 config.BASE_DIR（= 本文件所在目录）推导，**与当前工作目录无关**，
  从任何目录用完整路径调用本脚本，产物都落在项目文件夹内。
- Token 只从 .env / 环境变量读取，绝不硬编码。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# 让"从任意目录运行"都能 import 项目模块（必须先于其它项目内 import）
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import config  # noqa: E402

# 打包成 exe 后 __file__ 指向临时解包目录，以 config 解析出的目录为准
BASE_DIR = config.BASE_DIR

from config import (  # noqa: E402
    DATA_DIR,
    ENV_EXAMPLE_FILE,
    ENV_FILE,
    LOG_DIR,
    LOG_DATEFMT,
    LOG_FORMAT,
    MODE_FULL,
    MODE_QUOTE,
    OUTPUT_CSV,
    OUTPUT_DIR,
    OUTPUT_JSON,
    REPORT_DIR,
    SCRIPTS_DIR,
    VALID_MODES,
    config_summary,
    get_token,
    mask_token,
)

APP_NAME = "cn-stock-screener"
VERSION = "0.2.0"
SEP = "=" * 74
THIN = "-" * 74

logger = logging.getLogger("main")


# ==========================================================================
# 日志
# ==========================================================================
def setup_logging(verbose: bool = False, tag: str = "cli") -> Path:
    """同时输出到控制台和 output/logs/ 下的日志文件，返回日志文件路径。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"{tag}_{datetime.now():%Y%m%d}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else getattr(logging, config.LOG_LEVEL, logging.INFO))
    for h in list(root.handlers):     # 避免重复添加（GUI 里多次调用）
        root.removeHandler(h)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATEFMT))
    root.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else getattr(logging, config.LOG_LEVEL, logging.INFO))
    ch.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    root.addHandler(ch)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return log_file


def print_banner(mode: str = "") -> None:
    print(SEP)
    print(f"{APP_NAME} v{VERSION}  |  {datetime.now():%Y-%m-%d %H:%M:%S}"
          + (f"  |  mode={mode}" if mode else ""))
    print(f"项目根目录 : {BASE_DIR}")
    print(f"当前工作目录: {Path.cwd()}")
    print(f"Python     : {platform.python_version()} ({sys.executable})")
    print(SEP)


# ==========================================================================
# 子命令：--paths
# ==========================================================================
def _disp_width(s: str) -> int:
    """终端显示宽度（中文算 2 列），用于对齐表格。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _disp_width(s))


def cmd_paths(args: argparse.Namespace) -> int:
    print_banner()
    rows = [
        ("BASE_DIR", config.BASE_DIR),
        ("DATA_DIR", DATA_DIR),
        ("CACHE_DIR", config.CACHE_DIR),
        ("INDICATORS_DIR", config.INDICATORS_DIR),
        ("SCREENER_DIR", config.SCREENER_DIR),
        ("TRIGGER_DIR", config.TRIGGER_DIR),
        ("OUTPUT_DIR", OUTPUT_DIR),
        ("LOG_DIR", LOG_DIR),
        ("REPORT_DIR", REPORT_DIR),
        ("SCRIPTS_DIR", SCRIPTS_DIR),
        ("ENV_FILE", ENV_FILE),
        ("ENV_EXAMPLE_FILE", ENV_EXAMPLE_FILE),
    ]
    name_w = max(_disp_width("名称"), max(_disp_width(n) for n, _ in rows)) + 2
    print(_pad("名称", name_w) + "在项目内  路径")
    print(THIN)
    all_inside = True
    for name, p in rows:
        try:
            inside = p.resolve().is_relative_to(BASE_DIR.resolve())
        except AttributeError:  # py<3.9
            inside = str(p.resolve()).startswith(str(BASE_DIR.resolve()))
        all_inside &= bool(inside)
        print(_pad(name, name_w) + f"{'是' if inside else '否':<10}{p}")
    print(THIN)
    print(f"全部路径均在项目文件夹内：{'是' if all_inside else '否'}")
    print(f"CWD 是否影响产物位置：否（所有路径由 __file__ 推导，与 CWD 无关）")
    print(f"当前 CWD：{Path.cwd()}")
    return 0 if all_inside else 1


# ==========================================================================
# 子命令：--config
# ==========================================================================
def cmd_config(args: argparse.Namespace) -> int:
    from data.cache import LocalCache

    print_banner()
    print(config_summary())
    print(THIN)
    print("【缓存概览】")
    summary = LocalCache().summary()
    for ds, info in summary["datasets"].items():
        print(
            f"  {ds:<14} 分片={info['shards']:<4} 行数={info['rows']:<8} "
            f"最新={info.get('latest') or '-':<10} 更新={info.get('modified_at') or '-'}"
        )
    print(f"\n缓存根目录：{summary['root']}")
    print(THIN)
    print(f"Token：{mask_token()}  ({config.token_source()})")
    return 0


# ==========================================================================
# 子命令：--self-test
# ==========================================================================
class SelfTest:
    """极简自检框架：记录 PASS / WARN / FAIL，最后汇总。"""

    def __init__(self, offline: bool = False) -> None:
        self.offline = offline
        self.results: list[tuple[str, str, str]] = []   # (状态, 名称, 详情)

    def check(self, name: str, ok: bool, detail: str = "", warn_only: bool = False,
              skip: bool = False) -> bool:
        if skip:
            status = "SKIP"
        elif ok:
            status = "PASS"
        elif warn_only:
            status = "WARN"
        else:
            status = "FAIL"
        self.results.append((status, name, detail))
        icon = {"PASS": "[√]", "WARN": "[!]", "FAIL": "[×]", "SKIP": "[-]"}[status]
        print(f"  {icon} {name:<34} {detail}")
        return ok

    @property
    def failed(self) -> int:
        return sum(1 for s, _, _ in self.results if s == "FAIL")

    @property
    def warned(self) -> int:
        return sum(1 for s, _, _ in self.results if s == "WARN")

    def summary(self) -> str:
        n = len(self.results)
        p = sum(1 for s, _, _ in self.results if s == "PASS")
        sk = sum(1 for s, _, _ in self.results if s == "SKIP")
        head = "全部通过 ✔" if self.failed == 0 and self.warned == 0 else (
            f"{self.failed} 项失败，{self.warned} 项警告" if self.failed else f"{self.warned} 项警告"
        )
        return f"{head}  (PASS={p} WARN={self.warned} FAIL={self.failed} SKIP={sk} / 共 {n} 项)"


def cmd_self_test(args: argparse.Namespace) -> int:
    from data.cache import LocalCache
    from data.tushare_client import RateLimiter, TushareClient

    log_file = setup_logging(verbose=args.verbose, tag="selftest")
    print_banner()
    print(f"离线自检：{'是' if args.offline else '否'}\n")
    t = SelfTest(offline=args.offline)

    # ---- 1. 运行环境 ----
    print("【1/9 运行环境】")
    t.check("Python 版本 >= 3.9", sys.version_info >= (3, 9), platform.python_version())
    t.check("64 位解释器", sys.maxsize > 2**32, platform.machine())
    t.check("操作系统为 Windows", os.name == "nt",
            platform.system(), warn_only=True)

    # ---- 2. 依赖 ----
    print("\n【2/9 依赖包】")
    for mod, pkg in (("pandas", "pandas>=1.5"), ("tushare", "tushare>=1.2"), ("dotenv", "python-dotenv")):
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "?")
            t.check(f"依赖 {pkg}", True, f"v{ver}")
        except Exception as exc:  # noqa: BLE001
            t.check(f"依赖 {pkg}", False, f"未安装：{exc}；请执行 python -m pip install -r requirements.txt")

    # ---- 3. 路径自包含 ----
    print("\n【3/9 路径自包含（不依赖 CWD）】")
    t.check("BASE_DIR 由 __file__ 推导", config.BASE_DIR.resolve() == BASE_DIR.resolve(),
            str(BASE_DIR))
    for name in ("DATA_DIR", "OUTPUT_DIR", "LOG_DIR", "REPORT_DIR", "SCRIPTS_DIR", "INDICATORS_DIR",
                 "SCREENER_DIR", "TRIGGER_DIR"):
        p: Path = getattr(config, name)
        try:
            inside = p.resolve().is_relative_to(BASE_DIR.resolve())
        except AttributeError:
            inside = str(p.resolve()).startswith(str(BASE_DIR.resolve()))
        t.check(f"{name} 在项目内", inside, str(p))
    t.check("无 CWD 相对路径写法", all(
        not str(getattr(config, n)).startswith(("./", "../", "data/")) for n in ("DATA_DIR", "OUTPUT_DIR")
    ), "所有路径均为绝对路径")

    # ---- 4. 目录可写 ----
    print("\n【4/9 目录读写】")
    for name, d in (("data/", DATA_DIR), ("data/cache/", config.CACHE_DIR),
                    ("output/", OUTPUT_DIR), ("output/logs/", LOG_DIR), ("output/reports/", REPORT_DIR)):
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / f".selftest_{os.getpid()}.tmp"
            probe.write_text("ok", encoding="utf-8")
            ok = probe.read_text(encoding="utf-8") == "ok"
            probe.unlink(missing_ok=True)
            t.check(f"{name} 可读写", ok, str(d))
        except Exception as exc:  # noqa: BLE001
            t.check(f"{name} 可读写", False, f"{exc}")

    # ---- 5. 缓存往返（在独立临时目录里做，绝不写生产缓存）----
    print("\n【5/9 本地缓存（按 trade_date 分片）】")
    cache = LocalCache()                                   # 生产缓存：只读校验用
    tmp_root = OUTPUT_DIR / "_selftest_cache"
    try:
        import shutil

        import pandas as pd

        shutil.rmtree(tmp_root, ignore_errors=True)         # 上一轮残留先清掉
        tmp_cache = LocalCache(root=tmp_root)              # 自检专用缓存
        key = "19990101"
        dummy = pd.DataFrame({"ts_code": ["000001.SZ", "600000.SH"], "trade_date": [key, key],
                              "close": [10.0, 20.0], "vol": [1000.0, 2000.0]})
        path = tmp_cache.write("daily", key, dummy, source="selftest")
        back = tmp_cache.read("daily", key)
        t.check("缓存写入/读取往返", back is not None and len(back) == 2,
                str(path.relative_to(BASE_DIR)))
        t.check("分片按 trade_date 命名", path.name == f"{key}.csv", path.name)
        t.check("分片出现在分片列表", key in tmp_cache.list_shards("daily"),
                f"临时缓存 {len(tmp_cache.list_shards('daily'))} 个分片")
        t.check("缓存元数据可解析", isinstance(tmp_cache.summary(), dict),
                json.dumps({k: v["shards"] for k, v in tmp_cache.summary()["datasets"].items()},
                           ensure_ascii=False))
        t.check("自检不污染生产缓存",
                not (config.CACHE_DIR / "daily" / f"{key}.csv").exists()
                and key not in cache.meta.get("daily", {}),
                "临时目录 output/_selftest_cache，跑完即删")
    except Exception as exc:  # noqa: BLE001
        t.check("缓存读写", False, str(exc))
    finally:
        import shutil

        shutil.rmtree(tmp_root, ignore_errors=True)

    # ---- 5b. 缓存完整性（只读校验，发现问题只报告、不删除）----
    try:
        from data.integrity import DAILY_DATASETS, check_cache, inventory_table

        report = check_cache()
        for dataset in DAILY_DATASETS:
            rows = [d for d in report.inventory if d["dataset"] == dataset]
            issues = [i for i in report.issues if i.dataset == dataset]
            days = len(rows)
            # adj_factor 是可选数据集（首次回补前没有分片），空目录算 SKIP 不算失败
            skip_it = days == 0 and dataset == "adj_factor"
            total = sum(r["rows"] for r in rows)
            min_rows = min((r["rows"] for r in rows), default=0)
            dup = sum(1 for r in rows if r["ts_codes"] != r["rows"])
            multi_date = sum(1 for r in rows if r["trade_dates"] != 1)
            t.check(
                f"{dataset} 分片完整性",
                not issues and days > 0,
                (f"{days} 个分片/共 {total} 行，最小分片 {min_rows} 行（下限 5000），"
                 f"ts_code 重复分片 {dup} 个，多日期分片 {multi_date} 个")
                if days else "尚未回补该数据集（--cli --backfill 后自动校验）",
                skip=skip_it,
            )

        meta_issues = [i for i in report.issues if i.kind in
                       ("orphan", "meta_missing", "rows_mismatch", "size_mismatch")]
        t.check(
            "_meta.json 与文件一致",
            not meta_issues,
            f"{len(meta_issues)} 条不一致" if meta_issues else "行数/字节数/存在性全部一致",
        )

        # 分片清单（日期 + 行数），用于人工核对补齐的结果
        table = inventory_table(report)
        if not table.empty:
            print(f"\n  本地分片清单（{len(table)} 个）：")
            for dataset, grp in table.groupby("dataset", sort=False):
                cells = [f"{r.shard}:{r.rows}" for r in grp.itertuples()]
                print(f"    {dataset:<12}{'  '.join(cells)}")

        print(f"\n  issues 列表（{len(report.issues)} 条，未做任何删除）：")
        if report.issues:
            for issue in report.issues:
                print(f"    - {issue}")
        else:
            print("    [] （空）")
    except Exception as exc:  # noqa: BLE001
        t.check("缓存完整性校验", False, f"{type(exc).__name__}: {exc}")

    # ---- 6. 模块与接口 ----
    print("\n【6/9 模块与接口】")
    for mod in ("data", "data.tushare_client", "data.cache", "data.loader",
                "indicators", "screener", "trigger", "trigger.calendar", "trigger.scheduler"):
        try:
            __import__(mod)
            t.check(f"导入 {mod}", True)
        except Exception as exc:  # noqa: BLE001
            t.check(f"导入 {mod}", False, str(exc))
    try:
        from indicators import compute_all, compute_panel, list_indicators
        from screener import ScreenerEngine, screen
        import pandas as pd

        sample = pd.DataFrame({"ts_code": ["000001.SZ"] * 3, "trade_date": ["19990101"] * 3,
                               "open": [1, 2, 3], "high": [2, 3, 4], "low": [0.5, 1, 2],
                               "close": [1.5, 2.5, 3.5], "vol": [10, 20, 30]})
        t.check("indicators.compute_all 可调用", len(compute_all(sample)) == 3,
                f"已注册指标 {len(list_indicators())} 个")
        t.check("indicators.compute_panel 可调用", len(compute_panel(sample)) == 3)
        res = ScreenerEngine().run(sample, trade_date="19990101")
        t.check("screener 引擎可调用", res.universe_size == 3, "已注册规则 0 条（占位）")
    except Exception as exc:  # noqa: BLE001
        t.check("指标/筛选接口", False, str(exc))

    # ---- 7. 触发与调度 ----
    print("\n【7/9 触发与调度】")
    try:
        from trigger.scheduler import Scheduler
        from trigger.calendar import TradeCalendar, is_weekend

        cal = TradeCalendar(client=None)          # 自检不联网探测
        sched = Scheduler(calendar=cal)
        t.check("周末判定正确", is_weekend("20250104") and not is_weekend("20250103"),
                "20250104(周六)=True")
        d = sched.decide(now=datetime(2025, 1, 3, 10, 0), enforce_window=True, probe_calendar=False)
        t.check("盘中触发被拦截", not d.should_run, d.reason)
        d2 = sched.decide(mode=MODE_FULL, now=datetime(2025, 1, 3, 22, 30),
                          enforce_window=True, probe_calendar=False)
        t.check("22:30 full 触发放行", d2.should_run, d2.reason)
        d3 = sched.decide(mode=MODE_QUOTE, now=datetime(2025, 1, 3, 17, 30),
                          enforce_window=True, probe_calendar=False)
        t.check("17:30 quote 触发放行", d3.should_run, d3.reason)
        d4 = sched.decide(mode=MODE_FULL, now=datetime(2025, 1, 4, 22, 30),
                          enforce_window=True, probe_calendar=False)
        t.check("非交易日被跳过", not d4.should_run, "周六")
    except Exception as exc:  # noqa: BLE001
        t.check("调度决策", False, str(exc))

    # ---- 8. Token / 限流 / 计划任务脚本 ----
    print("\n【8/9 Token 与限流与脚本】")
    token = get_token()
    if args.offline:
        t.check("Tushare Token 配置", True, "离线自检，已跳过", skip=True)
    else:
        t.check("Tushare Token 已配置", bool(token),
                mask_token() if token else
                f"未配置：请复制 {ENV_EXAMPLE_FILE.name} 为 .env 并填入 TUSHARE_TOKEN")
    # exe（PyInstaller）部署时没有源码树：.gitignore 与 scripts\*.bat 都不存在，
    # 这些属于"源码仓库卫生"检查，冻结运行时按 SKIP 处理（不是故障）。
    frozen = bool(getattr(sys, "frozen", False))
    t.check(".env 已被 .gitignore 忽略", frozen or _gitignore_has_env(),
            "exe 部署无源码目录，跳过" if frozen else ".gitignore", skip=frozen)
    t.check(".env.example 存在", frozen or ENV_EXAMPLE_FILE.is_file(),
            "exe 内置 Token，无需模板，跳过" if frozen else ENV_EXAMPLE_FILE.name, skip=frozen)

    limiter = RateLimiter(per_minute=3, window_seconds=60, min_interval=0.3)
    t0 = time.monotonic()
    limiter.acquire()
    limiter.acquire()
    gap = time.monotonic() - t0
    t.check("限流最小间隔 >= 0.3s", gap >= 0.29, f"两次调用间隔 {gap:.3f}s")

    for bat, desc in (("run_screener.bat", "执行入口"),
                      ("register_task.bat", "注册计划任务"),
                      ("unregister_task.bat", "卸载计划任务")):
        p = SCRIPTS_DIR / bat
        ok = p.is_file() and "%~dp0" in p.read_text(encoding="utf-8", errors="ignore")
        t.check(f"scripts/{bat}（{desc}）", ok,
                "exe 部署无源码脚本，跳过" if frozen else "存在且使用 %~dp0 定位项目根目录",
                skip=frozen)

    # ---- 9: 技术指标与筛选（纯本地计算）----
    print("\n【9/9 技术指标与筛选（纯本地计算）】")
    try:
        from indicators import describe as describe_indicators
        from indicators.selftest import run_checks as run_indicator_checks

        t.check(
            "指标注册表",
            len(describe_indicators()) >= 9,
            ", ".join(describe_indicators()["name"].tolist()),
        )
        for c in run_indicator_checks():
            t.check(c.name, c.ok, c.detail, warn_only=c.warn_only, skip=c.skip)

        from data.tushare_client import call_stats, is_local_only

        stats = call_stats()
        t.check(
            "本地模式已复位（不残留 local-only）",
            not is_local_only(),
            f"attempts={stats['attempts']} blocked={stats['blocked']} "
            f"success={stats['success']} local_only={stats['local_only']}",
        )
    except Exception as exc:  # noqa: BLE001
        t.check("技术指标自检", False, f"{type(exc).__name__}: {exc}")

    # ---- 联网抽检 ----
    if not args.offline and token:
        print("\n【附加】Tushare 连通性抽检")
        try:
            client = TushareClient(token=token)
            basic = client.stock_basic()
            t.check("stock_basic 调用成功", not basic.empty, f"{len(basic)} 只股票（含行业分类）")
            cal2 = TradeCalendar(client=client)
            latest = cal2.latest_trade_date(probe=True)
            df = client.daily(trade_date=latest)
            t.check("daily 调用成功", not df.empty, f"{latest}: {len(df)} 行")
            db = client.daily_basic(trade_date=latest)
            t.check("daily_basic 调用成功", not db.empty, f"{latest}: {len(db)} 行")
            t.check("API 权限白名单生效", _whitelist_ok(), "禁用接口会被拒绝")
            print(f"  API 调用次数：{len(client.call_log)}，限流统计：{client.stats()['limiter']}")
        except Exception as exc:  # noqa: BLE001
            t.check("Tushare 连通性", False, f"{type(exc).__name__}: {exc}")

    # ---- 汇总 ----
    print("\n" + SEP)
    print(f"自检结果：{t.summary()}")
    print(f"日志文件：{log_file}")
    print(f"产物目录：{OUTPUT_DIR}")
    print(SEP)
    if t.failed:
        print("自检未通过，请按上面的 [×] 项修复后重试。")
    return 0 if t.failed == 0 else 1


def _gitignore_has_env() -> bool:
    p = BASE_DIR / ".gitignore"
    if not p.is_file():
        return False
    lines = [ln.strip() for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines()]
    return any(ln in (".env", "/.env", "*.env") for ln in lines)


def _whitelist_ok() -> bool:
    from data.tushare_client import ForbiddenApiError, TushareClient

    try:
        TushareClient.check_api_allowed("stk_mins")
    except ForbiddenApiError:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


# ==========================================================================
# 子命令：--cli / --scheduled
# ==========================================================================
def run_pipeline(args: argparse.Namespace) -> int:
    """核心流程：同步数据 -> 计算指标 -> 筛选 -> 落盘结果。"""
    from data import DataLoader
    from data.tushare_client import TokenMissingError, TushareError
    from indicators import compute_panel
    from screener import ScreenerEngine
    from trigger import Scheduler

    log_file = setup_logging(verbose=args.verbose, tag="screener")
    # 手工运行默认 quote；计划任务不指定则由 Scheduler 按当前时间决定
    requested_mode = args.mode
    args.mode = args.mode or MODE_QUOTE
    print_banner(mode=args.mode)
    logger.info("项目根目录=%s | CWD=%s | token=%s", BASE_DIR, Path.cwd(), mask_token())

    # 1) 触发决策
    scheduler = Scheduler()
    if args.scheduled:
        decision = scheduler.decide(mode=requested_mode, enforce_window=True,
                                    probe_calendar=not args.offline)
        print(THIN)
        print(f"计划任务触发判断：{decision.describe()}")
        for w in decision.warnings:
            print(f"  ! {w}")
        print(THIN)
        if not decision.should_run:
            logger.info("本次触发被跳过：%s", decision.reason)
            print(f"\n[跳过] {decision.reason}")
            return 0
        args.mode = decision.mode

    # 2) 数据同步（本地模式跳过：只读缓存）
    from data.tushare_client import TushareClient

    loader = DataLoader(client=TushareClient(offline=args.offline))
    if args.local_only:
        print(THIN)
        print("本地模式（--local-only）：跳过数据同步，只读 data/cache/，任何联网调用都会抛异常")
        print(THIN)
        return screen_local(args, args.date, loader, log_file, source="local")

    try:
        result = loader.sync(
            mode=args.mode,
            trade_date=args.date,
            force=args.force,
            include_financials=(args.mode == MODE_FULL) and not args.no_financials,
            init_days=args.days,
            backfill=args.backfill,
            ignore_calendar=args.ignore_calendar,
            financial_limit=args.financial_limit,
        )
    except TokenMissingError as exc:
        logger.error("%s", exc)
        print(f"\n[配置错误] {exc}")
        print(f"提示：把 {ENV_EXAMPLE_FILE.name} 复制为 .env，并填入你自己的 Tushare Token。")
        return 2
    except TushareError as exc:
        logger.error("数据拉取失败：%s", exc)
        print(f"\n[数据错误] {exc}")
        return 3

    print(THIN)
    print(f"数据同步：{result.describe()}")
    for w in result.warnings:
        print(f"  ! {w}")
    print(THIN)

    if result.skipped:
        print(f"\n[跳过] {result.skip_reason}")
        return 0

    # 3) 本地筛选（指标 + 规则）—— 这一阶段全程 local_only，禁止任何网络调用
    return screen_local(args, result.trade_date, loader, log_file,
                        source="cli", sync_result=result)


# ==========================================================================
# 本地流水线：缓存 → 指标 → 截面 → 筛选 → 落盘（全程零网络调用）
# ==========================================================================
#: 历史面板里的原始 OHLCV 列，不属于"指标列"，合并截面时要排除
_BASE_PANEL_COLS = frozenset({
    "ts_code", "trade_date", "open", "high", "low", "close", "pre_close",
    "change", "pct_chg", "vol", "amount",
})

#: CSV 里优先展示的列（存在则排前面，其余列自动跟在后面）
_CSV_FIRST_COLS = (
    "score", "tier", "hit_count",
    "ts_code", "name", "industry", "trade_date", "close", "pct_chg", "turnover",
    "turnover_rate", "volume_ratio_5", "ma20", "dif", "dea", "macd", "k", "d", "j",
    "resonance_daily", "resonance_weekly", "resonance_monthly", "resonance_dwm",
    "double_bottom", "double_top", "db_neckline", "db_breakout_date", "db_gap",
    "dt_neckline", "dt_breakout_date", "dt_gap", "hammer", "shooting_star",
)


def _coerce_param(value: str):
    """把命令行里的参数值转成 bool / int / float / str。"""
    text = str(value).strip()
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("none", "null"):
        return None
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            continue
    return text


def _parse_filter_names(args: argparse.Namespace) -> list[dict]:
    """解析 ``--filters``，返回规则规格列表（可带参数覆盖）。

    支持三种写法，可混用：

        turnover                                  # 默认参数
        volume_surge:mode=percentile              # 冒号 + key=value（逗号分隔多条）
        volume_shrink(mode=percentile, max_ratio=0.6)   # 括号写法

    ``None`` = 用配置默认链；``none``/``off``/``0`` = 不筛选。
    """
    raw = getattr(args, "filters", None)
    if raw is None:
        return [{"name": n} for n in config.DEFAULT_FILTER_CHAIN]
    text = str(raw).strip()
    if text.lower() in ("", "none", "off", "0"):
        return []

    specs: list[dict] = []
    for chunk in text.split(","):
        part = chunk.strip()
        if not part:
            continue
        # 括号写法：name(k1=v1, k2=v2)
        m = re.match(r"^([A-Za-z_][\w]*)\s*\((.*)\)$", part)
        params: dict = {}
        if m:
            name, inner = m.group(1), m.group(2)
            for kv in inner.split(","):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    params[k.strip()] = _coerce_param(v)
        else:
            # 冒号写法：name:k1=v1
            name, _, inner = part.partition(":")
            name = name.strip()
            for kv in inner.split(":") if inner else []:
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    params[k.strip()] = _coerce_param(v)
        if name:
            specs.append({"name": name, **params})
    return specs


def _filter_labels(specs: list[dict]) -> list[str]:
    """把规则规格格式化成展示用标签（含非默认参数）。"""
    return [
        s["name"] + (f"({', '.join(f'{k}={v}' for k, v in s.items() if k != 'name')})"
                     if len(s) > 1 else "")
        for s in specs
    ]


def _history_diagnosis(n_dates: int) -> list[str]:
    """历史长度不足时给出可执行的诊断，避免"5002 -> 0"让人误以为筛选坏了。"""
    req = config.HISTORY_REQUIREMENTS
    checks = [
        ("日线 MA20 / 日线共振", req["ma20"]),
        ("日线 MACD 预热", req["macd"]),
        ("周线 MA20", req["weekly_ma20"]),
        ("月线 MA20（日周月共振前提）", req["monthly_ma20"]),
    ]
    return [
        f"历史不足：{label} 需要约 {need} 根日线，本地仅 {n_dates} 根"
        for label, need in checks
        if n_dates < need
    ]


def _order_columns(df: pd.DataFrame) -> pd.DataFrame:
    """把关键列排到前面，便于直接看 CSV。"""
    head = [c for c in _CSV_FIRST_COLS if c in df.columns]
    rest = [c for c in df.columns if c not in head]
    return df[head + rest]


def screen_local(
    args: argparse.Namespace,
    trade_date: str | None,
    loader,
    log_file: Path,
    *,
    source: str = "local",
    sync_result=None,
) -> int:
    """本地筛选主流程：只读缓存 + 纯本地计算，全程禁止网络调用。

    source: "cli"（先联网同步过）/ "local"（--local-only）/ "replay"（--replay）
    """
    from data.tushare_client import local_only
    from indicators import compute_panel
    from indicators.adjust import apply_qfq, write_adjust_events
    from indicators.volume import ensure_turnover
    from screener import ScreenerEngine
    from screener.filters import default_chain
    from screener.scoring import Scorer, apply_tier_caps

    with local_only():
        # ---- 1) 确定交易日（只能用本地分片）----
        dates = loader.local_trade_dates(trade_date)
        if not dates:
            print(
                f"\n[错误] 本地没有任何日线分片（<= {trade_date or '今天'}）。"
                "\n       请先联网拉一次数据：python main.py --cli"
            )
            return 6
        if trade_date and trade_date not in dates:
            print(f"[提示] 本地没有 {trade_date} 的分片，回退到最近可用交易日 {dates[-1]}")
            trade_date = dates[-1]
        trade_date = trade_date or dates[-1]

        # ---- 2) 目标日截面（股票池 + daily_basic 字段）----
        universe = loader.load_universe(trade_date)
        if universe.empty:
            print(f"\n[警告] {trade_date} 股票池为空：当日行情可能尚未入库（15:00~17:00 之间）。")
            return 0
        logger.info("股票池：%d 只（trade_date=%s）", len(universe), trade_date)

        # ---- 3) 多日历史面板 → 指标 ----
        panel = loader.load_history_panel(
            trade_date,
            codes=universe["ts_code"].astype(str).tolist(),
            max_bars=getattr(args, "history_bars", None),
        )
        if panel.empty:
            print("\n[错误] 本地没有可用的日线历史，无法计算指标。")
            return 6

        # ---- 3b) 前复权（基准日 = 目标交易日；--replay D 时自然就是 D）----
        adjust_info: dict[str, object] = {"basis_date": trade_date}
        adj_long = loader.load_adj_long(
            basis_date=trade_date,
            codes=universe["ts_code"].astype(str).tolist(),
        )
        adjust_info["adj_rows"] = int(len(adj_long))
        if adj_long.empty:
            adjust_info["applied"] = False
            print("[警告] 本地没有复权因子分片：跳过前复权（ratio=1），"
                  "指标将基于原始价计算。补数据请跑 --cli --backfill")
        else:
            panel = apply_qfq(panel, adj_long, basis_date=trade_date)
            adjust_info["applied"] = True
            adjust_info["adj_missing_rows"] = int(panel["adj_missing"].sum())
            adjust_info["stocks"] = int(panel["ts_code"].nunique())
            events_path, events_rows = write_adjust_events(
                adj_long, trade_date, LOG_DIR,
                codes=universe["ts_code"].astype(str).tolist(),
            )
            adjust_info["events_rows"] = events_rows
            adjust_info["events_file"] = events_path.name if events_path else None
            logger.info(
                "前复权：basis=%s，因子 %d 行，缺失 %d 行，除权事件 %d 条",
                trade_date, len(adj_long), adjust_info["adj_missing_rows"], events_rows,
            )

        computed = compute_panel(panel, last_trade_date=trade_date)
        feature_cols = [c for c in computed.columns if c not in _BASE_PANEL_COLS]
        snapshot = computed.loc[computed["trade_date"] == trade_date, ["ts_code"] + feature_cols]

        merged = universe.merge(snapshot, on="ts_code", how="left", suffixes=("", "_ind"))
        merged = ensure_turnover(merged)

        # ---- 3c) 评分（可选：--score；不加时行为与以前完全一致）----
        scorer = None
        score_info: dict | None = None
        if getattr(args, "score", False):
            tiers = dict(config.SCORE_TIERS)
            if getattr(args, "min_score", None) is not None:
                tiers["candidate"] = float(args.min_score)
            scorer = Scorer(tiers=tiers)
            merged = scorer.score(merged)
            logger.info("评分：满分 %.0f，分档门槛 %s", scorer.max_total, tiers)

        # ---- 3d) 日线买卖信号（T+1：每个交易日只评估一次，买入当日不许卖）----
        # 只给"日线榜里的股票"发信号（+ 已持仓的，保证能平仓），不对全市场发。
        signal_events: list[dict] = []
        if getattr(config, "SIGNALS_ENABLED", True):
            try:
                from screener.signals import open_positions
                from screener.signals import update as update_signals

                holdings = open_positions()
                if "tier" in merged.columns:
                    mask = (merged["tier"].astype(str) != "") | merged["ts_code"].astype(
                        str).isin(list(holdings))
                    sig_universe = merged[mask]
                else:
                    sig_universe = merged
                sig_out, signal_events = update_signals(sig_universe, trade_date)
                cols = [c for c in ("ts_code", "signal", "signal_note", "hold_days",
                                    "entry_date", "t1_blocked") if c in sig_out.columns]
                merged = merged.merge(sig_out[cols], on="ts_code", how="left")
                if "signal" in merged.columns:
                    merged["signal"] = merged["signal"].fillna("")
                for ev in signal_events:
                    print(f"  [信号] {ev['date']} {ev['ts_code']} {ev.get('name', '')} "
                          f"{ev['signal']} @ {ev['price']:.2f}  （{ev['note']}）")
                logger.info("信号：%d 只入选股参与评估，新增 %d 笔（当前持仓 %d 只）",
                            len(sig_universe), len(signal_events), len(holdings))
            except Exception as exc:  # noqa: BLE001
                logger.warning("信号更新失败（不影响筛选）：%s", exc)

        # ---- 4) 筛选 ----
        # 评分模式且未显式给 --filters 时，默认不做漏斗（输出全市场评分排行）
        if scorer is not None and getattr(args, "filters", None) is None:
            specs: list[dict] = []
        else:
            specs = _parse_filter_names(args)
        filters = default_chain(specs) if specs else []
        if specs and len(filters) < len(specs):
            from screener import FILTER_REGISTRY

            print(f"[提示] 部分规则未注册，已忽略；可用规则：{', '.join(sorted(FILTER_REGISTRY))}")
        labels = _filter_labels(specs)
        screened = ScreenerEngine(filters=filters).run(
            merged, trade_date=trade_date, mode=getattr(args, "mode", None) or "local",
            sort_by=("score",) if scorer else None,
        )

        # ---- 4b) 档位裁剪（仅 --score）----
        if scorer is not None:
            caps = {} if getattr(args, "no_caps", False) else dict(config.TIER_CAPS)
            capped, cap_stats = apply_tier_caps(screened.matched, caps=caps)
            if getattr(args, "top", 0):
                capped = capped.head(int(args.top))
            screened.matched = capped
            score_info = {
                "enabled": True,
                "max_total": scorer.max_total,
                "weights": scorer.describe(),
                "tiers": tiers,
                "tier_labels": dict(config.SCORE_TIER_LABELS),
                "min_hit": scorer.min_hit,
                "caps": caps,
                "cap_stats": cap_stats,
                "result": scorer.last.as_dict(),
            }
            logger.info(
                "档位裁剪：入榜 %d 只（截断 %d，低于门槛剔除 %d）",
                len(capped), cap_stats.get("capped", 0), cap_stats.get("dropped", 0),
            )

        # ---- 5) 落盘 ----
        out_files = _write_screen_outputs(
            args, screened, trade_date, dates, feature_cols,
            source=source, sync_result=sync_result, log_file=log_file,
            adjust_info=adjust_info, score_info=score_info,
        )

        # ---- 6) 报告 ----
        n_dates = len(dates)
        print(THIN)
        print(
            f"本地数据：{n_dates} 个交易日（{dates[0]}~{dates[-1]}）"
            f" | 股票池 {len(universe)} 只 | 指标列 {len(feature_cols)} 个 | 规则 {len(filters)} 条"
        )
        if adjust_info.get("applied"):
            print(
                f"前复权：basis={adjust_info['basis_date']}"
                f" | 因子 {adjust_info['adj_rows']} 行"
                f" | 缺失 {adjust_info.get('adj_missing_rows', 0)} 行"
                f" | 除权事件 {adjust_info.get('events_rows', 0)} 条"
            )
        else:
            print(f"前复权：未应用（basis={adjust_info['basis_date']}，本地无因子分片）")
        for w in _history_diagnosis(n_dates):
            print(f"  ! {w}")
        for w in screened.warnings:
            print(f"  ! {w}")
        print(THIN)
        if screened.funnel:
            print(f"{'#':<3}{'规则':<20}{'输入':>7}{'输出':>7}{'剔除':>7}  审计明细")
            for i, stat in enumerate(screened.funnel, 1):
                brief = ", ".join(
                    f"{k}={v}" for k, v in (stat.get("detail") or {}).items()
                    if not isinstance(v, (list, dict, tuple))
                )
                print(f"{i:<3}{stat['rule']:<20}{stat['in']:>7}{stat['out']:>7}"
                      f"{stat['dropped']:>7}  {brief}")
        else:
            print("（未启用筛选规则：--filters none）")

        # ---- 评分排行（--score）----
        if score_info:
            res_stats = score_info["result"]
            tiers_count = res_stats.get("tiers", {})
            print(THIN)
            print(
                f"评分排行：满分 {score_info['max_total']:.0f} | "
                + " / ".join(f"{k} {v} 只" for k, v in tiers_count.items())
                + f" | 入榜 {len(screened.matched)} 只"
                + (f"（档位上限截断 {score_info['cap_stats'].get('capped', 0)} 只）"
                   if score_info["caps"] else "（未设档位上限）")
                + f" | 低于门槛剔除 {score_info['cap_stats'].get('dropped', 0)} 只"
            )
            top = screened.matched.head(10)
            if not top.empty:
                def _fmt(value, spec: str) -> str:
                    """缓存读进来的数值列是 str，打印前统一转 float。"""
                    try:
                        return format(float(value), spec)
                    except (TypeError, ValueError):
                        return format(float("nan"), spec)

                print(f"{'#':<3}{'score':>6}  {'档位':<6}{'代码':<11}{'名称':<9}"
                      f"{'收盘':>8}{'换手%':>7}{'命中':>4}")
                for i, row in enumerate(top.itertuples(), 1):
                    name = str(getattr(row, "name", ""))[:8]
                    print(f"{i:<3}{_fmt(getattr(row, 'score', 0), '>6.1f')}  "
                          f"{str(getattr(row, 'tier', '')):<6}{row.ts_code:<11}{name:<9}"
                          f"{_fmt(getattr(row, 'close', None), '>8.2f')}"
                          f"{_fmt(getattr(row, 'turnover', None), '>7.2f')}"
                          f"{int(getattr(row, 'hit_count', 0)):>4}")
        print(THIN)
        for f in out_files:
            print(f"  -> {f}")
        print(THIN)

    # 需求约定的固定输出行
    print(f"\n数据就绪，{len(universe)}只股票")
    print(f"筛选命中 {screened.matched_count} 只"
          f"（{' → '.join(labels) if labels else '未筛选'}）")
    return 0


def _write_screen_outputs(
    args: argparse.Namespace,
    screened,
    trade_date: str,
    dates: list[str],
    feature_cols: list[str],
    *,
    source: str,
    sync_result,
    log_file: Path,
    adjust_info: dict | None = None,
    score_info: dict | None = None,
) -> list[Path]:
    """写出三份产物：明细 CSV、运行摘要 JSON、funnel（逐规则漏斗）JSON。"""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    # 带微秒：并行跑多条规则时不会互相覆盖（秒级时间戳在多进程下会撞名）
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    prefix = "replay" if source == "replay" else "screen"
    files: list[Path] = []
    adjust_info = adjust_info or {}

    matched = _order_columns(screened.matched) if not screened.matched.empty else screened.matched

    funnel_payload = {
        "app": APP_NAME,
        "version": VERSION,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "trade_date": trade_date,
        "adjust": dict(adjust_info),
        "score": score_info,
        "history": {
            "bars": len(dates),
            "first": dates[0] if dates else None,
            "last": dates[-1] if dates else None,
        },
        "universe_size": screened.universe_size,
        "indicator_columns": len(feature_cols),
        "filters": [
            {"order": i, **stat} for i, stat in enumerate(screened.funnel, 1)
        ],
        "final_count": screened.matched_count,
        "warnings": screened.warnings,
        "requirements": dict(config.HISTORY_REQUIREMENTS),
    }

    summary = {
        "app": APP_NAME,
        "version": VERSION,
        "generated_at": funnel_payload["generated_at"],
        "source": source,
        "trade_date": trade_date,
        "adjust": dict(adjust_info),
        "score": score_info,
        "screen": {
            "universe_size": screened.universe_size,
            "matched_count": screened.matched_count,
            "rules": [s["rule"] for s in screened.funnel],
            "warnings": screened.warnings,
        },
        "sync": sync_result.to_dict() if sync_result is not None else None,
        "log_file": str(log_file),
    }

    if OUTPUT_CSV:
        p = REPORT_DIR / f"{prefix}_{trade_date}_{stamp}.csv"
        matched.to_csv(p, index=False, encoding="utf-8-sig")
        files.append(p)

    if OUTPUT_JSON:
        p = REPORT_DIR / f"{prefix}_{trade_date}_{stamp}.json"
        p.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        files.append(p)

        p = REPORT_DIR / f"funnel_{prefix}_{trade_date}_{stamp}.json"
        p.write_text(json.dumps(funnel_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        files.append(p)

    logger.info("产物：%s", ", ".join(f.name for f in files))
    return files


# ==========================================================================
# 子命令：--gui
# ==========================================================================
def cmd_replay(args: argparse.Namespace) -> int:
    """--replay YYYYMMDD：只用本地缓存回放某一天的筛选（零网络）。"""
    from data import DataLoader
    from data.tushare_client import local_only

    log_file = setup_logging(verbose=args.verbose, tag="replay")
    print_banner(mode=f"replay {args.replay}")
    args.mode = args.mode or "local"

    loader = DataLoader()
    dates = loader.local_trade_dates()
    if not dates:
        print("\n[错误] 本地没有任何日线分片，无法回放。请先联网跑一次：python main.py --cli")
        return 6
    print(f"本地可用日线：{len(dates)} 个交易日（{dates[0]}~{dates[-1]}）")
    if args.replay not in dates:
        print(f"\n[错误] 本地没有 {args.replay} 的日线分片，无法回放。")
        print(f"       可选范围：{dates[0]} ~ {dates[-1]}（共 {len(dates)} 天）")
        print("       提示：--replay 只读缓存，不会联网补数据；要补请用 --cli --date YYYYMMDD")
        return 7

    with local_only():
        return screen_local(args, args.replay, loader, log_file, source="replay")


def cmd_watch(args: argparse.Namespace) -> int:
    """--watch / --loop：盘中盯梢（免费快照源，零大模型调用）。"""
    from intraday.monitor import cmd_watch as run_watch

    setup_logging(verbose=args.verbose, tag="watch")
    codes = [c.strip() for c in (args.watch_codes or "").split(",") if c.strip()] or None
    return run_watch(
        top_n=args.watch_top,
        codes=codes,
        source=args.quote_source,
        loop=bool(args.loop),
        interval=args.interval,
        allow_outside_session=bool(args.allow_outside_session),
    )


def cmd_serve(args: argparse.Namespace) -> int:
    """--serve：把本地结果渲染成网页看板。"""
    from web.server import serve as run_server

    setup_logging(verbose=args.verbose, tag="web")
    print(THIN)
    print(f"网页看板：http://{args.host}:{args.port}/")
    print(f"项目目录：{config.BASE_DIR}")
    print("按 Ctrl+C 停止")
    print(THIN)
    try:
        run_server(host=args.host, port=int(args.port))
    except KeyboardInterrupt:
        print("\n[停止] 网页服务已关闭")
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    """--publish：把当前结果渲染成静态站点（index.html + api/*.json）。"""
    import json as _json

    from web.server import collect, render_index

    setup_logging(verbose=args.verbose, tag="publish")
    out = Path(args.publish or "docs")
    if not out.is_absolute():
        out = config.BASE_DIR / out
    out.mkdir(parents=True, exist_ok=True)

    data = collect()
    (out / "index.html").write_text(render_index(), encoding="utf-8")

    api = out / "api"
    api.mkdir(exist_ok=True)
    for key in ("ranking", "intraday", "industry", "alerts"):
        (api / f"{key}.json").write_text(
            _json.dumps(data.get(key, {}), ensure_ascii=False, indent=1), encoding="utf-8")
    (api / "all.json").write_text(
        _json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    counts = {k: len((data.get(k) or {}).get("rows") or [])
              for k in ("ranking", "intraday", "industry", "alerts")}
    print(THIN)
    print(f"静态站点已生成：{out}")
    print(f"  行数：{counts}")
    print(f"  文件：index.html + api/*.json")
    print(THIN)
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """--scan：全市场盘中扫描 → 先排行业，再在强行业里给个股打分。"""
    from intraday.scan import (enrich_picks, format_table, industry_table,
                               pick_by_industry, save_scan, scan_market, score_market)

    setup_logging(verbose=args.verbose, tag="scan")
    print(THIN)
    print(f"全市场盘中扫描 | {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(THIN)
    t0 = time.time()
    try:
        raw = scan_market(source=args.quote_source)
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] 行情抓取失败：{type(exc).__name__}: {exc}")
        return 3
    if raw.empty:
        print("[错误] 没有拿到任何有效报价（可能非交易时段/网络异常）")
        return 3

    scored = score_market(raw)
    ind = industry_table(scored, top=10)
    picks = pick_by_industry(scored, top_industries=5, per_industry=5, max_total=20)
    picks = enrich_picks(picks)          # 15 分钟量比/MACD/KDJ（只抓这 20 只）

    # ---- 行业强度榜 ----
    print(f"\n【行业强度】按平均涨幅 + 上涨家数占比（扫描 {len(scored)} 只）")
    print(f"{'#':<4}{'行业':<14}{'家数':>5}{'平均涨幅':>9}{'上涨占比':>9}{'成交额(亿)':>11}  领涨")
    print("-" * 72)
    for r in ind.itertuples(index=False):
        print(f"{r.industry_rank:<4}{str(r.industry)[:12]:<14}{r.count:>5}"
              f"{r.avg_pct:>+9.2f}{r.up_ratio * 100:>8.0f}%{r.amount / 10000:>11.1f}  {str(r.best)[:8]}")

    # ---- 强行业内部选股 ----
    print(f"\n【强行业内选股】前 5 行业 × 每行业最多 5 只，按盘中分排序")
    print(f"{'#':<4}{'行业':<10}{'代码':<11}{'名称':<9}{'盘中分':>7}{'现价':>7}"
          f"{'K线收':>7}{'15分%':>8}{'今日%':>8}{'量比':>6}{'MACD':>5}{'KDJ':>5}")
    print("-" * 92)
    for r in picks.itertuples(index=False):
        dist = getattr(r, "dist_ma20_pct", None)
        dist_txt = f"{float(dist):>+8.2f}%" if dist is not None and dist == dist else "     -  "
        def _n(v, spec="{:>7.2f}"):
            try:
                fv = float(v)
            except (TypeError, ValueError):
                return "      -"
            return spec.format(fv) if fv == fv else "      -"
        vr15 = getattr(r, "vr_15m", None)
        bar_c = getattr(r, "bar_close", None)
        bar_p = getattr(r, "bar_pct", None)
        macd15 = getattr(r, "macd_15m", None) or "-"
        kdj15 = getattr(r, "kdj_15m", None) or "-"
        print(f"{r.rank:<4}{str(r.industry)[:8]:<10}{r.ts_code:<11}{str(r.name)[:8]:<9}"
              f"{r.score:>7.1f}{_n(r.price)}{_n(bar_c)}{_n(bar_p, '{:>+8.2f}')}"
              f"{r.pct_chg:>+8.2f}{_n(vr15, '{:>6.2f}')}{macd15:>5}{kdj15:>5}")

    pick_path = save_scan(picks, name="intraday_rank_latest.csv")
    save_scan(ind, name="intraday_industry_latest.csv")
    print(THIN)
    print(f"扫描 {len(scored)} 只 | 入选 {len(picks)} 只 | 耗时 {time.time() - t0:.0f}s")
    if pick_path:
        print(f"  -> {pick_path}")
    print(THIN)
    return 0


def _num_or_zero(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from gui.app import launch
    except Exception as exc:  # noqa: BLE001
        print(f"[GUI] 无法加载桌面界面：{exc}")
        print("可以改用命令行：python main.py --cli")
        return 4
    return launch(BASE_DIR, offline=args.offline)


# ==========================================================================
# 参数解析
# ==========================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py",
        description=f"{APP_NAME} v{VERSION} — A股技术指标筛选器（第一阶段：数据骨架）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--cli", action="store_true", help="命令行跑一次：拉数据 + 写缓存 + 跑筛选")
    g.add_argument("--self-test", action="store_true", help="基础自检")
    g.add_argument("--gui", action="store_true", help="打开桌面窗口")
    g.add_argument("--paths", action="store_true", help="打印解析后的绝对路径")
    g.add_argument("--config", action="store_true", help="打印配置摘要与缓存概览")
    g.add_argument("--watch", action="store_true",
                   help="盘中盯梢：拉免费实时快照，评估告警（默认单次；配合 --loop 常驻）")
    g.add_argument("--scan", action="store_true",
                   help="全市场盘中扫描：抓 ~5000 只快照，输出涨幅/跌幅榜（约 30 秒）")
    g.add_argument("--serve", action="store_true",
                   help="启动网页看板（浏览器打开 /，默认 127.0.0.1:8000）")
    g.add_argument("--publish", nargs="?", const="docs", default=None, metavar="DIR",
                   help="把看板渲染成静态文件到 DIR（默认 docs/），供 GitHub Pages 等托管")

    p.add_argument("--scheduled", action="store_true",
                   help="计划任务入口：严格执行交易日/非盘中/数据已入库判断")
    p.add_argument("--mode", choices=VALID_MODES, default=None,
                   help="full=含财务数据（22:30）；quote=纯行情（17:30）。"
                        "手工运行默认 quote；--scheduled 时不指定则按当前时间自动选择")
    p.add_argument("--date", default=None, help="指定交易日 YYYYMMDD，默认自动探测最近交易日")
    p.add_argument("--days", type=int, default=config.DEFAULT_INIT_DAYS,
                   help=f"补齐最近 N 个交易日的行情，默认 {config.DEFAULT_INIT_DAYS}")
    p.add_argument("--backfill", action="store_true", help="回补最近 30 个交易日的日线")
    p.add_argument("--force", action="store_true", help="忽略缓存，强制重新拉取")
    p.add_argument("--no-financials", action="store_true", help="即使 full 模式也跳过三大报表")
    p.add_argument("--financial-limit", type=int, default=0, metavar="N",
                   help="三大报表整表按报告期拉取不可用时，逐只拉取前 N 只股票的财务数据"
                        "（每只 3 次调用，N=300 约需 5 分钟；默认 0=不逐只拉取）")
    p.add_argument("--ignore-calendar", action="store_true", help="跳过交易日判断（调试用）")
    p.add_argument("--offline", action="store_true", help="离线模式（自检用，不联网）")

    # ---- 本地计算相关 ----
    p.add_argument("--local-only", action="store_true",
                   help="只读本地缓存：不拉数据，任何联网调用都抛 NetworkBlockedError")
    p.add_argument("--replay", metavar="YYYYMMDD", default=None,
                   help="回放某个交易日：只用本地缓存（<= 该日）重算指标与筛选，隐含 --local-only")
    p.add_argument("--filters", metavar="NAMES", default=None,
                   help="筛选链，逗号分隔，按顺序执行（如 turnover,resonance_dwm,double_bottom）；"
                        "none = 不筛选（输出全股票池）。默认取 config.DEFAULT_FILTER_CHAIN")
    p.add_argument("--history-bars", type=int, default=None, metavar="N",
                   help="指标计算最多使用最近 N 个本地日线分片（默认用全部本地分片）")

    # ---- 评分排行 ----
    p.add_argument("--score", action="store_true",
                   help="启用评分排行与推荐分层（核心/观察/候选），按总分排序输出")
    p.add_argument("--top", type=int, default=0, metavar="N",
                   help="评分模式下最终最多输出 N 只（默认 0=按档位名额）")
    p.add_argument("--min-score", type=float, default=None, metavar="S",
                   help="入榜最低分（默认取 config.SCORE_TIERS['candidate']=65）")
    p.add_argument("--no-caps", action="store_true",
                   help="不按档位名额限制（默认核心5/观察10/候选15）")

    # ---- 盘中盯梢（免费快照源，不需要 Tushare Token）----
    p.add_argument("--loop", action="store_true",
                   help="盯梢常驻循环（配合 --watch），直到离开交易时段")
    p.add_argument("--interval", type=int, default=None, metavar="SEC",
                   help=f"循环间隔秒数（默认 {config.INTRADAY_INTERVAL_SECONDS}）")
    p.add_argument("--watch-top", type=int, default=None, metavar="N",
                   help=f"盯梢范围取榜单前 N 只（默认 {config.INTRADAY_WATCH_TOP_N}）")
    p.add_argument("--watch-codes", default=None, metavar="CODES",
                   help="只盯指定代码（逗号分隔，如 300006.SZ,605577.SH）")
    p.add_argument("--quote-source", choices=["tencent", "sina"], default=None,
                   help="行情快照源（默认 tencent，失败自动回退）")
    p.add_argument("--allow-outside-session", action="store_true",
                   help="不在交易时段也强制跑一次盯梢（调试用）")
    p.add_argument("--host", default="127.0.0.1", metavar="ADDR",
                   help="网页看板监听地址（默认 127.0.0.1；公网用 0.0.0.0）")
    p.add_argument("--port", type=int, default=8000, metavar="PORT",
                   help="网页看板端口（默认 8000）")

    p.add_argument("--verbose", "-v", action="store_true", help="输出 DEBUG 日志")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.paths:
        return cmd_paths(args)
    if args.config:
        return cmd_config(args)
    if args.self_test:
        return cmd_self_test(args)
    if args.gui:
        return cmd_gui(args)
    if args.replay:
        return cmd_replay(args)
    if args.watch or args.loop:
        return cmd_watch(args)
    if args.scan:
        return cmd_scan(args)
    if args.serve:
        return cmd_serve(args)
    if args.publish is not None:
        return cmd_publish(args)
    if args.cli or args.scheduled or args.local_only:
        return run_pipeline(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
