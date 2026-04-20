from __future__ import annotations

import asyncio
import copy
import logging
import time
from dataclasses import dataclass
from typing import Any

from backend.core.database import AsyncJsonDB

log = logging.getLogger("qwen2api.responses.store")


@dataclass(slots=True)
class StoredResponse:
    response_id: str
    payload: dict[str, Any]
    history_messages: list[dict[str, Any]]


@dataclass(slots=True)
class _StoredResponseRecord:
    response_id: str
    payload: dict[str, Any]
    history_messages: list[dict[str, Any]]
    saved_at: float
    expires_at: float


class PersistentResponseStore:
    def __init__(
        self,
        path: str,
        *,
        ttl_seconds: int = 86400,
        max_items: int = 1000,
    ) -> None:
        self._db = AsyncJsonDB(path, default_data={"items": []})
        self._ttl_seconds = max(60, int(ttl_seconds))
        self._max_items = max(1, int(max_items))
        self._lock = asyncio.Lock()
        self._loaded = False
        self._items: dict[str, _StoredResponseRecord] = {}

    async def load(self) -> None:
        async with self._lock:
            await self._ensure_loaded_locked()

    async def save(self, response_id: str, payload: dict[str, Any], history_messages: list[dict[str, Any]]) -> None:
        async with self._lock:
            await self._ensure_loaded_locked()
            now = time.time()
            self._items[response_id] = _StoredResponseRecord(
                response_id=response_id,
                payload=copy.deepcopy(payload),
                history_messages=copy.deepcopy(history_messages),
                saved_at=now,
                expires_at=now + self._ttl_seconds,
            )
            self._prune_locked(now=now)
            await self._save_locked()

    async def get(self, response_id: str) -> StoredResponse | None:
        async with self._lock:
            await self._ensure_loaded_locked()
            changed = self._prune_locked()
            item = self._items.get(response_id)
            if changed:
                await self._save_locked()
            if item is None:
                return None
            return StoredResponse(
                response_id=item.response_id,
                payload=copy.deepcopy(item.payload),
                history_messages=copy.deepcopy(item.history_messages),
            )

    async def _ensure_loaded_locked(self) -> None:
        if self._loaded:
            return
        raw = await self._db.load()
        self._items = self._deserialize_items(raw)
        self._loaded = True
        if self._prune_locked():
            await self._save_locked()

    async def _save_locked(self) -> None:
        await self._db.save(
            {
                "items": [
                    {
                        "response_id": item.response_id,
                        "payload": item.payload,
                        "history_messages": item.history_messages,
                        "saved_at": item.saved_at,
                        "expires_at": item.expires_at,
                    }
                    for item in sorted(self._items.values(), key=lambda current: current.saved_at)
                ]
            }
        )

    def _deserialize_items(self, raw: Any) -> dict[str, _StoredResponseRecord]:
        items: list[Any]
        if isinstance(raw, dict) and isinstance(raw.get("items"), list):
            items = raw["items"]
        elif isinstance(raw, list):
            items = raw
        else:
            return {}

        records: dict[str, _StoredResponseRecord] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            response_id = str(item.get("response_id", "") or "").strip()
            if not response_id:
                continue
            payload = item.get("payload", {})
            history_messages = item.get("history_messages", [])
            saved_at = float(item.get("saved_at", 0) or 0)
            expires_at = float(item.get("expires_at", 0) or 0)
            if not isinstance(payload, dict) or not isinstance(history_messages, list):
                continue
            if saved_at <= 0:
                saved_at = time.time()
            if expires_at <= 0:
                expires_at = saved_at + self._ttl_seconds
            records[response_id] = _StoredResponseRecord(
                response_id=response_id,
                payload=payload,
                history_messages=history_messages,
                saved_at=saved_at,
                expires_at=expires_at,
            )
        return records

    def _prune_locked(self, *, now: float | None = None) -> bool:
        changed = False
        current_time = now if now is not None else time.time()

        expired_ids = [response_id for response_id, item in self._items.items() if item.expires_at <= current_time]
        for response_id in expired_ids:
            self._items.pop(response_id, None)
            changed = True

        if len(self._items) <= self._max_items:
            return changed

        overflow = len(self._items) - self._max_items
        for item in sorted(self._items.values(), key=lambda current: current.saved_at)[:overflow]:
            self._items.pop(item.response_id, None)
            changed = True

        if changed:
            log.info(
                "[ResponsesStore] pruned items=%s remaining=%s ttl_seconds=%s max_items=%s",
                len(expired_ids) + max(0, overflow),
                len(self._items),
                self._ttl_seconds,
                self._max_items,
            )
        return changed

