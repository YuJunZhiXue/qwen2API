"""Chat ID 预热池：预先为每个可用账号创建若干 chat_id 放在队列里，
请求到来时直接从队列 pop 一个省去 /chats/new 握手（实测 500ms~6s 不等）。

典型收益：每次请求节省 500~3000ms 握手时延；最坏情况抖动时节省 5~6s。

工作流：
- 服务启动 → 每账号预建 target_per_account 个 chat_id
- 请求用掉一个 chat_id → 后台立即补位一个
- 每账号池大小上限：target_per_account (默认 3)
- chat_id 有 TTL (默认 30 分钟)，超时背景任务丢弃+重建
- 请求取不到预热 chat_id 时：fallback 到同步 create_chat（当前行为）
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Optional

log = logging.getLogger("qwen2api.chat_pool")


class _Entry:
    __slots__ = ("chat_id", "created_at")

    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        self.created_at = time.time()


class ChatIdPool:
    """按账号邮箱 key 的 chat_id 队列。线程/协程安全。"""

    def __init__(
        self,
        client,
        *,
        target_per_account: int = 5,
        ttl_seconds: float = 10 * 60,
        default_model: str = "qwen3.6-plus",
    ):
        self._client = client
        self._target = target_per_account
        self._ttl = ttl_seconds
        self._default_model = default_model
        self._queues: dict[str, deque[_Entry]] = {}
        self._lock = asyncio.Lock()
        self._refill_task: Optional[asyncio.Task] = None
        self._shutdown = False

    @property
    def target(self) -> int:
        return self._target

    @property
    def ttl(self) -> float:
        return self._ttl

    def update_config(self, *, target: int | None = None, ttl_seconds: float | None = None) -> None:
        """运行时热更新参数。target 调小会在下一轮 refill 时把多余的 chat_id 丢掉；
        调大会在下一轮补位时扩容。TTL 变化影响下一次 acquire 的过期判断。"""
        if target is not None:
            self._target = max(0, int(target))
        if ttl_seconds is not None:
            self._ttl = max(30.0, float(ttl_seconds))
        log.info(f"[ChatIdPool] config updated target={self._target} ttl={self._ttl}s")

    async def start(self) -> None:
        """服务启动时调用，完成首轮预热（全量）+ 启动后台补位 loop。"""
        # 立即全量预热，不等 delay
        try:
            await self._refill_once(fill_per_account=self._target)
        except Exception as e:
            log.warning(f"[ChatIdPool] initial prewarm failed: {e}")
        # 启动后台补位 loop
        self._refill_task = asyncio.create_task(self._refill_loop())
        log.info(f"[ChatIdPool] started (target={self._target}, ttl={self._ttl}s)")

    async def stop(self) -> None:
        self._shutdown = True
        if self._refill_task:
            self._refill_task.cancel()
            try:
                await self._refill_task
            except (asyncio.CancelledError, Exception):
                pass

    async def acquire(self, email: str, model: str | None = None) -> Optional[str]:
        """优先从预热池取 chat_id；池空或过期则返回 None（调用方走同步 create_chat）。"""
        if not email:
            return None
        async with self._lock:
            q = self._queues.get(email)
            if not q:
                return None
            now = time.time()
            while q:
                entry = q.popleft()
                if now - entry.created_at < self._ttl:
                    log.debug(f"[ChatIdPool] HIT email={email} chat_id={entry.chat_id}")
                    return entry.chat_id
                # 过期就丢弃继续找下一个
                log.debug(f"[ChatIdPool] expired chat_id={entry.chat_id} email={email}")
            return None

    async def _prewarm_one(self, account, model: str) -> None:
        """为某账号预建一个 chat_id 加入队列。
        注意：必须直接调用 HTTP 创建，不走 executor.create_chat()，
        因为 executor.create_chat() 内部会先 chat_id_pool.acquire()，
        导致刚创建的 chat_id 被自己偷回，pool 永远填不满。"""
        try:
            token = account.token
            email = account.email
            if not token:
                log.warning(f"[ChatIdPool] prewarm skipped email={email}: missing token")
                return
            # 直接用 HTTP 创建 chat，不走 pool acquire
            chat_id = await self._create_chat_direct(token, model)
            async with self._lock:
                q = self._queues.setdefault(email, deque())
                q.append(_Entry(chat_id))
                log.info(f"[ChatIdPool] prewarmed email={email} chat_id={chat_id} pool_size={len(q)}")
        except Exception as e:
            # Make sure empty-string exceptions still show class name
            err = str(e) or type(e).__name__
            log.warning(f"[ChatIdPool] prewarm failed email={getattr(account, 'email', '?')}: {err}")

    async def _create_chat_direct(self, token: str, model: str) -> str:
        """直接通过 HTTP 创建 chat，不调用 executor.create_chat() 避免 pool acquire 循环。"""
        import json as _json
        import time as _time
        engine = self._client
        request_fn = getattr(engine, "_request_json", None)
        if request_fn is None:
            raise Exception("request transport unavailable")
        ts = int(_time.time())
        body = {
            "title": f"api_{ts}",
            "models": [model],
            "chat_mode": "normal",
            "chat_type": "t2t",
            "timestamp": ts,
        }
        r = await request_fn("POST", "/api/v2/chats/new", token, body, timeout=30.0)
        if r["status"] != 200:
            raise Exception(f"create_chat HTTP {r['status']}: {r.get('body', '')[:100]}")
        data = _json.loads(r.get("body", "{}"))
        if not data.get("success") or "id" not in data.get("data", {}):
            raise Exception("Qwen API returned error or missing id")
        return data["data"]["id"]

    async def _refill_loop(self) -> None:
        """定期轮询：每账号池低于 target 则补位。30 秒一轮。"""
        interval = 30.0
        while not self._shutdown:
            try:
                await self._refill_once(fill_per_account=1)
            except Exception as e:
                log.warning(f"[ChatIdPool] refill error: {e}")
            await asyncio.sleep(interval)

    async def _refill_once(self, fill_per_account: int = 1) -> None:
        """遍历账号池里所有 valid 账号，每个不足 target 就补位。"""
        pool = getattr(self._client, "account_pool", None)
        if pool is None:
            return
        all_accounts = getattr(pool, "accounts", []) or []

        # 只对有 token 的账号预热（用 is_available() 判断而非 status_code 字符串，
        # 因为 Account.__init__ 设置 status_code="" 空字符串，!= "valid"）
        valid = [a for a in all_accounts if getattr(a, "token", "") and a.is_available()]

        for acc in valid:
            async with self._lock:
                q_size = len(self._queues.get(acc.email, []))
            deficit = self._target - q_size
            # fill_per_account 控制单轮最多补多少个
            to_fill = max(0, min(deficit, fill_per_account))
            for _ in range(to_fill):
                await self._prewarm_one(acc, self._default_model)

    async def invalidate(self, email: str, chat_id: str) -> None:
        """标记某个 chat_id 为坏的——从池里移除，防止下次又被取到。

        用于上游返回空响应 / 5xx / 超时后的清理。"""
        if not email or not chat_id:
            return
        async with self._lock:
            q = self._queues.get(email)
            if not q:
                return
            remaining = deque(e for e in q if e.chat_id != chat_id)
            self._queues[email] = remaining
            if len(remaining) != len(q):
                log.info(f"[ChatIdPool] invalidated email={email} chat_id={chat_id}")

    async def flush_account(self, email: str) -> int:
        """把某账号池里的所有 chat_id 清空。用于该账号命中空响应/5xx 后的保守处理，
        防止同批次预热的其他 chat_id 也是坏的。返回清理数量。"""
        if not email:
            return 0
        async with self._lock:
            q = self._queues.get(email)
            if not q:
                return 0
            n = len(q)
            self._queues[email] = deque()
            if n:
                log.info(f"[ChatIdPool] flushed {n} entries for email={email}")
            return n

    async def size(self, email: str) -> int:
        async with self._lock:
            return len(self._queues.get(email, []))

    async def total_size(self) -> int:
        async with self._lock:
            return sum(len(q) for q in self._queues.values())
