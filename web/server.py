"""
web.server — 把本地结果渲染成网页（纯标准库，零新增依赖）

为什么用 http.server 而不是 FastAPI/Flask
------------------------------------------
项目一直只有 3 个依赖（tushare / pandas / python-dotenv）。
个人看盘不需要框架：几个只读页面 + 几个 JSON 接口，标准库足够，
部署到服务器时也不用装额外的东西。

页面
----
    GET /                 总览：日线榜 + 盘中榜 + 行业强度 + 今日告警
    GET /api/ranking.json 日线榜（最近一次 --score 产物）
    GET /api/intraday.json 盘中榜（行业优先选股 + 15分钟指标）
    GET /api/industry.json 行业强度
    GET /api/alerts.json  今日告警
    GET /healthz          健康检查（部署用）

安全
----
- 默认只监听 127.0.0.1；
- 设了环境变量 ``WEB_USER`` / ``WEB_PASS`` 就启用 HTTP Basic 认证；
- 公网部署**务必**开认证或放在 Nginx + HTTPS 后面。
"""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import config

logger = logging.getLogger(__name__)

#: 统一用北京时间（GitHub runner 是 UTC，不处理会导致"8 小时前"这种错觉）
TZ_CN = timezone(timedelta(hours=8))

#: 页面自动刷新间隔（秒）
REFRESH_SECONDS = 60


# --------------------------------------------------------------------------
# 数据读取
# --------------------------------------------------------------------------
#: 各表只展示这些列（原始列名 -> 中文表头）；不在表里的列不显示，避免 100+ 列糊成一片
COLUMN_SPEC = {
    "ranking": [
        ("score", "评分"), ("tier", "档位"), ("signal", "信号"), ("state", "状态"),
        ("ts_code", "代码"), ("name", "名称"), ("industry", "行业"),
        ("close", "收盘"), ("pct_chg", "涨跌%"), ("ma20", "MA20"),
        ("turnover", "换手%"), ("volume_ratio_5", "量比"),
        ("macd_side", "MACD"), ("kdj_side", "KDJ"), ("hit_count", "命中"),
    ],
    "intraday": [
        ("rank", "#"), ("industry", "行业"), ("ts_code", "代码"), ("name", "名称"),
        ("score", "盘中分"), ("price", "现价"), ("bar_close", "K线收"),
        ("bar_pct", "15分%"), ("pct_chg", "今日%"), ("dist_ma20_pct", "距MA20"),
        ("vr_15m", "量比"), ("macd_15m", "MACD"), ("kdj_15m", "KDJ"),
    ],
    "industry": [
        ("industry_rank", "#"), ("industry", "行业"), ("count", "家数"),
        ("avg_pct", "平均涨幅"), ("up_ratio", "上涨占比"), ("best", "领涨"),
        ("amount", "成交额(万)"),
    ],
    "alerts": [
        ("fired_at", "时间"), ("ts_code", "代码"), ("name", "名称"),
        ("rule_label", "规则"), ("level_label", "级别"), ("message", "说明"),
    ],
}


def _select(cols: list[str], rows: list[list[str]], key: str) -> tuple[list[str], list[list[str]]]:
    """按白名单裁剪列并翻译表头。"""
    spec = COLUMN_SPEC.get(key) or []
    if not spec or not cols:
        return cols, rows
    index = {c: i for i, c in enumerate(cols)}
    keep = [(c, label) for c, label in spec if c in index]
    if not keep:
        return cols, rows
    out_cols = [label for _, label in keep]
    out_rows = [[row[index[c]] for c, _ in keep] for row in rows]
    return out_cols, out_rows


def _latest_scored_csv() -> Path | None:
    d = Path(config.REPORT_DIR)
    if not d.is_dir():
        return None
    for p in sorted([p for p in d.glob("*.csv") if p.name.startswith(("screen_", "replay_"))],
                    key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
        try:
            head = p.open(encoding="utf-8-sig").readline()
        except Exception:  # noqa: BLE001
            continue
        if "score" in head:
            return p
    return None


def _read_csv(path: Path | None, limit: int = 60) -> tuple[list[str], list[list[str]]]:
    if path is None or not path.is_file():
        return [], []
    try:
        import pandas as pd

        df = pd.read_csv(path).head(limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 %s 失败：%s", path, exc)
        return [], []
    return [str(c) for c in df.columns], [
        ["" if v != v else (f"{v:.2f}" if isinstance(v, float) else str(v)) for v in row]
        for row in df.itertuples(index=False, name=None)
    ]


def collect() -> dict:
    """汇总页面需要的全部数据。"""
    ranking_path = _latest_scored_csv()
    ranking_cols, ranking_rows = _read_csv(ranking_path, limit=60)
    ranking_cols, ranking_rows = _with_sides(ranking_cols, ranking_rows)
    ranking_cols, ranking_rows = _select(ranking_cols, ranking_rows, "ranking")
    intraday_cols, intraday_rows = _read_csv(
        Path(config.REPORT_DIR) / "intraday_rank_latest.csv", limit=40)
    intraday_cols, intraday_rows = _select(intraday_cols, intraday_rows, "intraday")
    industry_cols, industry_rows = _read_csv(
        Path(config.REPORT_DIR) / "intraday_industry_latest.csv", limit=12)
    industry_cols, industry_rows = _select(industry_cols, industry_rows, "industry")

    alerts_path = Path(config.ALERT_DIR) / f"intraday_{datetime.now(TZ_CN):%Y%m%d}.csv"
    alert_cols, alert_rows = _read_csv(alerts_path, limit=80)
    alert_cols, alert_rows = _select(alert_cols, alert_rows, "alerts")

    ranking_ts = ""
    if ranking_path is not None:
        try:
            ranking_ts = datetime.fromtimestamp(
                ranking_path.stat().st_mtime, TZ_CN).isoformat(timespec="seconds")
        except Exception:  # noqa: BLE001
            pass
    intraday_path = Path(config.REPORT_DIR) / "intraday_rank_latest.csv"
    intraday_ts = ""
    if intraday_path.is_file():
        try:
            intraday_ts = datetime.fromtimestamp(
                intraday_path.stat().st_mtime, TZ_CN).isoformat(timespec="seconds")
        except Exception:  # noqa: BLE001
            pass

    return {
        "generated_at": _now(),
        "data_ts": ranking_ts,
        "intraday_ts": intraday_ts,
        "ranking": {"file": ranking_path.name if ranking_path else None,
                    "columns": ranking_cols, "rows": ranking_rows},
        "intraday": {"columns": intraday_cols, "rows": intraday_rows},
        "industry": {"columns": industry_cols, "rows": industry_rows},
        "alerts": {"columns": alert_cols, "rows": alert_rows},
    }


def _with_sides(cols: list[str], rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """给日线榜派生 MACD/KDJ 方向（由 dif/dea、k/d 判断，NaN 给 "-"）。"""
    if not cols:
        return cols, rows
    idx = {c: i for i, c in enumerate(cols)}

    def side(a: str, b: str, up: str, down: str) -> str:
        try:
            va, vb = float(a), float(b)
        except (TypeError, ValueError):
            return "-"
        if va != va or vb != vb:
            return "-"
        return up if va > vb else down

    new_cols = list(cols) + ["macd_side", "kdj_side"]
    new_rows = []
    for row in rows:
        r = list(row)
        try:
            macd = side(r[idx["dif"]], r[idx["dea"]], "多头", "空头") if {"dif", "dea"} <= idx.keys() else "-"
            kdjv = side(r[idx["k"]], r[idx["d"]], "金叉", "死叉") if {"k", "d"} <= idx.keys() else "-"
        except Exception:  # noqa: BLE001
            macd = kdjv = "-"
        new_rows.append(r + [macd, kdjv])
    return new_cols, new_rows


def _now() -> str:
    return datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# HTML 渲染
# --------------------------------------------------------------------------
_CSS = """
:root{--bg:#0f1419;--card:#161d24;--line:#233040;--fg:#e6edf3;--dim:#8b9bab;
      --up:#f85149;--down:#3fb950;--gold:#d29922}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:13px/1.5 -apple-system,"Microsoft YaHei UI","Segoe UI",sans-serif}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;
       align-items:baseline;gap:16px;flex-wrap:wrap}
h1{font-size:17px;margin:0}
.meta{color:var(--dim);font-size:12px}
section{margin:18px 20px;background:var(--card);border:1px solid var(--line);
        border-radius:8px;overflow:hidden}
h2{font-size:13px;margin:0;padding:10px 14px;border-bottom:1px solid var(--line);
   color:var(--gold);font-weight:600;letter-spacing:.5px}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{padding:6px 10px;text-align:right;white-space:nowrap;
      border-bottom:1px solid rgba(35,48,64,.5)}
th{position:sticky;top:0;background:#1b242d;color:var(--dim);font-weight:500;
   text-align:right;z-index:1}
th:nth-child(-n+5),td:nth-child(-n+5){text-align:left}
tbody tr:hover{background:rgba(35,48,64,.45)}
td.up{color:var(--up)}
td.down{color:var(--down)}
.empty{padding:18px 14px;color:var(--dim)}
.wrap{max-height:460px;overflow:auto}
footer{color:var(--dim);font-size:11px;padding:10px 20px 30px}
.health{padding:9px 20px;font-size:12px;font-weight:600;border-bottom:1px solid var(--line)}
.health.ok{background:#12301c;color:#4ade80}
.health.warn{background:#3a2f10;color:#fbbf24}
.health.bad{background:#3a1414;color:#f87171}
.health .sub{font-weight:400;color:var(--dim);margin-left:10px}
"""


def _cell_class(value: str) -> str:
    v = str(value).strip()
    if v.startswith("+"):
        return ' class="up"'
    if v.startswith("-"):
        return ' class="down"'
    return ""


def _table(cols: list[str], rows: list[list[str]], limit: int = 60) -> str:
    if not cols:
        return '<div class="empty">暂无数据</div>'
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    body = []
    for row in rows[:limit]:
        tds = "".join(
            f"<td{_cell_class(v)}>{html.escape(str(v))}</td>" for v in row
        )
        body.append(f"<tr>{tds}</tr>")
    return (
        '<div class="wrap"><table><thead><tr>'
        + head
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
    )


def render_index() -> str:
    data = collect()
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>A股筛选器 · 看板</title><style>{_CSS}</style></head>
<body>
<div id="health" class="health">检查数据新鲜度…</div>
<header>
  <h1>A股筛选器 · 看板</h1>
  <span class="meta">生成于 {data['generated_at']}（北京时间）· 每 {REFRESH_SECONDS} 秒自动刷新</span>
</header>

<section><h2>日线榜（每日收盘选股）{('· ' + data['ranking']['file']) if data['ranking']['file'] else ''}</h2>
{_table(data['ranking']['columns'], data['ranking']['rows'])}</section>

<section><h2>盘中榜（行业优先 · 15 分钟）</h2>
{_table(data['intraday']['columns'], data['intraday']['rows'], limit=40)}</section>

<section><h2>行业强度</h2>
{_table(data['industry']['columns'], data['industry']['rows'], limit=12)}</section>

<section><h2>今日盘中告警</h2>
{_table(data['alerts']['columns'], data['alerts']['rows'], limit=80)}</section>

<footer>本页仅展示本地计算结果，不构成投资建议。数据：Tushare（日线）+
腾讯/新浪（实时快照）+ 东财（15分钟K线）。</footer>
</body></html>"""
    return html.replace("</body>", _health_script(data.get("data_ts") or "") + "\n</body>", 1)


#: 健康条脚本：数据新鲜度在**浏览器端**计算，静态页放几天也能判断是否过期
_HEALTH_JS = """
<script>
(function () {
  var dataTs = "__TS__";
  var el = document.getElementById("health");
  if (!el) return;
  function human(h) {
    if (h < 1) return "不到 1 小时";
    if (h < 48) return Math.round(h) + " 小时";
    return Math.round(h / 24) + " 天";
  }
  function render() {
    if (!dataTs) {
      el.className = "health bad";
      el.innerHTML = "\u26a0 还没有榜单数据<span class='sub'>去 GitHub Actions 看是否跑过 rank 模式</span>";
      return;
    }
    var t = new Date(dataTs);
    var h = (Date.now() - t.getTime()) / 3600000;
    var when = t.toLocaleString("zh-CN", {month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit"});
    if (h <= 30) {
      el.className = "health ok";
      el.innerHTML = "\u2713 数据正常<span class='sub'>榜单更新于 " + when + "（" + human(h) + "前）</span>";
    } else if (h <= 80) {
      el.className = "health warn";
      el.innerHTML = "\u25b3 数据可能未更新<span class='sub'>最后更新 " + when + "（" + human(h) + "前）\u00b7 若今天是交易日且已过 22:30，检查 GitHub Actions</span>";
    } else {
      el.className = "health bad";
      el.innerHTML = "\u2715 数据已过期<span class='sub'>最后更新 " + when + "（" + human(h) + "前）\u00b7 多半是定时任务挂了</span>";
    }
  }
  render();
  setInterval(render, 60000);
})();
</script>
"""


def _health_script(data_ts: str) -> str:
    """生成健康条脚本，注入数据时间戳。"""
    import json as _json
    safe = _json.dumps(data_ts or "")[1:-1]      # 转义为 JS 字符串内容
    return _HEALTH_JS.replace("__TS__", safe)


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------
def _auth_ok(header: str | None) -> bool:
    user = os.getenv("WEB_USER", "").strip()
    pwd = os.getenv("WEB_PASS", "").strip()
    if not user or not pwd:
        return True                      # 未配置 = 不启用认证
    if not header or not header.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(header[6:]).decode("utf-8")
        got_user, _, got_pwd = raw.partition(":")
    except Exception:  # noqa: BLE001
        return False
    return got_user == user and got_pwd == pwd


class Handler(BaseHTTPRequestHandler):
    server_version = "cn-stock-screener"

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _deny(self) -> None:
        self._send(401, b"auth required", "text/plain; charset=utf-8",
                   {"WWW-Authenticate": 'Basic realm="cn-stock-screener"'})

    def do_GET(self) -> None:  # noqa: N802
        if not _auth_ok(self.headers.get("Authorization")):
            self._deny()
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/healthz":
                self._send(200, b"ok", "text/plain; charset=utf-8")
            elif path == "/api/all.json":
                self._send(200, json.dumps(collect(), ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif path in ("/api/ranking.json", "/api/intraday.json",
                          "/api/industry.json", "/api/alerts.json"):
                key = path.split("/")[2].split(".")[0]
                payload = json.dumps(collect().get(key, {}), ensure_ascii=False)
                self._send(200, payload.encode("utf-8"), "application/json; charset=utf-8")
            elif path == "/":
                self._send(200, render_index().encode("utf-8"), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.exception("请求失败 %s", path)
            self._send(500, f"error: {type(exc).__name__}: {exc}".encode("utf-8"),
                       "text/plain; charset=utf-8")

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        logger.debug("%s - %s", self.address_string(), fmt % args)


def serve(host: str = "127.0.0.1", port: int = 8000, background: bool = False):
    """启动网页服务（默认阻塞）。返回 (server, thread)。"""
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    logger.info("网页看板：http://%s:%d/  （认证：%s）",
                host, port, "开" if os.getenv("WEB_PASS") else "关")
    if not background:
        try:
            thread.join()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.shutdown()
    return httpd, thread


__all__ = ["serve", "render_index", "collect", "REFRESH_SECONDS"]
