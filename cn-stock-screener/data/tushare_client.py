"""
data.tushare_client — Tushare Pro 调用封装

职责：
- 懒加载 tushare，未安装时给出可执行的提示
- 权限白名单校验：只允许 config.ALLOWED_APIS，明确禁止分钟线/实时行情等
- 限流：滑动窗口（200 次/分钟）+ 最小调用间隔（0.3 秒）
- 重试：网络异常 / 频次超限自动退避重试
- 分页：单次返回达到上限时自动翻页拼接
- **本地模式**：``set_local_only(True)`` / ``with local_only():`` 之后任何调用都抛
  ``NetworkBlockedError``；全局调用计数器供自检断言"计算期间零调用"

本模块不关心缓存，只负责"把一次 API 调用安全地做成一个 DataFrame"。
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from config import (
    ALLOWED_APIS,
    API_MAX_RETRIES,
    API_PAGE_LIMIT,
    API_RETRY_BACKOFF,
    API_TIMEOUT,
    FORBIDDEN_APIS,
    MIN_CALL_INTERVAL,
    RATE_LIMIT_PER_MINUTE,
    RATE_WINDOW_SECONDS,
    get_token,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------
class TushareError(RuntimeError):
    """Tushare 调用失败的基类。"""


class TokenMissingError(TushareError):
    """未配置 TUSHARE_TOKEN。"""


class PermissionDeniedError(TushareError):
    """接口不在当前账号（2000 积分）权限范围内。"""


class RateLimitError(TushareError):
    """触发 Tushare 频次限制。"""


class ForbiddenApiError(TushareError):
    """调用了本项目明确禁用的接口。"""


class DependencyMissingError(TushareError):
    """缺少第三方依赖（如未安装 tushare）。"""


class NetworkBlockedError(TushareError):
    """本地模式（--local-only / --replay）下试图发起网络调用。"""


# --------------------------------------------------------------------------
# 本地模式 + 全局调用计数
#
# 这两个东西存在的唯一目的：让"--local-only 时零网络调用"和
# "指标计算期间 Tushare 调用计数为 0"都能被**断言**，而不是靠人工检查。
# 计数器是模块级的，任何 TushareClient 实例共享。
# --------------------------------------------------------------------------
_LOCAL_ONLY: bool = False
_COUNTER_LOCK = threading.Lock()
_COUNTERS: dict[str, int] = {"attempts": 0, "blocked": 0, "success": 0}


def set_local_only(enabled: bool = True) -> None:
    """打开/关闭本地模式（关闭后恢复联网能力）。"""
    global _LOCAL_ONLY
    _LOCAL_ONLY = bool(enabled)
    logger.debug("本地模式（local-only）=%s", _LOCAL_ONLY)


def is_local_only() -> bool:
    """当前是否处于本地模式。"""
    return _LOCAL_ONLY


@contextlib.contextmanager
def local_only(enabled: bool = True):
    """上下文管理器：``with local_only(): ...`` 期间禁止任何网络调用。"""
    previous = _LOCAL_ONLY
    set_local_only(enabled)
    try:
        yield
    finally:
        set_local_only(previous)


def reset_call_count() -> None:
    """清零调用计数器。"""
    with _COUNTER_LOCK:
        for key in _COUNTERS:
            _COUNTERS[key] = 0


def get_attempt_count() -> int:
    """累计"尝试调用"次数（含被本地模式拦截的）。这是断言用的主计数。"""
    with _COUNTER_LOCK:
        return _COUNTERS["attempts"]


def get_call_count() -> int:
    """累计真正拿到响应的调用次数。"""
    with _COUNTER_LOCK:
        return _COUNTERS["success"]


def get_blocked_count() -> int:
    """累计被本地模式拦截的调用次数。"""
    with _COUNTER_LOCK:
        return _COUNTERS["blocked"]


def call_stats() -> dict[str, int | bool]:
    with _COUNTER_LOCK:
        return {**_COUNTERS, "local_only": _LOCAL_ONLY}


def _bump(key: str) -> None:
    with _COUNTER_LOCK:
        _COUNTERS[key] = _COUNTERS.get(key, 0) + 1


def _bump_attempt(blocked: bool = False) -> None:
    with _COUNTER_LOCK:
        _COUNTERS["attempts"] += 1
        if blocked:
            _COUNTERS["blocked"] += 1


# --------------------------------------------------------------------------
# 限流器
# --------------------------------------------------------------------------
class RateLimiter:
    """滑动窗口限流 + 最小调用间隔（线程安全）。

    - window: 最近 RATE_WINDOW_SECONDS 秒内的调用时间戳
    - min_interval: 任意两次调用之间的最小间隔（默认 0.3 秒）
    """

    def __init__(
        self,
        per_minute: int = RATE_LIMIT_PER_MINUTE,
        window_seconds: int = RATE_WINDOW_SECONDS,
        min_interval: float = MIN_CALL_INTERVAL,
    ) -> None:
        self.per_minute = max(1, int(per_minute))
        self.window_seconds = float(window_seconds)
        self.min_interval = max(0.0, float(min_interval))
        self._calls: deque[float] = deque()
        self._last_call: float = 0.0
        self._lock = threading.Lock()
        self.total_calls = 0
        self.total_wait = 0.0

    def _prune(self, now: float) -> None:
        while self._calls and now - self._calls[0] > self.window_seconds:
            self._calls.popleft()

    def acquire(self) -> float:
        """阻塞直到可以发起下一次调用，返回实际等待秒数。"""
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._prune(now)

                # 1) 最小间隔
                gap = self.min_interval - (now - self._last_call)
                # 2) 窗口配额
                quota_wait = 0.0
                if len(self._calls) >= self.per_minute:
                    quota_wait = self.window_seconds - (now - self._calls[0]) + 0.01

                delay = max(gap, quota_wait, 0.0)
                if delay <= 0:
                    self._last_call = now
                    self._calls.append(now)
                    self.total_calls += 1
                    self.total_wait += waited
                    return waited

            time.sleep(min(delay, 1.0))
            waited += min(delay, 1.0)

    @property
    def calls_in_window(self) -> int:
        with self._lock:
            self._prune(time.monotonic())
            return len(self._calls)

    def stats(self) -> dict[str, Any]:
        return {
            "total_calls": self.total_calls,
            "calls_in_window": self.calls_in_window,
            "total_wait_seconds": round(self.total_wait, 3),
            "per_minute": self.per_minute,
            "min_interval": self.min_interval,
        }


# --------------------------------------------------------------------------
# 结果容器
# --------------------------------------------------------------------------
@dataclass
class CallResult:
    """一次 API 调用的结果。"""

    api: str
    params: dict[str, Any]
    df: pd.DataFrame
    rows: int = 0
    from_cache: bool = False
    elapsed: float = 0.0
    attempts: int = 1

    def __post_init__(self) -> None:
        self.rows = int(len(self.df))


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------
class TushareClient:
    """Tushare Pro 薄封装。

    用法::

        client = TushareClient()
        df = client.daily(trade_date="20250102")
    """

    def __init__(
        self,
        token: str | None = None,
        limiter: RateLimiter | None = None,
        timeout: int = API_TIMEOUT,
        offline: bool = False,
    ) -> None:
        self._token = (token or get_token()).strip()
        self.limiter = limiter or RateLimiter()
        self.timeout = timeout
        self.offline = offline          # 离线模式：只用于自检，不真正联网
        self._pro: Any | None = None
        self._import_error: str | None = None
        self.call_log: list[dict[str, Any]] = []

    # ---------------------------------------------------------------- 基础
    @property
    def token(self) -> str:
        return self._token

    def has_token(self) -> bool:
        return bool(self._token)

    def require_token(self) -> str:
        if not self._token:
            raise TokenMissingError(
                "未找到 Tushare Token。请把 .env.example 复制为 .env，"
                f"并在其中填写 TUSHARE_TOKEN=你的Token（路径：项目根目录/.env）"
            )
        return self._token

    def _get_pro(self) -> Any:
        if self._pro is not None:
            return self._pro
        try:
            import tushare as ts  # 延迟导入，未安装时给出明确提示
        except Exception as exc:  # noqa: BLE001
            self._import_error = str(exc)
            raise DependencyMissingError(
                "未安装 tushare。请在项目根目录执行： "
                "python -m pip install -r requirements.txt"
            ) from exc

        self.require_token()
        ts.set_token(self._token)
        # 用库自带的 endpoint，不要自己改 URL：
        # 当前 tushare 版本默认打 api.waditu.com/dataapi，改成其它域名可能"请求成功但返回空数据"。
        self._pro = ts.pro_api(self._token, timeout=self.timeout)
        return self._pro

    # ------------------------------------------------------------ 权限校验
    @staticmethod
    def check_api_allowed(api: str) -> None:
        if api in FORBIDDEN_APIS:
            raise ForbiddenApiError(
                f"接口 {api} 属于本项目明确禁用的范围"
                "（分钟线/实时行情/高积分特色数据），请勿调用。"
            )
        if api not in ALLOWED_APIS:
            raise ForbiddenApiError(
                f"接口 {api} 不在已授权的白名单内。"
                f"当前允许：{', '.join(sorted(ALLOWED_APIS))}"
            )

    # ---------------------------------------------------------------- 调用
    def call(
        self,
        api: str,
        *,
        retries: int | None = None,
        paginate: bool = False,
        **params: Any,
    ) -> pd.DataFrame:
        """调用一个 Tushare 接口并返回 DataFrame（带限流与重试）。

        **本地模式（``--local-only`` / ``--replay``）下，这里会直接抛
        ``NetworkBlockedError``**，从根上保证"指标与筛选计算期间零网络调用"。
        每次进来都会给全局计数器 +1，因此可以用 ``get_attempt_count()``
        断言"计算期间没有发生任何调用尝试"。
        """
        _bump_attempt(blocked=is_local_only())
        if is_local_only():
            raise NetworkBlockedError(
                f"本地模式（--local-only）禁止网络调用，已拦截接口 {api}。"
                "本地模式只能读 data/cache/ 下的缓存。"
            )
        self.check_api_allowed(api)
        if self.offline:
            raise TushareError(f"离线模式不允许调用 {api}")

        max_attempts = (API_MAX_RETRIES if retries is None else retries) + 1
        last_exc: Exception | None = None
        offset = 0
        frames: list[pd.DataFrame] = []

        for attempt in range(1, max_attempts + 1):
            try:
                self.limiter.acquire()
                t0 = time.monotonic()
                pro = self._get_pro()
                kwargs = dict(params)
                if paginate:
                    kwargs = {**kwargs, "offset": offset, "limit": API_PAGE_LIMIT}
                df = pro.query(api, **kwargs)
                elapsed = time.monotonic() - t0
                _bump("success")

                if df is None:
                    df = pd.DataFrame()
                df = pd.DataFrame(df)

                self.call_log.append({
                    "api": api,
                    "params": {k: v for k, v in params.items()},
                    "rows": int(len(df)),
                    "elapsed": round(elapsed, 3),
                    "attempt": attempt,
                })
                logger.debug("Tushare %s %s -> %d 行 (%.2fs)", api, params, len(df), elapsed)

                frames.append(df)
                if paginate and len(df) >= API_PAGE_LIMIT:
                    offset += len(df)
                    continue  # 继续翻页
                break

            except (ForbiddenApiError, TokenMissingError, DependencyMissingError):
                # 配置/权限类问题重试没有意义，立即上报
                raise
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                last_exc = exc
                if self._is_permission_error(msg):
                    raise PermissionDeniedError(
                        f"接口 {api} 无访问权限（当前账号积分不足或未开通）：{msg}"
                    ) from exc
                if self._is_param_error(msg):
                    # 参数不合法（例如三大报表在 2000 积分下必须传 ts_code），重试无用
                    raise TushareError(f"接口 {api} 参数错误：{msg}") from exc
                if attempt >= max_attempts:
                    break
                wait = API_RETRY_BACKOFF * attempt
                if self._is_rate_error(msg):
                    wait = max(wait, 60.0)
                    logger.warning("接口 %s 触发频次限制，等待 %.0fs 后重试 (%d/%d)",
                                   api, wait, attempt, max_attempts - 1)
                else:
                    logger.warning("接口 %s 调用失败：%s；%.0fs 后重试 (%d/%d)",
                                   api, msg, wait, attempt, max_attempts - 1)
                time.sleep(wait)

        if not frames:
            raise TushareError(f"接口 {api} 调用失败：{last_exc}") from last_exc

        out = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
        if paginate and len(frames) > 1:
            out = out.drop_duplicates(subset=["ts_code", "trade_date"], ignore_index=True)
        return out

    @staticmethod
    def _is_permission_error(msg: str) -> bool:
        keys = ("没有访问该接口的权限", "权限", "积分", "permission", "not authorized")
        return any(k in msg for k in keys)

    @staticmethod
    def _is_rate_error(msg: str) -> bool:
        keys = ("每分钟", "频次", "频率", "too many", "limit", "访问过快")
        return any(k in msg for k in keys)

    @staticmethod
    def _is_param_error(msg: str) -> bool:
        keys = ("必填参数", "参数错误", "invalid", "Invalid", "没有传入")
        return any(k in msg for k in keys)

    # ------------------------------------------------------- 具体接口封装
    def stock_basic(self, list_status: str = "L", **kw: Any) -> pd.DataFrame:
        from config import FIELDS_STOCK_BASIC

        return self.call(
            "stock_basic",
            exchange="",
            list_status=list_status,
            fields=FIELDS_STOCK_BASIC,
            **kw,
        )

    def daily(self, trade_date: str | None = None, ts_code: str | None = None, **kw: Any) -> pd.DataFrame:
        from config import FIELDS_DAILY

        params: dict[str, Any] = {"fields": FIELDS_DAILY, **kw}
        if trade_date:
            params["trade_date"] = trade_date
        if ts_code:
            params["ts_code"] = ts_code
        return self.call("daily", **params)

    def daily_basic(self, trade_date: str | None = None, ts_code: str | None = None, **kw: Any) -> pd.DataFrame:
        from config import FIELDS_DAILY_BASIC

        params: dict[str, Any] = {"fields": FIELDS_DAILY_BASIC, **kw}
        if trade_date:
            params["trade_date"] = trade_date
        if ts_code:
            params["ts_code"] = ts_code
        return self.call("daily_basic", **params)

    def adj_factor(self, trade_date: str | None = None, ts_code: str | None = None, **kw: Any) -> pd.DataFrame:
        """复权因子（按交易日整表拉取，或按 ts_code 拉）。

        只返回 ts_code / trade_date / adj_factor 三列；本项目把它按 trade_date 分片缓存，
        组装时再按 ts_code 跨日期拼接成长表。
        """
        from config import FIELDS_ADJ_FACTOR

        params: dict[str, Any] = {"fields": FIELDS_ADJ_FACTOR, **kw}
        if trade_date:
            params["trade_date"] = trade_date
        if ts_code:
            params["ts_code"] = ts_code
        return self.call("adj_factor", **params)

    def daily_range(
        self,
        start_date: str,
        end_date: str,
        limit: int = API_PAGE_LIMIT,
        fields: str | None = None,
    ) -> pd.DataFrame:
        """按日期区间拉取日线（用于一次性定位"最近有数据的交易日"）。

        Tushare 返回按 trade_date 倒序，因此取 max(trade_date) 即为最近交易日，
        比逐日探测（每天 1 次调用）更省额度，也不怕单日请求偶发返回空表。
        """
        from config import FIELDS_DAILY

        return self.call(
            "daily",
            fields=fields or FIELDS_DAILY,
            start_date=str(start_date),
            end_date=str(end_date),
            limit=int(limit),
        )

    def income(self, **kw: Any) -> pd.DataFrame:
        from config import FIELDS_INCOME

        return self.call("income", fields=FIELDS_INCOME, **kw)

    def balancesheet(self, **kw: Any) -> pd.DataFrame:
        from config import FIELDS_BALANCESHEET

        return self.call("balancesheet", fields=FIELDS_BALANCESHEET, **kw)

    def cashflow(self, **kw: Any) -> pd.DataFrame:
        from config import FIELDS_CASHFLOW

        return self.call("cashflow", fields=FIELDS_CASHFLOW, **kw)

    # 宏观经济：保持同名直通，方便第二阶段接入
    def macro(self, api: str, **kw: Any) -> pd.DataFrame:
        if not api.startswith("cn_") and api not in ALLOWED_APIS:
            raise ForbiddenApiError(f"宏观经济接口 {api} 不在白名单内")
        return self.call(api, **kw)

    def stats(self) -> dict[str, Any]:
        return {"limiter": self.limiter.stats(), "api_calls": len(self.call_log)}


def make_client(offline: bool = False, token: str | None = None) -> TushareClient:
    """工厂函数，便于测试替换。"""
    return TushareClient(token=token, offline=offline)
