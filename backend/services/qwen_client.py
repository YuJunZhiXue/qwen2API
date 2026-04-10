import asyncio
import json
import logging
import time
import uuid
from typing import Optional, Any
from backend.core.account_pool import AccountPool, Account
from backend.core.config import settings
from backend.services.auth_resolver import AuthResolver

log = logging.getLogger("qwen2api.client")

AUTH_FAIL_KEYWORDS = ("token", "unauthorized", "expired", "forbidden", "401", "403", "invalid", "login", "activation", "pending activation", "not activated")
PENDING_ACTIVATION_KEYWORDS = ("pending activation", "please check your email", "not activated")
BANNED_KEYWORDS = ("banned", "suspended", "blocked", "disabled", "risk control", "violat", "forbidden by policy")

def _is_auth_error(error_msg: str) -> bool:
    msg = error_msg.lower()
    return any(keyword in msg for keyword in AUTH_FAIL_KEYWORDS)

def _is_pending_activation_error(error_msg: str) -> bool:
    msg = error_msg.lower()
    return any(keyword in msg for keyword in PENDING_ACTIVATION_KEYWORDS)

def _is_banned_error(error_msg: str) -> bool:
    msg = error_msg.lower()
    return any(keyword in msg for keyword in BANNED_KEYWORDS)

class QwenClient:
    def __init__(self, engine: Any, account_pool: AccountPool):
        self.engine = engine
        self.account_pool = account_pool
        self.auth_resolver = AuthResolver(account_pool)
        self.active_chat_ids: set[str] = set()  # 正在使用中的 chat_id，GC 不得焚烧

    async def create_chat(self, token: str, model: str, chat_type: str = "t2t") -> str:
        ts = int(time.time())
        body = {"title": f"api_{ts}", "models": [model], "chat_mode": "normal",
                "chat_type": chat_type, "timestamp": ts}

        # chat 生命周期接口也优先走浏览器，更贴近真人使用路径
        if hasattr(self.engine, "browser_engine") and getattr(self.engine, "browser_engine") is not None:
            r = await self.engine.browser_engine.api_call("POST", "/api/v2/chats/new", token, body)
            status = r.get("status")
            body_text = (r.get("body") or "").lower()
            should_fallback = (
                status == 0
                or status in (401, 403, 429)
                or "waf" in body_text
                or "<!doctype" in body_text
                or "forbidden" in body_text
                or "unauthorized" in body_text
            )
            if should_fallback:
                preview = (r.get("body") or "")[:160].replace("\n", "\\n")
                log.warning(f"[QwenClient] create_chat 浏览器失败，回退到默认引擎 status={status} body_preview={preview!r}")
                r = await self.engine.api_call("POST", "/api/v2/chats/new", token, body)
        else:
            r = await self.engine.api_call("POST", "/api/v2/chats/new", token, body)
        if r["status"] == 429:
            raise Exception("429 Too Many Requests (Engine Queue Full)")

        body_text = r.get("body", "")
        if r["status"] != 200:
            body_lower = body_text.lower()
            if (r["status"] in (401, 403)
                    or "unauthorized" in body_lower or "forbidden" in body_lower
                    or "token" in body_lower or "login" in body_lower
                    or "401" in body_text or "403" in body_text):
                raise Exception(f"unauthorized: create_chat HTTP {r['status']}: {body_text[:100]}")
            raise Exception(f"create_chat HTTP {r['status']}: {body_text[:100]}")

        try:
            data = json.loads(body_text)
            if not data.get("success") or "id" not in data.get("data", {}):
                raise Exception("Qwen API returned error or missing id")
            return data["data"]["id"]
        except Exception as e:
            body_lower = body_text.lower()
            if any(kw in body_lower for kw in ("html", "login", "unauthorized", "activation",
                                                "pending", "forbidden", "token", "expired", "invalid")):
                raise Exception(f"unauthorized: account issue: {body_text[:200]}")
            raise Exception(f"create_chat parse error: {e}, body={body_text[:200]}")

    async def delete_chat(self, token: str, chat_id: str):
        if hasattr(self.engine, "browser_engine") and getattr(self.engine, "browser_engine") is not None:
            r = await self.engine.browser_engine.api_call("DELETE", f"/api/v2/chats/{chat_id}", token)
            status = r.get("status")
            body_text = (r.get("body") or "").lower()
            should_fallback = (
                status == 0
                or status in (401, 403, 429)
                or "waf" in body_text
                or "<!doctype" in body_text
                or "forbidden" in body_text
                or "unauthorized" in body_text
            )
            if should_fallback:
                preview = (r.get("body") or "")[:160].replace("\n", "\\n")
                log.warning(f"[QwenClient] delete_chat 浏览器失败，回退到默认引擎 chat_id={chat_id} status={status} body_preview={preview!r}")
                await self.engine.api_call("DELETE", f"/api/v2/chats/{chat_id}", token)
            return
        await self.engine.api_call("DELETE", f"/api/v2/chats/{chat_id}", token)

    async def verify_token(self, token: str) -> bool:
        """Verify token validity via direct HTTP (no browser page needed)."""
        if not token:
            return False

        try:
            import httpx
            from backend.services.auth_resolver import BASE_URL

            # 伪造浏览器指纹，避免被 Aliyun WAF 拦截
            headers = {
                "Authorization": f"Bearer {token}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": "https://chat.qwen.ai/",
                "Origin": "https://chat.qwen.ai",
                "Connection": "keep-alive"
            }

            async with httpx.AsyncClient(timeout=15) as hc:
                resp = await hc.get(
                    f"{BASE_URL}/api/v1/auths/",
                    headers=headers,
                )
            if resp.status_code != 200:
                return False

            # 增加对空响应/非 JSON 响应的容错，防止 GFW 拦截或代理返回假 200 OK 导致崩溃
            try:
                data = resp.json()
                return data.get("role") == "user"
            except Exception as e:
                log.warning(f"[verify_token] JSON parse error (可能是被拦截或代理异常): {e}, status={resp.status_code}, text={resp.text[:100]}")
                # 如果遇到阿里云 WAF 拦截，通常是因为 httpx 直接请求被墙，或者 token 本身就是正常的。
                # 由于这是为了快速验证，如果被 WAF 拦截 (HTML)，我们姑且假定它是活着的，交给后面的浏览器引擎去真实处理
                if "aliyun_waf" in resp.text.lower() or "<!doctype" in resp.text.lower():
                    log.info(f"[verify_token] 遇到 WAF 拦截页面，放行交给底层无头浏览器引擎处理。")
                    return True
                return False
        except Exception as e:
            log.warning(f"[verify_token] HTTP error: {e}")
            return False

    async def list_models(self, token: str) -> list:
        try:
            import httpx
            from backend.services.auth_resolver import BASE_URL

            headers = {
                "Authorization": f"Bearer {token}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://chat.qwen.ai/",
                "Origin": "https://chat.qwen.ai",
                "Connection": "keep-alive"
            }

            async with httpx.AsyncClient(timeout=10) as hc:
                resp = await hc.get(
                    f"{BASE_URL}/api/models",
                    headers=headers,
                )
            if resp.status_code != 200:
                return []
            try:
                return resp.json().get("data", [])
            except Exception as e:
                log.warning(f"[list_models] JSON parse error: {e}, status={resp.status_code}, text={resp.text[:100]}")
                return []
        except Exception:
            return []

    def _build_payload(self, chat_id: str, model: str, content: str, has_custom_tools: bool = False) -> dict:
        ts = int(time.time())
        # 有工具时关闭思考模式——工具调用只需要输出结构化 JSON，思考会白白浪费几十秒
        feature_config = {
            "thinking_enabled": not has_custom_tools,
            "output_schema": "phase",
            "research_mode": "normal",
            "auto_thinking": not has_custom_tools,
            "thinking_mode": "off" if has_custom_tools else "Auto",
            "thinking_format": "summary",
            "auto_search": not has_custom_tools,
            "code_interpreter": not has_custom_tools,
            "function_calling": bool(has_custom_tools and settings.NATIVE_TOOL_PASSTHROUGH),
            "plugins_enabled": False if has_custom_tools else True,
        }
        return {
            "stream": True, "version": "2.1", "incremental_output": True,
            "chat_id": chat_id, "chat_mode": "normal", "model": model, "parent_id": None,
            "messages": [{
                "fid": str(uuid.uuid4()), "parentId": None, "childrenIds": [str(uuid.uuid4())],
                "role": "user", "content": content, "user_action": "chat", "files": [],
                "timestamp": ts, "models": [model], "chat_type": "t2t",
                "feature_config": feature_config,
                "extra": {"meta": {"subChatType": "t2t"}}, "sub_chat_type": "t2t", "parent_id": None,
            }],
            "timestamp": ts,
        }

    def _build_image_payload(self, chat_id: str, model: str, prompt: str) -> dict:
        ts = int(time.time())
        feature_config = {
            "thinking_enabled": False,
            "output_schema": "phase",
            "auto_thinking": False,
            "thinking_mode": "off",
            "auto_search": False,
            "code_interpreter": False,
            "function_calling": False,
            "plugins_enabled": True,
            "image_generation": True,
            "default_aspect_ratio": "16:9",
        }
        return {
            "stream": True,
            "version": "2.1",
            "incremental_output": True,
            "chat_id": chat_id,
            "chat_mode": "normal",
            "model": model,
            "parent_id": None,
            "messages": [{
                "fid": str(uuid.uuid4()),
                "parentId": None,
                "childrenIds": [str(uuid.uuid4())],
                "role": "user",
                "content": prompt,
                "user_action": "chat",
                "files": [],
                "timestamp": ts,
                "models": [model],
                "chat_type": "t2t",
                "feature_config": feature_config,
                "extra": {"meta": {"subChatType": "t2i", "mode": "image_generation", "aspectRatio": "16:9"}},
                "sub_chat_type": "t2i",
                "parent_id": None,
            }],
            "timestamp": ts,
        }

    def parse_sse_chunk(self, chunk: str) -> list[dict]:
        events = []
        for line in chunk.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
                events.append(obj)
            except Exception:
                continue

        parsed = []
        for evt in events:
            if evt.get("choices"):
                delta = evt["choices"][0].get("delta", {})
                parsed.append({
                    "type": "delta",
                    "phase": delta.get("phase", "answer"),
                    "content": delta.get("content", ""),
                    "status": delta.get("status", ""),
                    "extra": delta.get("extra", {})
                })
            elif evt.get("phase"):
                parsed.append({
                    "type": "delta",
                    "phase": evt.get("phase", "answer"),
                    "content": evt.get("content", "") or evt.get("text", "") or "",
                    "status": evt.get("status", ""),
                    "extra": evt.get("extra", {})
                })
        return parsed

    async def chat_stream_events_with_retry(self, model: str, content: str, has_custom_tools: bool = False, exclude_accounts: Optional[set[str]] = None):
        """无感容灾重试逻辑：上游挂了自动换号"""
        exclude = set(exclude_accounts or set())
        for attempt in range(settings.MAX_RETRIES):
            acc = await self.account_pool.acquire_wait(timeout=60, exclude=exclude)
            if not acc:
                pool_status = self.account_pool.status()
                raise Exception(
                    "No available accounts in pool "
                    f"(total={pool_status['total']}, valid={pool_status['valid']}, "
                    f"invalid={pool_status['invalid']}, activation_pending={pool_status.get('activation_pending', 0)}, "
                    f"rate_limited={pool_status['rate_limited']}, in_use={pool_status['in_use']}, waiting={pool_status['waiting']})"
                )
                
            chat_id: Optional[str] = None
            try:
                log.info(f"[重试 {attempt+1}/{settings.MAX_RETRIES}] 获取账号：account={acc.email} model={model} tools={has_custom_tools} exclude={sorted(exclude)}")
                # 本地节流：同账号两次上游请求之间保持最小间隔，降低自动化痕迹
                min_interval = max(0, settings.ACCOUNT_MIN_INTERVAL_MS) / 1000.0
                now = time.time()
                wait_s = max(0.0, (acc.last_request_started + min_interval) - now)
                if wait_s > 0:
                    log.info(f"[节流] 账号冷却等待：account={acc.email} wait={wait_s:.2f}s")
                    await asyncio.sleep(wait_s)
                chat_id = await self.create_chat(acc.token, model)
                self.active_chat_ids.add(chat_id)
                payload = self._build_payload(chat_id, model, content, has_custom_tools)
                log.info(
                    f"[重试 {attempt+1}/{settings.MAX_RETRIES}] 已创建会话：account={acc.email} chat_id={chat_id} "
                    f"engine={self.engine.__class__.__name__} function_calling={payload['messages'][0]['feature_config'].get('function_calling')} "
                    f"thinking_enabled={payload['messages'][0]['feature_config'].get('thinking_enabled')}"
                )

                # First yield the chat_id and account to the consumer
                yield {"type": "meta", "chat_id": chat_id, "acc": acc}

                buffer = ""
                # 始终用流式模式：可实时发现 NativeBlock 并早期中止，不用等 3 分钟
                async for chunk_result in self.engine.fetch_chat(acc.token, chat_id, payload, buffered=False):
                    if chunk_result.get("status") == 429:
                        log.warning(f"[本地背压 {attempt+1}/{settings.MAX_RETRIES}] 引擎队列已满：account={acc.email} chat_id={chat_id}")
                        raise Exception("local_backpressure: engine queue full")
                    if chunk_result.get("status") != 200 and chunk_result.get("status") != "streamed":
                        body_preview = (chunk_result.get("body", "")[:120]).replace("\n", "\\n")
                        log.warning(
                            f"[重试 {attempt+1}/{settings.MAX_RETRIES}] 上游分片异常：account={acc.email} chat_id={chat_id} "
                            f"status={chunk_result.get('status')} body_preview={body_preview!r}"
                        )
                        raise Exception(f"HTTP {chunk_result['status']}: {chunk_result.get('body', '')[:100]}")

                    if "chunk" in chunk_result:
                        buffer += chunk_result["chunk"]
                        while "\n\n" in buffer:
                            msg, buffer = buffer.split("\n\n", 1)
                            events = self.parse_sse_chunk(msg)
                            for evt in events:
                                yield {"type": "event", "event": evt}
                    elif "body" in chunk_result and chunk_result["body"] and chunk_result["body"] != "streamed":
                        buffer += chunk_result["body"]
                
                if buffer:
                    events = self.parse_sse_chunk(buffer)
                    for evt in events:
                        yield {"type": "event", "event": evt}
                log.info(f"[重试 {attempt+1}/{settings.MAX_RETRIES}] 流式完成：account={acc.email} chat_id={chat_id} buffered_chars={len(buffer)}")
                self.active_chat_ids.discard(chat_id)
                return

            except Exception as e:
                if chat_id:
                    self.active_chat_ids.discard(chat_id)  # type: ignore[arg-type]
                err_msg = str(e).lower()
                should_save = False
                if "local_backpressure" in err_msg or "engine queue full" in err_msg:
                    acc.last_error = str(e)
                    log.warning(f"[重试 {attempt+1}/{settings.MAX_RETRIES}] 本地背压：account={acc.email} error={e}")
                elif "429" in err_msg or "rate limit" in err_msg or "too many" in err_msg:
                    self.account_pool.mark_rate_limited(acc, error_message=str(e))
                    exclude.add(acc.email)
                    log.warning(f"[重试 {attempt+1}/{settings.MAX_RETRIES}] 标记为限流：account={acc.email} error={e}")
                elif _is_pending_activation_error(err_msg):
                    self.account_pool.mark_invalid(acc, reason="pending_activation", error_message=str(e))
                    exclude.add(acc.email)
                    acc.activation_pending = True
                    should_save = True
                    log.warning(f"[重试 {attempt+1}/{settings.MAX_RETRIES}] 标记为待激活：account={acc.email} error={e}")
                    asyncio.create_task(self.auth_resolver.auto_heal_account(acc))
                elif _is_banned_error(err_msg):
                    self.account_pool.mark_invalid(acc, reason="banned", error_message=str(e))
                    exclude.add(acc.email)
                    log.warning(f"[重试 {attempt+1}/{settings.MAX_RETRIES}] 标记为封禁：account={acc.email} error={e}")
                elif _is_auth_error(err_msg):
                    self.account_pool.mark_invalid(acc, reason="auth_error", error_message=str(e))
                    exclude.add(acc.email)
                    log.warning(f"[重试 {attempt+1}/{settings.MAX_RETRIES}] 标记为鉴权失败：account={acc.email} error={e}")
                    asyncio.create_task(self.auth_resolver.auto_heal_account(acc))
                else:
                    acc.last_error = str(e)
                    log.warning(f"[重试 {attempt+1}/{settings.MAX_RETRIES}] 瞬态错误：account={acc.email} error={e}")

                if should_save:
                    await self.account_pool.save()

                self.account_pool.release(acc)
                log.warning(f"[重试 {attempt+1}/{settings.MAX_RETRIES}] 账号失败，准备重试：account={acc.email} error={e}")
                
        raise Exception(f"All {settings.MAX_RETRIES} attempts failed. Please check upstream accounts.")

    def _extract_urls_from_extra(self, extra: dict) -> list[str]:
        """从 SSE event 的 extra 字段提取图片 URL。

        已知格式：
        - extra.tool_result[0].image  (image_gen_tool finished 事件，最主要路径)
        - extra.image_url / extra.wanx_image_url / extra.imageUrl
        - extra.image_urls / extra.images / extra.imageUrls (列表)
        """
        urls = []
        if not extra or not isinstance(extra, dict):
            return urls

        # ① image_gen_tool 完成事件：extra.tool_result[].image
        tool_result = extra.get("tool_result")
        if isinstance(tool_result, list):
            for item in tool_result:
                if isinstance(item, dict):
                    for key in ("image", "url", "src", "imageUrl", "image_url"):
                        val = item.get(key)
                        if isinstance(val, str) and val.startswith("http"):
                            urls.append(val)
                elif isinstance(item, str) and item.startswith("http"):
                    urls.append(item)

        # ② 平铺字段
        for key in ("image_url", "wanx_image_url", "imageUrl"):
            val = extra.get(key)
            if isinstance(val, str) and val.startswith("http"):
                urls.append(val)

        # ③ 列表字段
        for key in ("image_urls", "images", "imageUrls"):
            val = extra.get(key)
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, str) and item.startswith("http"):
                        urls.append(item)
                    elif isinstance(item, dict):
                        for sub_key in ("url", "src", "image", "imageUrl"):
                            sub_val = item.get(sub_key)
                            if isinstance(sub_val, str) and sub_val.startswith("http"):
                                urls.append(sub_val)
        return urls

    async def image_generate_with_retry(self, model: str, prompt: str, exclude_accounts: Optional[set[str]] = None) -> tuple[str, "Account", str]:
        """调用千问 T2I 生成图片，返回 (原始响应文本, 使用的账号, chat_id)"""
        exclude = set(exclude_accounts or set())
        for attempt in range(settings.MAX_RETRIES):
            acc = await self.account_pool.acquire_wait(timeout=60, exclude=exclude)
            if not acc:
                pool_status = self.account_pool.status()
                raise Exception(
                    f"No available accounts in pool "
                    f"(valid={pool_status['valid']}, rate_limited={pool_status['rate_limited']})"
                )

            chat_id: Optional[str] = None
            try:
                chat_id = await self.create_chat(acc.token, model, chat_type="t2i")
                self.active_chat_ids.add(chat_id)
                payload = self._build_image_payload(chat_id, model, prompt)

                raw_body_parts: list[str] = []  # 保存原始 SSE body 用于 debug
                answer_text = ""
                extra_urls: list[str] = []
                buffer = ""

                async for chunk_result in self.engine.fetch_chat(acc.token, chat_id, payload):
                    if chunk_result.get("status") == 429:
                        raise Exception("Engine Queue Full")
                    if chunk_result.get("status") not in (200, "streamed"):
                        raise Exception(f"HTTP {chunk_result['status']}: {chunk_result.get('body', '')[:200]}")

                    # 把原始文本拼进 buffer
                    raw = ""
                    if "chunk" in chunk_result:
                        raw = chunk_result["chunk"]
                    elif "body" in chunk_result:
                        raw = chunk_result.get("body", "") or ""
                    if not raw:
                        continue

                    raw_body_parts.append(raw)
                    buffer += raw

                # 处理整个 buffer（不论流式还是一次性返回）
                raw_body = "".join(raw_body_parts)
                log.info(f"[T2I] 原始 SSE body 前 1000 字符: {raw_body[:1000]!r}")

                for line in raw_body.splitlines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str or data_str == "[DONE]":
                        continue
                    try:
                        obj = json.loads(data_str)
                    except Exception:
                        continue

                    # 打印每个 SSE 事件用于诊断
                    log.info(f"[T2I-SSE] 事件: {json.dumps(obj, ensure_ascii=False)[:400]}")

                    # 从 choices[0].delta 提取
                    if obj.get("choices"):
                        delta = obj["choices"][0].get("delta", {})
                        content = delta.get("content", "")
                        phase = delta.get("phase", "answer")
                        extra = delta.get("extra", {})
                        log.info(f"[T2I-SSE] phase={phase!r} content_len={len(content)} content_preview={content[:100]!r}")
                        # 捕获所有文本内容
                        answer_text += content
                        # 捕获 extra 字段里的图片 URL
                        extra_urls.extend(self._extract_urls_from_extra(extra))
                    elif obj.get("phase"):
                        # 直接顶层 phase 格式
                        content = obj.get("content", "") or obj.get("text", "") or ""
                        phase = obj.get("phase", "")
                        extra = obj.get("extra", {})
                        log.info(f"[T2I-SSE] 顶层 phase={phase!r} content_len={len(content)} content_preview={content[:100]!r}")
                        answer_text += content
                        extra_urls.extend(self._extract_urls_from_extra(extra))

                # 如果 extra 里找到了图片 URL，把它们拼成 Markdown 图片格式追加进 answer_text
                if extra_urls:
                    log.info(f"[T2I] 从 extra 字段提取到 {len(extra_urls)} 个图片 URL: {extra_urls}")
                    for url in extra_urls:
                        answer_text += f"\n![image]({url})"

                # 如果 answer_text 为空就用原始 body 作为保底
                if not answer_text:
                    answer_text = raw_body

                self.active_chat_ids.discard(chat_id)
                log.info(f"[T2I] 生成完成，响应长度={len(answer_text)}: {answer_text[:200]!r}")
                return answer_text, acc, chat_id

            except Exception as e:
                if chat_id:
                    self.active_chat_ids.discard(chat_id)  # type: ignore[arg-type]
                err_msg = str(e).lower()
                if "429" in err_msg or "rate limit" in err_msg or "too many" in err_msg:
                    self.account_pool.mark_rate_limited(acc, error_message=str(e))
                elif _is_pending_activation_error(err_msg):
                    self.account_pool.mark_invalid(acc, reason="pending_activation", error_message=str(e))
                    asyncio.create_task(self.auth_resolver.auto_heal_account(acc))
                elif _is_banned_error(err_msg):
                    self.account_pool.mark_invalid(acc, reason="banned", error_message=str(e))
                elif _is_auth_error(err_msg):
                    self.account_pool.mark_invalid(acc, reason="auth_error", error_message=str(e))
                    asyncio.create_task(self.auth_resolver.auto_heal_account(acc))
                    exclude.add(acc.email)
                elif _is_banned_error(err_msg):
                    exclude.add(acc.email)
                elif "429" in err_msg or "rate limit" in err_msg or "too many" in err_msg:
                    pass  # already handled above, mark_rate_limited excludes implicitly
                # 泛化错误不排除账号，允许用同一账号重试
                self.account_pool.release(acc)
                log.warning(f"[T2I Retry {attempt+1}/{settings.MAX_RETRIES}] Account {acc.email} failed: {e}")

        raise Exception(f"All {settings.MAX_RETRIES} T2I attempts failed.")
