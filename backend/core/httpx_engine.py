"""
httpx_engine.py — 用 httpx 直连 Qwen API，替代浏览器引擎
优点：无编码问题、支持流式早期中止、启动无需等待浏览器
"""

import asyncio
import json
import logging

import httpx

log = logging.getLogger("qwen2api.httpx_engine")

BASE_URL = "https://chat.qwen.ai"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://chat.qwen.ai/",
    "Origin": "https://chat.qwen.ai",
    "Connection": "keep-alive",
}


class HttpxEngine:
    """Direct httpx engine — same interface as BrowserEngine."""

    def __init__(self, pool_size: int = 3, base_url: str = BASE_URL):
        self.base_url = base_url
        self._started = False
        self._ready = asyncio.Event()

    async def start(self):
        self._started = True
        self._ready.set()
        log.info("[HttpxEngine] 已启动（直连模式，无需浏览器）")

    async def stop(self):
        self._started = False
        log.info("[HttpxEngine] 已停止")

    def _auth_headers(self, token: str) -> dict:
        return {**_HEADERS, "Authorization": f"Bearer {token}"}

    async def api_call(self, method: str, path: str, token: str, body: dict = None) -> dict:
        url = self.base_url + path
        headers = {**self._auth_headers(token), "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.request(
                    method, url,
                    headers=headers,
                    content=json.dumps(body, ensure_ascii=False).encode() if body else None,
                )
            return {"status": resp.status_code, "body": resp.text}
        except Exception as e:
            log.error(f"[HttpxEngine] api_call error: {e}")
            return {"status": 0, "body": str(e)}

    async def fetch_chat(self, token: str, chat_id: str, payload: dict, buffered: bool = False):
        """Stream Qwen SSE; yield chunks as they arrive. Abort early on NativeBlock."""
        url = self.base_url + f"/api/v2/chat/completions?chat_id={chat_id}"
        headers = {
            **self._auth_headers(token),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        body_bytes = json.dumps(payload, ensure_ascii=False).encode()

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=1800)) as client:
                async with client.stream("POST", url, headers=headers, content=body_bytes) as resp:
                    if resp.status_code != 200:
                        text = await resp.aread()
                        yield {"status": resp.status_code, "body": text.decode(errors="replace")[:2000]}
                        return

                    buf = ""
                    async for raw_chunk in resp.aiter_bytes():
                        chunk = raw_chunk.decode("utf-8", errors="replace")
                        buf += chunk
                        yield {"status": "streamed", "chunk": chunk}

        except httpx.TimeoutException as e:
            log.warning(f"[HttpxEngine] timeout: {e}")
            yield {"status": 0, "body": f"Timeout: {e}"}
        except Exception as e:
            log.error(f"[HttpxEngine] fetch_chat error: {e}")
            yield {"status": 0, "body": str(e)}
