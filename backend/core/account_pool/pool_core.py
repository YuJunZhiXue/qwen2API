"""
账号池核心逻辑 - 对齐 ds2api 的 pool_core.go
"""
import asyncio
import heapq
import logging
import time
from typing import Optional

from backend.core.database import AsyncJsonDB
from backend.core.config import load_env_accounts, settings

log = logging.getLogger("qwen2api.accounts.core")


class Account:
    """账号对象"""
    def __init__(
        self,
        email="",
        password="",
        token="",
        cookies="",
        username="",
        activation_pending=False,
        status_code="",
        last_error="",
        source="file",
        env_name="",
        **kwargs,
    ):
        self.email = email
        self.password = password
        self.token = token
        self.cookies = cookies
        self.username = username
        self.activation_pending = activation_pending
        self.valid = not activation_pending
        self.last_used = 0.0
        self.inflight = 0
        self.rate_limited_until = 0.0
        self.healing = False
        self.status_code = status_code or ("pending_activation" if activation_pending else "valid")
        self.last_error = last_error or ""
        self.source = source or "file"
        self.env_name = env_name or ""
        self.last_request_started = float(kwargs.get("last_request_started", 0.0) or 0.0)
        self.last_request_finished = float(kwargs.get("last_request_finished", 0.0) or 0.0)
        self.consecutive_failures = int(kwargs.get("consecutive_failures", 0) or 0)
        self.rate_limit_strikes = int(kwargs.get("rate_limit_strikes", 0) or 0)
        self._pool_heap_version = 0

    def is_rate_limited(self) -> bool:
        return self.rate_limited_until > time.time()

    def is_available(self) -> bool:
        return self.valid and not self.is_rate_limited()

    def next_available_at(self) -> float:
        min_interval = max(0, settings.ACCOUNT_MIN_INTERVAL_MS) / 1000.0
        return max(self.rate_limited_until, self.last_request_started + min_interval)

    def get_status_code(self) -> str:
        if self.activation_pending:
            return "pending_activation"
        if self.is_rate_limited():
            return "rate_limited"
        if self.valid:
            return "valid"
        if self.status_code == "banned":
            return "banned"
        if self.status_code == "auth_error":
            return "auth_error"
        return self.status_code or "invalid"

    def get_status_text(self) -> str:
        status_map = {
            "valid": "正常",
            "pending_activation": "待激活",
            "rate_limited": "限流",
            "banned": "封禁",
            "auth_error": "鉴权失败",
            "invalid": "失效",
            "unknown": "未知",
        }
        return status_map.get(self.get_status_code(), "未知")

    def to_dict(self):
        return {
            "email": self.email,
            "password": self.password,
            "token": self.token,
            "cookies": self.cookies,
            "username": self.username,
            "activation_pending": self.activation_pending,
            "status_code": self.status_code,
            "last_error": self.last_error,
            "source": self.source,
            "env_name": self.env_name,
            "last_request_started": self.last_request_started,
            "last_request_finished": self.last_request_finished,
            "consecutive_failures": self.consecutive_failures,
            "rate_limit_strikes": self.rate_limit_strikes,
        }


class AccountPool:
    """
    账号池 - 对齐 ds2api 的 4 层并发控制

    4 层限流：
    1. maxInflightPerAccount: 每账号最大并发
    2. recommendedConcurrency: 推荐并发值（账号数 × 每账号并发）
    3. maxQueueSize: 等待队列上限
    4. globalMaxInflight: 全局最大并发
    """

    def __init__(self, db: AsyncJsonDB, max_inflight: int = settings.MAX_INFLIGHT_PER_ACCOUNT):
        self.db = db
        self.accounts: list[Account] = []
        self._lock = asyncio.Lock()

        # 4 层并发控制（对齐 ds2api）
        self.max_inflight_per_account = max_inflight
        self.recommended_concurrency = 0
        self.max_queue_size = 0
        self.global_max_inflight = 0

        # 等待队列（使用 asyncio.Queue 更接近 Go channel）
        self._waiters_queue: asyncio.Queue = asyncio.Queue()
        self._sticky_email: Optional[str] = None

        # 全局并发计数
        self.global_in_use = 0
        self.ready_set_threshold = max(1, int(settings.ACCOUNT_READY_SET_THRESHOLD))
        self.ready_set_enabled = False
        self._valid_account_count = 0
        self._account_by_email: dict[str, Account] = {}
        self._ready_heap: list[tuple[float, float, float, int, str, int]] = []
        self._cooldown_heap: list[tuple[float, int, str, int]] = []
        self._heap_seq = 0

    async def load(self):
        """加载账号并初始化并发参数"""
        data = await self.db.load()
        file_accounts = [Account(**d) for d in data] if isinstance(data, list) else []
        env_accounts = [Account(**d) for d in load_env_accounts()]
        env_emails = {account.email for account in env_accounts}
        self.accounts = env_accounts + [account for account in file_accounts if account.email not in env_emails]
        self._reset_concurrency_limits()
        if env_accounts:
            log.info(f"Loaded {len(env_accounts)} upstream account(s) from environment variables")
        log.info(f"Loaded {len(self.accounts)} upstream account(s)")

    def _reset_concurrency_limits(self):
        """重置并发限制 - 对齐 ds2api 的 Reset() 逻辑"""
        account_count = len([a for a in self.accounts if a.is_available()])
        ready_set_account_count = len([a for a in self.accounts if a.valid])
        self._valid_account_count = ready_set_account_count
        self.ready_set_threshold = max(1, int(settings.ACCOUNT_READY_SET_THRESHOLD))
        self.ready_set_enabled = ready_set_account_count >= self.ready_set_threshold

        # 计算推荐并发值（对齐 ds2api）
        self.recommended_concurrency = account_count * self.max_inflight_per_account

        # 队列上限 = 推荐并发值（可配置）
        self.max_queue_size = self.recommended_concurrency

        # 全局并发上限 = 推荐并发值（可配置）
        self.global_max_inflight = self.recommended_concurrency

        log.info(
            f"[init_account_queue] initialized: "
            f"total={account_count}, "
            f"max_inflight_per_account={self.max_inflight_per_account}, "
            f"global_max_inflight={self.global_max_inflight}, "
            f"recommended_concurrency={self.recommended_concurrency}, "
            f"max_queue_size={self.max_queue_size}, "
            f"ready_set_accounts={ready_set_account_count}, "
            f"ready_set_enabled={self.ready_set_enabled}, "
            f"ready_set_threshold={self.ready_set_threshold}"
        )
        self._rebuild_ready_set()

    def _next_heap_version(self, acc: Account) -> int:
        self._heap_seq += 1
        acc._pool_heap_version = self._heap_seq
        return acc._pool_heap_version

    def _rebuild_ready_set(self) -> None:
        self._account_by_email = {a.email: a for a in self.accounts if a.email}
        self._ready_heap = []
        self._cooldown_heap = []
        self._heap_seq = 0
        now = time.time()
        if not self.ready_set_enabled:
            for acc in self.accounts:
                acc._pool_heap_version = 0
            return
        for acc in self.accounts:
            self._index_account_locked(acc, now=now)

    def _index_account_locked(self, acc: Account, *, now: float | None = None) -> None:
        """Refresh one account in the large-pool ready/cooldown index."""
        if not self.ready_set_enabled or not acc or not acc.email:
            return
        now = time.time() if now is None else now
        version = self._next_heap_version(acc)
        if not acc.valid or acc.inflight >= self.max_inflight_per_account:
            return
        ready_at = acc.next_available_at()
        if ready_at <= now:
            heapq.heappush(
                self._ready_heap,
                (
                    float(acc.inflight),
                    float(acc.last_request_started or 0.0),
                    float(acc.last_used or 0.0),
                    version,
                    acc.email,
                    version,
                ),
            )
            return
        heapq.heappush(self._cooldown_heap, (float(ready_at), version, acc.email, version))

    def _promote_ready_set_locked(self, now: float) -> None:
        if not self.ready_set_enabled:
            return
        while self._cooldown_heap and self._cooldown_heap[0][0] <= now:
            _, _, email, version = heapq.heappop(self._cooldown_heap)
            acc = self._account_by_email.get(email)
            if acc is None or acc._pool_heap_version != version:
                continue
            self._index_account_locked(acc, now=now)

    def _account_ready_now_locked(self, acc: Account, now: float) -> bool:
        return (
            acc.is_available()
            and acc.inflight < self.max_inflight_per_account
            and acc.next_available_at() <= now
        )

    def _pop_ready_set_locked(self, *, now: float, exclude: Optional[set] = None) -> Optional[Account]:
        if not self.ready_set_enabled:
            return None
        self._promote_ready_set_locked(now)
        deferred: list[tuple[float, float, float, int, str, int]] = []
        try:
            while self._ready_heap:
                entry = heapq.heappop(self._ready_heap)
                _, _, _, _, email, version = entry
                acc = self._account_by_email.get(email)
                if acc is None or acc._pool_heap_version != version:
                    continue
                if exclude and email in exclude:
                    deferred.append(entry)
                    continue
                if not self._account_ready_now_locked(acc, now):
                    self._index_account_locked(acc, now=now)
                    continue
                return acc
            return None
        finally:
            for entry in deferred:
                heapq.heappush(self._ready_heap, entry)

    def _next_ready_set_time_locked(self, now: float) -> float:
        if not self.ready_set_enabled:
            return now
        self._promote_ready_set_locked(now)
        if self._ready_heap:
            return now
        while self._cooldown_heap:
            ready_at, _, email, version = self._cooldown_heap[0]
            acc = self._account_by_email.get(email)
            if acc is None or acc._pool_heap_version != version:
                heapq.heappop(self._cooldown_heap)
                continue
            return ready_at
        return now + 0.5

    async def save(self):
        await self.db.save([a.to_dict() for a in self.accounts if a.source != "env"])

    async def add(self, account: Account):
        async with self._lock:
            self.accounts = [a for a in self.accounts if a.email != account.email]
            self.accounts.append(account)
        await self.save()
        self._reset_concurrency_limits()

    async def remove(self, email: str):
        async with self._lock:
            self.accounts = [a for a in self.accounts if a.email != email]
        await self.save()
        self._reset_concurrency_limits()

    def set_max_inflight(self, value: int):
        self.max_inflight_per_account = max(1, int(value))
        self._reset_concurrency_limits()

    def get_by_email(self, email: str) -> Optional[Account]:
        account = self._account_by_email.get(email)
        if account is not None:
            return account
        return next((a for a in self.accounts if a.email == email), None)

    def _can_acquire_global(self) -> bool:
        """检查全局并发限制"""
        if self.global_max_inflight <= 0:
            return True
        return self.global_in_use < self.global_max_inflight

    def _can_queue(self) -> bool:
        """检查是否可以加入等待队列"""
        if self.max_queue_size <= 0:
            return False
        return self._waiters_queue.qsize() < self.max_queue_size

    def status(self):
        """返回账号池状态"""
        available = [a for a in self.accounts if a.is_available()]
        rate_limited = [a for a in self.accounts if a.get_status_code() == "rate_limited"]
        invalid = [a for a in self.accounts if a.get_status_code() not in ("valid", "rate_limited")]
        activation_pending = [a for a in self.accounts if a.get_status_code() == "pending_activation"]
        banned = [a for a in self.accounts if a.get_status_code() == "banned"]
        in_use = sum(a.inflight for a in self.accounts)

        return {
            "total": len(self.accounts),
            "valid": len(available),
            "rate_limited": len(rate_limited),
            "invalid": len(invalid),
            "activation_pending": len(activation_pending),
            "banned": len(banned),
            "in_use": in_use,
            "global_in_use": self.global_in_use,
            "max_inflight_per_account": self.max_inflight_per_account,
            "recommended_concurrency": self.recommended_concurrency,
            "max_queue_size": self.max_queue_size,
            "global_max_inflight": self.global_max_inflight,
            "ready_set_enabled": self.ready_set_enabled,
            "ready_set_threshold": self.ready_set_threshold,
            "ready_set_accounts": self._valid_account_count,
            "ready_heap_size": len(self._ready_heap),
            "cooldown_heap_size": len(self._cooldown_heap),
            "waiting": self._waiters_queue.qsize(),
            "account_min_interval_ms": getattr(settings, "ACCOUNT_MIN_INTERVAL_MS", 0),
        }
