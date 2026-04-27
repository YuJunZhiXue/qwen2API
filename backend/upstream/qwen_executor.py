import asyncio
import json
import logging
import time

from backend.core.config import settings
from backend.core.request_logging import update_request_context
from backend.services.auth_resolver import AuthResolver
from backend.upstream.payload_builder import build_chat_payload
from backend.upstream.sse_consumer import parse_sse_chunk

log = logging.getLogger("qwen2api.executor")


class QwenExecutor:
    def __init__(self, engine, account_pool):
        self.engine = engine
        self.account_pool = account_pool
        self.auth_resolver = AuthResolver(account_pool) if account_pool is not None else None
        # 会在 app 启动时被 main.py 注入；若未注入则为 None，走同步 create_chat
        self.chat_id_pool = None

    async def create_chat(self, token: str, model: str, chat_type: str = "t2t") -> str:
        # 预热池快路径：如果能从池里拿到一个已预建的 chat_id 直接用
        # 需要 token 反查 email（通过 account_pool）
        if self.chat_id_pool is not None and self.account_pool is not None:
            try:
                acc = next((a for a in self.account_pool.accounts if a.token == token), None)
                if acc is not None:
                    cached = await self.chat_id_pool.acquire(acc.email, model)
                    if cached:
                        log.info(f"[上游] 预热池命中 邮箱={acc.email} 会话={cached}")
                        # 新创建的 chat_id 可能需要短暂时间才能完成上游 session 初始化，
                        # 不加延迟会导致上游返回空 SSE 流（仅结构事件，~185 bytes 無內容）。
                        # 300ms 远小于池化省下的 500ms-6s，net 仍然大幅收益。
                        await asyncio.sleep(0.3)
                        return cached
            except Exception as e:
                log.debug(f"[Executor] chat_id_pool lookup failed: {e}")

        request_fn = getattr(self.engine, "_request_json", None) or getattr(self.engine, "api_call", None)
        if request_fn is None:
            raise Exception("request transport unavailable")

        ts = int(time.time())
        body = {
            "title": f"api_{ts}",
            "models": [model],
            "chat_mode": "normal",
            "chat_type": chat_type,
            "timestamp": ts,
        }

        if getattr(self.engine, "_request_json", None) is not None:
            r = await request_fn("POST", "/api/v2/chats/new", token, body, timeout=30.0)
        else:
            r = await request_fn("POST", "/api/v2/chats/new", token, body)
        body_text = r.get("body", "")
        if r["status"] != 200:
            body_lower = body_text.lower()
            if (
                r["status"] in (401, 403)
                or "unauthorized" in body_lower
                or "forbidden" in body_lower
                or "token" in body_lower
                or "login" in body_lower
                or "401" in body_text
                or "403" in body_text
            ):
                raise Exception(f"unauthorized: create_chat HTTP {r['status']}: {body_text[:100]}")
            if r["status"] == 429:
                raise Exception("429 Too Many Requests")
            raise Exception(f"create_chat HTTP {r['status']}: {body_text[:100]}")

        try:
            data = json.loads(body_text)
            if not data.get("success") or "id" not in data.get("data", {}):
                raise Exception("Qwen API returned error or missing id")
            return data["data"]["id"]
        except Exception as e:
            body_lower = body_text.lower()
            if any(
                kw in body_lower
                for kw in (
                    "html",
                    "login",
                    "unauthorized",
                    "activation",
                    "pending",
                    "forbidden",
                    "token",
                    "expired",
                    "invalid",
                )
            ):
                raise Exception(f"unauthorized: account issue: {body_text[:200]}")
            raise Exception(f"create_chat parse error: {e}, body={body_text[:200]}")

    async def stream(
        self,
        token: str,
        chat_id: str,
        model: str,
        content: str,
        has_custom_tools: bool = False,
        files: list[dict] | None = None,
    ):
        stream_fn = getattr(self.engine, "stream_chat_once", None) or getattr(self.engine, "fetch_chat", None)
        if stream_fn is None:
            raise Exception("stream transport unavailable")

        payload = build_chat_payload(chat_id, model, content, has_custom_tools, files=files)
        buffer = ""
        started_at = time.perf_counter()
        first_event_logged = False
        last_chunk_time = time.perf_counter()
        total_output_chars = 0  # 方案4：统计输出字符数

        feature_config = payload.get("messages", [{}])[0].get("feature_config", {})
        prompt_len = len(content)
        log.info(f"[上游] 开始流式 会话={chat_id} 模型={model} 自定义工具={has_custom_tools} prompt长度={prompt_len} ({prompt_len/1024:.1f}KB)")
        log.info(f"[上游] 功能配置: function_calling={feature_config.get('function_calling')} auto_search={feature_config.get('auto_search')} code_interpreter={feature_config.get('code_interpreter')} plugins_enabled={feature_config.get('plugins_enabled')}")

        prompt_content = payload.get("messages", [{}])[0].get("content", "")
        if "##TOOL_CALL##" in prompt_content:
            log.info(f"[上游] prompt 包含 ##TOOL_CALL## 标记（正常）")
        else:
            log.warning(f"[上游] prompt 缺少 ##TOOL_CALL## 标记 — 可能导致上游拦截")
        log.info(f"[上游] prompt 前 500 字预览: {prompt_content[:500]}")

        try:
            async for chunk_result in stream_fn(token, chat_id, payload):
                last_chunk_time = time.perf_counter()

                if chunk_result.get("status") not in (None, 200, "streamed"):
                    body = chunk_result.get("body", b"")
                    if isinstance(body, bytes):
                        body = body.decode("utf-8", errors="ignore")
                    raise Exception(f"HTTP {chunk_result['status']}: {str(body)[:100]}")

                if "chunk" in chunk_result:
                    buffer += chunk_result["chunk"]
                    total_output_chars += len(chunk_result["chunk"])
                    while "\n\n" in buffer:
                        msg, buffer = buffer.split("\n\n", 1)
                        for evt in parse_sse_chunk(msg):
                            if not first_event_logged:
                                first_event_logged = True
                                log.info(
                                    f"[上游] 首个事件耗时 {(time.perf_counter() - started_at):.3f}s 会话={chat_id}"
                                )
                            yield evt
        except Exception as e:
            elapsed = time.perf_counter() - started_at
            idle_time = time.perf_counter() - last_chunk_time
            error_type = type(e).__name__
            log.error(
                f"[上游] 流错误 会话={chat_id} 错误类型={error_type} "
                f"已耗时={elapsed:.3f}s 空闲={idle_time:.3f}s 错误={str(e)[:200]}"
            )
            raise

        if buffer:
            for evt in parse_sse_chunk(buffer):
                if not first_event_logged:
                    first_event_logged = True
                    log.info(
                        f"[上游] 首个事件耗时 {(time.perf_counter() - started_at):.3f}s 会话={chat_id}"
                    )
                yield evt

        elapsed = time.perf_counter() - started_at
        # 检测异常短回复（通常是上游超时的信号）
        if has_custom_tools and total_output_chars < 20 and elapsed > 5.0:
            log.warning(f"[上游] 异常短回复 仅 {total_output_chars} 字符 耗时 {elapsed:.1f}s — 疑似上游超时")
            raise Exception(f"Upstream timeout suspected: only {total_output_chars} chars in {elapsed:.1f}s")

        log.info(f"[上游] 流结束 会话={chat_id} 总耗时={elapsed:.3f}s 流字节={total_output_chars}")

    async def chat_stream_events_with_retry(
        self,
        model: str,
        content: str,
        has_custom_tools: bool = False,
        files: list[dict] | None = None,
        fixed_account=None,
        existing_chat_id: str | None = None,
    ):
        exclude = set()
        if fixed_account is not None:
            update_request_context(upstream_attempt=1)
            acc = fixed_account
            try:
                log.info(f"[上游] 使用指定账号 账号={acc.email} 模型={model}")
                chat_id = existing_chat_id or await self.create_chat(acc.token, model)
                update_request_context(chat_id=chat_id)
                if existing_chat_id:
                    log.info(f"[上游] 复用会话 会话={chat_id} 账号={acc.email}")
                else:
                    log.info(f"[上游] 创建会话 会话={chat_id} 账号={acc.email}")
                yield {"type": "meta", "chat_id": chat_id, "acc": acc}
                async for evt in self.stream(acc.token, chat_id, model, content, has_custom_tools, files=files):
                    yield {"type": "event", "event": evt}
                return
            except Exception:
                self.account_pool.release(acc)
                raise

        for attempt in range(settings.MAX_RETRIES):
            update_request_context(upstream_attempt=attempt + 1)
            acquire_start = time.perf_counter()
            acc = await self.account_pool.acquire_wait(timeout=60, exclude=exclude)
            acquire_elapsed = time.perf_counter() - acquire_start
            if not acc:
                raise Exception("No available accounts in pool (all busy or rate limited)")

            try:
                log.info(f"[上游] 账号已获取 账号={acc.email} 模型={model} 第{attempt + 1}次 获取耗时={acquire_elapsed:.3f}s")
                create_start = time.perf_counter()
                chat_id = await self.create_chat(acc.token, model)
                create_elapsed = time.perf_counter() - create_start
                update_request_context(chat_id=chat_id)
                log.info(f"[上游] 创建会话 会话={chat_id} 账号={acc.email} 耗时={create_elapsed:.3f}s")
                yield {"type": "meta", "chat_id": chat_id, "acc": acc}

                async for evt in self.stream(acc.token, chat_id, model, content, has_custom_tools, files=files):
                    yield {"type": "event", "event": evt}
                return

            except Exception as e:
                err_msg = str(e).lower()
                is_timeout = (
                    "timeout" in err_msg
                    or "timed out" in err_msg
                    or "readtimeout" in err_msg
                    or type(e).__name__ in ("ReadTimeout", "TimeoutError", "TimeoutException")
                )

                if is_timeout:
                    log.warning(f"[上游] 超时 第{attempt + 1}/{settings.MAX_RETRIES}次 账号={acc.email} 错误={e}")
                    exclude.add(acc.email)
                elif "429" in err_msg or "rate limit" in err_msg or "too many" in err_msg:
                    self.account_pool.mark_rate_limited(acc)
                    exclude.add(acc.email)
                elif "unauthorized" in err_msg or "401" in err_msg or "403" in err_msg:
                    self.account_pool.mark_invalid(acc)
                    exclude.add(acc.email)
                    if "activation" in err_msg or "pending" in err_msg:
                        acc.activation_pending = True
                    if self.auth_resolver is not None:
                        asyncio.create_task(self.auth_resolver.auto_heal_account(acc))
                else:
                    exclude.add(acc.email)

                self.account_pool.release(acc)
                log.warning(
                    f"[上游] 重试 第{attempt + 1}/{settings.MAX_RETRIES}次 账号={acc.email} 错误={e}"
                )

        raise Exception(f"All {settings.MAX_RETRIES} attempts failed. Please check upstream accounts.")
