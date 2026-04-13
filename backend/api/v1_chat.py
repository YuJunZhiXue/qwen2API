from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
import asyncio as aio
import json
import logging
import uuid
import time
import re
from typing import Optional
from backend.core.account_pool import Account
from backend.services.qwen_client import QwenClient
from backend.services.token_calc import calculate_usage
from backend.services.prompt_builder import messages_to_prompt
from backend.services.tool_parser import parse_tool_calls, inject_format_reminder, build_tool_blocks_from_native_chunks, should_block_tool_call
from backend.core.config import resolve_model, settings, IMAGE_MODEL_DEFAULT

log = logging.getLogger("qwen2api.chat")
router = APIRouter()

async def _stream_items_with_keepalive(client, model: str, prompt: str, has_custom_tools: bool, exclude_accounts=None):
    queue: aio.Queue = aio.Queue()

    async def _producer():
        try:
            async for item in client.chat_stream_events_with_retry(model, prompt, has_custom_tools=has_custom_tools, exclude_accounts=exclude_accounts):
                await queue.put(("item", item))
        except Exception as e:
            await queue.put(("error", e))
        finally:
            await queue.put(("done", None))

    producer_task = aio.create_task(_producer())
    try:
        while True:
            try:
                kind, payload = await aio.wait_for(queue.get(), timeout=max(1, settings.STREAM_KEEPALIVE_INTERVAL))
            except aio.TimeoutError:
                yield {"type": "keepalive"}
                continue

            if kind == "item":
                yield payload
            elif kind == "error":
                raise payload
            elif kind == "done":
                break
    finally:
        if not producer_task.done():
            producer_task.cancel()
            try:
                await producer_task
            except aio.CancelledError:
                pass

def _extract_blocked_tool_names(text: str) -> list[str]:
    if not text:
        return []
    return re.findall(r"Tool\s+([A-Za-z0-9_.:-]+)\s+does not exists?\.?", text)

def _has_recent_unchanged_read_result(messages) -> bool:
    checked = 0
    for msg in reversed(messages or []):
        checked += 1
        content = msg.get("content", "")
        texts = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    t = part.get("type")
                    if t == "text":
                        texts.append(part.get("text", ""))
                    elif t == "tool_result":
                        inner = part.get("content", "")
                        if isinstance(inner, str):
                            texts.append(inner)
                        elif isinstance(inner, list):
                            for p in inner:
                                if isinstance(p, dict) and p.get("type") == "text":
                                    texts.append(p.get("text", ""))
                elif isinstance(part, str):
                    texts.append(part)
        merged = "\n".join(t for t in texts if t)
        if "Unchanged since last read" in merged:
            return True
        if checked >= 10:
            break
    return False

_T2I_PATTERN = re.compile(
    r'(生成图片|画(一|个|张)?图|draw|generate\s+image|create\s+image|make\s+image|图片生成|文生图|生成一张|画一张)',
    re.IGNORECASE
)
_T2V_PATTERN = re.compile(
    r'(生成视频|make\s+video|generate\s+video|create\s+video|视频生成|文生视频)',
    re.IGNORECASE
)

def _detect_media_intent(messages: list) -> str:
    """Return 't2i', 't2v', or 't2t' based on last user message."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                text = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
            else:
                text = str(content)
            if _T2V_PATTERN.search(text):
                return "t2v"
            if _T2I_PATTERN.search(text):
                return "t2i"
            break
    return "t2t"

def _extract_last_user_text(messages: list) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
            return str(content)
    return ""

def _extract_image_urls(text: str) -> list[str]:
    urls: list[str] = []
    for u in re.findall(r'!\[.*?\]\((https?://[^\s\)]+)\)', text):
        urls.append(u.rstrip(").,;"))
    if not urls:
        for u in re.findall(r'"(?:url|image|src|imageUrl|image_url)"\s*:\s*"(https?://[^"]+)"', text):
            urls.append(u)
    if not urls:
        cdn_pattern = r'https?://(?:wanx\.alicdn\.com|img\.alicdn\.com|[^\s"<>]+\.(?:jpg|jpeg|png|webp|gif))[^\s"<>]*'
        for u in re.findall(cdn_pattern, text, re.IGNORECASE):
            urls.append(u.rstrip(".,;)\"'>"))
    seen: set[str] = set()
    result: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


@router.post("/completions")
@router.post("/chat/completions")
@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    app = request.app
    users_db = app.state.users_db
    client: QwenClient = app.state.qwen_client

    # 鉴权
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""

    if not token:
        token = request.headers.get("x-api-key", "").strip()
    if not token:
        token = request.query_params.get("key", "").strip() or request.query_params.get("api_key", "").strip()

    from backend.core.config import API_KEYS
    admin_k = settings.ADMIN_KEY

    if API_KEYS:
        if token != admin_k and token not in API_KEYS and not token:
            raise HTTPException(status_code=401, detail="Invalid API Key")

    # 获取下游用户并处理配额
    users = await users_db.get()
    user = next((u for u in users if u["id"] == token), None)
    if user and user.get("quota", 0) <= user.get("used_tokens", 0):
        raise HTTPException(status_code=402, detail="Quota Exceeded")
        
    try:
        req_data = await request.json()
    except Exception:
        raise HTTPException(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}})
        
    model_name = req_data.get("model", "gpt-3.5-turbo")
    qwen_model = resolve_model(model_name)
    stream = req_data.get("stream", False)
    
    prompt, tools = messages_to_prompt(req_data)
    log.info(f"[OAI] model={qwen_model}, stream={stream}, tools={[t.get('name') for t in tools]}, prompt_len={len(prompt)}")
    history_messages = req_data.get("messages", [])

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    # Media intent routing: auto-detect image / video generation requests
    media_intent = _detect_media_intent(history_messages)
    if media_intent == "t2v":
        log.warning("[OAI] t2v intent detected but not yet validated; falling back to t2t")
        media_intent = "t2t"

    if media_intent == "t2i":
        image_prompt = _extract_last_user_text(history_messages)
        log.info(f"[OAI-T2I] Routing to image generation, model={IMAGE_MODEL_DEFAULT}, prompt={image_prompt[:80]!r}")

        if stream:
            async def generate_image_stream():
                mk = lambda delta, finish=None: json.dumps({
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": model_name,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]
                }, ensure_ascii=False)
                try:
                    answer_text, acc, chat_id = await client.image_generate_with_retry(IMAGE_MODEL_DEFAULT, image_prompt)
                    client.account_pool.release(acc)
                    aio.create_task(client.delete_chat(acc.token, chat_id))
                    image_urls = _extract_image_urls(answer_text)
                    content = "\n".join(f"![generated]({u})" for u in image_urls) if image_urls else answer_text
                    yield f"data: {mk({'role': 'assistant'})}\n\n"
                    yield f"data: {mk({'content': content})}\n\n"
                    yield f"data: {mk({}, 'stop')}\n\n"
                    yield "data: [DONE]\n\n"
                except Exception as e:
                    log.error(f"[OAI-T2I] 生成失败: {e}")
                    yield f"data: {json.dumps({'error': str(e)})}\n\n"
            return StreamingResponse(generate_image_stream(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        else:
            try:
                answer_text, acc, chat_id = await client.image_generate_with_retry(IMAGE_MODEL_DEFAULT, image_prompt)
                client.account_pool.release(acc)
                aio.create_task(client.delete_chat(acc.token, chat_id))
                image_urls = _extract_image_urls(answer_text)
                content = "\n".join(f"![generated]({u})" for u in image_urls) if image_urls else answer_text
                from fastapi.responses import JSONResponse
                return JSONResponse({
                    "id": completion_id, "object": "chat.completion", "created": created, "model": model_name,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                    "images": image_urls,
                    "usage": {"prompt_tokens": len(image_prompt), "completion_tokens": len(content),
                              "total_tokens": len(image_prompt) + len(content)}
                })
            except Exception as e:
                log.error(f"[OAI-T2I] 生成失败: {e}")
                raise HTTPException(status_code=500, detail=str(e))

    if stream:
        async def generate():
            current_prompt = prompt
            excluded_accounts = set()
            max_attempts = settings.TOOL_MAX_RETRIES if tools else settings.MAX_RETRIES
            for stream_attempt in range(max_attempts):
              try:
                events = []
                chat_id: Optional[str] = None
                acc: Optional[Account] = None

                # ── 无工具：事件到来立即转发给客户端（真流式）──────────────
                if not tools:
                    sent_role = False
                    streamed_len = 0
                    async for item in _stream_items_with_keepalive(client, qwen_model, current_prompt, has_custom_tools=False, exclude_accounts=excluded_accounts):
                        if item["type"] == "keepalive":
                            yield ": keepalive\n\n"
                            continue
                        if item["type"] == "meta":
                            chat_id = item["chat_id"]
                            meta_acc = item["acc"]
                            if isinstance(meta_acc, Account):
                                acc = meta_acc
                            yield ": upstream-connected\n\n"
                            continue
                        if item["type"] != "event":
                            continue
                        evt = item["event"]
                        if evt.get("type") != "delta":
                            continue
                        phase = evt.get("phase", "")
                        content = evt.get("content", "")
                        if phase == "answer" and content:
                            if not sent_role:
                                mk = lambda delta, finish=None: json.dumps({
                                    "id": completion_id, "object": "chat.completion.chunk",
                                    "created": created, "model": model_name,
                                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]
                                }, ensure_ascii=False)
                                yield f"data: {mk({'role': 'assistant'})}\n\n"
                                sent_role = True
                            streamed_len += len(content)
                            yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model_name, 'choices': [{'index': 0, 'delta': {'content': content}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"

                    # 空响应重试（还没发过内容才重试）
                    if streamed_len == 0 and stream_attempt < min(settings.EMPTY_RESPONSE_RETRIES, max_attempts - 1):
                        if acc is not None:
                            client.account_pool.release(acc)
                            if chat_id:
                                aio.create_task(client.delete_chat(acc.token, chat_id))
                            excluded_accounts.add(acc.email)
                        log.warning(f"[Stream] 空响应，重试 (attempt {stream_attempt+1}/{settings.MAX_RETRIES})")
                        await aio.sleep(0.3)
                        continue

                    if not sent_role:
                        yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model_name, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model_name, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"

                    users = await users_db.get()
                    for u in users:
                        if u["id"] == token:
                            u["used_tokens"] += streamed_len + len(prompt)
                            break
                    await users_db.save(users)
                    if acc is not None:
                        client.account_pool.release(acc)
                        if chat_id:
                            aio.create_task(client.delete_chat(acc.token, chat_id))
                    return

                # ── 有工具：缓冲完整响应后解析工具调用（原逻辑）──────────────
                async for item in _stream_items_with_keepalive(client, qwen_model, current_prompt, has_custom_tools=bool(tools), exclude_accounts=excluded_accounts):
                    if item["type"] == "keepalive":
                        yield ": keepalive\n\n"
                        continue
                    if item["type"] == "meta":
                        chat_id = item["chat_id"]
                        meta_acc = item["acc"]
                        if isinstance(meta_acc, Account):
                            acc = meta_acc
                        yield ": upstream-connected\n\n"
                        continue
                    if item["type"] == "event":
                        events.append(item["event"])

                answer_text = ""
                reasoning_text = ""
                native_tc_chunks: dict = {}
                for evt in events:
                    if evt["type"] != "delta":
                        continue
                    phase = evt.get("phase", "")
                    content = evt.get("content", "")
                    if phase in ("think", "thinking_summary") and content:
                        reasoning_text += content
                    elif phase == "answer" and content:
                        answer_text += content
                    elif phase == "tool_call" and content:
                        tc_id = evt.get("extra", {}).get("tool_call_id", "tc_0")
                        if tc_id not in native_tc_chunks:
                            native_tc_chunks[tc_id] = {"name": "", "args": ""}
                        try:
                            chunk = json.loads(content)
                            if "name" in chunk:
                                native_tc_chunks[tc_id]["name"] = chunk["name"]
                            if "arguments" in chunk:
                                native_tc_chunks[tc_id]["args"] += chunk["arguments"]
                        except (json.JSONDecodeError, ValueError):
                            native_tc_chunks[tc_id]["args"] += content
                    if evt.get("status") == "finished" and phase == "answer":
                        break

                log.info(
                    f"[OAI-诊断] 流式轮次={stream_attempt+1}/{settings.MAX_RETRIES} answer_len={len(answer_text)} reasoning_len={len(reasoning_text)} "
                    f"native_tc_count={len(native_tc_chunks)} event_count={len(events)}"
                )
                if native_tc_chunks and not answer_text:
                    log.info(f"[SSE-stream] 检测到 Qwen 原生 tool_call 事件: {list(native_tc_chunks.keys())}")
                tool_blocks, stop = build_tool_blocks_from_native_chunks(native_tc_chunks, tools) if tools else ([], "end_turn")
                if tool_blocks and stop == "tool_use":
                    tool_names = [b.get("name") for b in tool_blocks if b.get("type") == "tool_use"]
                    log.info(f"[NativePass-OAI] 直接使用原生工具调用分片，count={len(tool_blocks)} tools={tool_names}")
                else:
                    tool_blocks, stop = parse_tool_calls(answer_text, tools)
                has_tool_call = stop == "tool_use"

                blocked_names = _extract_blocked_tool_names(answer_text.strip())
                if blocked_names:
                    log.info(f"[OAI-诊断] 检测到上游拦截工具名 blocked_names={blocked_names} has_tool_call={has_tool_call} native_tc_count={len(native_tc_chunks)}")
                if blocked_names and tools and not has_tool_call and stream_attempt < max_attempts - 1:
                    blocked_name = blocked_names[0]
                    if acc is not None:
                        client.account_pool.release(acc)
                        if chat_id:
                            aio.create_task(client.delete_chat(acc.token, chat_id))
                        excluded_accounts.add(acc.email)
                    log.warning(f"[NativeBlock-Stream] Qwen拦截原生工具调用 '{blocked_name}'，注入格式纠正后重试 (attempt {stream_attempt+1}/{settings.MAX_RETRIES})")
                    current_prompt = inject_format_reminder(current_prompt, blocked_name)
                    await aio.sleep(0.15)
                    continue

                if has_tool_call:
                    first_tool = next((b for b in tool_blocks if b.get("type") == "tool_use"), None)
                    if first_tool:
                        blocked_tool_call, blocked_reason = should_block_tool_call(history_messages, first_tool.get("name", ""), first_tool.get("input", {}))
                        if blocked_tool_call and stream_attempt < max_attempts - 1:
                            if acc:
                                client.account_pool.release(acc)
                                if chat_id:
                                    aio.create_task(client.delete_chat(acc.token, chat_id))
                            current_prompt = current_prompt.rstrip()
                            force_text = (
                                f"[MANDATORY NEXT STEP]: {blocked_reason}. "
                                f"Do NOT call the same tool with the same arguments again. "
                                f"Choose another tool or provide final answer."
                            )
                            if current_prompt.endswith("Assistant:"):
                                current_prompt = current_prompt[:-len("Assistant:")] + force_text + "\nAssistant:"
                            else:
                                current_prompt += "\n\n" + force_text + "\nAssistant:"
                            log.warning(f"[ToolLoop-OAI] 阻止重复工具调用：tool={first_tool.get('name')} reason={blocked_reason} (attempt {stream_attempt+1}/{max_attempts})")
                            await aio.sleep(0.15)
                            continue
                    if (first_tool and first_tool.get("name") == "Read"
                            and _has_recent_unchanged_read_result(history_messages)
                            and stream_attempt < max_attempts - 1):
                        if acc:
                            client.account_pool.release(acc)
                            if chat_id:
                                aio.create_task(client.delete_chat(acc.token, chat_id))
                        current_prompt = current_prompt.rstrip()
                        force_text = (
                            "[MANDATORY NEXT STEP]: You just received 'Unchanged since last read'. "
                            "Do NOT call Read again on the same target. "
                            "Choose another tool now."
                        )
                        if current_prompt.endswith("Assistant:"):
                            current_prompt = current_prompt[:-len("Assistant:")] + force_text + "\nAssistant:"
                        else:
                            current_prompt += "\n\n" + force_text + "\nAssistant:"
                        log.warning(f"[ToolLoop-OAI] 检测到 Unchanged since last read，立即阻止重复 Read (attempt {stream_attempt+1}/{settings.MAX_RETRIES})")
                        await aio.sleep(0.15)
                        continue

                mk = lambda delta, finish=None: json.dumps({
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": model_name,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]
                }, ensure_ascii=False)

                # Role chunk
                yield f"data: {mk({'role': 'assistant'})}\n\n"

                if has_tool_call:
                    # Emit tool_calls chunks (OpenAI streaming format)
                    tc_list = [b for b in tool_blocks if b["type"] == "tool_use"]
                    for idx, tc in enumerate(tc_list):
                        # Function name chunk
                        yield f"data: {mk({'tool_calls': [{'index': idx, 'id': tc['id'], 'type': 'function', 'function': {'name': tc['name'], 'arguments': ''}}]})}\n\n"
                        # Arguments chunk
                        yield f"data: {mk({'tool_calls': [{'index': idx, 'function': {'arguments': json.dumps(tc.get('input', {}), ensure_ascii=False)}}]})}\n\n"
                    yield f"data: {mk({}, 'tool_calls')}\n\n"
                else:
                    # Thinking chunks
                    if reasoning_text:
                        yield f"data: {mk({'reasoning_content': reasoning_text})}\n\n"
                    # Content chunks
                    if answer_text:
                        yield f"data: {mk({'content': answer_text})}\n\n"
                    yield f"data: {mk({}, 'stop')}\n\n"

                yield "data: [DONE]\n\n"
                
                users = await users_db.get()
                for u in users:
                    if u["id"] == token:
                        u["used_tokens"] += len(answer_text) + len(prompt)
                        break
                await users_db.save(users)
                
                if acc:
                    client.account_pool.release(acc)
                    if chat_id:
                        import asyncio
                        aio.create_task(client.delete_chat(acc.token, chat_id))
                return  # success — exit the retry loop
              except HTTPException as he:
                yield f"data: {json.dumps({'error': he.detail})}\n\n"
                return
              except Exception as e:
                if acc and acc.inflight > 0:
                    client.account_pool.release(acc)
                    if chat_id:
                        import asyncio
                        aio.create_task(client.delete_chat(acc.token, chat_id))
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                return

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    else:
        current_prompt = prompt
        excluded_accounts = set()
        max_attempts = settings.TOOL_MAX_RETRIES if tools else settings.MAX_RETRIES
        acc: Optional[Account] = None
        chat_id: Optional[str] = None
        for stream_attempt in range(max_attempts):
            try:
                events = []
                chat_id = None
                acc = None
                
                async for item in client.chat_stream_events_with_retry(qwen_model, current_prompt, has_custom_tools=bool(tools), exclude_accounts=excluded_accounts):
                    if item["type"] == "meta":
                        chat_id = item["chat_id"]
                        acc = item["acc"]
                        continue
                    if item["type"] == "event":
                        events.append(item["event"])

                answer_text = ""
                reasoning_text = ""
                native_tc_chunks: dict = {}
                for evt in events:
                    if evt["type"] != "delta":
                        continue
                    phase = evt.get("phase", "")
                    content = evt.get("content", "")
                    if phase in ("think", "thinking_summary") and content:
                        reasoning_text += content
                    elif phase == "answer" and content:
                        answer_text += content
                    elif phase == "tool_call" and content:
                        tc_id = evt.get("extra", {}).get("tool_call_id", "tc_0")
                        if tc_id not in native_tc_chunks:
                            native_tc_chunks[tc_id] = {"name": "", "args": ""}
                        try:
                            chunk = json.loads(content)
                            if "name" in chunk:
                                native_tc_chunks[tc_id]["name"] = chunk["name"]
                            if "arguments" in chunk:
                                native_tc_chunks[tc_id]["args"] += chunk["arguments"]
                        except (json.JSONDecodeError, ValueError):
                            native_tc_chunks[tc_id]["args"] += content
                    if evt.get("status") == "finished" and phase == "answer":
                        break
                        
                if native_tc_chunks and not answer_text:
                    tc_parts = []
                    for tc_id, tc in native_tc_chunks.items():
                        name = tc["name"]
                        try:
                            inp = json.loads(tc["args"]) if tc["args"] else {}
                        except (json.JSONDecodeError, ValueError):
                            inp = {"raw": tc["args"]}
                        tc_parts.append(f'<tool_call>{{"name": {json.dumps(name)}, "input": {json.dumps(inp, ensure_ascii=False)}}}</tool_call>')
                    answer_text = "\n".join(tc_parts)

                blocked_names = _extract_blocked_tool_names(answer_text.strip())
                if blocked_names and tools and stream_attempt < max_attempts - 1:
                    blocked_name = blocked_names[0]
                    if acc:
                        client.account_pool.release(acc)
                        if chat_id:
                            aio.create_task(client.delete_chat(acc.token, chat_id))
                    current_prompt = inject_format_reminder(current_prompt, blocked_name)
                    await aio.sleep(0.15)
                    continue

                tool_blocks, stop = parse_tool_calls(answer_text, tools)
                has_tool_call = stop == "tool_use"
                if has_tool_call:
                    first_tool = next((b for b in tool_blocks if b.get("type") == "tool_use"), None)
                    if first_tool:
                        blocked_tool_call, blocked_reason = should_block_tool_call(history_messages, first_tool.get("name", ""), first_tool.get("input", {}))
                        if blocked_tool_call and stream_attempt < max_attempts - 1:
                            if acc:
                                client.account_pool.release(acc)
                                if chat_id:
                                    aio.create_task(client.delete_chat(acc.token, chat_id))
                            current_prompt = current_prompt.rstrip()
                            force_text = (
                                f"[MANDATORY NEXT STEP]: {blocked_reason}. "
                                f"Do NOT call the same tool with the same arguments again. "
                                f"Choose another tool or provide final answer."
                            )
                            if current_prompt.endswith("Assistant:"):
                                current_prompt = current_prompt[:-len("Assistant:")] + force_text + "\nAssistant:"
                            else:
                                current_prompt += "\n\n" + force_text + "\nAssistant:"
                            log.warning(f"[ToolLoop-OAI] 阻止重复工具调用：tool={first_tool.get('name')} reason={blocked_reason} (attempt {stream_attempt+1}/{max_attempts})")
                            await aio.sleep(0.15)
                            continue
                    if (first_tool and first_tool.get("name") == "Read"
                            and _has_recent_unchanged_read_result(history_messages)
                            and stream_attempt < max_attempts - 1):
                        if acc:
                            client.account_pool.release(acc)
                            if chat_id:
                                aio.create_task(client.delete_chat(acc.token, chat_id))
                        current_prompt = current_prompt.rstrip()
                        force_text = (
                            "[MANDATORY NEXT STEP]: You just received 'Unchanged since last read'. "
                            "Do NOT call Read again on the same target. "
                            "Choose another tool now."
                        )
                        if current_prompt.endswith("Assistant:"):
                            current_prompt = current_prompt[:-len("Assistant:")] + force_text + "\nAssistant:"
                        else:
                            current_prompt += "\n\n" + force_text + "\nAssistant:"
                        log.warning(f"[ToolLoop-OAI] 检测到 Unchanged since last read，立即阻止重复 Read (attempt {stream_attempt+1}/{settings.MAX_RETRIES})")
                        await aio.sleep(0.15)
                        continue

                if has_tool_call:
                    tc_list = [b for b in tool_blocks if b["type"] == "tool_use"]
                    oai_tool_calls = [{
                        "id": tc["id"], "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc.get("input", {}), ensure_ascii=False)
                        }
                    } for tc in tc_list]
                    msg = {"role": "assistant", "content": None, "tool_calls": oai_tool_calls}
                    finish_reason = "tool_calls"
                else:
                    msg = {"role": "assistant", "content": answer_text}
                    if reasoning_text:
                        msg["reasoning_content"] = reasoning_text
                    finish_reason = "stop"

                users = await users_db.get()
                for u in users:
                    if u["id"] == token:
                        u["used_tokens"] += len(answer_text) + len(prompt)
                        break
                await users_db.save(users)

                if acc:
                    client.account_pool.release(acc)
                    if chat_id:
                        import asyncio
                        aio.create_task(client.delete_chat(acc.token, chat_id))

                from fastapi.responses import JSONResponse
                return JSONResponse({
                    "id": completion_id, "object": "chat.completion", "created": created, "model": model_name,
                    "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
                    "usage": {"prompt_tokens": len(prompt), "completion_tokens": len(answer_text),
                              "total_tokens": len(prompt) + len(answer_text)}
                })
            except Exception as e:
                if acc and acc.inflight > 0:
                    client.account_pool.release(acc)
                    if chat_id:
                        import asyncio
                        aio.create_task(client.delete_chat(acc.token, chat_id))
                if stream_attempt == settings.MAX_RETRIES - 1:
                    raise HTTPException(status_code=500, detail=str(e))
                await aio.sleep(1)
