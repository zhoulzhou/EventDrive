import random
import asyncio
import logging
import threading
import time
from datetime import date
from typing import Dict, Optional
from urllib.parse import urlparse

from app.config import settings

_delay_logger = logging.getLogger("anti_crawl.delay")
_limit_logger = logging.getLogger("anti_crawl.limit")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
]


def get_random_user_agent() -> str:
    return random.choice(USER_AGENTS)


def get_random_headers(referer: str = None) -> Dict[str, str]:
    headers = {
        "User-Agent": get_random_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Cache-Control": "max-age=0"
    }
    if referer:
        headers["Referer"] = referer
    return headers


def random_delay(min_delay: int = None, max_delay: int = None, source: str = "") -> None:
    min_d = min_delay or settings.MIN_DELAY
    max_d = max_delay or settings.MAX_DELAY
    delay = random.uniform(min_d, max_d)
    tag = f"[{source}] " if source else ""
    _delay_logger.debug(f"{tag}sync_delay start: {delay:.2f}s")
    time.sleep(delay)
    _delay_logger.debug(f"{tag}sync_delay done: {delay:.2f}s")


async def async_random_delay(min_delay: int = None, max_delay: int = None, source: str = "") -> None:
    min_d = min_delay or settings.MIN_DELAY
    max_d = max_delay or settings.MAX_DELAY
    delay = random.uniform(min_d, max_d)
    tag = f"[{source}] " if source else ""
    loop = asyncio.get_event_loop()
    start = loop.time()
    pending = len(asyncio.all_tasks(loop))
    _delay_logger.debug(f"{tag}async_delay start: {delay:.2f}s (pending_tasks={pending})")
    await asyncio.sleep(delay)
    elapsed = loop.time() - start
    pending_after = len(asyncio.all_tasks(loop))
    _delay_logger.debug(f"{tag}async_delay done: planned={delay:.2f}s actual={elapsed:.2f}s (pending_tasks={pending_after})")


# ======================================================================
# 抓取限流
# ======================================================================
# 设计目标（2026-10-17）：
#   1. 不要频繁抓取  —— 同一 host 两次请求之间强制最小间隔；两次完整任务之间强制冷却
#   2. 不要一次抓多  —— 单次任务内每个 host 的请求数封顶，避免连续打同一来源
#   3. 不要把来源打挂 —— 失败按指数退避，连续失败后长时间冷却；每日配额封顶
#
# 计量粒度为 host（域名），各来源独立计数，互不影响。
# 状态保存在进程内存（含线程锁），支持同步与 asyncio 两种调用方式。


class RateLimitExceeded(Exception):
    """触发限流阈值（配额耗尽 / 退避冷却中 / 单次任务请求数超限）。"""


class _HostState:
    """单个 host 的限流状态。"""

    __slots__ = (
        "tokens", "last_refill", "last_request_at", "last_run_at",
        "daily_count", "daily_date", "fail_streak", "blocked_until", "run_count",
    )

    def __init__(self, burst: int, now: float):
        self.tokens = float(burst)          # 令牌桶余额
        self.last_refill = now               # 上次补充令牌的时间
        self.last_request_at = 0.0           # 上次「实际发出请求」的时间
        self.last_run_at = 0.0               # 上次「完整抓取任务」开始时间
        self.daily_count = 0                 # 今日已请求次数
        self.daily_date = date.today()       # 计数所属日期
        self.fail_streak = 0                 # 连续失败次数（用于指数退避）
        self.blocked_until = 0.0             # 退避冷却截止时间戳
        self.run_count = 0                   # 本次任务内已用请求数（由 begin_run 重置）


class RateLimiter:
    """全局抓取限流器（按 host 计量）。

    典型用法::

        limiter = get_limiter()
        limiter.begin_run("eastmoney")          # 任务开始，重置单次计数 + 检查冷却
        try:
            limiter.acquire("https://.../api")  # 同步：申请令牌，不够则等待
            resp = httpx.get(url)
            limiter.report_success("eastmoney")
        except RateLimitExceeded:
            ...                                   # 配额/退避，放弃本轮
        except Exception:
            limiter.report_failure("eastmoney")  # 失败，退避 +1
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._hosts: Dict[str, _HostState] = {}

    # ---------------- 内部工具 ----------------
    @staticmethod
    def host_of(url: str) -> str:
        try:
            return urlparse(url).netloc.lower() or url.lower()
        except Exception:
            return url.lower()

    def _state(self, host: str) -> _HostState:
        st = self._hosts.get(host)
        if st is None:
            st = _HostState(settings.RATE_LIMIT_BURST, time.monotonic())
            self._hosts[host] = st
        return st

    def _refill(self, st: _HostState) -> None:
        """按时间补充令牌桶。"""
        now = time.monotonic()
        interval = max(settings.RATE_LIMIT_MIN_INTERVAL, 0.1)
        elapsed = now - st.last_refill
        if elapsed <= 0:
            return
        st.tokens = min(
            float(settings.RATE_LIMIT_BURST),
            st.tokens + elapsed / interval,
        )
        st.last_refill = now

    def _rollover_daily(self, st: _HostState) -> None:
        """跨天则重置每日计数。"""
        today = date.today()
        if st.daily_date != today:
            st.daily_date = today
            st.daily_count = 0

    def _backoff_seconds(self, streak: int) -> float:
        base = settings.RATE_LIMIT_BACKOFF_BASE
        if base <= 0:
            return 0.0
        return min(base * (2 ** max(0, streak - 1)), settings.RATE_LIMIT_BACKOFF_MAX)

    # ---------------- 对外接口 ----------------
    def peek_wait(self, url_or_host: str) -> float:
        """查看还需等待多少秒才能发下一次请求（不占用令牌）。"""
        host = url_or_host if "//" not in url_or_host else self.host_of(url_or_host)
        with self._lock:
            st = self._state(host)
            self._rollover_daily(st)
            wait = 0.0
            now = time.monotonic()
            if st.blocked_until > now:
                wait = max(wait, st.blocked_until - now)
            self._refill(st)
            if st.tokens < 1.0:
                interval = max(settings.RATE_LIMIT_MIN_INTERVAL, 0.1)
                wait = max(wait, (1.0 - st.tokens) * interval)
            return wait

    def acquire(self, url_or_host: str, timeout: Optional[float] = None) -> float:
        """同步申请一个令牌；不足则阻塞等待。

        Returns:
            实际等待的秒数（0 表示无需等待）。

        Raises:
            RateLimitExceeded: 已处于退避冷却中，或等待超过 timeout。
        """
        host = url_or_host if "//" not in url_or_host else self.host_of(url_or_host)
        deadline = None if timeout is None else time.monotonic() + timeout
        waited = 0.0

        while True:
            with self._lock:
                st = self._state(host)
                self._rollover_daily(st)
                now = time.monotonic()

                # 1) 退避冷却中：默认直接放弃本轮
                if st.blocked_until > now:
                    remain = st.blocked_until - now
                    if settings.RATE_LIMIT_SKIP_ON_EXCEED:
                        raise RateLimitExceeded(
                            f"{host} 处于失败退避中，剩余 {remain:.0f}s"
                        )
                    if deadline is not None and now + remain > deadline:
                        raise RateLimitExceeded(f"{host} 退避剩余 {remain:.0f}s，超时放弃")
                    time.sleep(min(remain, 5.0))
                    waited += min(remain, 5.0)
                    continue

                # 2) 每日配额
                quota = settings.RATE_LIMIT_DAILY_QUOTA
                if quota and st.daily_count >= quota:
                    raise RateLimitExceeded(f"{host} 今日请求已达上限 {quota} 次")

                # 3) 单次任务请求数封顶
                per_run = settings.RATE_LIMIT_MAX_REQS_PER_RUN
                if per_run and st.run_count >= per_run:
                    raise RateLimitExceeded(
                        f"{host} 本次任务请求数已达上限 {per_run} 次，跳过剩余请求"
                    )

                # 4) 令牌桶
                self._refill(st)
                if st.tokens >= 1.0:
                    st.tokens -= 1.0
                    st.last_request_at = now
                    st.daily_count += 1
                    st.run_count += 1
                    if waited > 0:
                        _limit_logger.info(f"{host} 等待限流令牌 {waited:.1f}s 后放行")
                    return waited

                interval = max(settings.RATE_LIMIT_MIN_INTERVAL, 0.1)
                need = (1.0 - st.tokens) * interval

            if deadline is not None and time.monotonic() + need > deadline:
                raise RateLimitExceeded(f"{host} 令牌不足，需再等 {need:.1f}s")
            time.sleep(min(need, 5.0))
            waited += min(need, 5.0)

    async def acquire_async(self, url_or_host: str, timeout: Optional[float] = None) -> float:
        """异步版本，用 ``asyncio.sleep`` 等待，不阻塞事件循环。"""
        host = url_or_host if "//" not in url_or_host else self.host_of(url_or_host)
        deadline = None if timeout is None else time.monotonic() + timeout
        waited = 0.0

        while True:
            with self._lock:
                st = self._state(host)
                self._rollover_daily(st)
                now = time.monotonic()

                if st.blocked_until > now:
                    remain = st.blocked_until - now
                    if settings.RATE_LIMIT_SKIP_ON_EXCEED:
                        raise RateLimitExceeded(
                            f"{host} 处于失败退避中，剩余 {remain:.0f}s"
                        )
                    if deadline is not None and now + remain > deadline:
                        raise RateLimitExceeded(f"{host} 退避剩余 {remain:.0f}s，超时放弃")
                    nap = min(remain, 5.0)
                    await asyncio.sleep(nap)
                    waited += nap
                    continue

                quota = settings.RATE_LIMIT_DAILY_QUOTA
                if quota and st.daily_count >= quota:
                    raise RateLimitExceeded(f"{host} 今日请求已达上限 {quota} 次")

                per_run = settings.RATE_LIMIT_MAX_REQS_PER_RUN
                if per_run and st.run_count >= per_run:
                    raise RateLimitExceeded(
                        f"{host} 本次任务请求数已达上限 {per_run} 次"
                    )

                self._refill(st)
                if st.tokens >= 1.0:
                    st.tokens -= 1.0
                    st.last_request_at = now
                    st.daily_count += 1
                    st.run_count += 1
                    if waited > 0:
                        _limit_logger.info(f"{host} 等待限流令牌 {waited:.1f}s 后放行")
                    return waited

                interval = max(settings.RATE_LIMIT_MIN_INTERVAL, 0.1)
                need = (1.0 - st.tokens) * interval

            if deadline is not None and time.monotonic() + need > deadline:
                raise RateLimitExceeded(f"{host} 令牌不足，需再等 {need:.1f}s")
            nap = min(need, 5.0)
            await asyncio.sleep(nap)
            waited += nap

    def begin_run(self, url_or_host: str) -> None:
        """标记一次完整抓取任务的开始：重置单次计数，并检查跨轮次冷却。"""
        host = url_or_host if "//" not in url_or_host else self.host_of(url_or_host)
        with self._lock:
            st = self._state(host)
            self._rollover_daily(st)
            st.run_count = 0
            now = time.monotonic()
            st.last_run_at = now

    def report_success(self, url_or_host: str) -> None:
        """请求成功：清零连续失败计数。"""
        host = url_or_host if "//" not in url_or_host else self.host_of(url_or_host)
        with self._lock:
            st = self._state(host)
            if st.fail_streak:
                _limit_logger.info(f"{host} 请求恢复，连续失败计数清零（原 {st.fail_streak}）")
            st.fail_streak = 0
            st.blocked_until = 0.0

    def report_failure(self, url_or_host: str) -> float:
        """请求失败：连续失败 +1，并按指数退避设置冷却。

        Returns:
            本次触发的冷却秒数。
        """
        host = url_or_host if "//" not in url_or_host else self.host_of(url_or_host)
        with self._lock:
            st = self._state(host)
            st.fail_streak += 1
            cool = self._backoff_seconds(st.fail_streak)
            if cool > 0:
                st.blocked_until = time.monotonic() + cool
            _limit_logger.warning(
                f"{host} 请求失败，连续 {st.fail_streak} 次，退避 {cool:.0f}s"
            )
            return cool

    def snapshot(self) -> Dict[str, Dict[str, object]]:
        """导出各 host 的限流状态，便于排查与运维观察。"""
        with self._lock:
            out = {}
            for host, st in self._hosts.items():
                self._rollover_daily(st)
                self._refill(st)
                out[host] = {
                    "今日请求": st.daily_count,
                    "本次已用": st.run_count,
                    "可用令牌": round(st.tokens, 2),
                    "连续失败": st.fail_streak,
                    "退避剩余": max(0, round(st.blocked_until - time.monotonic())),
                }
            return out


_limiter: Optional[RateLimiter] = None
_limiter_lock = threading.Lock()

# ---- 任务级配额与互斥（用于 akshare 等内部封装、无法逐请求插桩的来源）----
_task_guard = threading.Lock()
_task_daily_count: Dict[str, int] = {}
_task_daily_date: Dict[str, date] = {}
_task_running: set = set()


def check_task_allowed(task_name: str) -> bool:
    """检查某任务今日是否还有执行额度，以及是否已在运行中（防重入）。

    Args:
        task_name: 任务标识，如 "macro" / "stock_temp"。

    Returns:
        True 表示允许执行；False 表示已达配额或正在运行中。
    """
    with _task_guard:
        today = date.today()
        if _task_daily_date.get(task_name) != today:
            _task_daily_date[task_name] = today
            _task_daily_count[task_name] = 0

        if task_name in _task_running:
            _limit_logger.info(f"任务 {task_name} 正在运行中，跳过本次触发（防重入）")
            return False

        quota = settings.TASK_DAILY_QUOTA
        used = _task_daily_count.get(task_name, 0)
        if quota and used >= quota:
            _limit_logger.warning(
                f"任务 {task_name} 今日已执行 {used}/{quota} 次，达到配额上限，跳过"
            )
            return False
        return True


def mark_task_started(task_name: str) -> None:
    """标记任务开始：占用一次额度并加运行锁。"""
    with _task_guard:
        today = date.today()
        if _task_daily_date.get(task_name) != today:
            _task_daily_date[task_name] = today
            _task_daily_count[task_name] = 0
        _task_daily_count[task_name] = _task_daily_count.get(task_name, 0) + 1
        _task_running.add(task_name)


def mark_task_finished(task_name: str, success: bool = True) -> None:
    """标记任务结束：释放运行锁。"""
    with _task_guard:
        _task_running.discard(task_name)


def task_quota_snapshot() -> Dict[str, object]:
    """导出任务级配额与运行状态，便于排查。"""
    with _task_guard:
        today = date.today()
        return {
            "今日已执行": {k: v for k, v in _task_daily_count.items() if _task_daily_date.get(k) == today},
            "运行中": sorted(_task_running),
            "每日配额": settings.TASK_DAILY_QUOTA,
        }


def get_limiter() -> RateLimiter:
    """获取全局单例限流器。"""
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                _limiter = RateLimiter()
    return _limiter


def reset_limiter() -> None:
    """重置限流器状态（仅供测试使用）。"""
    global _limiter
    with _limiter_lock:
        _limiter = RateLimiter()
