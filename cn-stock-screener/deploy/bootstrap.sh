#!/usr/bin/env bash
# =============================================================================
#  cn-stock-screener 一键部署（Ubuntu / Debian 22.04+）
#
#  用法（在服务器上，压缩包已解压到任意目录后）：
#      cd /opt/cn-stock-screener
#      sudo bash deploy/bootstrap.sh
#
#  它会做这些事：
#      1. 检查 Python / 装依赖
#      2. 建服务账号 stock
#      3. 让你填 Tushare Token（写进 .env，权限 600）
#      4. 首次全量拉数据（约 770 次 API 调用，5~10 分钟）
#      5. 装 systemd：网页服务 + 每晚 22:30 选股 + 交易时段每 15 分钟扫描
#      6. 打印访问地址
#
#  不做什么：不装 Nginx、不配域名证书（见 deploy/README.md 第七节）
# =============================================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="stock"
WEB_PORT="${WEB_PORT:-8000}"

info()  { printf '\033[36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn()  { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die()   { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "请用 root 运行：sudo bash deploy/bootstrap.sh"

info "项目目录：$APP_DIR"

# ---------------------------------------------------------------- 1. 依赖
info "检查 Python…"
command -v python3 >/dev/null || die "没有 python3，先执行：apt update && apt install -y python3 python3-pip"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "Python 版本太低（需要 3.9+）：$(python3 -V)"

info "安装依赖（约 1 分钟）…"
python3 -m pip install --quiet --upgrade pip
python3 -m pip install --quiet -r "$APP_DIR/requirements.txt"

# ---------------------------------------------------------------- 2. 用户
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  info "创建服务账号 $SERVICE_USER"
  useradd -m -s /bin/bash "$SERVICE_USER"
fi
mkdir -p "$APP_DIR"/{data/cache,output/reports,output/logs}
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

# ---------------------------------------------------------------- 3. Token
if [[ -f "$APP_DIR/.env" ]] && grep -q '^TUSHARE_TOKEN=[0-9a-f]\{30,\}' "$APP_DIR/.env"; then
  info "已有 .env，跳过 Token 输入"
else
  echo
  echo "请输入你的 Tushare Token（在 https://tushare.pro/user/token 获取）"
  read -rp "TUSHARE_TOKEN: " TOKEN
  [[ ${#TOKEN} -ge 30 ]] || die "Token 长度不对（应 40 位以上）"
  umask 077
  cat > "$APP_DIR/.env" <<EOF
# 由 bootstrap.sh 生成，权限 600，不要提交到任何仓库
TUSHARE_TOKEN=$TOKEN
EOF
  chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  info ".env 已写入（权限 600）"
fi

# ---------------------------------------------------------------- 4. 首次取数
info "首次全量拉取历史（约 770 次调用，5~10 分钟，请耐心）…"
sudo -u "$SERVICE_USER" bash -lc "cd '$APP_DIR' && PYTHONUTF8=1 python3 -m main --cli --mode full" \
  || warn "首次同步未完全成功，可稍后手动执行：python3 -m main --cli --mode full"
info "跑一次评分排行，生成首份榜单…"
sudo -u "$SERVICE_USER" bash -lc "cd '$APP_DIR' && PYTHONUTF8=1 python3 -m main --cli --local-only --score" \
  || warn "首次评分失败，稍后可手动重跑"

# ---------------------------------------------------------------- 5. systemd
info "安装 systemd 服务与定时器…"
install -m 644 "$APP_DIR/deploy/cn-stock-screener-web.service"      /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/cn-stock-screener-nightly.service"  /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/cn-stock-screener-nightly.timer"    /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/cn-stock-screener-scan.service"     /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/cn-stock-screener-scan.timer"       /etc/systemd/system/

# 用实际路径与端口覆盖模板里的占位
sed -i "s#/opt/cn-stock-screener#$APP_DIR#g" /etc/systemd/system/cn-stock-screener-*.service
sed -i "s#--port 8000#--port $WEB_PORT#g"   /etc/systemd/system/cn-stock-screener-web.service
if [[ -f "$APP_DIR/.env" ]]; then
  TOKEN_LINE="Environment=TUSHARE_TOKEN=$(grep '^TUSHARE_TOKEN=' "$APP_DIR/.env" | cut -d= -f2-)"
  for f in /etc/systemd/system/cn-stock-screener-nightly.service \
           /etc/systemd/system/cn-stock-screener-scan.service; do
    sed -i "s#^Environment=TUSHARE_TOKEN=.*#$TOKEN_LINE#" "$f"
  done
fi

systemctl daemon-reload
# 生成随机登录密码（不要用模板里的 change-me-please）
WEB_PASS="$(python3 -c 'import secrets,string; print("".join(secrets.choice(string.ascii_letters+string.digits) for _ in range(14)))')"
sed -i "s#^Environment=WEB_PASS=.*#Environment=WEB_PASS=$WEB_PASS#" \
  /etc/systemd/system/cn-stock-screener-web.service

systemctl daemon-reload
systemctl enable --now cn-stock-screener-web.service
systemctl enable --now cn-stock-screener-nightly.timer
systemctl enable --now cn-stock-screener-scan.timer

# ---------------------------------------------------------------- 6. 结果
sleep 2
if systemctl is-active --quiet cn-stock-screener-web; then
  IP=$(hostname -I | awk '{print $1}')
  echo
  echo "=============================================================="
  echo " 部署完成"
  echo "  本机访问   : http://127.0.0.1:$WEB_PORT/"
  echo "  局域网/公网: http://$IP:$WEB_PORT/"
  echo
  echo "  登录用户名 : admin"
  echo "  登录密码   : $WEB_PASS"
  echo "  （密码随时可改：nano /etc/systemd/system/cn-stock-screener-web.service"
  echo "    改 Environment=WEB_PASS= 后 systemctl daemon-reload && systemctl restart cn-stock-screener-web）"
  echo
  echo "  网页服务   : systemctl status cn-stock-screener-web"
  echo "  日志        : journalctl -u cn-stock-screener-web -f"
  echo "  定时器      : systemctl list-timers | grep stock"
  echo
  echo "  下一步（公网+域名+HTTPS）：见 deploy/README.md 第七节"
  echo "  记得在云控制台的安全组放行 $WEB_PORT（或 80/443）"
  echo "=============================================================="
else
  warn "网页服务没起来，看日志：journalctl -u cn-stock-screener-web -n 50"
fi
