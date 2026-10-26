"""
config.py — cn-stock-screener 全局配置

设计原则（重要，勿破坏）：
1. 完全自包含：所有路径都从 __file__ 推导，与"当前工作目录 (CWD)"完全无关。
   不允许出现 "./data"、"data/" 这类依赖 CWD 的写法。
2. Token 不硬编码：只从环境变量 TUSHARE_TOKEN 读取；若不存在，则从项目根目录的
   .env 文件读取（.env 已加入 .gitignore，不随代码分发）。
3. 阈值参数集中在此文件，方便后续（第二阶段）接入技术指标与筛选逻辑。

数据权限范围（Tushare Pro 2000 积分，勿越界调用）：
    可用：stock_basic / daily / daily_basic / income / balancesheet / cashflow / 宏观经济
    禁用：分钟线、实时行情、以及任何需要更高积分的特色数据
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ==========================================================================
# 1. 路径 —— 全部由项目根目录推导，永不依赖 CWD
# ==========================================================================
def _resolve_base_dir() -> Path:
    """项目根目录。

    - 源码运行：本文件所在目录（与 CWD 无关）；
    - PyInstaller 打包后：**exe 所在目录**，而不是 ``sys._MEIPASS`` 临时解包目录。
      否则缓存会找不到、产物会写进临时目录并随程序退出被删掉。
    """
    if getattr(sys, "frozen", False):          # 冻结（exe）运行
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR: Path = _resolve_base_dir()

DATA_DIR: Path = BASE_DIR / "data"                 # 数据缓存根目录
CACHE_DIR: Path = DATA_DIR / "cache"               # 分片缓存（按 trade_date / period）
INDICATORS_DIR: Path = BASE_DIR / "indicators"     # 技术指标
SCREENER_DIR: Path = BASE_DIR / "screener"         # 筛选逻辑
TRIGGER_DIR: Path = BASE_DIR / "trigger"           # 触发与调度
OUTPUT_DIR: Path = BASE_DIR / "output"             # 产物根目录
LOG_DIR: Path = OUTPUT_DIR / "logs"                # 运行日志
REPORT_DIR: Path = OUTPUT_DIR / "reports"          # 筛选结果 / 报告
SCRIPTS_DIR: Path = BASE_DIR / "scripts"           # 批处理脚本
ENV_FILE: Path = BASE_DIR / ".env"                 # Token（本机私密，不入库）
ENV_EXAMPLE_FILE: Path = BASE_DIR / ".env.example" # Token 模板
REQUIREMENTS_FILE: Path = BASE_DIR / "requirements.txt"

# 运行期必需目录，导入 config 时即保证存在（从任意目录运行都不会缺目录）
for _d in (DATA_DIR, CACHE_DIR, OUTPUT_DIR, LOG_DIR, REPORT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ==========================================================================
# 2. 环境变量 / Token
# ==========================================================================
def load_dotenv(path: Path | None = None, override: bool = False) -> Path | None:
    """极简 .env 加载器（优先用 python-dotenv，缺失时回退到内置实现）。

    已存在的真实环境变量默认优先（override=False），便于临时覆盖。
    返回实际读取到的 .env 路径；未找到返回 None。
    """
    p = Path(path) if path is not None else ENV_FILE
    if not p.is_file():
        return None

    if not override:
        # 记录调用前的既有环境变量，读取后恢复，等价于 override=False
        _existing = dict(os.environ)

    loaded: list[tuple[str, str]] = []
    try:
        try:
            from dotenv import dotenv_values  # type: ignore

            for k, v in (dotenv_values(p) or {}).items():
                if k:
                    loaded.append((str(k), "" if v is None else str(v)))
        except Exception:  # python-dotenv 不可用 → 内置解析器
            for raw in p.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                if key.startswith("export "):
                    key = key[len("export "):].strip()
                val = val.split(" #")[0].strip().strip('"').strip("'")
                if key:
                    loaded.append((key, val))
    except Exception as exc:  # noqa: BLE001 - 配置读取失败不应阻断启动
        print(f"[config] .env 读取失败: {exc}")
        return p

    for k, v in loaded:
        if override or k not in _existing:
            os.environ[k] = v
    return p


_DOTENV_PATH = load_dotenv()

#: Token 占位值，出现在 .env.example 中，遇到时视为"未配置"
TOKEN_PLACEHOLDER = "在这里填入你的_Tushare_Token"

#: 视为"还没填"的占位串
_TOKEN_PLACEHOLDERS = {"your_token", "xxx", "changeme", TOKEN_PLACEHOLDER.lower()}


def _bundled_token() -> str:
    """打包 exe 时内置的 Token（**仅在**环境变量与 .env 都没有时兜底）。

    ``_bundled_token.py`` 由打包脚本在编译前生成、**不入库**（见 .gitignore），
    源码运行时根本不存在，所以开发模式下仍然强制"自己填 .env"。
    优先级最低：只要用户自己配了环境变量或 .env，就用用户的。
    """
    try:
        from _bundled_token import TUSHARE_TOKEN as _t  # type: ignore

        return str(_t or "").strip()
    except Exception:  # noqa: BLE001 - 模块不存在属于正常情况
        return ""


def get_token(explicit: str | None = None) -> str:
    """返回 Tushare Token（绝不打印完整值）。

    优先级：显式参数 > 环境变量 TUSHARE_TOKEN > 项目内 .env > 打包内置。
    未配置或仍为占位符时返回空字符串。
    """
    token = (explicit or os.getenv("TUSHARE_TOKEN", "") or "").strip()
    if not token or token.lower() in _TOKEN_PLACEHOLDERS:
        token = _bundled_token()
    if not token or token.lower() in _TOKEN_PLACEHOLDERS:
        return ""
    return token


def mask_token(token: str | None = None) -> str:
    """脱敏显示 Token，用于日志/自检输出。"""
    t = (token if token is not None else get_token()).strip()
    if not t:
        return "(未配置)"
    if len(t) <= 8:
        return t[0] + "*" * (len(t) - 1)
    return f"{t[:4]}{'*' * 8}{t[-4:]} (len={len(t)})"


def token_source() -> str:
    """Token 的来源描述，便于排错。"""
    if (os.getenv("TUSHARE_TOKEN") or "").strip():
        if _DOTENV_PATH is not None:
            try:
                env_text = _DOTENV_PATH.read_text(encoding="utf-8", errors="ignore")
                if "TUSHARE_TOKEN" in env_text:
                    return f"环境变量或 {_DOTENV_PATH.name}"
            except Exception:  # noqa: BLE001
                pass
        return "环境变量 TUSHARE_TOKEN"
    if _bundled_token():
        return "打包内置（exe 编译时写入）"
    return "未配置（请复制 .env.example 为 .env 并填入 Token）"


# ==========================================================================
# 3. 数据源 / 限流
# ==========================================================================
#: Tushare 官方 endpoint（仅作说明用，代码不覆盖它）。
#: 当前版本的 tushare 会自己请求 http://api.waditu.com/dataapi/<接口名>；
#: 曾经验证过：把域名改成 api.tushare.pro 会"请求成功但返回空表"，因此不要改。
TUSHARE_API_URL = "http://api.waditu.com/dataapi"

#: Tushare Pro 2000 积分限流：200 次/分钟。实际每日调用量极小（约 2~5 次），
#: 这里只做保守保护：任意两次调用至少间隔 MIN_CALL_INTERVAL 秒。
RATE_LIMIT_PER_MINUTE = 200
RATE_WINDOW_SECONDS = 60
MIN_CALL_INTERVAL = 0.3          # 秒，两次 API 调用之间的最小间隔
API_TIMEOUT = 30                 # 单次请求超时（秒）
API_MAX_RETRIES = 3              # 失败重试次数（含网络错误与限流错误）
API_RETRY_BACKOFF = 3.0          # 重试退避基数（秒），实际等待 = backoff * 尝试次数

#: 批量接口单次返回上限（Tushare 单次最多 6000 行），超出需分页
API_PAGE_LIMIT = 6000

#: 是否允许使用 trade_cal（交易日历）。
#: 权限清单里没有它，因此默认关闭：交易日判断改用 daily 接口探测（见 trigger/calendar.py），
#: 这样 100% 落在已授权的接口范围内。
USE_TRADE_CAL_API = False

# ==========================================================================
# 4. 数据权限白名单（防止误调用高积分接口）
# ==========================================================================
ALLOWED_APIS: frozenset[str] = frozenset({
    # 基础信息
    "stock_basic",
    # 行情
    "daily",
    # 每日指标
    "daily_basic",
    # 复权因子（前复权用；按交易日整表拉取）
    "adj_factor",
    # 三大报表
    "income",
    "balancesheet",
    "cashflow",
    # 宏观经济
    "cn_gdp", "cn_cpi", "cn_ppi", "cn_m", "cn_pmi",
    "shibor", "shibor_lpr", "us_tycr", "us_trycr", "us_tbr",
})

#: 明确禁用（调用了直接抛错提醒）
FORBIDDEN_APIS: frozenset[str] = frozenset({
    "stk_mins", "rt_k", "rt_tick", "realtime_quote", "pro_bar",
    "index_dailybasic", "ths_index", "moneyflow_dc", "cyq_perf",
})

# ==========================================================================
# 5. 数据缓存分片
# ==========================================================================
#: 每个缓存数据集对应的分片键含义
CACHE_DATASETS: dict[str, str] = {
    "stock_basic": "snapshot",       # 全市场股票列表（快照，整表一个文件）
    "daily": "trade_date",           # 日线，按交易日分片
    "daily_basic": "trade_date",     # 每日指标，按交易日分片
    "adj_factor": "trade_date",      # 复权因子，按交易日分片（列：ts_code/trade_date/adj_factor）
    "income": "period",              # 利润表，按报告期分片
    "balancesheet": "period",        # 资产负债表，按报告期分片
    "cashflow": "period",            # 现金流量表，按报告期分片
}

#: 数据集刷新间隔（秒），用于判断缓存是否过期；None = 永不过期（只增不重拉）
CACHE_TTL: dict[str, int | None] = {
    "stock_basic": 7 * 24 * 3600,    # 股票列表：7 天
    "daily": None,                   # 历史日线：已入库即稳定
    "daily_basic": None,
    "adj_factor": None,              # 历史复权因子：已入库即稳定
    "income": None,
    "balancesheet": None,
    "cashflow": None,
}

#: 日线回看窗口（交易日数）：首次运行/补齐缓存时最多回溯多少天
#: 750 个交易日 ≈ 3 年：够算周线 MA20（≈100 根）与月线 MA20（≈420 根），
#: 也让月线 MACD(26/9) 接近收敛（约需 35 个月 ≈ 735 个交易日）
DAILY_BACKFILL_DAYS = 750
#: 默认拉取的近端交易日数量（--cli 默认只补最近 N 个交易日，避免一把拉爆）
DEFAULT_INIT_DAYS = 5

# 各接口返回字段（显式指定可减少传输量）
FIELDS_STOCK_BASIC = (
    "ts_code,symbol,name,area,industry,market,exchange,list_status,list_date,delist_date,is_hs"
)
FIELDS_DAILY = "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"
FIELDS_ADJ_FACTOR = "ts_code,trade_date,adj_factor"
FIELDS_DAILY_BASIC = (
    "ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,pe,pe_ttm,"
    "pb,ps,ps_ttm,dv_ratio,dv_ttm,total_share,float_share,free_share,total_mv,circ_mv"
)
FIELDS_INCOME = (
    "ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,total_revenue,revenue,"
    "oper_cost,operate_profit,total_profit,income_tax,n_income,n_income_attr_p,basic_eps,"
    "total_cogs,sell_exp,admin_exp,fin_exp,rd_exp"
)
FIELDS_BALANCESHEET = (
    "ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,total_assets,total_liab,"
    "total_hldr_eqy_exc_min_int,total_hldr_eqy_inc_min_int,total_cur_assets,total_cur_liab,"
    "money_cap,accounts_receiv,inventories,fix_assets,goodwill,lt_borr,st_borr"
)
FIELDS_CASHFLOW = (
    "ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,n_cashflow_act,"
    "n_cashflow_inv_act,n_cash_flows_fnc_act,c_fr_sale_sg,c_paid_goods_s,"
    "free_cashflow,cash_recp_disp_fiolta"
)

# ==========================================================================
# 6. 股票池过滤（第一阶段：粗筛，只用 stock_basic + daily 可得信息）
# ==========================================================================
UNIVERSE_EXCLUDE_ST = True            # 剔除 ST / *ST / 退市整理
UNIVERSE_EXCLUDE_BJ = True            # 剔除北交所（8xxxxx/4xxxxx，流动性与数据质量差异较大）
UNIVERSE_EXCLUDE_DELISTED = True      # 只保留 list_status == 'L'
UNIVERSE_MIN_LIST_DAYS = 60           # 上市满 N 个自然日才纳入
UNIVERSE_MIN_AMOUNT = 0.0             # 当日成交额下限（千元），0 = 不过滤
UNIVERSE_MIN_TOTAL_MV = 0.0           # 总市值下限（万元），0 = 不过滤

# ==========================================================================
# 7. 技术指标参数（第二阶段填充具体逻辑，这里先给标准默认值）
# ==========================================================================
MA_PERIODS: tuple[int, ...] = (5, 10, 20, 60, 120)
EMA_PERIODS: tuple[int, ...] = (12, 26)
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
RSI_PERIODS: tuple[int, ...] = (6, 14)
KDJ_N, KDJ_M1, KDJ_M2 = 9, 3, 3
BOLL_N, BOLL_K = 20, 2.0
ATR_PERIOD = 14
VOLUME_MA_PERIODS: tuple[int, ...] = (5, 10, 20)
#: MACD 柱 = MULT * (DIF - DEA)；国内软件（通达信/同花顺）习惯取 2.0
MACD_HIST_MULTIPLIER = 2.0
#: 指标计算所需的最小日线长度（交易日）
MIN_HISTORY_BARS = 120

# ---- 重采样周期（周线 / 月线）----
#: 周线重采样规则（周五收盘）
WEEKLY_RULE = "W-FRI"
#: 月线重采样规则（月末；pandas 3.x 用 ME，旧别名 M 已废弃）
MONTHLY_RULE = "ME"

# ---- K 线形态（锤子线 / 射击之星）----
#: 实体长度上限（占全幅比例）
CANDLE_BODY_MAX_RATIO = 0.35
#: 长影线至少是实体的几倍
CANDLE_SHADOW_MULT = 2.0
#: 是否要求形态出现在趋势之后（锤子=下跌后，射击之星=上涨后）
CANDLE_TREND_FILTER = True

# ---- 双重底 / 双重顶 ----
#: 摆动点（pivot）窗口半径：前后各 N 根
SWING_N = 5
#: 两个摆动点的价差容忍度：|p1-p2| / ((p1+p2)/2) ≤ 3%
PATTERN_PRICE_TOL = 0.03
#: 两个摆动点的最小间隔（交易日）
PATTERN_MIN_GAP = 10
#: 两个摆动点的最大间隔（交易日），避免跨年配对
PATTERN_MAX_GAP = 250
#: 颈线与摆动点的最小距离（1%），颈线太薄弱不作为形态
PATTERN_NECKLINE_MIN_PCT = 0.01
#: 突破后元数据向前填充的根数（便于看盘核对）
PATTERN_META_FFILL = 20
#: 生成"最近 N 根内突破过"的派生列，供筛选器直接使用
PATTERN_BREAKOUT_LOOKBACKS: tuple[int, ...] = (5, 10, 20)

# ---- 形态信号的"当前有效性"口径（第 5 步新增，默认开启）----
#: 双重底：要求**当前收盘价仍在颈线之上**才算命中。
#:   只要求"最近 N 日内曾突破"会把"突破后又跌回颈线下"的失效形态也算进来
#:   （2026-10-14 实测：496 只里有 279 只已跌回颈线下方）。
PATTERN_REQUIRE_ABOVE_NECKLINE = True
#: 双重顶：要求**当前收盘价仍在颈线之下**才算命中（实测 1014 只里 182 只已反弹回颈线上方）。
PATTERN_REQUIRE_BELOW_NECKLINE = True

# ---- 日 / 周 / 月共振 ----
#: 共振用的均线周期
RESONANCE_MA_WINDOW = 20

# ==========================================================================
# 8. 筛选阈值（占位，第二阶段按你的技术指标逻辑调整）
# ==========================================================================
SCREEN_PARAMS: dict = {
    "min_turnover_rate": 0.5,       # 换手率下限（%）
    "max_turnover_rate": 25.0,      # 换手率上限（%）
    "pe_ttm_min": 0.0,              # PE(TTM) 下限，<=0 视为亏损（由 enabled 控制是否剔除）
    "pe_ttm_max": 100.0,
    "exclude_negative_pe": False,   # 是否剔除亏损股
    "pb_min": 0.0,
    "pb_max": 30.0,
    "min_total_mv": 0.0,            # 总市值下限（万元）
    "max_total_mv": 0.0,            # 0 = 不限制
    "min_price": 2.0,
    "max_price": 0.0,               # 0 = 不限制
    "require_positive_net_profit": False,
    "max_result_rows": 200,         # 结果集上限
}

# ---- 技术指标筛选链（第二阶段实现的规则，见 screener/filters/）----
#: 默认筛选链：**换手率必须排第一**，因为它的"全市场 80 分位"是按当前传入的池计算的，
#: 放到其它规则之后算分位数会失真。
DEFAULT_FILTER_CHAIN: tuple[str, ...] = ("turnover", "resonance_dwm", "double_bottom")

#: 各筛选规则的默认参数（可在命令行用 --filter-params 覆盖，或在代码里直接传）
FILTER_PARAMS: dict[str, dict] = {
    "turnover": {"min_turnover": 3.0, "percentile": 0.80},
    "resonance_daily": {},
    "resonance_dwm": {},
    "double_bottom": {"lookback": 10},
    "double_top": {"lookback": 10},
    "hammer": {},
    "shooting_star": {},
    "macd_bull": {},
    "kdj_bull": {},
    "volume_surge": {"mode": "absolute", "min_ratio": 1.5, "surge_percentile": 80, "ma_window": 5},
    "volume_shrink": {"mode": "absolute", "max_ratio": 0.7, "shrink_percentile": 20, "ma_window": 5},
}

#: 量能规则默认阈值模式（absolute=绝对量比阈值；percentile=横截面分位）
#: 分位模式在**全市场截面**上算，不受漏斗上游过滤影响
VOLUME_MODE = "absolute"

#: 各指标所需的最小日线根数（用于历史不足时给出可执行的提示）
#: 周线 1 根 ≈ 5 个交易日，月线 1 根 ≈ 21 个交易日
HISTORY_REQUIREMENTS: dict[str, int] = {
    "ma20": 20,
    "macd": 35,
    "kdj": 9,
    "double_bottom": 2 * 5 + 10 + 5,          # 两个摆动点确认 + 间隔 + 突破确认
    "weekly_ma20": 20 * 5,
    "weekly_macd": 35 * 5,
    "monthly_ma20": 20 * 21,
    "monthly_macd": 35 * 21,
}

# ==========================================================================
# 9. 触发时间（Windows 计划任务）
# ==========================================================================
SCHEDULE_FULL_TIME = "22:30"     # 主触发：交易日 22:30，全市场扫描（含财务）
SCHEDULE_QUOTE_TIME = "17:30"    # 辅助触发：交易日 17:30，纯行情扫描
SCHEDULE_WEEKDAYS = "MON,TUE,WED,THU,FRI"
TASK_NAME_FULL = "CNStockScreener_Daily_2230"
TASK_NAME_QUOTE = "CNStockScreener_Quote_1730"

#: 交易时段（禁止盘中触发）：09:15~15:05 视为盘中
MARKET_OPEN_TIME = "09:15"
MARKET_CLOSE_TIME = "15:05"

# ==========================================================================
# 10. 运行模式
# ==========================================================================
MODE_FULL = "full"    # 含财务数据（22:30 主触发）
MODE_QUOTE = "quote"  # 纯行情（17:30 辅助触发）
VALID_MODES = (MODE_FULL, MODE_QUOTE)

# ==========================================================================
# 11. 评分与推荐分层（--score，可选；不加 --score 时行为与以前完全一致）
# ==========================================================================
#: 各分项权重。趋势项 dwm / daily **二选一不叠加**，
#: 所以满分 = 30 + 20 + 12 + 10 + 8 + 12 + 8 = 100
SCORE_WEIGHTS: dict[str, float] = {
    "resonance_dwm": 30.0,      # 日周月共振
    "resonance_daily": 12.0,    # 仅日线共振（次级替代，与 dwm 二选一）
    "double_bottom": 20.0,      # 双重底突破且当前仍在颈线上方
    "volume_surge": 12.0,       # 当日放量
    "macd_bull": 10.0,          # DIF > DEA
    "kdj_bull": 8.0,            # K > D
    "turnover": 12.0,           # 活跃度（分档）
    "hammer": 8.0,              # 锤子线
}

#: 换手率分档：(阈值%, 得分)，取满足的最高档；最高档得分应等于 SCORE_WEIGHTS["turnover"]
TURNOVER_SCORE_TIERS: tuple[tuple[float, float], ...] = (
    (8.0, 12.0),
    (5.0, 10.0),
    (3.0, 8.0),
    (2.0, 4.0),
)

#: 分档门槛（分数）
SCORE_TIERS: dict[str, float] = {"core": 85.0, "watch": 75.0, "candidate": 65.0}
#: 分档显示名
SCORE_TIER_LABELS: dict[str, str] = {"core": "核心", "watch": "观察", "candidate": "候选"}
#: 每档名额上限（0 = 不限）
TIER_CAPS: dict[str, int] = {"core": 5, "watch": 10, "candidate": 15}
#: 至少命中几个独立信号才算入榜（防止靠单项高分混进来）
SCORE_MIN_HIT = 2

# ==========================================================================
# 12. 盘中盯梢（--watch）
# ==========================================================================
#: 硬规则：本项目**不调用任何大模型 API**，所有计算都在本机完成。
#: 这个开关是给自检和 README 用的声明标记；代码里没有任何 LLM SDK 依赖。
LLM_FREE = True

#: 允许联网的主机白名单（自检会扫描全部 .py 里的 URL，不在名单内即失败）
#: - api.waditu.com        : Tushare 数据源（拉日线/复权因子）
#: - qt.gtimg.cn           : 腾讯免费行情快照（盘中盯梢，无需 Token）
#: - hq.sinajs.cn          : 新浪免费行情快照（备用）
#: - gu.qq.com             : 腾讯接口要求的 Referer 头（只做请求头，不发请求）
#: - finance.sina.com.cn   : 新浪接口要求的 Referer 头（同上）
#: - push2his.eastmoney.com: 东财 15 分钟 K 线（盘中量比/MACD/KDJ，无需 Token）
ALLOWED_NETWORK_HOSTS: frozenset[str] = frozenset({
    "api.waditu.com",
    "qt.gtimg.cn",
    "hq.sinajs.cn",
    "gu.qq.com",
    "finance.sina.com.cn",
    "push2his.eastmoney.com",
    "money.finance.sina.com.cn",   # 15 分钟 K 线备用源
})

#: 盘中盯梢总开关
INTRADAY_ENABLED = True
#: 行情快照源：tencent（推荐，字段全）| sina（备用）
INTRADAY_QUOTE_SOURCE = os.getenv("SCREENER_QUOTE_SOURCE", "tencent").strip().lower()
#: 单次请求超时（秒）
INTRADAY_QUOTE_TIMEOUT = 8
#: 一次请求最多带多少只代码（腾讯接口支持批量）
INTRADAY_BATCH_SIZE = 40
#: 盯梢范围：取评分榜单前 N 只
INTRADAY_WATCH_TOP_N = 30
#: 循环模式的间隔（秒）
INTRADAY_INTERVAL_SECONDS = 300
#: 交易时段（只在这些区间内盯梢；集合竞价与午休不盯）
INTRADAY_SESSIONS: tuple[tuple[str, str], ...] = (("09:30", "11:30"), ("13:00", "15:00"))
#: 告警去重粒度：同一个 (股票, 规则) 每天只报一次
INTRADAY_DEDUPE = True

#: 盘中告警阈值（全部为确定性规则，无任何模型参与）
INTRADAY_RULES: dict[str, float | bool] = {
    "break_ma20": True,          # 现价跌破 20 日线
    "break_neckline": True,      # 现价跌破双底颈线（形态失效）
    "drop_pct": -5.0,            # 当日跌幅 ≤ 该值 → 急跌
    "surge_pct": 7.0,            # 当日涨幅 ≥ 该值 → 拉升
    "near_limit_pct": 2.0,       # 距涨停/跌停 ≤ 该百分比 → 提示
    "volume_burst_ratio": 1.0,   # 今日成交量 / 扫描日成交量 ≥ 该值 → 放量
}

#: 告警 CSV 输出目录（项目内）
ALERT_DIR: Path = OUTPUT_DIR / "alerts"

# ==========================================================================
# 13. 日线交易信号（T+1）与全市场盘中扫描
# ==========================================================================
#: 是否在每次日线扫描时更新买卖信号（状态存 output/signals/signals.json）
SIGNALS_ENABLED = True
#: 买入条件里的量比门槛（与评分里的"放量"口径一致）
SIGNAL_VOLUME_RATIO = 1.5
#: 每个交易日最多发几笔买入信号（按评分从高到低取，避免一天买几十只）
SIGNAL_MAX_BUYS_PER_DAY = 5
#: 盘中扫描榜：涨幅前 N / 跌幅前 N
INTRADAY_SCAN_TOP = 20
INTRADAY_SCAN_BOTTOM = 10

# ==========================================================================
# 14. 行业（板块）强度 —— 日线口径
# ==========================================================================
SECTOR_ENABLED = True
#: 行业强度只统计成分股 >= 该数量的行业（避免一只票撑起一个板块）
SECTOR_MIN_MEMBERS = 5
#: --sector-first 时取前几个行业
SECTOR_TOP_N = 5
#: 每个行业最多取几只
SECTOR_PER_INDUSTRY = 5
#: 行业排名落后于该名次、但个股进了核心/观察档 → 标记为"逆势"
SECTOR_CONTRARIAN_RANK = 20

LOG_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_LEVEL = os.getenv("SCREENER_LOG_LEVEL", "INFO").upper()

#: 结果文件是否同时输出 CSV 与 JSON
OUTPUT_CSV = True
OUTPUT_JSON = True


def ensure_dirs() -> None:
    """确保所有项目内目录存在（幂等）。"""
    for d in (DATA_DIR, CACHE_DIR, OUTPUT_DIR, LOG_DIR, REPORT_DIR, SCRIPTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def config_summary() -> str:
    """一行配置摘要，启动时打印。"""
    return (
        f"BASE_DIR={BASE_DIR} | mode_dirs=OK | token={mask_token()} | "
        f"rate={RATE_LIMIT_PER_MINUTE}/min interval={MIN_CALL_INTERVAL}s | "
        f"cwd={Path.cwd()}"
    )


if __name__ == "__main__":  # pragma: no cover - 手工查看配置
    print(config_summary())
    print(f"python={sys.version.split()[0]}  executable={sys.executable}")
    for name in ("BASE_DIR", "DATA_DIR", "CACHE_DIR", "OUTPUT_DIR", "LOG_DIR", "REPORT_DIR"):
        print(f"{name:12s} = {globals()[name]}")
