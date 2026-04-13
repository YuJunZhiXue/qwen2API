"""
httpx_engine.py — 用 curl_cffi 直连 Qwen API（Chrome TLS 指纹）
优点：TLS 指纹与真实 Chrome 一致，无编码问题，支持流式早期中止
"""

import asyncio
import json
import logging

log = logging.getLogger("qwen2api.httpx_engine")

BASE_URL = "https://chat.qwen.ai"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://chat.qwen.ai/",
    "Origin": "https://chat.qwen.ai",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

_IMPERSONATE = "chrome124"


class HttpxEngine:
    """Direct curl_cffi engine — Chrome TLS fingerprint, same interface as BrowserEngine."""

    def __init__(self, pool_size: int = 3, base_url: str = BASE_URL):
        self.base_url = base_url
        self._started = False
        self._ready = asyncio.Event()

    async def start(self):
        self._started = True
        self._ready.set()
        log.info("[HttpxEngine] 已启动（curl_cffi Chrome指纹直连模式）")

    async def stop(self):
        self._started = False
        log.info("[HttpxEngine] 已停止")

    def _auth_headers(self, token: str) -> dict:
        return {**_HEADERS, "Authorization": f"Bearer {token}"}

    async def api_call(self, method: str, path: str, token: str, body: dict = None) -> dict:
        from curl_cffi.requests import AsyncSession
        url = self.base_url + path
        headers = {**self._auth_headers(token), "Content-Type": "application/json"}
        data = json.dumps(body, ensure_ascii=False).encode() if body else None
        try:
            async with AsyncSession(impersonate=_IMPERSONATE, timeout=30) as client:
                resp = await client.request(method, url, headers=headers, data=data)
            return {"status": resp.status_code, "body": resp.text}
        except Exception as e:
            log.error(f"[HttpxEngine] api_call error: {e}")
            return {"status": 0, "body": str(e)}

    async def fetch_chat(self, token: str, chat_id: str, payload: dict, buffered: bool = False):
        """Stream Qwen SSE via curl_cffi with Chrome TLS fingerprint."""
        from curl_cffi.requests import AsyncSession
        url = self.base_url + f"/api/v2/chat/completions?chat_id={chat_id}"
        headers = {
            **self._auth_headers(token),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        body_bytes = json.dumps(payload, ensure_ascii=False).encode()

        try:
            async with AsyncSession(impersonate=_IMPERSONATE, timeout=1800) as client:
                async with client.stream("POST", url, headers=headers, data=body_bytes) as resp:
                    if resp.status_code != 200:
                        body_chunks = []
                        async for chunk in resp.aiter_content():
                            body_chunks.append(chunk)
                        body_text = b"".join(body_chunks).decode(errors="replace")[:2000]
                        yield {"status": resp.status_code, "body": body_text}
                        return

                    async for chunk in resp.aiter_content():
                        decoded = chunk.decode("utf-8", errors="replace")
                        yield {"status": "streamed", "chunk": decoded}

        except Exception as e:
            log.error(f"[HttpxEngine] fetch_chat error: {e}")
            yield {"status": 0, "body": str(e)}
