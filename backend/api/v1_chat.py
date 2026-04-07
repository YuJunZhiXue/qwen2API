from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
import asyncio
import json
import logging
import uuid
from backend.services.qwen_client import QwenClient
from backend.services.token_calc import calculate_usage
from backend.services.prompt_builder import build_prompt_with_tools

log = logging.getLogger("qwen2api.chat")
router = APIRouter()

@router.post("/completions")
@router.post("/chat/completions")
@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    app = request.app
    users_db = app.state.users_db
    client: QwenClient = app.state.qwen_client

    # 鉴权 (完全复原单文件逻辑)
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""

    if not token:
        token = request.headers.get("x-api-key", "").strip()
    if not token:
        token = request.query_params.get("key", "").strip() or request.query_params.get("api_key", "").strip()

    from backend.core.config import API_KEYS, settings
    admin_k = settings.ADMIN_KEY

    # 兼容处理逻辑：
    # 1. 没有配置 API_KEYS 则默认放行
    # 2. 若配置了，则接受 admin_key 或存在于 API_KEYS 中的 key
    # 3. 甚至接受任何非空 key（放宽限制，以支持各种三方工具自带 key）
    if API_KEYS:
        if token != admin_k and token not in API_KEYS and not token:
            raise HTTPException(status_code=401, detail="Invalid API Key")

    # 获取下游用户并处理配额（如果该功能启用且存在对应的用户）
    users = await users_db.get()
    user = next((u for u in users if u["id"] == token), None)
    if user and user.get("quota", 0) <= user.get("used_tokens", 0):
        raise HTTPException(status_code=402, detail="Quota Exceeded")
        
    body = await request.json()
    from backend.core.config import resolve_model
    model = resolve_model(body.get("model", "gpt-3.5-turbo"))
    messages = body.get("messages", [])
    tools = body.get("tools", [])
    
    # 构建带指令劫持的 Prompt
    content = build_prompt_with_tools(messages, tools)
    
    log.info(f"[OAI] model={model}, stream=True, tools={[t.get('function', {}).get('name') for t in tools]}, prompt_len={len(content)}")
    
    # 诊断日志1：截取最后一条用户消息和生成的 prompt 尾部，确认 User 内容已带上
    last_user_msg = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "None")
    log.info(f"[Debug-Input] 最新用户输入: {str(last_user_msg)[:200]}...")
    log.info(f"[Debug-Prompt] 构建出的 Prompt 尾部: {content[-500:]!r}")

    # 无感重试调用
    async def generate():
        current_prompt = content

        for stream_attempt in range(5):
            try:
                chat_id = None
                acc = None
                
                # Start stream
                completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
                yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'model': model, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})}\n\n"

                answer_chunks = []
                thinking_chunks = []
                native_tc_chunks = {}
                
                async for item in client.chat_stream_events_with_retry(model, current_prompt, has_custom_tools=bool(tools)):
                    if item["type"] == "meta":
                        chat_id = item["chat_id"]
                        acc = item["acc"]
                        continue
                    
                    if item["type"] == "event":
                        evt = item["event"]
                        if evt.get("type") != "delta":
                            continue
                        
                        phase = evt.get("phase", "")
                        cont = evt.get("content", "")
                        
                        if phase in ("think", "thinking_summary") and cont:
                            # 隐藏思考过程，不在客户端显示
                            thinking_chunks.append(cont)
                            
                        elif phase == "answer" and cont:
                            answer_chunks.append(cont)
                            # For answer, we buffer it to parse tools at the end
                            # If we wanted full text streaming, we could stream it here, but we need to intercept ##TOOL_CALL##
                            
                        elif phase == "tool_call" and cont:
                            tc_id = evt.get("extra", {}).get("tool_call_id", "tc_0")
                            if tc_id not in native_tc_chunks:
                                native_tc_chunks[tc_id] = {"name": "", "args": ""}
                            try:
                                chunk_json = json.loads(cont)
                                if "name" in chunk_json:
                                    native_tc_chunks[tc_id]["name"] = chunk_json["name"]
                                if "arguments" in chunk_json:
                                    native_tc_chunks[tc_id]["args"] += chunk_json["arguments"]
                            except (json.JSONDecodeError, ValueError):
                                native_tc_chunks[tc_id]["args"] += cont

                answer_text = "".join(answer_chunks)

                # 诊断日志2：打印模型完整的原始返回内容（包含 thinking 阶段）
                log.info(f"[Debug-Output] 模型原始 thinking: {''.join(thinking_chunks)[:500]!r}...")
                log.info(f"[Debug-Output] 模型原始 answer: {answer_text[:500]!r}...")

                if not answer_text.strip() and thinking_chunks and tools:
                    thinking_text = "".join(thinking_chunks)
                    if "✿ACTION✿" in thinking_text or "<tool_call>" in thinking_text:
                        log.info("[OAI] 在思考阶段发现工具调用，提取使用")
                        answer_text = thinking_text

                if not answer_text.strip() and not native_tc_chunks and tools and stream_attempt < 4:
                    log.warning(f"[OAI] answer为空但有工具定义，可能是模型只在思考阶段输出了内容，尝试重试 (attempt {stream_attempt+1}/5)")
                    if acc:
                        client.account_pool.release(acc)
                        if chat_id:
                            import asyncio
                            asyncio.create_task(client.delete_chat(acc.token, chat_id))
                    
                    # 避免在末尾重复累加多次 <think> 提醒
                    if current_prompt.endswith("Assistant: <think>\n"):
                        current_prompt = current_prompt[:-19]
                    elif current_prompt.endswith("Assistant:"):
                        current_prompt = current_prompt[:-10]
                        
                    # 使用更加强烈的中文+英文双语提醒，并且直接替换结尾
                    reminder = "\n\n【IMPORTANT: You MUST respond with a tool call using ✿ACTION✿ format. Do NOT just think silently. 必须输出✿ACTION✿格式工具调用！】\n\nAssistant: <think>\n"
                    current_prompt += reminder
                        
                    import asyncio
                    await asyncio.sleep(0.5)
                    continue
                
                if native_tc_chunks:
                    log.info(f"[Native-TC] 收到原生工具调用事件: {list(native_tc_chunks.keys())}")
                    tc_parts = []
                    for tc_id, tc in native_tc_chunks.items():
                        name = tc["name"]
                        try:
                            inp = json.loads(tc["args"]) if tc["args"] else {}
                        except (json.JSONDecodeError, ValueError):
                            inp = {"raw": tc["args"]}
                        tc_parts.append(f'✿ACTION✿\n{{"action": {json.dumps(name)}, "args": {json.dumps(inp, ensure_ascii=False)}}}\n✿END_ACTION✿')
                    
                    if not answer_text:
                        answer_text = "\n\n".join(tc_parts)
                    else:
                        answer_text += "\n\n" + "\n\n".join(tc_parts)

                # Detect Qwen native tool call interception
                import re
                native_blocked_m = re.search(r'Tool (\w+) does not exists?\.?', answer_text.strip(), re.IGNORECASE)
                if native_blocked_m and tools and stream_attempt < 4:
                    blocked_name = native_blocked_m.group(1)
                    if acc:
                        client.account_pool.release(acc)
                        if chat_id:
                            import asyncio
                            asyncio.create_task(client.delete_chat(acc.token, chat_id))
                    log.warning(f"[NativeBlock-OAI] Qwen拦截原生工具调用 '{blocked_name}'，重试 (attempt {stream_attempt+1}/5)")
                    from backend.services.tool_parser import inject_format_reminder
                    current_prompt = inject_format_reminder(current_prompt, blocked_name)
                    import asyncio
                    await asyncio.sleep(0.5)
                    continue

                # Parse tools
                from backend.services.tool_parser import parse_tool_calls
                if tools:
                    blocks, stop_reason = parse_tool_calls(answer_text, tools)
                else:
                    blocks = [{"type": "text", "text": answer_text}]
                    stop_reason = "stop"

                if stop_reason == "end_turn":
                    stop_reason = "stop"

                has_tool_call = stop_reason == "tool_use" or stop_reason == "tool_calls"

                if has_tool_call:
                    # 前置文本如果有，吐出
                    txt_list = [b for b in blocks if b["type"] == "text" and b.get("text")]
                    for blk in txt_list:
                        chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "model": model,
                            "choices": [{"index": 0, "delta": {"content": blk["text"]}, "finish_reason": None}]
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"

                    # 吐出 tool_calls
                    tc_list = [b for b in blocks if b["type"] == "tool_use"]
                    for idx, tc in enumerate(tc_list):
                        tc_name_chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "model": model,
                            "choices": [{"index": 0, "delta": {
                                "tool_calls": [{
                                    "index": idx,
                                    "id": tc["id"],
                                    "type": "function",
                                    "function": {"name": tc["name"], "arguments": ""}
                                }]
                            }, "finish_reason": None}]
                        }
                        yield f"data: {json.dumps(tc_name_chunk)}\n\n"

                        tc_args_chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "model": model,
                            "choices": [{"index": 0, "delta": {
                                "tool_calls": [{
                                    "index": idx,
                                    "function": {"arguments": json.dumps(tc.get("input", {}), ensure_ascii=False)}
                                }]
                            }, "finish_reason": None}]
                        }
                        yield f"data: {json.dumps(tc_args_chunk)}\n\n"

                    stop_reason = "tool_calls"
                else:
                    # 纯文本（非 tool_call），直接吐出
                    txt_list = [b for b in blocks if b["type"] == "text" and b.get("text")]
                    for blk in txt_list:
                        chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "model": model,
                            "choices": [{"index": 0, "delta": {"content": blk["text"]}, "finish_reason": None}]
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"

                final_chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": stop_reason}]
                }
                yield f"data: {json.dumps(final_chunk)}\n\n"
                yield "data: [DONE]\n\n"

                log.info(f"[OAI] Request complete. Generated {len(answer_text)} characters.")

                users = await users_db.get()
                for u in users:
                    if u["id"] == token:
                        u["used_tokens"] += len(answer_text) + len(current_prompt)
                        break
                await users_db.save(users)

                if acc:
                    client.account_pool.release(acc)
                    if chat_id:
                        import asyncio
                        asyncio.create_task(client.delete_chat(acc.token, chat_id))
                return

            except Exception as e:
                log.error(f"Chat request failed: {e}")
                return

    return StreamingResponse(generate(), media_type="text/event-stream")
