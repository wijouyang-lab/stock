# cn-stock-screener — A股技术指标筛选器

一个**完全自包含**的 A 股筛选程序：整个文件夹拷到任何地方都能独立运行，
不依赖文件夹外的任何路径，也不需要数据库或服务器。

- 数据源：**Tushare Pro**（2000 积分权限）
- 数据缓存：全部分片存放在项目内 `data/cache/`，按 `trade_date` / 报告期切分
- 触发方式：Windows 计划任务，交易日 **22:30** 全量扫描 + **17:30** 纯行情扫描
- 计算方式：技术指标与筛选**全部本地计算**，`--local-only` / `--replay` 下零网络调用
- 当前进度：
  - 第一阶段 ✅ 数据层、分片缓存、计划任务、自包含路径
  - 第二阶段 ✅ 周/月线重采样、K 线形态、双重底/顶、MACD、KDJ、换手率/量比、
    日周月共振、11 条筛选规则、CSV + funnel JSON 产物、`--replay` 回放
  - 第三阶段 ✅ 评分排行与推荐分层（`--score`：核心/观察/候选，见 6.9）
  - 第四阶段 ✅ 盘中盯梢（`--watch`：免费实时快照 + 7 条告警规则，见 4.5；**不调用任何大模型**）

---

> **本程序不调用任何大模型 API，所有指标计算、筛选、回放均在本机完成，不产生大模型 token 消耗。**
> 需要 Tushare Token 仅用于拉取行情数据（首次回补或每日增量）；已有缓存时可用 `--local-only` 完全离线运行。

---

## 一、30 秒上手（朋友拿到文件夹后照做）

前提：Windows + Python 3.9 以上（推荐 3.10~3.13 的 64 位官方版，安装时勾选
**Add python.exe to PATH**）。

```bat
:: 1) 进入项目文件夹（路径随你放哪都行，盘符、目录名都无所谓）
cd /d D:\cn-stock-screener

:: 2) 安装依赖（只装 3 个包）
python -m pip install -r requirements.txt

:: 3) 复制 Token 模板并填入自己的 Token
copy .env.example .env
notepad .env          :: 把 TUSHARE_TOKEN= 后面换成你自己的 Token 后保存

:: 4) 自检（全绿说明环境没问题）
python main.py --self-test

:: 5) 跑一次（拉数据 + 写缓存 + 跑筛选）
python main.py --cli
```

> 填 Tushare Token 只是为了**拉行情数据**（数据源鉴权），不是为了让程序"思考"——
> 本项目不依赖任何大模型，指标与筛选都是本机的确定性计算。
> 只想看结果、不拉新数据时，连 Token 都不需要，直接跑
> `python main.py --cli --local-only --replay 20261014 --filters turnover`。

第 5 步成功后，屏幕最后一行会打印：

```
数据就绪，5002只股票
```

（数字随当日行情入库情况变化；如果提示"非交易日已跳过"或"股票池为空"，
请看第十节的常见问题。）

### Token 怎么来

1. 注册并登录 <https://tushare.pro>
2. 打开 <https://tushare.pro/user/token> 复制那串 40~60 位的 Token
3. 粘贴进项目根目录 `.env` 的 `TUSHARE_TOKEN=` 后面

> `.env` 已写入 `.gitignore`，不会被打包/提交；程序也从不硬编码 Token。
> 也可以不建 `.env`，直接设置系统环境变量 `TUSHARE_TOKEN`（环境变量优先）。

---

## 二、目录结构

```
cn-stock-screener/
├─ data/                    # 数据缓存根目录（可分片、可整体删掉重拉）
│   └─ cache/
│       ├─ stock_basic.csv          # 全市场股票列表（含行业分类）
│       ├─ daily/20250102.csv       # 日线行情，按交易日分片
│       ├─ daily_basic/20250102.csv # 每日指标（PE/PB/换手率/市值），按交易日分片
│       ├─ income/20260630.csv      # 利润表，按报告期分片
│       ├─ balancesheet/20260630.csv
│       ├─ cashflow/20260630.csv
│       ├─ _meta.json               # 每个分片的行数/写入时间
│       └─ _trade_dates.json        # 交易日探测结果（避免重复调接口）
├─ indicators/              # 技术指标（纯本地计算）
│   ├─ __init__.py          #   注册表 + compute_all / compute_panel
│   ├─ _util.py             #   EMA/SMA/滚动窗口等工具
│   ├─ resample.py          #   日线 → 周线(W-FRI) / 月线(ME)
│   ├─ candles.py           #   hammer / shooting_star
│   ├─ patterns.py          #   double_bottom / double_top（突破确认）
│   ├─ momentum.py          #   macd(12/26/9) / kdj(9/3/3)
│   ├─ volume.py            #   turnover / volume_ratio(vol/MA5)
│   ├─ resonance.py         #   日周月共振
│   └─ selftest.py          #   合成数据自检 + 零调用断言
├─ screener/                # 筛选逻辑与规则引擎
│   ├─ __init__.py          #   Filter 基类 / register_filter / ScreenerEngine
│   └─ filters/             #   9 条内置规则（导入即注册）
│       ├─ volume.py        #     turnover
│       ├─ resonance.py     #     resonance_daily / resonance_dwm
│       ├─ patterns.py      #     double_bottom / double_top
│       ├─ candles.py       #     hammer / shooting_star
│       └─ momentum.py      #     macd_bull / kdj_bull
├─ trigger/                 # 触发与调度（交易日判定、22:30/17:30 决策）
├─ gui/                     # 桌面窗口（tkinter，标准库，可选用）
├─ output/                  # 全部产物
│   ├─ logs/                # 运行日志 screener_YYYYMMDD.log
│   └─ reports/             # 明细 CSV / 摘要 JSON / funnel JSON
├─ scripts/
│   ├─ run_screener.bat     # 执行入口（计划任务调用的就是它）
│   ├─ register_task.bat    # 注册计划任务
│   └─ unregister_task.bat  # 卸载计划任务
├─ main.py                  # 入口（--cli / --local-only / --replay / --self-test / --gui）
├─ config.py                # 路径、Token、指标与筛选参数
├─ requirements.txt
├─ .env.example             # Token 模板
├─ .env                     # 你的真实 Token（不入库）
├─ .gitignore
└─ README.md
```

**路径规范**：所有路径由 `config.BASE_DIR = Path(__file__).resolve().parent` 推导，
代码中不存在 `"./data"`、`"data/"` 这类相对当前目录的写法；
三个 `.bat` 全部用 `%~dp0` 定位脚本所在目录。所以：
**从任何目录、用任何方式启动，产物都只会落在项目文件夹内。**

---

## 三、命令速查

| 命令 | 作用 |
| --- | --- |
| `python main.py --cli` | 拉数据 + 写缓存 + 跑一遍筛选（默认 quote 模式） |
| `python main.py --cli --mode full` | 含三大报表的完整同步 |
| `python main.py --cli --mode quote` | 只拉行情（不含财务） |
| `python main.py --cli --date 20250102` | 指定交易日 |
| `python main.py --cli --force` | 忽略缓存强制重拉 |
| `python main.py --cli --backfill` | 回补最近 30 个交易日的日线 |
| `python main.py --cli --days 10` | 补齐最近 10 个交易日 |
| `python main.py --cli --financial-limit 300` | 三大报表逐只拉取前 300 只（按市值降序） |
| `python main.py --scheduled --mode full` | **计划任务入口**：严格执行"交易日 + 非盘中 + 数据已入库"判断 |
| **纯本地（零网络）** | |
| `python main.py --local-only` | 不拉数据，只读缓存算指标 + 跑默认筛选链 |
| `python main.py --replay 20261015` | 回放某个交易日（只能回放本地已有分片的日期） |
| `python main.py --local-only --filters turnover,macd_bull` | 指定筛选链（按顺序执行） |
| `python main.py --local-only --filters none` | 不筛选，输出全股票池（看指标值用） |
| `python main.py --replay 20261015 --history-bars 30` | 只用最近 30 个本地分片算指标 |
| **评分排行（可选，见 6.9）** | |
| `python main.py --local-only --score` | 全市场评分 + 核心/观察/候选分层（不做漏斗） |
| `python main.py --local-only --score --top 20` | 只看前 20 名 |
| `python main.py --local-only --score --min-score 70 --no-caps` | 自定义入榜门槛、不按档位名额限制 |
| `python main.py --local-only --score --filters turnover` | 先漏斗筛，再对结果评分排序 |
| **盘中盯梢（可选，见 4.5）** | |
| `python main.py --watch` | 拉一次实时快照，评估 7 条告警规则（不需要 Tushare Token） |
| `python main.py --watch --loop` | 常驻循环，每 300 秒一轮，直到收盘 |
| `python main.py --watch --watch-codes 300006.SZ` | 只盯指定股票 |
| `scripts\register_watch_task.bat` | 注册每 15 分钟自动盯梢的计划任务 |
| **自检与信息** | |
| `python main.py --self-test` | 基础自检（含真实接口连通性抽检 + 指标自检） |
| `python main.py --self-test --offline` | 离线自检（不校验 Token、不联网） |
| `python -m indicators.selftest` | 只跑指标/筛选/评分的合成数据自检（29 项） |
| `python main.py --paths` | 打印所有解析后的绝对路径（验证自包含） |
| `python main.py --config` | 配置摘要 + 缓存概览 |
| `python main.py --gui` | 打开桌面窗口 |

常用附加参数：`--verbose`（DEBUG 日志）、`--no-financials`、
`--ignore-calendar`（跳过交易日判断，调试用）。

---

## 四、自动触发（Windows 计划任务）

### 4.1 注册

```bat
cd /d D:\cn-stock-screener\scripts
register_task.bat
```

会创建两个任务（都调用 `scripts\run_screener.bat`）：

| 任务名 | 时间 | 模式 | 说明 |
| --- | --- | --- | --- |
| `CNStockScreener_Daily_2230` | 交易日 22:30 | `full` | 全市场扫描，**含**三大报表 |
| `CNStockScreener_Quote_1730` | 交易日 17:30 | `quote` | 纯行情扫描，**不含**财务 |

- 只在周一~周五触发（`schtasks /D MON,TUE,WED,THU,FRI`）；
- 法定节假日由 `main.py` 自己判断（用 `daily` 接口探测当天是否有行情），
  非交易日会打印「非交易日，跳过」并安静退出，不会误拉数据；
- 计划任务**不会**在盘中触发：09:15~15:05 一律拒绝执行；
- 任务路径用 `%~dp0` 解析，**项目文件夹被移动/改名后，重新跑一次
  `register_task.bat` 即可**（注册时记录的是当时的绝对路径）。

### 4.2 查看 / 手动触发

```bat
schtasks /Query /TN CNStockScreener_Daily_2230 /FO LIST     :: 查看状态
schtasks /Run   /TN CNStockScreener_Daily_2230              :: 立刻跑一次（模拟 22:30）
register_task.bat query                                     :: 查看两个任务状态
```

### 4.3 卸载

```bat
scripts\unregister_task.bat
```

只删任务，不动 `data/`、`output/` 里的任何数据。

### 4.4 直接手动运行（不经过计划任务）

```bat
scripts\run_screener.bat            :: 按当前时间自动选模式，并走时间窗口检查
scripts\run_screener.bat full       :: 22:30 模式（含财务）
scripts\run_screener.bat quote      :: 17:30 模式（纯行情）
scripts\run_screener.bat manual     :: 旁路时间窗口，立刻完整跑一次（首次验证用这个）
scripts\run_screener.bat gui        :: 打开桌面窗口
scripts\run_screener.bat full --force   :: 关键字后可以继续跟 main.py 的参数
```

`run_screener.bat` 会自动找 Python：优先项目内 `.venv`，其次 `py -3`，最后 `python`。

### 4.5 盘中盯梢（可选，`--watch`）

**为什么单独做一套**：收盘扫描要读 750 个日线分片（约 1 GB）并逐只重算指标，**5~6 分钟**一次；
盘中每 15 分钟做这件事既慢又无意义（日线一天只有一根）。所以盘中只盯**已经入榜的 30 只**，
只比较价格与量能，**秒级**完成。

**数据源**（都不需要 Tushare Token，也不消耗任何大模型 token）：

| 源 | 地址 | 说明 |
| --- | --- | --- |
| 腾讯 | `qt.gtimg.cn` | 主用，字段最全（含涨停/跌停/换手率/时间戳） |
| 新浪 | `hq.sinajs.cn` | 备用，腾讯失败时自动回退 |

主机白名单写在 `config.ALLOWED_NETWORK_HOSTS`，自检会扫描全部 `.py` 里的 URL，
出现名单外主机即判失败——这条断言同时保证了"不可能偷偷调用大模型 API"。

**盯梢范围**：`output/reports/` 下最近一次 `--score` 产物的前 N 只（默认 30）。

**7 条告警规则**（全部是阈值比较，零模型调用）：

| 规则 | 条件 | 级别 |
| --- | --- | --- |
| `break_ma20` | 现价 < 扫描日 MA20 | 风险 |
| `break_neckline` | 现价 < 双底颈线（形态失效） | 风险 |
| `drop` | 当日跌幅 ≤ -5% | 风险 |
| `surge` | 当日涨幅 ≥ +7% | 机会 |
| `near_limit_up` / `near_limit_down` | 距涨停/跌停 ≤ 2% | 机会 / 风险 |
| `volume_burst` | 今日成交量 / 扫描日成交量 ≥ 1.0 | 提示 |

阈值在 `config.INTRADAY_RULES`，置 `False` 或 `0` 即关闭该条。

**用法**

```bat
python main.py --watch                             :: 单次检查（给计划任务用）
python main.py --watch --loop                      :: 常驻，每 300 秒一轮，直到收盘
python main.py --watch --watch-top 10              :: 只盯前 10 只
python main.py --watch --watch-codes 300006.SZ,605577.SH
python main.py --watch --quote-source sina         :: 换备用源
python main.py --watch --allow-outside-session     :: 非交易时段强制跑一次（调试）
```

**计划任务（每 15 分钟）**

```bat
scripts\register_watch_task.bat          :: 注册 CNStockScreener_Watch_15m
scripts\register_watch_task.bat query    :: 查看
schtasks /Delete /TN CNStockScreener_Watch_15m /F   :: 卸载
```

任务从 09:30 开始每 15 分钟触发、持续 5.5 小时（到 15:00），只在周一~周五；
**午休（11:30~13:00）和任何非交易时段由程序自己跳过**，所以不会刷屏也不会误报。

**输出**：`output/alerts/intraday_YYYYMMDD.csv`
（`fired_at, trade_date, ts_code, name, rule, rule_label, level, level_label, message, price, pct_chg, score, tier`）。
同一只股票的同一规则**每天只记一次**（`config.INTRADAY_DEDUPE`），15 分钟一轮不会重复刷。

**已知限制**（务必知道）：

1. 名单来自收盘扫描，`ma20`/`neckline` 是**扫描日价格空间**的值；个股若在扫描日之后除权，
   原始价会跳空，可能误触「跌破 MA20」。收盘任务每天 22:30 刷新名单，偏移最多一个交易日。
2. 盘中**不重算指标**，所以评分与榜位不会变，动的只有价格和量能。
3. 免费接口有频率与稳定性限制；抓取失败会自动重试并回退新浪，连续失败时本轮返回码 `3`。
4. 仅作提示，不构成交易指令。

---

## 五、数据权限与限流

### 5.1 只使用这些接口（Tushare Pro 2000 积分）

| 接口 | 用途 | 调用量 |
| --- | --- | --- |
| `stock_basic` | 股票列表（含行业分类） | 1 次 / 7 天 |
| `daily` | 日线行情（按交易日整表拉取） | 1 次 / 交易日 |
| `daily_basic` | PE/PB/换手率/市值 | 1 次 / 交易日 |
| `income` / `balancesheet` / `cashflow` | 三大报表 | 整表 3 次 / 报告期，或逐只 |
| 宏观经济接口 | 预留 | 按需 |

**关于三大报表的重要说明（已实测）**：
官方文档说可以只传 `period` 一次拉全市场，但实测当前账号/接口版本会报
`必填参数, ts_code` —— 也就是**必须逐只股票查**。因此本项目的策略是：

1. 先尝试"整表按报告期"（若账号权限放开，3 次调用即可拿全市场，最省额度）；
2. 一旦遇到 `必填参数, ts_code`，且传了 `--financial-limit N`，就自动切换为
   **逐只拉取市值最大的 N 只**（按当日 `total_mv` 降序，保证先拿到大票）；
3. 逐只拉取的结果按 `ts_code` 合并进**同一个报告期分片**
   （如 `data/cache/income/20260630.csv`），下次直接命中缓存，不会重复调用。

时间预算（2000 积分 = 200 次/分钟，每只股票 3 次调用）：

| `--financial-limit` | 调用次数 | 预计耗时 |
| --- | --- | --- |
| 0（默认） | 3 | 秒级（整表不可用则不逐只拉） |
| 300 | 900 | 约 5 分钟 |
| 1000 | 3000 | 约 15 分钟 |

默认 `--financial-limit 0`，即"拿不到整表就只做行情"，不影响主流程。
想给全市场补财务数据，建议分批跑，已拉到的会留在缓存里。

**明确不调用**：分钟级行情、实时行情、以及任何需要更高积分的特色数据。
代码里做了白名单硬校验（`config.ALLOWED_APIS` / `FORBIDDEN_APIS`），
误调用会直接抛 `ForbiddenApiError`，不会悄悄把额度烧掉。

> 交易日判断刻意**没有使用 `trade_cal`**（不在授权清单内）：
> 优先用一次区间查询 `daily(start_date=..., end_date=...)` 取 `max(trade_date)`，
> 回退方案才是逐日探测，结果缓存在 `data/cache/_trade_dates.json`。

### 5.2 限流

2000 积分上限 200 次/分钟，本项目每日行情类调用约 2~5 次。
`data/tushare_client.py` 做了两层保护：

- 任意两次调用之间至少间隔 **0.3 秒**（`config.MIN_CALL_INTERVAL`）
- 60 秒滑动窗口计数上限 200 次（`config.RATE_LIMIT_PER_MINUTE`）

失败会自动退避重试（网络错误 3/6/9 秒，频次超限至少等 60 秒）；
但参数类错误（如 `必填参数, ts_code`）会立即上报，不做无意义的重试。

---

## 六、技术指标与筛选（已实现，纯本地计算）

这一层**不做任何网络调用**：输入是 `data/cache/` 里的日线分片，输出是 CSV + JSON。
`--local-only` / `--replay` 模式下任何联网尝试都会抛 `NetworkBlockedError`
（`--self-test` 里断言"指标计算期间 Tushare 调用计数 = 0"）。

### 6.1 模块与列名

| 模块 | 内容 | 新增列 |
| --- | --- | --- |
| `indicators/resample.py` | 日线 → 周线（`W-FRI`）/ 月线（`ME`） | `period_end`（周期末标签）、`trade_date`（周期内最后一个交易日）、OHLCV、`bar_count` |
| `indicators/candles.py` | 锤子线、射击之星 | `hammer`、`shooting_star`、`body_ratio`、`lower_shadow_ratio`、`upper_shadow_ratio` |
| `indicators/patterns.py` | 双重底 / 双重顶 | `double_bottom`、`double_bottom_5d/10d/20d`、`db_low1/db_low2/db_neckline/db_breakout_date/db_gap`（顶部为 `dt_*`） |
| `indicators/momentum.py` | MACD(12/26/9)、KDJ(9/3/3) | `ema_fast/ema_slow/dif/dea/macd`、`rsv/k/d/j/kdj_warmup` |
| `indicators/volume.py` | 换手率、量比 | `turnover`、`turnover_source`、`ma5_vol`（前 5 日均量，不含当日）、`volume_ratio_5` |
| `indicators/resonance.py` | 日/周/月共振 | `resonance_daily/weekly/monthly/dwm`、`ma20`、`weekly_ma20/close/dif/dea/bars`、`monthly_*`、`resonance_bars` |
| `screener/filters/` | 11 条筛选规则 | 见 6.3 |

`indicators/_util.py` 放 EMA/SMA/滚动窗口等纯计算工具；`resample.py` 刻意**不注册**为指标
（它改变行数，不满足"同长度、新增列"的指标契约）。

### 6.2 三条固定的参数口径

1. **双重底/顶只认突破确认**：两个摆动点形成后，必须出现"收盘价突破（跌破）颈线"才算成立；
   只形成两个底、但收盘始终没越过颈线的，一律不计入。
2. **共振用均线 + MACD**：某周期"成立" = `收盘 > MA20` 且 `DIF > DEA`；
   日/周/月三个周期同时成立才是 `resonance_dwm`。
3. **换手率 ≥ 3% 且 ≥ 全市场 80 分位**：阈值取两者的较大值 `max(3.0, q80)`，
   实际阈值写进 funnel JSON 的 `detail.threshold`，可审计。

### 6.3 筛选规则（`@register_filter` 注册，共 11 条）

| 规则名 | 条件 | 默认在链上 |
| --- | --- | --- |
| `turnover` | 换手率 ≥ 3% 且 ≥ 全市场 80 分位 | ✅ |
| `resonance_dwm` | 日 + 周 + 月 同时 收盘>MA20 且 DIF>DEA | ✅ |
| `double_bottom` | 最近 10 根内出现过"双重底收盘突破颈线"，且当前 close > 颈线 | ✅ |
| `resonance_daily` | 仅日线共振 | — |
| `double_top` | 最近 10 根内出现过"双重顶收盘跌破颈线"，且当前 close < 颈线 | — |
| `hammer` / `shooting_star` | 目标交易日当根为锤子线 / 射击之星 | — |
| `macd_bull` | `DIF > DEA` | — |
| `kdj_bull` | `K > D`（可选 `max_j` 过滤超买） | — |
| `volume_surge` | 放量：`volume_ratio_5 ≥ 1.5` | — |
| `volume_shrink` | 缩量：`volume_ratio_5 ≤ 0.7` | — |

#### 量能两条规则的口径（`volume_surge` / `volume_shrink`）

**公式**（与常见软件的"量比"不同，这里刻意用简单口径）：

```
volume_ratio_5 = vol(t) / mean( vol(t-5) … vol(t-1) )      ← 分母不含当日
```

- **分母是"前 5 个交易日"均量，绝不包含当日**。实现上先 `shift(1)` 再 `rolling(5)`；
  若直接 `rolling(5).mean()`，当日成交量会进分母，放量会被系统性低估。
  自检里有硬断言：`vol=[100,100,100,100,100,300]` → 当日比值必须 = **3.0**
  （含当日的错误实现会得 300/140 ≈ 2.14）。
- 对应列：`volume_ratio_5`、`vol`、`ma5_vol`、`threshold`（判定阈值，写进 CSV 便于复核）。
- 参数：`volume_surge(min_ratio=1.5, ma_window=5)`、`volume_shrink(max_ratio=0.7, ma_window=5)`。
- **两者互斥**：同一只股票同一天不可能同时命中（`>=1.5` 与 `<=0.7` 不可能同时成立）。
- **次新股处理**：日线不足 `ma_window + 1` 根时 `volume_ratio_5 = NaN`，
  判为不命中，在 funnel 的 `detail.missing` 里计数，**不抛异常、不阻断**。
- 阈值是**绝对阈值**，命中率会随市场量能状态漂移：2026-10-14 全市场量比中位数仅 0.803
  （当日量能低于前 5 日均量），实测 `volume_surge` 命中 7.5%、`volume_shrink` 命中 31.9%；
  量能平稳的日子两者大致在 15~25% 区间。想要"每天命中率稳定"，用下面的 percentile 模式。

#### 两种阈值模式：`absolute`（默认）/ `percentile`

| 模式 | `volume_surge` | `volume_shrink` | 特点 |
| --- | --- | --- | --- |
| `absolute`（默认） | `volume_ratio_5 ≥ 1.5` | `volume_ratio_5 ≤ 0.7` | 语义直观、跨日可比；命中率随市场量能漂移 |
| `percentile` | `≥ 全市场 80 分位` | `≤ 全市场 20 分位` | **命中率稳定**（各约 20%）；阈值随市场自适应 |

**分位必须在全市场算，不受漏斗上游影响**。这是最容易做歪的地方：filter 的 `mask(df)`
只拿到上游过滤后的子集（例如 `turnover` 之后的 1001 只），若在该子集上算分位，
阈值会随漏斗位置漂移。实现方式是给 `Filter` 加了一个 opt-in 钩子：

```python
# screener/__init__.py
ScreenerEngine.run(universe)  # 开跑前对每条规则调用 flt.bind_universe(universe)
```

`bind_universe()` 只保存**原始全市场截面**的引用，`mask(df)` 契约、AND 链语义都不变；
已有规则默认忽略它。量能规则在 `percentile` 模式下从 `self.universe` 取分布算分位，
funnel 的 `detail` 里会记录 `threshold_source=universe|input` 与 `reference_rows`，
可核对"分位到底在多大的池子上算的"。

若规则被单独使用（没经过引擎、没注入 universe），会自动回退到当前输入，
并在 `threshold_source` 标注 `input`。

**2026-10-14 实测**（5002 只）：

| 模式 | 阈值 | 命中 |
| --- | --- | --- |
| `volume_surge` absolute | 1.5 | 377（7.5%） |
| `volume_surge` percentile | **1.0769**（= 全市场 80 分位） | **1001（20.0%）** |
| `volume_shrink` absolute | 0.7 | 1597（31.9%） |
| `volume_shrink` percentile | **0.6302**（= 全市场 20 分位） | **1001（20.0%）** |

**怎么切换**：

```bat
:: ① 命令行临时覆盖（推荐，改一行不用动代码）
python main.py --local-only --replay 20261014 --filters volume_surge:mode=percentile
python main.py --local-only --replay 20261014 --filters "turnover,volume_shrink(mode=percentile)"

:: ② 改 config.py 全局默认（FILTER_PARAMS 里把 mode 改成 percentile）
"volume_surge": {"mode": "percentile", "min_ratio": 1.5, "surge_percentile": 80, "ma_window": 5}
```

```python
# ③ 代码里直接传参
VolumeSurgeFilter(mode="percentile", surge_percentile=80)      # 80 分位可调
VolumeShrinkFilter(mode="percentile", shrink_percentile=20)    # 20 分位可调
```

两种模式互不干扰：`absolute` 的判定与引入 `mode` 参数之前完全一致
（实测仍为 377 / 1597），自检里有专门的断言覆盖。

默认链由 `config.DEFAULT_FILTER_CHAIN` 决定，顺序有意义：
**`turnover` 必须排第一**，因为它的"全市场 80 分位"分母是当前传入的池，
放到后面算会把分位数抬高。用 `--filters` 可任意覆盖：

```bat
python main.py --local-only --filters turnover,macd_bull,kdj_bull
python main.py --local-only --filters none        :: 不筛选，输出全股票池
```

### 6.4 为什么不会用到未来数据

- 所有均线/EMA/滚动窗口都是**因果**计算（只看当前及之前）；
- 周线/月线用 `merge_asof(direction="backward")` 对齐，匹配键是**周期末标签**：
  周三看盘时，本周 bar 的标签（本周五）还没到，会自动退回用**上一根已收完**的周线；
- 双重底要求突破日 `k >= 第二摆动点 + n`，因为摆动点要 `n` 根之后才能确认；
- 自检里用"把历史截断到第 800 根再重算"验证：同一天的共振取值必须与全量计算一致。

### 6.5 历史长度要求（当前只有 6 个交易日，务必看这段）

| 指标 | 大致需要 | 当前 6 根是否够 |
| --- | --- | --- |
| `turnover` / `volume_ratio_5` | 1~2 根 | ✅ |
| `hammer` / `shooting_star` | 6 根（含 MA5 前值） | ✅ |
| `kdj` | 9 根（不足也给近似值） | ⚠️ 近似 |
| `macd` 预热完成 | 35 根 | ❌ |
| 日线 `ma20` / `resonance_daily` | 20 根 | ❌ |
| 周线 `ma20` | ≈100 根 | ❌ |
| 月线 `ma20`（`resonance_dwm` 前提） | ≈420 根 | ❌ |

**所以现在跑默认链会得到 `5002 → 1001 → 0 → 0`：换手率筛出 1001 只，共振那步全灭。**
这不是 bug —— funnel JSON 里的 `weekly_history_missing` / `monthly_history_missing`
会明确写出"历史不足"。要看到真实共振结果，先补历史（**这一步需要联网**）：

```bat
python main.py --cli --backfill          :: 回补最近 30 个交易日
```

`--backfill` 受 `config.DAILY_BACKFILL_DAYS`（30）限制；要一年以上的日线，
把该常量调大后重跑。补完再用 `--local-only`，就完全回到"纯本地"路径。

### 6.6 产物（`output/reports/`）

| 文件 | 内容 |
| --- | --- |
| `screen_<交易日>_<时间戳>.csv`（回放为 `replay_...csv`） | 命中标的明细（关键列排前面，含全部指标列） |
| `screen_...json` / `replay_...json` | 运行摘要（股票池、命中数、规则名、同步信息） |
| `funnel_...json` | **逐规则漏斗**：每步的 `in/out/dropped` + 生效参数 + 审计明细（分位阈值等） |

### 6.7 一个命名坑：`volume_ratio` ≠ `volume_ratio_5`

`daily_basic.volume_ratio` 是 Tushare 的"量比"（每分钟均量口径）；
本项目按需求算的是 `vol / MA5(vol)`，为了不覆盖原始字段，列名用 **`volume_ratio_5`**。
两个值都能在 CSV 里看到，别混用。

### 6.8 性能与"离线"的确切含义

**性能**（实测，5000 只全市场）：

| 场景 | 端到端耗时 |
| --- | --- |
| 9 个交易日历史（当前缓存） | ≈ **105 秒** |
| 历史越长 | 近似线性增长（周线/月线重采样 + 形态扫描是主要成本） |

指标计算是**按股票逐只循环**的（形态/共振本质上需要每只股票独立的时间序列），
所以耗时与 `股票数 × 历史根数` 成正比。想快速迭代可以：

```bat
python main.py --local-only --history-bars 30 --filters turnover
```

**`--offline` 与 `--local-only` 的区别**：

| 参数 | 行为 |
| --- | --- |
| `--offline` | 数据层禁止联网（`--cli` 会停在"数据同步"并报 `离线模式不允许调用 xxx`），用于自检 |
| `--local-only` | **整个流程**读缓存 + `local_only` 守卫，任何联网直接抛 `NetworkBlockedError` |
| `--replay YYYYMMDD` | 等于 `--local-only` + 指定历史日期，用于复盘"那天如果跑会选出什么" |

### 6.9 评分与推荐分层（`--score`，可选）

默认**关闭**；不加 `--score` 时行为与之前完全一致（布尔漏斗 + 按换手率排序）。

**打分表（满分 100）**

| 分项 | 条件 | 满分 |
| --- | --- | --- |
| 趋势共振 | 日周月共振 `resonance_dwm`；否则仅日线共振 | **30** / 12（二选一，不叠加） |
| 双重底 | 最近 10 日突破颈线 **且** 当前仍在颈线上方 | 20 |
| 放量 | `volume_surge`（默认 ≥1.5；切成 percentile 时 ≥80 分位） | 12 |
| MACD | `DIF > DEA` | 10 |
| KDJ | `K > D` | 8 |
| 活跃度 | 换手率 ≥8% / ≥5% / ≥3% / ≥2% → 12 / 10 / 8 / 4 | 12 |
| 锤子线 | `hammer` | 8 |

**分档（`config.SCORE_TIERS` / `config.TIER_CAPS`）**

| 档位 | 分数 | 名额上限 |
| --- | --- | --- |
| 核心 | ≥85 | 5 |
| 观察 | ≥75 | 10 |
| 候选 | ≥65 | 15 |
| 不入榜 | <65 | — |

另加一道门槛：**至少命中 2 个独立信号**（`config.SCORE_MIN_HIT`），避免"单项高分"混进榜。

**用法**

```bat
:: 全市场评分排行（不带 --filters 时不做漏斗，直接输出排行）
python main.py --local-only --score

:: 只看前 20，或自定义门槛
python main.py --local-only --score --top 20
python main.py --local-only --score --min-score 70 --no-caps

:: 先跑漏斗，再给筛出来的股票打分排序
python main.py --local-only --score --filters turnover,resonance_dwm
```

**输出**

- CSV 增加 `score` / `tier` / `hit_count` 三列，外加 7 个 `pts_*` 分项列——
  一行就能看出"这 92 分是怎么来的"；
- funnel JSON 的 `score` 段包含完整权重表、分档门槛、档位统计与截断数。

**2026-10-14 实测**（5004 只 / 746 个交易日历史）：

```
核心 3 只 / 观察 7 只 / 候选 77 只 → 档位裁剪后入榜 25 只（截断 62，低于门槛 4917）

#   score  档位   代码        名称      收盘    换手%  命中
1    92.0  核心   605577.SH  龙版传媒  18.95  14.82   6
2    92.0  核心   300006.SZ  莱美药业   7.90  16.29   6
3    92.0  核心   002811.SZ  郑中设计  20.70  13.33   6
4    84.0  观察   600938.SH  中国海油  35.75   2.46   6
```

**改权重**：编辑 `config.SCORE_WEIGHTS`，注意**满分必须保持 100**
（自检里有"权重累加 = 100"的断言，改完跑 `python -m indicators.selftest` 自动校验）。
`--min-score` 只调"候选"门槛，核心/观察门槛仍在 `config.SCORE_TIERS`。

---

## 七、数据更新节奏

Tushare 的各类数据不是收盘那一刻同时到位的，而是**分时段入库**。这张表决定了本项目
的触发时间——数据还没入库就跑，拿到的只会是残缺或上一交易日的结果。

| 数据 | 入库时间 | 对应触发 |
| --- | --- | --- |
| 日线行情（`daily`） | 交易日 **15:00~17:00** 入库 | 17:30 辅助触发 / 22:30 主触发 |
| 每日指标（`daily_basic`：PE/PB/换手率/市值） | 交易日 **17:00~19:00** 更新 | 22:30 主触发 |
| 财务数据（`income` / `balancesheet` / `cashflow`） | 披露当晚 **21:00~23:00** 更新 | 22:30 主触发 |
| A 股清算 | **18:00~22:00** 期间数据逐步稳定 | 22:30 最稳 |

**为什么主触发定在 22:30：**

- 17:30 时 `daily_basic` 才刚开始更新（17:00~19:00），换手率、市值可能缺失或未稳定；
- 财务数据要 21:00 之后才开始在当晚更新，22:30 才能拿到较完整的披露结果；
- 18:00~22:00 是 A 股清算时段，行情与指标会被逐步修正，22:30 已基本稳定；
- 所以 22:30 是"当天所有数据都到位"的最早稳妥时点，作为**主触发**
  （`full` 模式，含三大报表）；
- 17:30 作为**辅助触发**（`quote` 模式，纯行情），牺牲 `daily_basic` 的完整性，
  换取提前约 5 小时看到行情结果。

**盘中一律不触发**：09:15~15:05 之间 `daily` 尚未入库，跑出来的只会是上一个交易日的数据，
容易误判。`trigger/scheduler.py` 在 `--scheduled` 模式下会直接拒绝执行；
手工运行时也会给出明确告警。

**分阶段到位带来的实际影响**：

- 想在 21:00 之前手工跑 `--mode full`，程序会提示"早于财务数据可用的 21:00"并拒绝；
  提前看行情请用 `--mode quote`；
- 17:30 那次触发即使成功，结果里也不含财务字段——这是设计预期，不是故障。

---

## 八、已知边界（实测踩到的三处 Tushare 限制）

这三条都是**实测确认**的上游限制，不是本项目的 bug；代码里都已经做了应对。
写在这里是为了避免以后重复踩坑。

### 8.1 三大报表强制要求 `ts_code`，不能按 `period` 整表拉

- **现象**：官方文档说可以只传 `period` 一次拉全市场，但实测当前账号/接口版本直接报
  `必填参数, ts_code`，`income` / `balancesheet` / `cashflow` 三个接口都如此。
- **应对**：先照常尝试整表按报告期拉取（若哪天权限放开，3 次调用即可拿全市场，最省额度）；
  一旦遇到该错误**且**传了 `--financial-limit N`，就自动退化为
  **按当日 `total_mv` 降序逐只拉取前 N 只**（保证先拿到大票，而不是随机抓到一批小票），
  结果按 `ts_code` 合并进**同一个报告期分片**
  （如 `data/cache/income/20260630.csv`），下次直接命中缓存，不会重复调用。
- **代价**：逐只拉取是每只 3 次调用，300 只 ≈ 900 次调用 ≈ 5 分钟。
  默认 `--financial-limit 0`，即"整表拿不到就不逐只拉"，只做行情与指标，不影响主流程。
  时间预算见 5.1 节。
- **调用示例**：`python main.py --cli --mode full --financial-limit 300`

### 8.2 交易日探测会被上游偶发空表毒化

- **现象**：用 `daily(trade_date=YYYYMMDD)` 逐天探测交易日时，上游会偶发返回空表
  （同一日期稍后重试又能正常返回数据）。若把这个空表当成"非交易日"写进缓存，
  真正的交易日就会被永久标记成休市，之后每次运行都会跳过当天，且不会报错。
- **应对**：双管齐下
  1. 最近交易日改为**一次区间查询**
     `daily(start_date=..., end_date=..., limit=6000)` 取 `max(trade_date)`，
     不再逐天试探（逐日探测仅作为回退路径）；
  2. "非交易日"的否定结论只对**距今 10 天以上**的历史日期落缓存
     （`trigger/calendar.py` 的 `NEGATIVE_CACHE_LAG_DAYS`），近期日期每次都重新探测，
     这样偶发空表不会留下永久性的错误结论。
- **兜底**：真怀疑日历有问题，删掉 `data/cache/_trade_dates.json` 让它重新探测即可。

### 8.3 tushare 的 endpoint 不能改成 `api.tushare.pro`

- **现象**：当前 tushare 版本默认请求 `http://api.waditu.com/dataapi/<接口名>`。
  曾经改成看起来更"官方"的 `http://api.tushare.pro`，结果是
  **HTTP 请求成功、返回 `code == 0`，但 `items` 为空**——所有接口一律返回空表，
  且不抛任何异常，极难排查（当时被误判成"非交易日"，与 7.2 叠加后尤其隐蔽）。
- **应对**：不再覆盖库的 endpoint，改用库默认地址，并在
  `data/tushare_client.py` 的 `_get_pro()` 与 `config.TUSHARE_API_URL` 处加了注释警示。
- **排查提示**：若发现"所有接口都返回空表但没有任何报错"，
  第一件事就是确认有没有人改过 endpoint（包括环境变量、代理、hosts）。

### 8.4 形态类信号采用"当前仍在颈线正确一侧"口径

- **背景**：早期的形态筛选只要求"最近 10 个交易日内曾突破（跌破）颈线"，
  不强求今天仍在颈线的正确一侧。2026-10-14 实测：
  双底 496 只里有 **279 只（56%）** 突破后又跌回颈线下方；
  双顶 1014 只里有 **182 只（18%）** 跌破后又反弹回颈线上方。
  这些是"已失效"的形态，按信号交易会被反复打脸。
- **现在的口径**（默认开启，见 `config.PATTERN_REQUIRE_ABOVE_NECKLINE` /
  `PATTERN_REQUIRE_BELOW_NECKLINE`）：
  - 双重底：`当前 close > db_neckline` 才算命中（`require_above_neckline=True`）；
  - 双重顶：`当前 close < dt_neckline` 才算命中（`require_below_neckline=True`）。
- **同一口径下命中数会显著收敛**：双底 496 → 217，双顶 1014 → 832。
- **可关闭**：需要看"曾突破"的全集时，显式传参即可：
  ```python
  from screener.filters.patterns import DoubleBottomFilter
  DoubleBottomFilter(require_above_neckline=False)   # 恢复旧口径
  ```
  funnel JSON 的 `detail.neckline_filtered_out` 会记录被该口径剔除的只数，
  便于对比两种口径的差异。

---

## 九、整包拷贝 / 换机器

整个 `cn-stock-screener` 文件夹是一个自包含单元，可以：

```bat
:: 拷到 U 盘、另一台电脑、另一个盘符
robocopy D:\cn-stock-screener E:\backup\cn-stock-screener /E
```

在新位置只需要做两件事：

1. 新机器上 `python -m pip install -r requirements.txt`
2. 在项目根目录重新放好 `.env`（Token 不随文件夹走，需要自己填）

然后在新位置重新注册计划任务：

```bat
E:\backup\cn-stock-screener\scripts\register_task.bat
```

已经拉过的 `data/cache/` 可以一起拷走，程序会直接命中缓存、不重复调用接口。

---

## 十、常见问题

**Q1：`--self-test` 报 "Tushare Token 已配置" 失败？**
`.env` 还没填或还是模板里的占位文字。把 `.env.example` 复制成 `.env` 并填入 Token。
离线环境可用 `python main.py --self-test --offline` 跳过 Token 与联网检查。

**Q2：提示「非交易日，跳过」或「股票池为空」？**
- 周末/节假日：正常行为，不需要处理；
- 交易日的 15:00 之前：当天日线还没入库，程序会回退到上一个交易日；
- 交易日的 17:00 之前：`daily_basic` 还没入库，此时只能拿到行情部分。

想强制指定日期：`python main.py --cli --date 20250102`。

**Q3：接口报「无访问权限 / 积分不足」或「必填参数, ts_code」？**
- 本项目只用了 2000 积分范围内的接口；若报权限错误，通常是账号积分不足；
- `必填参数, ts_code` 是三大报表的已知限制（见 5.1 节与 8.1 节），
  加 `--financial-limit N` 即可自动切换为逐只拉取；不加也能正常跑行情部分；
- 遇到这类错误程序只记录 warning 并继续跑完，不会中断。

**Q4：交易日的判断会不会误判？**
不会长期误判。最近交易日优先用**一次区间查询**直接取 `max(trade_date)`，
逐日探测只作回退；"非交易日"的否定结论只对距今 10 天以上的历史日期写缓存，
避免网络偶发返回空表时把交易日记成休市。
真遇到异常，删掉 `data/cache/_trade_dates.json` 让它重新探测即可。

**Q5：计划任务没反应？**
1. 双击 `scripts\run_screener.bat manual` 看能否手动跑通；
2. `schtasks /Query /TN CNStockScreener_Daily_2230 /FO LIST` 看"上次运行结果"；
3. 看 `output\logs\` 下当天的日志；
4. 如果是 "Access is denied"，用管理员身份重跑 `register_task.bat`。

**Q6：想清空缓存重拉？**
直接删掉 `data\cache\` 下对应的分片文件或整个子目录，下次运行会自动重新拉取。
保留 `_meta.json` 与 `_trade_dates.json` 也没问题。

**Q7：能不能不放计划任务，用别的方式定时？**
可以，任何能执行 `scripts\run_screener.bat full` 的定时器都行
（比如任务计划程序里手动新建、或者 Jenkins / 青龙面板等）。

---

## 十一、设计约定（给第二阶段的自己 / 同事看）

### 11.1 数据层

```python
from data import DataLoader

loader = DataLoader()
result = loader.sync(mode="full")          # 拉数据 + 写缓存
print(result.describe())                   # 一行摘要
universe = loader.load_universe(result.trade_date)   # 从缓存构建股票池（不联网）
```

`universe` 的列 = `stock_basic`（ts_code/name/industry/area/market/list_date…）
+ `daily`（open/high/low/close/pre_close/pct_chg/vol/amount）
+ `daily_basic`（turnover_rate/pe/pe_ttm/pb/total_mv/circ_mv…）。

### 11.2 指标接口（`indicators/`）

```python
from indicators import register, compute_panel

@register("ma", "简单移动平均", params={"periods": (5, 20, 60)})
def add_ma(df, periods=(5, 20, 60)):
    out = df.copy()
    for n in periods:
        out[f"ma{n}"] = out["close"].rolling(n).mean()
    return out

panel = compute_panel(universe)   # 按 ts_code 分组计算后拼回
```

约定：函数签名 `f(df, **params) -> DataFrame`（同长度、新增列、不改入参）；
列名小写蛇形；组合指标统一命名（`dif/dea/macd`、`rsi6/rsi14`、`k/d/j`…）。
参数默认值集中在 `config.py`（`MA_PERIODS`、`MACD_*`、`RSI_PERIODS`…）。

### 11.3 筛选接口（`screener/`）

```python
from screener import Filter, register_filter, ScreenerEngine

@register_filter
class MinTurnover(Filter):
    name = "min_turnover"
    def mask(self, df):
        return df["turnover_rate"].astype(float) >= self.params.get("threshold", 0.5)

engine = ScreenerEngine(filters=[MinTurnover(threshold=1.0)])
res = engine.run(panel, trade_date="20250102", mode="full")
res.funnel        # 每条规则的 输入/输出/剔除 统计（可审计的漏斗）
```

### 11.4 触发接口（`trigger/`）

```python
from trigger import Scheduler

d = Scheduler().decide(enforce_window=True)   # 计划任务场景
if d.should_run:
    ...   # d.mode / d.trade_date / d.reason
```

---

## 十二、免责声明

本项目只做数据整理与技术指标筛选，**不构成任何投资建议**。
数据来自 Tushare Pro，其准确性与及时性由数据源决定；
因使用本程序产生的任何投资决策及其后果，由使用者自行承担。
