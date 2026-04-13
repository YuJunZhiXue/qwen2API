import asyncio
import logging
import random
import time
from typing import Optional
from backend.core.database import AsyncJsonDB
from backend.core.config import settings

log = logging.getLogger("qwen2api.accounts")


def _jitter_seconds() -> float:
    low = max(0, settings.REQUEST_JITTER_MIN_MS)
    high = max(low, settings.REQUEST_JITTER_MAX_MS)
    return random.uniform(low, high) / 1000.0


class Account:
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
        self.last_request_started = float(kwargs.get("last_request_started", 0.0) or 0.0)
        self.last_request_finished = float(kwargs.get("last_request_finished", 0.0) or 0.0)
        self.consecutive_failures = int(kwargs.get("consecutive_failures", 0) or 0)
        self.rate_limit_strikes = int(kwargs.get("rate_limit_strikes", 0) or 0)

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
            "last_request_started": self.last_request_started,
            "last_request_finished": self.last_request_finished,
            "consecutive_failures": self.consecutive_failures,
            "rate_limit_strikes": self.rate_limit_strikes,
        }


class AccountPool:
    def __init__(self, db: AsyncJsonDB, max_inflight: int = settings.MAX_INFLIGHT_PER_ACCOUNT):
        self.db = db
        self.max_inflight = max_inflight
        self.accounts: list[Account] = []
        self._lock = asyncio.Lock()
        self._waiters: list[asyncio.Event] = []
        self._sticky_email: Optional[str] = None

    async def load(self):
        data = await self.db.load()
        self.accounts = [Account(**d) for d in data] if isinstance(data, list) else []
        log.info(f"Loaded {len(self.accounts)} upstream account(s)")

    async def save(self):
        await self.db.save([a.to_dict() for a in self.accounts])

    async def add(self, account: Account):
        async with self._lock:
            self.accounts = [a for a in self.accounts if a.email != account.email]
            self.accounts.append(account)
        await self.save()

    async def remove(self, email: str):
        async with self._lock:
            self.accounts = [a for a in self.accounts if a.email != email]
        await self.save()

    def set_max_inflight(self, value: int):
        self.max_inflight = max(1, int(value))

    async def acquire(self, exclude: set = None) -> Optional[Account]:
        async with self._lock:
            now = time.time()
            available = [a for a in self.accounts if a.is_available() and (not exclude or a.email not in exclude)]
            if not available:
                return None

            ready = [a for a in available if a.inflight < self.max_inflight and a.next_available_at() <= now]
            if not ready:
                return None

            ready.sort(key=lambda a: (a.inflight, a.last_request_started or 0.0, a.last_used or 0.0))
            best = ready[0]
            best.inflight += 1
            best.last_used = now
            best.last_request_started = now + _jitter_seconds()
            self._sticky_email = best.email if len(ready) == 1 else None
            return best

    async def acquire_wait(self, timeout: float = 60, exclude: set = None) -> Optional[Account]:
        deadline = time.time() + timeout
        while True:
            acc = await self.acquire(exclude)
            if acc:
                return acc

            async with self._lock:
                candidates = [
                    a for a in self.accounts
                    if a.valid and (not exclude or a.email not in exclude)
                ]
                if not candidates:
                    return None
                next_ready_at = min((a.next_available_at() for a in candidates), default=time.time())

            remaining = deadline - time.time()
            if remaining <= 0:
                return None

            evt = asyncio.Event()
            self._waiters.append(evt)
            wait_timeout = min(remaining, max(0.05, next_ready_at - time.time() + 0.05))
            try:
                await asyncio.wait_for(evt.wait(), timeout=wait_timeout)
            except asyncio.TimeoutError:
                pass
            finally:
                if evt in self._waiters:
                    self._waiters.remove(evt)

    def release(self, acc: Account):
        acc.inflight = max(0, acc.inflight - 1)
        acc.last_request_finished = time.time()
        if self._waiters:
            evt = self._waiters.pop(0)
            evt.set()

    def mark_invalid(self, acc: Account, reason: str = "invalid", error_message: str = ""):
        acc.valid = False
        acc.status_code = reason or "invalid"
        acc.last_error = error_message or acc.last_error
        acc.consecutive_failures += 1
        if reason == "pending_activation":
            acc.activation_pending = True
        if self._sticky_email == acc.email:
            self._sticky_email = None
        log.warning(f"[账号] {acc.email} 已标记为不可用，状态={acc.status_code}")

    def mark_success(self, acc: Account):
        acc.consecutive_failures = 0
        acc.rate_limit_strikes = 0
        if acc.status_code == "rate_limited":
            acc.status_code = "valid"
        if not acc.activation_pending:
            acc.valid = True

    def mark_rate_limited(self, acc: Account, cooldown: int | None = None, error_message: str = ""):
        acc.rate_limit_strikes += 1
        base = cooldown if cooldown is not None else settings.RATE_LIMIT_BASE_COOLDOWN
        dynamic = min(settings.RATE_LIMIT_MAX_COOLDOWN, int(base * (2 ** max(0, acc.rate_limit_strikes - 1))))
        dynamic += int(_jitter_seconds())
        acc.rate_limited_until = time.time() + dynamic
        acc.status_code = "rate_limited"
        acc.last_error = error_message or acc.last_error
        if self._sticky_email == acc.email:
            self._sticky_email = None
        log.warning(f"[账号] {acc.email} 已限流冷却 {dynamic} 秒")

    def status(self):
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
            "max_inflight": self.max_inflight,
            "waiting": len(self._waiters),
            "account_min_interval_ms": settings.ACCOUNT_MIN_INTERVAL_MS,
        }
