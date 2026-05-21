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
        """服务启动时调用，完成首轮预热 + 启动后台补位 loop。"""
        # 初次预热 & 启动补位 loop
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
        """为某账号预建一个 chat_id 加入队列。"""
        try:
            token = account.token
            email = account.email
            if not token:
                log.warning(f"[ChatIdPool] prewarm skipped email={email}: missing token")
                return
            chat_id = await self._client.executor.create_chat(token, model)
            async with self._lock:
                q = self._queues.setdefault(email, deque())
                q.append(_Entry(chat_id))
                log.info(f"[ChatIdPool] prewarmed email={email} chat_id={chat_id} pool_size={len(q)}")
        except Exception as e:
            # Make sure empty-string exceptions still show class name
            err = str(e) or type(e).__name__
            log.warning(f"[ChatIdPool] prewarm failed email={getattr(account, 'email', '?')}: {err}")

    async def _refill_loop(self) -> None:
        """定期轮询：每账号池低于 target 则补位。30 秒一轮。"""
        interval = 30.0
        # 初始化立即跑一轮
        await asyncio.sleep(1.0)
        while not self._shutdown:
            try:
                await self._refill_once()
            except Exception as e:
                log.warning(f"[ChatIdPool] refill error: {e}")
            await asyncio.sleep(interval)

    async def _refill_once(self) -> None:
        """遍历账号池里所有 valid 账号，每个不足 target 就补位。"""
        pool = getattr(self._client, "account_pool", None)
        if pool is None:
            return
        all_accounts = getattr(pool, "accounts", []) or []

        # 只对有 token + 状态 valid 的账号预热
        valid = [a for a in all_accounts if getattr(a, "token", "") and getattr(a, "status_code", "valid") == "valid"]

        for acc in valid:
            async with self._lock:
                q_size = len(self._queues.get(acc.email, []))
            deficit = self._target - q_size
            # 每轮每账号最多补 1 个，避免突发 API 压力
            if deficit > 0:
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

    async def force_reset(self, reason: str = "tool_call_threshold") -> int:
        """强制清空所有账号的 chat_id 池。
        用于工具调用标记清理：当历史中 ##TOOL_CALL## 标记积累过多时，
        强制所有账号使用全新 chat_id，确保 Qwen 服务端不积累标记。
        返回清理的总数量。"""
        total = 0
        async with self._lock:
            for email in list(self._queues.keys()):
                n = len(self._queues[email])
                total += n
                self._queues[email] = deque()
        if total:
            log.info(f"[ChatIdPool] force_reset reason={reason} cleared {total} chat_ids across {len(self._queues)} accounts")
        return total

    async def size(self, email: str) -> int:
        async with self._lock:
            return len(self._queues.get(email, []))

    async def total_size(self) -> int:
        async with self._lock:
            return sum(len(q) for q in self._queues.values())
