import json
import logging
import time
from typing import AsyncIterator

import httpx

from backend.core.account_pool import AccountPool
from backend.services.auth_resolver import BASE_URL, AuthResolver
from backend.upstream.payload_builder import build_chat_payload
from backend.upstream.qwen_executor import QwenExecutor
from backend.upstream.sse_consumer import parse_sse_chunk

log = logging.getLogger("qwen2api.client")


class QwenClient:
    def __init__(self, account_pool: AccountPool):
        self.account_pool = account_pool
        self.auth_resolver = AuthResolver(account_pool) if account_pool is not None else None
        self.executor = QwenExecutor(self, account_pool)

        # HTTP连接池配置（对齐 ds2api 的高性能设置）
        limits = httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=120.0,
        )
        # 增加 read timeout 以支持长任务（工具调用可能需要更长时间）
        timeout = httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)
        self._http_client = httpx.AsyncClient(
            limits=limits,
            timeout=timeout,
            http2=True,
            follow_redirects=True,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._http_client.aclose()
        return False

    @staticmethod
    def _build_headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": f"{BASE_URL}/",
            "Origin": BASE_URL,
            "Connection": "keep-alive",
            "Content-Type": "application/json",
        }

    async def _request_json(self, method: str, path: str, token: str, body: dict | None = None, timeout: float = 30.0) -> dict:
        resp = await self._http_client.request(
            method,
            f"{BASE_URL}{path}",
            headers=self._build_headers(token),
            json=body,
            timeout=timeout,
        )
        return {"status": resp.status_code, "body": resp.text}

    async def create_chat(self, token: str, model: str, chat_type: str = "t2t") -> str:
        return await self.executor.create_chat(token, model, chat_type=chat_type)

    async def delete_chat(self, token: str, chat_id: str):
        await self._request_json("DELETE", f"/api/v2/chats/{chat_id}", token, timeout=20.0)

    async def list_chats(self, token: str, limit: int = 50) -> list[dict]:
        res = await self._request_json("GET", f"/api/v2/chats?limit={limit}", token, timeout=20.0)
        if res["status"] != 200:
            return []
        try:
            data = json.loads(res.get("body", "{}"))
        except Exception:
            return []
        chats = data.get("data", [])
        return chats if isinstance(chats, list) else []

    async def verify_token(self, token: str) -> bool:
        """Verify token validity via direct HTTP (no browser page needed)."""
        if not token:
            return False

        try:
            resp = await self._http_client.get(
                f"{BASE_URL}/api/v1/auths/",
                headers=self._build_headers(token),
                timeout=15.0,
            )
            if resp.status_code != 200:
                return False

            try:
                data = resp.json()
                return data.get("role") == "user"
            except Exception as e:
                log.warning(f"[verify_token] JSON 解析失败（可能被拦截或代理异常）: {e}, status={resp.status_code}, text={resp.text[:100]}")
                if "aliyun_waf" in resp.text.lower() or "<!doctype" in resp.text.lower():
                    log.info("[verify_token] 遇到 WAF 拦截页面，放行交给浏览器自动化账号流程处理。")
                    return True
                return False
        except Exception as e:
            log.warning(f"[verify_token] HTTP 请求异常: {e}")
            return False

    async def list_models(self, token: str) -> list:
        try:
            resp = await self._http_client.get(
                f"{BASE_URL}/api/models",
                headers=self._build_headers(token),
                timeout=10.0,
            )
            if resp.status_code != 200:
                return []
            try:
                return resp.json().get("data", [])
            except Exception as e:
                log.warning(f"[list_models] JSON 解析失败: {e}, status={resp.status_code}, text={resp.text[:100]}")
                return []
        except Exception:
            return []

    # ---- cached upstream-pool model list ----
    # Fetches chat.qwen.ai /api/models using a valid token from the account pool
    # (not the caller's API KEY). Cached for _UPSTREAM_MODELS_TTL seconds.
    _UPSTREAM_MODELS_TTL = 300
    _upstream_models_cache: list[dict] = []
    _upstream_models_fetched_at: float = 0.0

    async def list_models_from_pool(self) -> list[dict]:
        now = time.time()
        if self._upstream_models_cache and (now - self._upstream_models_fetched_at) < self._UPSTREAM_MODELS_TTL:
            return self._upstream_models_cache
        if self.account_pool is None:
            return []
        acc = None
        try:
            acc = await self.account_pool.acquire_wait(timeout=5)
            if not acc:
                return []
            models = await self.list_models(acc.token)
            if models:
                QwenClient._upstream_models_cache = models
                QwenClient._upstream_models_fetched_at = now
            return models
        except Exception as e:
            log.warning(f"[list_models_from_pool] failed: {e}")
            return []
        finally:
            if acc is not None:
                self.account_pool.release(acc)

    def _build_payload(self, chat_id: str, model: str, content: str, has_custom_tools: bool = False, files: list[dict] | None = None) -> dict:
        return build_chat_payload(chat_id, model, content, has_custom_tools, files=files)

    def parse_sse_chunk(self, chunk: str) -> list[dict]:
        return parse_sse_chunk(chunk)

    async def stream(self, token: str, chat_id: str, model: str, content: str, has_custom_tools: bool = False, files: list[dict] | None = None):
        async for event in self.executor.stream(token, chat_id, model, content, has_custom_tools, files=files):
            yield event

    async def stream_chat_once(self, token: str, chat_id: str, payload: dict) -> AsyncIterator[dict]:
        # 使用全局连接池，复用连接（对齐 ds2api）
        async with self._http_client.stream(
            "POST",
            f"{BASE_URL}/api/v2/chat/completions?chat_id={chat_id}",
            headers=self._build_headers(token),
            json=payload,
        ) as resp:
            if resp.status_code != 200:
                yield {"status": resp.status_code, "body": await resp.aread()}
                return
            # 使用 aiter_text() 保证 UTF-8 正确处理和 SSE 格式完整
            async for chunk in resp.aiter_text():
                if chunk:
                    yield {"chunk": chunk}
            yield {"status": "streamed"}

    async def chat_stream_events_with_retry(
        self,
        model: str,
        content: str,
        has_custom_tools: bool = False,
        files: list[dict] | None = None,
        fixed_account=None,
        existing_chat_id: str | None = None,
    ):
        async for item in self.executor.chat_stream_events_with_retry(
            model,
            content,
            has_custom_tools,
            files=files,
            fixed_account=fixed_account,
            existing_chat_id=existing_chat_id,
        ):
            yield item
