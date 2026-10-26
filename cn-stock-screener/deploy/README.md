# 部署到云端（网页版）—— 电脑关机也能看

**核心前提**：手机要随时能打开，就必须有一台 **24 小时开机、独立于你电脑** 的机器。
本地 `--serve`、内网穿透都不满足这条（电脑一关就断）。

## 零、选一台 24/7 在线的机器

| 方案 | 一次性成本 | 月成本 | 优点 | 缺点 |
| --- | --- | --- | --- | --- |
| **云服务器（推荐）** | 0 | **¥60~100** | 买完即用，机房稳定，有公网 IP | 长期付费 |
| 家里旧笔记本/迷你主机 | 已有 | 电费 ~¥15 | 一次性投入，配置随便 | 要一直开着，家里断电/断网就没了；公网还需内网穿透 |
| 树莓派 4B/5（4G 内存） | ~¥500 | 电费 ~¥5 | 极省电，安静 | 算得慢（一次评分 20~40 分钟），需自己装系统 |

**配置要求**：`2 核 4G 内存 + 20G 盘` 起步。
内存不能低于 4G —— 全市场评分峰值 2.4 GB，2G 机器会被 OOM kill。
CPU 弱一点只是慢（树莓派也能跑，就是慢），不影响正确性。

## 一、一条命令部署

```bash
# 1) 本地：把压缩包传上去（或用 scp / 宝塔面板上传）
scp cn-stock-screener-exe-20261023.zip root@你的服务器:/opt/

# 2) 服务器上：解压
cd /opt && apt update && apt install -y unzip
unzip cn-stock-screener-exe-20261023.zip && mv cn-stock-screener-exe cn-stock-screener

# 3) 跑一键脚本（会问你要 Tushare Token）
cd /opt/cn-stock-screener && bash deploy/bootstrap.sh
```

脚本会自动：装依赖 → 建服务账号 → 写 `.env`（权限 600）→ **首次全量拉 750 天历史**（约 5~10 分钟）→
跑首份榜单 → 装 3 个 systemd 单元 → 生成随机登录密码 → 打印访问地址。

跑完你会看到：

```
 本机访问   : http://127.0.0.1:8000/
 局域网/公网: http://1.2.3.4:8000/
 登录用户名 : admin
 登录密码   : xxxxxxxxxxxxxx
```

**最后一步**：去云控制台的「安全组 / 防火墙」放行 8000 端口（或后面配 80/443）。

## 二、手机怎么用

直接用手机浏览器打开 `http://你的IP:8000/`，输入账号密码即可。
页面每 60 秒自动刷新，表格可左右滑动。

想更像 App：Chrome/Safari 里「添加到主屏幕」，就有了图标，点开全屏。

## 三、买什么（旧表，保留作参考）

| 方案 | 配置 | 价格 | 说明 |
| --- | --- | --- | --- |
| **轻量应用服务器（推荐）** | 2 核 2G / 40G 系统盘 | 约 ¥60~100/月 | 阿里云/腾讯云，国内机房，访问 Tushare 和行情源最快 |
| 海外 VPS | 1 核 1G | $4~6/月 | Hetzner/Vultr，便宜但抓国内行情可能慢或不稳 |
| 内网穿透 | 家里电脑 + Cloudflare Tunnel | 免费 | 不必买服务器，但电脑得一直开着 |

**磁盘**：缓存约 1 GB，加上代码和产物，留 5 GB 足够。
**内存**：跑一次全市场评分峰值约 2.4 GB —— **选 2G 会吃紧，建议 4G**。

## 二、上传代码

```bash
# 本地打包（排除缓存也能跑，首次会全量下载约 770 次 API 调用）
scp -r ./cn-stock-screener root@your-server:/opt/
ssh root@your-server
mkdir -p /opt/cn-stock-screener && mv /opt/cn-stock-screener*/* /opt/cn-stock-screener/
useradd -m stock && chown -R stock:stock /opt/cn-stock-screener
```

## 三、装依赖

```bash
apt update && apt install -y python3 python3-pip
cd /opt/cn-stock-screener
python3 -m pip install -r requirements.txt
```

## 四、配 Token（用环境变量，别写进代码）

```bash
# 方式 A：写 .env（最简单）
cp .env.example .env && nano .env      # 填 TUSHARE_TOKEN

# 方式 B：systemd 里注入（见 deploy/*.service 的 Environment=TUSHARE_TOKEN=...）
```

## 五、起服务

```bash
cp deploy/cn-stock-screener-web.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cn-stock-screener-web
systemctl status cn-stock-screener-web        # 应显示 active (running)
curl http://127.0.0.1:8000/healthz            # 返回 ok
```

## 六、定时任务（替代 Windows 任务计划）

```bash
cp deploy/cn-stock-screener-nightly.* /etc/systemd/system/
cp deploy/cn-stock-screener-scan.* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cn-stock-screener-nightly.timer
systemctl enable --now cn-stock-screener-scan.timer
systemctl list-timers | grep stock            # 看下次触发时间
```

如果你想用 cron 也行：

```cron
45,0,15,30 9-11,13-15 * * 1-5  cd /opt/cn-stock-screener && /usr/bin/python3 -m main --scan >> /var/log/scan.log 2>&1
30 22 * * 1-5                  cd /opt/cn-stock-screener && /usr/bin/python3 -m main --cli --mode full --score >> /var/log/nightly.log 2>&1
```

## 七、加域名和 HTTPS（可选但强烈建议）

```bash
apt install -y nginx certbot python3-certbot-nginx
cp deploy/nginx.conf /etc/nginx/sites-available/cn-stock-screener
ln -s /etc/nginx/sites-available/cn-stock-screener /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot --nginx -d your-domain.com          # 自动签证书 + 改配置
```

## 八、安全清单（公网必看）

- [ ] **必须**改掉 `WEB_PASS`，不要用默认的 `change-me-please`
- [ ] 用 HTTPS（否则密码是明文传输）
- [ ] 只开 80/443，不要暴露 8000
- [ ] 不要用 root 跑服务（service 里已经指定 `User=stock`）
- [ ] Token 只放环境变量或 `.env`，**不要提交到任何仓库**

## 九、接口

| 路径 | 内容 |
| --- | --- |
| `/` | 网页看板（每 60 秒自动刷新） |
| `/api/ranking.json` | 日线榜 |
| `/api/intraday.json` | 盘中榜（行业优先 + 15 分钟指标） |
| `/api/industry.json` | 行业强度 |
| `/api/alerts.json` | 今日告警 |
| `/api/all.json` | 全部数据（一次拿完） |
| `/healthz` | 健康检查 |


---

# 方案 B：完全免费的 GitHub

不想每月付 60~100 块，可以用 **GitHub Actions（跑计算）+ GitHub Pages（托管网页）**，成本 0。

## 能做到

| 项 | 情况 |
| --- | --- |
| 电脑关机手机能看 | ✅ 可以 |
| 费用 | ✅ 0（公开仓库 Actions 免费不限时长；私有仓库每月 2000 分钟） |
| 托管 | ✅ `https://你的用户名.github.io/仓库名/` |
| 支持 API | ✅ `api/*.json` 一起发布 |

## 做不到 / 有坑

| 坑 | 说明 |
| --- | --- |
| **定时不准** | GitHub 的 cron 最小 5 分钟，且高峰期会延迟 5~15 分钟，偶尔直接跳过。**不能当"准点盯盘"用** |
| **缓存要绕** | 1 GB 日线缓存不能进 git。用 Actions 缓存滚动保存（见模板），只保留最近几份 |
| **每次要装环境** | 每次运行都要 pip install pandas 等，约 40~60 秒，加上扫描总共 2~4 分钟 |
| **公开仓库** | Pages 只有公开仓库免费；私有仓库需要 GitHub Pro（$4/月） |
| **60 天休眠** | 公开仓库 60 天没有提交，定时任务会被自动停用 |
| **分钟数** | 私有仓库 2000 分钟/月。盘中 16 次/天 × 21 天 × 3 分钟 ≈ 1000 分钟，够用但没富余 |

## 怎么配

1. 建仓库，把整个项目推上去（**`.env` 已在 .gitignore 里，不会被推**）
2. `Settings → Secrets and variables → Actions`，加 `TUSHARE_TOKEN`
3. `Settings → Pages`，Source 选 `Deploy from a branch`，分支 `main`、目录 `/docs`
4. 把 `deploy/github-actions.yml` 复制到 `.github/workflows/stock.yml`
5. 手动触发一次（Actions → Run workflow），等它跑完
6. 手机打开 `https://你的用户名.github.io/仓库名/`

## 首份榜单怎么来

第一次跑是非交易时段会走"收盘选股"，会全量拉 750 天历史（约 770 次调用，5~10 分钟），
然后 `--publish docs` 生成网页并提交。之后每次增量。

## 和云服务器怎么选

| 你要的 | 选 |
| --- | --- |
| 免费、能看就行、每天看几次 | **GitHub** |
| 准点 15 分钟、稳定不掉 | **云服务器 ¥60~100/月** |
| 不想代码公开 | 云服务器，或私有仓库 + Cloudflare Pages（免费但需中转） |
