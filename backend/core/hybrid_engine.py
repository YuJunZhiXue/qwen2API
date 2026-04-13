"""
hybrid_engine.py — mix browser stability with httpx speed.
Phase 1 policy:
- api_call: httpx first, browser fallback on failures
- fetch_chat: browser first (real browser TLS/env), httpx fallback on browser failures
"""

import logging

log = logging.getLogger("qwen2api.hybrid_engine")


class HybridEngine:
    def __init__(self, browser_engine, httpx_engine):
        self.browser_engine = browser_engine
        self.httpx_engine = httpx_engine
        self._started = False
        self.base_url = getattr(browser_engine, "base_url", getattr(httpx_engine, "base_url", "https://chat.qwen.ai"))
        self.pool_size = getattr(browser_engine, "pool_size", 0)
        self._pages = getattr(browser_engine, "_pages", None)

    async def start(self):
        log.info("[HybridEngine] 启动开始：先启动 httpx 引擎")
        await self.httpx_engine.start()
        log.info("[HybridEngine] 第一步完成：httpx 已启动，继续启动浏览器引擎")
        await self.browser_engine.start()
        self._started = bool(getattr(self.httpx_engine, "_started", False) and getattr(self.browser_engine, "_started", False))
        log.info(f"[HybridEngine] 已启动：api_call=httpx优先，fetch_chat=browser优先，started={self._started} browser_started={getattr(self.browser_engine, '_started', False)} httpx_started={getattr(self.httpx_engine, '_started', False)}")

    async def stop(self):
        try:
            await self.httpx_engine.stop()
        finally:
            await self.browser_engine.stop()
        self._started = False
        log.info("[HybridEngine] 已停止")

    async def api_call(self, method: str, path: str, token: str, body: dict = None) -> dict:
        log.info(f"[HybridEngine] api_call 路由：优先走 httpx，method={method} path={path}")
        result = await self.httpx_engine.api_call(method, path, token, body)
        status = result.get("status")
        body_text = (result.get("body") or "").lower()
        should_fallback = (
            status == 0
            or status in (401, 403, 429)
            or "waf" in body_text
            or "<!doctype" in body_text
            or "forbidden" in body_text
            or "unauthorized" in body_text
        )
        if should_fallback:
            preview = (result.get("body") or "")[:160].replace("\n", "\\n")
            log.warning(f"[HybridEngine] api_call 回退到 browser，method={method} path={path} status={status} body_preview={preview!r}")
            return await self.browser_engine.api_call(method, path, token, body)
        log.info(f"[HybridEngine] api_call 实际由 httpx 完成，method={method} path={path} status={status}")
        return result

    async def fetch_chat(self, token: str, chat_id: str, payload: dict, buffered: bool = False):
        log.info(f"[HybridEngine] fetch_chat 路由：优先走 browser，chat_id={chat_id} buffered={buffered}")
        saw_success = False
        browser_error = None
        try:
            async for item in self.browser_engine.fetch_chat(token, chat_id, payload, buffered=buffered):
                status = item.get("status")
                if status in ("streamed", 200):
                    saw_success = True
                    yield item
                    continue
                # 浏览器返回错误，判断是否需要回退
                body_text = (item.get("body") or "").lower()
                is_hard_failure = (
                    status in (401, 403, 429)
                    or "waf" in body_text
                    or "<!doctype" in body_text
                    or "forbidden" in body_text
                    or "unauthorized" in body_text
                )
                if is_hard_failure and not saw_success:
                    browser_error = item
                    break
                # 浏览器引擎自身错误（evaluate失败等），也回退
                if status == 0 and not saw_success:
                    browser_error = item
                    break
                yield item
            if browser_error is None:
                return
        except Exception as e:
            if saw_success:
                return
            browser_error = {"status": 0, "body": str(e)}

        preview = ((browser_error.get("body") or "")[:160]).replace("\n", "\\n") if isinstance(browser_error, dict) else str(browser_error)[:160]
        log.warning(
            f"[HybridEngine] fetch_chat browser 失败，回退到 httpx：chat_id={chat_id} "
            f"status={browser_error.get('status') if isinstance(browser_error, dict) else 'unknown'} "
            f"body_preview={preview!r}"
        )
        async for item in self.httpx_engine.fetch_chat(token, chat_id, payload, buffered=buffered):
            yield item

    def status(self) -> dict:
        free_pages = 0
        queue = 0
        if self._pages is not None:
            try:
                free_pages = self._pages.qsize()
                queue = max(0, self.pool_size - free_pages)
            except Exception:
                free_pages = 0
                queue = 0
        return {
            "started": self._started,
            "mode": "hybrid",
            "stream_via": "browser_first",
            "api_via": "httpx_first",
            "browser_started": getattr(self.browser_engine, "_started", False),
            "httpx_started": getattr(self.httpx_engine, "_started", False),
            "pool_size": self.pool_size,
            "free_pages": free_pages,
            "queue": queue,
        }
