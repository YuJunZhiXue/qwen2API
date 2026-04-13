from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
import asyncio
import json
import logging
import uuid
import re
from typing import Optional
from backend.core.account_pool import Account
from backend.services.qwen_client import QwenClient
from backend.services.token_calc import calculate_usage
from backend.services.prompt_builder import messages_to_prompt
from backend.services.tool_parser import parse_tool_calls, inject_format_reminder, build_tool_blocks_from_native_chunks, should_block_tool_call
from backend.core.config import resolve_model, settings

log = logging.getLogger("qwen2api.anthropic")
router = APIRouter()

async def _stream_items_with_keepalive(client, model: str, prompt: str, has_custom_tools: bool, exclude_accounts=None):
    queue: asyncio.Queue = asyncio.Queue()

    async def _producer():
        try:
            async for item in client.chat_stream_events_with_retry(model, prompt, has_custom_tools=has_custom_tools, exclude_accounts=exclude_accounts):
                await queue.put(("item", item))
        except Exception as e:
            await queue.put(("error", e))
        finally:
            await queue.put(("done", None))

    producer_task = asyncio.create_task(_producer())
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=max(1, settings.STREAM_KEEPALIVE_INTERVAL))
            except asyncio.TimeoutError:
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
            except asyncio.CancelledError:
                pass

def _extract_blocked_tool_names(text: str) -> list[str]:
    if not text:
        return []
    return re.findall(r"Tool\s+([A-Za-z0-9_.:-]+)\s+does not exists?\.?", text)


def _parse_native_call_from_answer(answer_text: str, blocked_name: str) -> dict | None:
    """
    Last-resort: when native_tc_chunks is empty but the model output a native JSON
    tool call in the answer phase before the server added 'Tool X does not exists.',
    try to extract the tool name + args from the raw answer text.
    """
    # Split on the error marker to get the pre-block content
    lower = answer_text.lower()
    idx = lower.find(f"tool {blocked_name.lower()}")
    pre = answer_text[:idx].strip() if idx > 0 else answer_text.strip()
    if not pre:
        return None
    # Strip markdown code fences
    pre = re.sub(r'```(?:json)?\s*', '', pre).strip('`').strip()
    # Find the last top-level JSON object in pre
    last = None
    i = 0
    while i < len(pre):
        pos = pre.find('{', i)
        if pos == -1:
            break
        depth = 0
        for j in range(pos, len(pre)):
            if pre[j] == '{':
                depth += 1
            elif pre[j] == '}':
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(pre[pos:j + 1])
                        if isinstance(obj, dict) and ("name" in obj or "arguments" in obj):
                            last = obj
                    except (json.JSONDecodeError, ValueError):
                        pass
                    break
        i = pos + 1
    if not last:
        return None
    name = last.get("name", blocked_name)
    args = last.get("arguments", last.get("input", last.get("parameters", {})))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, ValueError):
            args = {"raw": args}
    return {"name": name, "input": args}

def _tool_identity(tool_name: str, tool_input=None) -> str:
    try:
        if tool_name == "Read" and isinstance(tool_input, dict):
            return f"Read::{tool_input.get('file_path','').strip()}"
        return f"{tool_name}::{json.dumps(tool_input or {}, ensure_ascii=False, sort_keys=True)}"
    except Exception:
        return tool_name or ""


def _recent_same_tool_identity_count(messages, tool_name: str, tool_input=None) -> int:
    target = _tool_identity(tool_name, tool_input)
    count = 0
    started = False
    for msg in reversed(messages or []):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            if started:
                break
            continue
        tools = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name")]
        if not tools:
            if started:
                break
            continue
        started = True
        if len(tools) == 1 and _tool_identity(tools[0].get("name", ""), tools[0].get("input", {})) == target:
            count += 1
            continue
        break
    return count

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
                    if part.get("type") == "text":
                        texts.append(part.get("text", ""))
                    elif part.get("type") == "tool_result":
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

@router.post("/messages")
@router.post("/v1/messages")
@router.post("/anthropic/v1/messages")
async def anthropic_messages(request: Request):
    app = request.app
    users_db = app.state.users_db
    client: QwenClient = app.state.qwen_client

    # 鉴权
    token = request.headers.get("x-api-key", "").strip()

    if not token:
        bearer = request.headers.get("Authorization", "")
        if bearer.startswith("Bearer "):
            token = bearer[7:].strip()

    if not token:
        token = request.query_params.get("key", "").strip() or request.query_params.get("api_key", "").strip()

    from backend.core.config import API_KEYS
    admin_k = settings.ADMIN_KEY

    if API_KEYS:
        if token != admin_k and token not in API_KEYS and not token:
            raise HTTPException(status_code=401, detail="Invalid API Key")

    users = await users_db.get()
    user = next((u for u in users if u["id"] == token), None)
    if user and user.get("quota", 0) <= user.get("used_tokens", 0):
        raise HTTPException(status_code=402, detail="Quota Exceeded")
        
    try:
        req_data = await request.json()
    except Exception:
        raise HTTPException(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}})
        
    model_name = req_data.get("model", "claude-3-5-sonnet")
    qwen_model = resolve_model(model_name)
    stream = req_data.get("stream", False)
    
    prompt, tools = messages_to_prompt(req_data)
    log.info(f"[ANT] model={qwen_model}, stream={stream}, tools={[t.get('name') for t in tools]}, prompt_len={len(prompt)}")

    msg_id = f"msg_{uuid.uuid4().hex[:12]}"
    history_messages = req_data.get("messages", [])

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

                answer_chunks = []
                thinking_chunks = []
                native_tc_chunks = {}
                for evt in events:
                    if evt["type"] != "delta":
                        continue
                    phase = evt.get("phase", "")
                    content = evt.get("content", "")
                    if phase in ("think", "thinking_summary") and content:
                        thinking_chunks.append(content)
                    elif phase == "answer" and content:
                        answer_chunks.append(content)
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
                        
                answer_text = "".join(answer_chunks)
                reasoning_text = "".join(thinking_chunks)
                log.info(
                    f"[ANT-诊断] 流式轮次={stream_attempt+1}/{max_attempts} answer_len={len(answer_text)} reasoning_len={len(reasoning_text)} "
                    f"native_tc_count={len(native_tc_chunks)} event_count={len(events)}"
                )

                blocks, stop_reason = build_tool_blocks_from_native_chunks(native_tc_chunks, tools) if tools else ([{"type": "text", "text": answer_text}], "end_turn")
                if blocks and stop_reason == "tool_use":
                    tool_names = [b.get("name") for b in blocks if b.get("type") == "tool_use"]
                    log.info(f"[NativePass-ANT] 直接使用原生工具调用分片，count={len(blocks)} tools={tool_names}")
                else:
                    blocks, stop_reason = parse_tool_calls(answer_text, tools) if tools else ([{"type": "text", "text": answer_text}], "end_turn")

                blocked_names = _extract_blocked_tool_names(answer_text.strip())
                if blocked_names:
                    log.info(f"[ANT-诊断] 检测到上游拦截工具名 blocked_names={blocked_names} stop_reason={stop_reason} native_tc_count={len(native_tc_chunks)}")
                if blocked_names and tools and stop_reason != "tool_use":
                    blocked_name = blocked_names[0]
                    # 如果 native_tc_chunks 有数据，直接转换格式，跳过重试（省 60s）
                    if native_tc_chunks:
                        tc = list(native_tc_chunks.values())[0]
                        tc_name = tc.get("name", blocked_name)
                        try:
                            tc_inp = json.loads(tc["args"]) if tc.get("args") else {}
                        except Exception:
                            tc_inp = {}
                        answer_text = f'##TOOL_CALL##\n{{"name": {json.dumps(tc_name)}, "input": {json.dumps(tc_inp, ensure_ascii=True)}}}\n##END_CALL##'
                        log.info(f"[NativeBlock-ANT] 直接转换原生调用 '{tc_name}' → ##TOOL_CALL## 格式，跳过重试")
                        blocked_names = []
                    else:
                        parsed_tc = _parse_native_call_from_answer(answer_text, blocked_name)
                        if parsed_tc:
                            answer_text = f'##TOOL_CALL##\n{{"name": {json.dumps(parsed_tc["name"])}, "input": {json.dumps(parsed_tc["input"], ensure_ascii=True)}}}\n##END_CALL##'
                            log.info(f"[NativeBlock-ANT] 从answer文本提取调用 '{parsed_tc['name']}' → ##TOOL_CALL##，跳过重试")
                            blocked_names = []
                        elif stream_attempt < max_attempts - 1:
                            if acc is not None:
                                client.account_pool.release(acc)
                                if chat_id:
                                    asyncio.create_task(client.delete_chat(acc.token, chat_id))
                                excluded_accounts.add(acc.email)
                            log.warning(f"[NativeBlock-ANT] Qwen拦截了工具 '{blocked_name}' 的原生调用，注入格式纠正后重试 (attempt {stream_attempt+1}/{max_attempts})")
                            current_prompt = inject_format_reminder(current_prompt, blocked_name)
                            await asyncio.sleep(0.15)
                            continue

                if tools:
                    if stop_reason != "tool_use" and reasoning_text:
                        rb, rs = parse_tool_calls(reasoning_text, tools)
                        if rs == "tool_use":
                            blocks, stop_reason = rb, rs
                            log.info("[ToolParse-ANT] 从 thinking 回退提取到工具调用")
                    if stop_reason == "tool_use":
                        tool_blk = next((b for b in blocks if b.get("type") == "tool_use"), None)
                        if tool_blk:
                            blocked_tool_call, blocked_reason = should_block_tool_call(history_messages, tool_blk.get("name", ""), tool_blk.get("input", {}))
                            if blocked_tool_call and stream_attempt < max_attempts - 1:
                                if acc:
                                    client.account_pool.release(acc)
                                    if chat_id:

                                        asyncio.create_task(client.delete_chat(acc.token, chat_id))
                                current_prompt = current_prompt.rstrip()
                                force_text = (
                                    f"[MANDATORY NEXT STEP]: {blocked_reason}. "
                                    f"Do NOT call the same tool with the same arguments again. "
                                    f"Either choose a different tool or provide final answer."
                                )
                                if current_prompt.endswith("Assistant:"):
                                    current_prompt = current_prompt[:-len("Assistant:")] + force_text + "\nAssistant:"
                                else:
                                    current_prompt += "\n\n" + force_text + "\nAssistant:"
                                log.warning(f"[ToolLoop-ANT] 阻止重复工具调用：tool={tool_blk.get('name')} reason={blocked_reason} (attempt {stream_attempt+1}/{max_attempts})")
                                await asyncio.sleep(0.15)
                                continue
                            recent_unchanged = _has_recent_unchanged_read_result(history_messages)
                            if tool_blk.get("name") == "Read" and recent_unchanged and stream_attempt < max_attempts - 1:
                                if acc:
                                    client.account_pool.release(acc)
                                    if chat_id:

                                        asyncio.create_task(client.delete_chat(acc.token, chat_id))
                                current_prompt = current_prompt.rstrip()
                                force_text = (
                                    "[MANDATORY NEXT STEP]: You just received 'Unchanged since last read'. "
                                    "Do NOT call Read again on the same target. "
                                    "Either choose a different tool (Glob/Grep) or provide final answer."
                                )
                                if current_prompt.endswith("Assistant:"):
                                    current_prompt = current_prompt[:-len("Assistant:")] + force_text + "\nAssistant:"
                                else:
                                    current_prompt += "\n\n" + force_text + "\nAssistant:"
                                log.warning(f"[ToolLoop-ANT] ??? Unchanged since last read????? Read (attempt {stream_attempt+1}/{max_attempts})")
                                await asyncio.sleep(0.15)
                                continue
                            same_tool_count = _recent_same_tool_identity_count(history_messages, tool_blk.get("name", ""), tool_blk.get("input", {}))
                            if tool_blk.get("name") != "Read" and same_tool_count >= 2 and stream_attempt < max_attempts - 1:
                                if acc:
                                    client.account_pool.release(acc)
                                    if chat_id:

                                        asyncio.create_task(client.delete_chat(acc.token, chat_id))
                                current_prompt = current_prompt.rstrip()
                                n = tool_blk.get("name", "")
                                force_text = (
                                    f"[MANDATORY NEXT STEP]: You have already called '{n}' at least 2 consecutive turns. "
                                    f"Now you MUST choose a different tool from the list. "
                                    f"Do not call '{n}' again this turn. "
                                    f"Output exactly one ##TOOL_CALL## block."
                                )
                                if current_prompt.endswith("Assistant:"):
                                    current_prompt = current_prompt[:-len("Assistant:")] + force_text + "\nAssistant:"
                                else:
                                    current_prompt += "\n\n" + force_text + "\nAssistant:"
                                log.warning(f"[ToolLoop-ANT] 工具 {n} 连续调用≥2次，强制切换工具 (attempt {stream_attempt+1}/{max_attempts})")
                                await asyncio.sleep(0.15)
                                continue
                    if stop_reason != "tool_use" and not answer_text.strip() and stream_attempt < max_attempts - 1:
                        if acc:
                            client.account_pool.release(acc)
                            if chat_id:

                                asyncio.create_task(client.delete_chat(acc.token, chat_id))
                        current_prompt = current_prompt.rstrip()
                        if current_prompt.endswith("Assistant:"):
                            current_prompt = (
                                current_prompt[:-len("Assistant:")]
                                + "[MANDATORY NEXT STEP]: You MUST output exactly one ##TOOL_CALL## block now. "
                                  "Choose the best tool from the provided list by yourself. "
                                  "Do not answer in plain text.\nAssistant:"
                            )
                        else:
                            current_prompt += (
                                "\n\n[MANDATORY NEXT STEP]: You MUST output exactly one ##TOOL_CALL## block now. "
                                "Choose the best tool from the provided list by yourself. "
                                "Do not answer in plain text.\nAssistant:"
                            )
                        log.warning(f"[ToolParse-ANT] 模型返回空响应或无工具调用，重试 (attempt {stream_attempt+1}/{max_attempts})")
                        await asyncio.sleep(0.15)
                        continue
                else:
                    blocks = [{"type": "text", "text": answer_text}]
                    stop_reason = "end_turn"

                yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [], 'model': model_name, 'stop_reason': None, 'usage': {'input_tokens': len(current_prompt), 'output_tokens': 0}}})}\n\n"

                block_idx = 0

                if reasoning_text:
                    yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': block_idx, 'content_block': {'type': 'thinking', 'thinking': ''}})}\n\n"
                    yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': block_idx, 'delta': {'type': 'thinking_delta', 'thinking': reasoning_text}})}\n\n"
                    yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_idx})}\n\n"
                    block_idx += 1

                for blk in blocks:
                    if blk["type"] == "text" and blk.get("text"):
                        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': block_idx, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
                        yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': block_idx, 'delta': {'type': 'text_delta', 'text': blk['text']}})}\n\n"
                        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_idx})}\n\n"
                        block_idx += 1
                    elif blk["type"] == "tool_use":
                        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': block_idx, 'content_block': {'type': 'tool_use', 'id': blk['id'], 'name': blk['name'], 'input': {}}})}\n\n"
                        yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': block_idx, 'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(blk.get('input', {}), ensure_ascii=False)}})}\n\n"
                        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_idx})}\n\n"
                        block_idx += 1

                yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason}, 'usage': {'output_tokens': len(answer_text)}})}\n\n"
                yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"
                
                users = await users_db.get()
                for u in users:
                    if u["id"] == token:
                        u["used_tokens"] += len(answer_text) + len(prompt)
                        break
                await users_db.save(users)

                if acc is not None:
                    client.account_pool.release(acc)
                    if chat_id:

                        asyncio.create_task(client.delete_chat(acc.token, chat_id))
                return
              except HTTPException as he:
                yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': he.detail}})}\n\n"
                return
              except Exception as e:
                if acc is not None and acc.inflight > 0:
                    client.account_pool.release(acc)
                    if chat_id:

                        asyncio.create_task(client.delete_chat(acc.token, chat_id))
                yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': str(e)}})}\n\n"
                return

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    else:
        current_prompt = prompt
        excluded_accounts = set()
        max_attempts = settings.MAX_RETRIES + (1 if tools else 0)
        excluded_accounts = set()
        acc: Optional[Account] = None
        chat_id: Optional[str] = None
        for stream_attempt in range(max_attempts):
            try:
                events = []
                chat_id: Optional[str] = None
                acc: Optional[Account] = None

                async for item in client.chat_stream_events_with_retry(qwen_model, current_prompt, has_custom_tools=bool(tools), exclude_accounts=excluded_accounts):
                    if item["type"] == "meta":
                        chat_id = item["chat_id"]
                        meta_acc = item["acc"]
                        if isinstance(meta_acc, Account):
                            acc = meta_acc
                        continue
                    if item["type"] == "event":
                        events.append(item["event"])

                answer_chunks = []
                thinking_chunks = []
                native_tc_chunks = {}
                for evt in events:
                    if evt["type"] != "delta":
                        continue
                    phase = evt.get("phase", "")
                    content = evt.get("content", "")
                    if phase in ("think", "thinking_summary") and content:
                        thinking_chunks.append(content)
                    elif phase == "answer" and content:
                        answer_chunks.append(content)
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
                        
                answer_text = "".join(answer_chunks)
                reasoning_text = "".join(thinking_chunks)
                log.info(
                    f"[ANT-诊断] 流式轮次={stream_attempt+1}/{max_attempts} answer_len={len(answer_text)} reasoning_len={len(reasoning_text)} "
                    f"native_tc_count={len(native_tc_chunks)} event_count={len(events)}"
                )

                blocks, stop_reason = build_tool_blocks_from_native_chunks(native_tc_chunks, tools) if tools else ([{"type": "text", "text": answer_text}], "end_turn")
                if blocks and stop_reason == "tool_use":
                    tool_names = [b.get("name") for b in blocks if b.get("type") == "tool_use"]
                    log.info(f"[NativePass-ANT] 直接使用原生工具调用分片，count={len(blocks)} tools={tool_names}")
                else:
                    blocks, stop_reason = parse_tool_calls(answer_text, tools) if tools else ([{"type": "text", "text": answer_text}], "end_turn")

                blocked_names = _extract_blocked_tool_names(answer_text.strip())
                if blocked_names:
                    log.info(f"[ANT-诊断] 检测到上游拦截工具名 blocked_names={blocked_names} stop_reason={stop_reason} native_tc_count={len(native_tc_chunks)}")
                if blocked_names and tools and stop_reason != "tool_use":
                    blocked_name = blocked_names[0]
                    # 如果 native_tc_chunks 有数据，直接转换格式，跳过重试（省 60s）
                    if native_tc_chunks:
                        tc = list(native_tc_chunks.values())[0]
                        tc_name = tc.get("name", blocked_name)
                        try:
                            tc_inp = json.loads(tc["args"]) if tc.get("args") else {}
                        except Exception:
                            tc_inp = {}
                        answer_text = f'##TOOL_CALL##\n{{"name": {json.dumps(tc_name)}, "input": {json.dumps(tc_inp, ensure_ascii=True)}}}\n##END_CALL##'
                        log.info(f"[NativeBlock-ANT] 直接转换原生调用 '{tc_name}' → ##TOOL_CALL## 格式，跳过重试")
                        blocked_names = []
                    else:
                        parsed_tc = _parse_native_call_from_answer(answer_text, blocked_name)
                        if parsed_tc:
                            answer_text = f'##TOOL_CALL##\n{{"name": {json.dumps(parsed_tc["name"])}, "input": {json.dumps(parsed_tc["input"], ensure_ascii=True)}}}\n##END_CALL##'
                            log.info(f"[NativeBlock-ANT] 从answer文本提取调用 '{parsed_tc['name']}' → ##TOOL_CALL##，跳过重试")
                            blocked_names = []
                        elif stream_attempt < max_attempts - 1:
                            if acc is not None:
                                client.account_pool.release(acc)
                                if chat_id:
                                    asyncio.create_task(client.delete_chat(acc.token, chat_id))
                                excluded_accounts.add(acc.email)
                            log.warning(f"[NativeBlock-ANT] Qwen拦截了工具 '{blocked_name}' 的原生调用，注入格式纠正后重试 (attempt {stream_attempt+1}/{max_attempts})")
                            current_prompt = inject_format_reminder(current_prompt, blocked_name)
                            await asyncio.sleep(0.15)
                            continue

                if tools:
                    blocks, stop_reason = parse_tool_calls(answer_text, tools)
                    if stop_reason != "tool_use" and reasoning_text:
                        rb, rs = parse_tool_calls(reasoning_text, tools)
                        if rs == "tool_use":
                            blocks, stop_reason = rb, rs
                            log.info("[ToolParse-ANT] 从 thinking 回退提取到工具调用")
                    if stop_reason == "tool_use":
                        tool_blk = next((b for b in blocks if b.get("type") == "tool_use"), None)
                        if tool_blk:
                            blocked_tool_call, blocked_reason = should_block_tool_call(history_messages, tool_blk.get("name", ""), tool_blk.get("input", {}))
                            if blocked_tool_call and stream_attempt < max_attempts - 1:
                                if acc:
                                    client.account_pool.release(acc)
                                    if chat_id:

                                        asyncio.create_task(client.delete_chat(acc.token, chat_id))
                                current_prompt = current_prompt.rstrip()
                                force_text = (
                                    f"[MANDATORY NEXT STEP]: {blocked_reason}. "
                                    f"Do NOT call the same tool with the same arguments again. "
                                    f"Either choose a different tool or provide final answer."
                                )
                                if current_prompt.endswith("Assistant:"):
                                    current_prompt = current_prompt[:-len("Assistant:")] + force_text + "\nAssistant:"
                                else:
                                    current_prompt += "\n\n" + force_text + "\nAssistant:"
                                log.warning(f"[ToolLoop-ANT] 阻止重复工具调用：tool={tool_blk.get('name')} reason={blocked_reason} (attempt {stream_attempt+1}/{max_attempts})")
                                await asyncio.sleep(0.15)
                                continue
                            recent_unchanged = _has_recent_unchanged_read_result(history_messages)
                            if tool_blk.get("name") == "Read" and recent_unchanged and stream_attempt < max_attempts - 1:
                                if acc:
                                    client.account_pool.release(acc)
                                    if chat_id:

                                        asyncio.create_task(client.delete_chat(acc.token, chat_id))
                                current_prompt = current_prompt.rstrip()
                                force_text = (
                                    "[MANDATORY NEXT STEP]: You just received 'Unchanged since last read'. "
                                    "Do NOT call Read again on the same target. "
                                    "Either choose a different tool (Glob/Grep) or provide final answer."
                                )
                                if current_prompt.endswith("Assistant:"):
                                    current_prompt = current_prompt[:-len("Assistant:")] + force_text + "\nAssistant:"
                                else:
                                    current_prompt += "\n\n" + force_text + "\nAssistant:"
                                log.warning(f"[ToolLoop-ANT] ??? Unchanged since last read????? Read (attempt {stream_attempt+1}/{max_attempts})")
                                await asyncio.sleep(0.15)
                                continue
                            same_tool_count = _recent_same_tool_identity_count(history_messages, tool_blk.get("name", ""), tool_blk.get("input", {}))
                            if tool_blk.get("name") != "Read" and same_tool_count >= 2 and stream_attempt < max_attempts - 1:
                                if acc:
                                    client.account_pool.release(acc)
                                    if chat_id:

                                        asyncio.create_task(client.delete_chat(acc.token, chat_id))
                                current_prompt = current_prompt.rstrip()
                                n = tool_blk.get("name", "")
                                force_text = (
                                    f"[MANDATORY NEXT STEP]: You have already called '{n}' at least 2 consecutive turns. "
                                    f"Now you MUST choose a different tool from the list. "
                                    f"Do not call '{n}' again this turn. "
                                    f"Output exactly one ##TOOL_CALL## block."
                                )
                                if current_prompt.endswith("Assistant:"):
                                    current_prompt = current_prompt[:-len("Assistant:")] + force_text + "\nAssistant:"
                                else:
                                    current_prompt += "\n\n" + force_text + "\nAssistant:"
                                if acc: excluded_accounts.add(acc.email)
                                log.warning(f"[ToolLoop-ANT] 工具 {n} 连续调用≥2次，强制切换工具 (attempt {stream_attempt+1}/{max_attempts})")
                                await asyncio.sleep(0.15)
                                continue
                            # 工具调用合法，继续构建响应（不 continue，不 break）
                        else:
                            if acc:
                                client.account_pool.release(acc)
                                if chat_id:
                                    asyncio.create_task(client.delete_chat(acc.token, chat_id))
                            current_prompt = current_prompt.rstrip()
                            if current_prompt.endswith("Assistant:"):
                                current_prompt = (
                                    current_prompt[:-len("Assistant:")]
                                    + "[MANDATORY NEXT STEP]: You MUST output exactly one ##TOOL_CALL## block now. "
                                      "Choose the best tool from the provided list by yourself. "
                                      "Do not answer in plain text.\nAssistant:"
                                )
                            else:
                                current_prompt += (
                                    "\n\n[MANDATORY NEXT STEP]: You MUST output exactly one ##TOOL_CALL## block now. "
                                    "Choose the best tool from the provided list by yourself. "
                                    "Do not answer in plain text.\nAssistant:"
                                )
                            log.warning(f"[ToolParse-ANT] 模型返回空响应或无工具调用，重试 (attempt {stream_attempt+1}/{max_attempts})")
                            await asyncio.sleep(0.15)
                            continue
                else:
                    blocks = [{"type": "text", "text": answer_text}]
                    stop_reason = "end_turn"

                content_blocks = []
                if reasoning_text:
                    content_blocks.append({"type": "thinking", "thinking": reasoning_text})
                content_blocks.extend(blocks)

                users = await users_db.get()
                for u in users:
                    if u["id"] == token:
                        u["used_tokens"] += len(answer_text) + len(prompt)
                        break
                await users_db.save(users)

                if acc is not None:
                    client.account_pool.release(acc)
                    if chat_id:

                        asyncio.create_task(client.delete_chat(acc.token, chat_id))

                from fastapi.responses import JSONResponse
                return JSONResponse({
                    "id": msg_id, "type": "message", "role": "assistant", "model": model_name,
                    "content": content_blocks, "stop_reason": stop_reason, "stop_sequence": None,
                    "usage": {"input_tokens": len(prompt), "output_tokens": len(answer_text)}
                })
            except Exception as e:
                if acc is not None and acc.inflight > 0:
                    client.account_pool.release(acc)
                    if chat_id:

                        asyncio.create_task(client.delete_chat(acc.token, chat_id))
                if stream_attempt == max_attempts - 1:
                    raise HTTPException(status_code=500, detail=str(e))
                await asyncio.sleep(1)
