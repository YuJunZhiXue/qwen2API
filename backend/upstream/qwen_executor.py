import asyncio
import json
import logging
import time

from backend.core.config import settings
from backend.core.request_logging import update_request_context
from backend.core.request_trace import find_test_markers, prompt_tail
from backend.services.auth_resolver import AuthResolver
from backend.upstream.payload_builder import build_chat_payload, normalize_upstream_chat_type
from backend.upstream.sse_consumer import parse_sse_chunk

log = logging.getLogger("qwen2api.executor")


def _format_upstream_error(obj: dict) -> str | None:
    if not isinstance(obj, dict):
        return None

    request_id = obj.get("request_id") or obj.get("response_id") or "-"
    if obj.get("success") is False:
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        code = data.get("code") or obj.get("code") or "upstream_error"
        details = data.get("details") or data.get("message") or obj.get("details") or obj.get("message") or ""
        return f"Qwen upstream error code={code} request_id={request_id} details={details}"

    error = obj.get("error")
    if isinstance(error, dict):
        code = error.get("code") or "upstream_error"
        details = error.get("details") or error.get("message") or error.get("type") or ""
        return f"Qwen upstream error code={code} request_id={request_id} details={details}"
    if isinstance(error, str) and error:
        return f"Qwen upstream error request_id={request_id} details={error}"
    return None


def _extract_upstream_error(text: str) -> str | None:
    """Find explicit upstream JSON errors in plain JSON or SSE chunks."""
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]" or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        message = _format_upstream_error(obj)
        if message:
            return message
    return None


def _common_prefix_len(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def _normalize_reasoning_snapshot(evt: dict, previous_snapshot: str) -> tuple[dict, str]:
    if evt.get("type") != "delta":
        return evt, previous_snapshot

    phase = evt.get("phase")
    if phase not in ("think", "thinking_summary"):
        if previous_snapshot and phase in ("answer", "tool_call"):
            return evt, ""
        return evt, previous_snapshot

    content = evt.get("content", "")
    if evt.get("status") == "finished" and not content:
        return evt, ""
    if not evt.get("content_is_snapshot") or not content:
        return evt, previous_snapshot

    prefix_len = _common_prefix_len(previous_snapshot, content)
    normalized = dict(evt)
    normalized["content"] = content[prefix_len:]
    normalized["content_is_snapshot"] = False
    return normalized, content


class QwenExecutor:
    def __init__(self, engine, account_pool):
        self.engine = engine
        self.account_pool = account_pool
        self.auth_resolver = AuthResolver(account_pool) if account_pool is not None else None
        # 会在 app 启动时被 main.py 注入；若未注入则为 None，走同步 create_chat
        self.chat_id_pool = None
        self._active_chat_ids: set[str] = set()

    def active_chat_ids(self) -> set[str]:
        """Return upstream chat IDs currently used by in-flight streams."""
        return set(self._active_chat_ids)

    async def _delete_chat_on_close(self, token: str, chat_id: str, *, source: str) -> None:
        background_delete = getattr(self.engine, "delete_chat_background", None)
        if background_delete is not None:
            background_delete(token, chat_id, source=source)
            return
        delete_fn = getattr(self.engine, "delete_chat_reliable", None)
        if delete_fn is not None:
            await delete_fn(token, chat_id, source=source)
            return
        raw_delete = getattr(self.engine, "delete_chat", None)
        if raw_delete is not None:
            try:
                await raw_delete(token, chat_id)
            except Exception as exc:
                log.warning("[DeleteChat] failed chat_id=%s source=%s error=%s", chat_id, source, exc)

    async def create_chat(self, token: str, model: str, chat_type: str = "t2t", *, use_prewarmed: bool = True) -> str:
        # 预热池快路径：如果能从池里拿到一个已预建的 chat_id 直接用
        # 需要 token 反查 email（通过 account_pool）
        if use_prewarmed and self.chat_id_pool is not None and self.account_pool is not None:
            try:
                acc = next((a for a in self.account_pool.accounts if a.token == token), None)
                if acc is not None:
                    cached = await self.chat_id_pool.acquire(acc.email, model)
                    if cached:
                        log.info(f"[上游] 预热池命中 邮箱={acc.email} 会话={cached}")
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
        chat_type: str = "t2t",
        image_options: dict | None = None,
        thinking_enabled: bool | None = None,
        enable_search: bool = False,
    ):
        stream_fn = getattr(self.engine, "stream_chat_once", None) or getattr(self.engine, "fetch_chat", None)
        if stream_fn is None:
            raise Exception("stream transport unavailable")

        payload = build_chat_payload(
            chat_id,
            model,
            content,
            has_custom_tools,
            files=files,
            chat_type=chat_type,
            image_options=image_options,
            thinking_enabled=thinking_enabled,
            enable_search=enable_search,
        )
        buffer = ""
        started_at = time.perf_counter()
        first_event_logged = False
        last_chunk_time = time.perf_counter()
        total_output_chars = 0  # 方案4：统计输出字符数
        parsed_event_count = 0
        raw_tail = ""
        reasoning_snapshot = ""

        feature_config = payload.get("messages", [{}])[0].get("feature_config", {})
        prompt_len = len(content)
        log.info(f"[上游] 开始流式 会话={chat_id} 模型={model} 自定义工具={has_custom_tools} prompt长度={prompt_len} ({prompt_len/1024:.1f}KB)")
        test_markers = find_test_markers(content)
        if test_markers or settings.TRACE_RESPONSE_FINGERPRINTS:
            log.info(
                "[UpstreamPrompt] marker=%s chat_id=%s model=%s prompt_len=%s prompt_tail=%r",
                ",".join(test_markers) if test_markers else "-",
                chat_id,
                model,
                prompt_len,
                prompt_tail(content),
            )
        log.info(f"[上游] 功能配置: chat_type={chat_type} thinking_enabled={feature_config.get('thinking_enabled')} auto_thinking={feature_config.get('auto_thinking')} thinking_mode={feature_config.get('thinking_mode')} function_calling={feature_config.get('function_calling')} auto_search={feature_config.get('auto_search')} code_interpreter={feature_config.get('code_interpreter')} plugins_enabled={feature_config.get('plugins_enabled')} default_aspect_ratio={feature_config.get('default_aspect_ratio')} image_size={feature_config.get('image_size')} image_ratio={feature_config.get('image_ratio')}")

        prompt_content = payload.get("messages", [{}])[0].get("content", "")
        if has_custom_tools:
            tool_marker_present = any(
                marker in prompt_content
                for marker in ("<|QNML|tool_calls", "<|QNML|invoke", "<tool_calls", "<invoke", "##TOOL_CALL##")
            )
            if tool_marker_present:
                log.debug("[Upstream] prompt contains QNML/legacy tool markers")
            else:
                log.warning("[Upstream] prompt missing QNML tool markers; upstream may block")
        log.debug(f"[Upstream] prompt preview first 500 chars: {prompt_content[:500]}")

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
                    raw_tail = (raw_tail + chunk_result["chunk"])[-500:]
                    while "\n\n" in buffer:
                        msg, buffer = buffer.split("\n\n", 1)
                        upstream_error = _extract_upstream_error(msg)
                        if upstream_error:
                            raise Exception(upstream_error)
                        for evt in parse_sse_chunk(msg):
                            evt, reasoning_snapshot = _normalize_reasoning_snapshot(evt, reasoning_snapshot)
                            parsed_event_count += 1
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
            upstream_error = _extract_upstream_error(buffer)
            if upstream_error:
                raise Exception(upstream_error)
            for evt in parse_sse_chunk(buffer):
                evt, reasoning_snapshot = _normalize_reasoning_snapshot(evt, reasoning_snapshot)
                parsed_event_count += 1
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
        if parsed_event_count == 0:
            upstream_error = _extract_upstream_error(raw_tail)
            if upstream_error:
                raise Exception(upstream_error)
            log.warning(
                "[上游] SSE 未解析到有效 delta 会话=%s 流字节=%s raw_tail=%r",
                chat_id,
                total_output_chars,
                raw_tail,
            )

    async def chat_stream_events_with_retry(
        self,
        model: str,
        content: str,
        has_custom_tools: bool = False,
        files: list[dict] | None = None,
        fixed_account=None,
        existing_chat_id: str | None = None,
        delete_on_close: bool = False,
        use_prewarmed: bool = True,
        chat_type: str = "t2t",
        image_options: dict | None = None,
        thinking_enabled: bool | None = None,
        enable_search: bool = False,
    ):
        exclude = set()
        last_error_message: str | None = None
        if fixed_account is not None:
            update_request_context(upstream_attempt=1)
            acc = fixed_account
            meta_yielded = False
            try:
                log.info(f"[上游] 使用指定账号 账号={acc.email} 模型={model}")
                create_chat_type = normalize_upstream_chat_type(chat_type)
                chat_id = existing_chat_id or await self.create_chat(acc.token, model, chat_type=create_chat_type, use_prewarmed=use_prewarmed)
                self._active_chat_ids.add(chat_id)
                update_request_context(chat_id=chat_id)
                if existing_chat_id:
                    log.info(f"[上游] 复用会话 会话={chat_id} 账号={acc.email}")
                else:
                    log.info(f"[上游] 创建会话 会话={chat_id} 账号={acc.email}")
                should_delete_chat = delete_on_close and not existing_chat_id
                try:
                    meta_yielded = True
                    yield {"type": "meta", "chat_id": chat_id, "acc": acc}
                    async for evt in self.stream(
                        acc.token,
                        chat_id,
                        model,
                        content,
                        has_custom_tools,
                        files=files,
                        chat_type=chat_type,
                        image_options=image_options,
                        thinking_enabled=thinking_enabled,
                        enable_search=enable_search,
                    ):
                        yield {"type": "event", "event": evt}
                finally:
                    self._active_chat_ids.discard(chat_id)
                    if should_delete_chat:
                        await self._delete_chat_on_close(acc.token, chat_id, source="stream_close")
                return
            except BaseException as e:
                if isinstance(e, asyncio.CancelledError) or isinstance(e, Exception) or not meta_yielded:
                    self.account_pool.release(acc)
                raise

        for attempt in range(settings.MAX_RETRIES):
            update_request_context(upstream_attempt=attempt + 1)
            acquire_start = time.perf_counter()
            acc = await self.account_pool.acquire_wait(timeout=60, exclude=exclude)
            meta_yielded = False
            acquire_elapsed = time.perf_counter() - acquire_start
            if not acc:
                raise Exception("No available accounts in pool (all busy or rate limited)")

            try:
                log.info(f"[上游] 账号已获取 账号={acc.email} 模型={model} 第{attempt + 1}次 获取耗时={acquire_elapsed:.3f}s")
                create_start = time.perf_counter()
                create_chat_type = normalize_upstream_chat_type(chat_type)
                chat_id = await self.create_chat(acc.token, model, chat_type=create_chat_type, use_prewarmed=use_prewarmed)
                self._active_chat_ids.add(chat_id)
                create_elapsed = time.perf_counter() - create_start
                update_request_context(chat_id=chat_id)
                log.info(f"[上游] 创建会话 会话={chat_id} 账号={acc.email} 耗时={create_elapsed:.3f}s")
                should_delete_chat = delete_on_close
                try:
                    meta_yielded = True
                    yield {"type": "meta", "chat_id": chat_id, "acc": acc}

                    async for evt in self.stream(
                        acc.token,
                        chat_id,
                        model,
                        content,
                        has_custom_tools,
                        files=files,
                        chat_type=chat_type,
                        image_options=image_options,
                        thinking_enabled=thinking_enabled,
                        enable_search=enable_search,
                    ):
                        yield {"type": "event", "event": evt}
                finally:
                    self._active_chat_ids.discard(chat_id)
                    if should_delete_chat:
                        await self._delete_chat_on_close(acc.token, chat_id, source="stream_close")
                return

            except BaseException as e:
                if not isinstance(e, Exception):
                    if isinstance(e, asyncio.CancelledError) or not meta_yielded:
                        self.account_pool.release(acc)
                    raise
                err_msg = str(e).lower()
                last_error_message = str(e)
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

        if last_error_message:
            raise Exception(f"All {settings.MAX_RETRIES} attempts failed. Last error: {last_error_message}")
        raise Exception(f"All {settings.MAX_RETRIES} attempts failed. Please check upstream accounts.")
