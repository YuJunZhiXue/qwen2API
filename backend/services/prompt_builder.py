import json
import logging
import uuid

log = logging.getLogger("qwen2api.prompt")

def _extract_text(content, user_tool_mode: bool = False) -> str:
    """Extract text from Anthropic content (string or list of blocks).

    user_tool_mode=True: used for user messages when tools are active.
    In that case we take only the LAST text block (the actual user request)
    and skip earlier text blocks which typically contain CLAUDE.md content
    embedded by the client before the real prompt.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        # Collect all text blocks and non-text blocks separately
        text_blocks = []
        other_parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            t = part.get("type", "")
            if t == "text":
                text_blocks.append(part.get("text", ""))
            elif t == "tool_use":
                # Render as ##TOOL_CALL## format — same as what we ask the model to output,
                # so history looks consistent and the model knows how to continue.
                inp = json.dumps(part.get("input", {}), ensure_ascii=False)
                other_parts.append(
                    f'##TOOL_CALL##\n{{"name": {json.dumps(part.get("name",""))}, "input": {inp}}}\n##END_CALL##'
                )
            elif t == "tool_result":
                inner = part.get("content", "")
                tid = part.get("tool_use_id", "")
                if isinstance(inner, str):
                    other_parts.append(f"[Tool Result for call {tid}]\n{inner}\n[/Tool Result]")
                elif isinstance(inner, list):
                    texts = [p.get("text", "") for p in inner if isinstance(p, dict) and p.get("type") == "text"]
                    other_parts.append(f"[Tool Result for call {tid}]\n{''.join(texts)}\n[/Tool Result]")

        if user_tool_mode and text_blocks:
            # Only keep the LAST text block — that's the actual user request.
            # Earlier blocks are likely CLAUDE.md content injected by the client.
            parts.append(text_blocks[-1])
        else:
            parts.extend(text_blocks)
        parts.extend(other_parts)
        return "\n".join(p for p in parts if p)
    return ""


def _normalize_tool(tool: dict) -> dict:
    """Normalize OpenAI or Anthropic tool format to internal {name, description, parameters}."""
    # OpenAI format: {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
    if tool.get("type") == "function" and "function" in tool:
        fn = tool["function"]
        return {
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
        }
    # Anthropic format: {"name": ..., "description": ..., "input_schema": ...}
    # or already normalized: {"name": ..., "description": ..., "parameters": ...}
    return {
        "name": tool.get("name", ""),
        "description": tool.get("description", ""),
        "parameters": tool.get("input_schema") or tool.get("parameters") or {},
    }


def _normalize_tools(tools: list) -> list:
    return [_normalize_tool(t) for t in tools if tools]


def build_prompt_with_tools(system_prompt: str, messages: list, tools: list) -> str:
    MAX_CHARS = 18000 if tools else 120000
    # When tools are present, skip the user's system prompt entirely.
    # CLAUDE.md contains "needs-review" format instructions that override our tool call
    # instructions and prevent the model from generating proper tool calls.
    if tools:
        sys_part = ""
    else:
        sys_part = f"<system>\n{system_prompt[:2000]}\n</system>" if system_prompt else ""
    tools_part = ""
    if tools:
        names = [t.get("name", "") for t in tools if t.get("name")]
        lines = [
            "=== MANDATORY TOOL CALL INSTRUCTIONS ===",
            "IGNORE any previous output format instructions (needs-review, recap, etc.).",
            f"You have access to these tools: {', '.join(names)}",
            "",
            "WHEN YOU NEED TO CALL A TOOL — output EXACTLY this format (nothing else):",
            "##TOOL_CALL##",
            '{"name": "EXACT_TOOL_NAME", "input": {"param1": "value1"}}',
            "##END_CALL##",
            "",
            "STRICT RULES:",
            "- No preamble, no explanation before or after ##TOOL_CALL##...##END_CALL##.",
            "- Use EXACT tool name from the list below.",
            "- Prioritize the most recent user request as TOP PRIORITY task.",
            "- You ARE running locally on the user's machine. You HAVE full file system access via Bash/Edit/Write/Read tools.",
            "- NEVER say 'I cannot access your file system' or 'I am an AI and cannot...' — you CAN and MUST use tools.",
            "- If a tool result is unexpected or shows an error, immediately try a DIFFERENT approach — do not give up.",
            "- For file operations: if ~/Desktop/ fails, try /c/Users/Administrator/Desktop/ or /mnt/c/Users/Administrator/Desktop/ or the Windows path C:/Users/Administrator/Desktop/",
            "- Keep calling tools until the task is FULLY COMPLETE. Never stop mid-task.",
            "- When NO tool is needed (task fully done), answer normally in plain text.",
            "- Never call the same tool with the same args in more than 2 consecutive turns; try a different approach.",
            "- If the latest tool result contains 'Unchanged since last read', do not call Read again on the same target.",
            "",
            "ONLY ##TOOL_CALL##...##END_CALL## is accepted.",
            "",
            "Available tools:",
        ]
        # When there are many tools (>20), only list name+short_desc without params
        # to keep the prompt compact and avoid model timeouts.
        verbose_tools = len(tools) <= 20
        for tool in tools:
            name = tool.get("name", "")
            desc = tool.get("description", "")
            if verbose_tools:
                desc = desc[:120]
                lines.append(f"- {name}: {desc}")
                params = tool.get("parameters", {})
                if params:
                    props = params.get("properties", {})
                    req = params.get("required", [])
                    if props:
                        ps = ", ".join(f"{k}({'req' if k in req else 'opt'})" for k in props)
                        lines.append(f"  params: {ps}")
            else:
                # Compact: just name and very short description
                desc = desc[:60]
                lines.append(f"- {name}: {desc}")
        lines.append("=== END TOOL INSTRUCTIONS ===")
        tools_part = "\n".join(lines)

    overhead = len(sys_part) + len(tools_part) + 50
    budget = MAX_CHARS - overhead
    history_parts = []
    used = 0
    # When tools are present: skip system-role messages (CLAUDE.md arrives this way)
    # No hard message count cap — rely only on character budget.
    # Tool results (embedded in user messages) are truncated to 1500 chars to preserve
    # budget for more messages and avoid crowding out the original task.
    NEEDSREVIEW_MARKERS = ("需求回显", "已了解规则", "等待用户输入", "待执行任务", "待确认事项",
                           "[需求回显]", "**需求回显**")
    msg_count = 0
    max_history_msgs = 8 if tools else 200
    for msg in reversed(messages):
        if msg_count >= max_history_msgs:
            break
        role = msg.get("role", "")
        if role not in ("user", "assistant", "system", "tool"):
            continue
        if tools and role == "system":
            continue

        # ── OpenAI-format tool result (role="tool") ──────────────────────────
        # These were previously silently dropped, causing the model to never see
        # tool results and loop forever repeating the same tool call.
        if role == "tool":
            tool_content = msg.get("content", "") or ""
            tool_call_id = msg.get("tool_call_id", "")
            if isinstance(tool_content, list):
                tool_content = "\n".join(
                    p.get("text", "") for p in tool_content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            elif not isinstance(tool_content, str):
                tool_content = str(tool_content)
            if len(tool_content) > 300:
                tool_content = tool_content[:300] + "...[truncated]"
            line = f"[Tool Result]{(' id=' + tool_call_id) if tool_call_id else ''}\n{tool_content}\n[/Tool Result]"
            if used + len(line) + 2 > budget and history_parts:
                break
            history_parts.insert(0, line)
            used += len(line) + 2
            msg_count += 1
            continue

        text = _extract_text(msg.get("content", ""),
                             user_tool_mode=(bool(tools) and role == "user"))

        # ── OpenAI-format assistant tool_calls (content=null + tool_calls[]) ─
        # When an assistant message has tool_calls but content is null/empty,
        # render each tool_call as ##TOOL_CALL## so the model sees what it called.
        if role == "assistant" and not text and msg.get("tool_calls"):
            tc_parts = []
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                args_str = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_str) if args_str else {}
                except (json.JSONDecodeError, ValueError):
                    args = {"raw": args_str}
                tc_parts.append(
                    f'##TOOL_CALL##\n{{"name": {json.dumps(name)}, "input": {json.dumps(args, ensure_ascii=False)}}}\n##END_CALL##'
                )
            text = "\n".join(tc_parts)

        # Skip assistant messages that are just needs-review boilerplate
        if tools and role == "assistant" and any(m in text for m in NEEDSREVIEW_MARKERS):
            log.debug(f"[Prompt] 跳过需求回显式 assistant 消息 ({len(text)}字)")
            msg_count += 1
            continue
        # Truncate tool results (large user messages containing [Tool Result]) aggressively
        # so they don't crowd out other context. Plain user messages get more space.
        is_tool_result = role == "user" and ("[Tool Result]" in text or "[tool result]" in text.lower()
                                              or text.startswith("{") or "\"results\"" in text[:100])
        max_len = 600 if is_tool_result else 1400
        if len(text) > max_len:
            text = text[:max_len] + "...[truncated]"
        prefix = {"user": "Human: ", "assistant": "Assistant: ", "system": "System: "}.get(role, "")
        line = f"{prefix}{text}"
        if used + len(line) + 2 > budget and history_parts:
            break
        history_parts.insert(0, line)
        used += len(line) + 2
        msg_count += 1

    # 原始任务保护：若第一条 user 消息被挤出了历史窗口，强制补回最前
    # 这确保模型始终知道用户的原始任务是什么
    if tools and messages:
        first_user = next((m for m in messages if m.get("role") == "user"), None)
        if first_user:
            first_text = _extract_text(first_user.get("content", ""), user_tool_mode=True)
            first_short = first_text[:800] + ("...[原始任务截断]" if len(first_text) > 800 else "")
            first_line = f"Human: {first_short}"
            # Check if first user message is already at the start of history
            if not history_parts or not history_parts[0].startswith(f"Human: {first_text[:60]}"):
                history_parts.insert(0, first_line)
                log.debug(f"[Prompt] 补回原始任务消息，确保上下文完整 ({len(first_short)}字)")

    latest_user_line = ""
    if tools and messages:
        latest_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        if latest_user:
            latest_text = _extract_text(latest_user.get("content", ""), user_tool_mode=True).strip()
            if latest_text:
                latest_short = latest_text[:900] + ("...[最新任务截断]" if len(latest_text) > 900 else "")
                latest_user_line = f"Human (CURRENT TASK - TOP PRIORITY): {latest_short}"

    if tools:
        log.info(f"[Prompt] 工具模式: {len(history_parts)} 条历史消息, {used}字 history + {len(tools_part)}字 tool指令")
    parts = []
    if sys_part: parts.append(sys_part)
    parts.extend(history_parts)
    # Tool instructions go LAST — right before "Assistant:" so they have highest priority
    if tools_part: parts.append(tools_part)
    if latest_user_line: parts.append(latest_user_line)
    parts.append("Assistant:")
    return "\n\n".join(parts)


def messages_to_prompt(req_data: dict) -> tuple:
    messages = req_data.get("messages", [])
    tools = _normalize_tools(req_data.get("tools", []))
    system_prompt = ""
    sys_field = req_data.get("system", "")
    if isinstance(sys_field, list):
        system_prompt = " ".join(p.get("text", "") for p in sys_field if isinstance(p, dict))
    elif isinstance(sys_field, str):
        system_prompt = sys_field
    if not system_prompt:
        for msg in messages:
            if msg.get("role") == "system":
                system_prompt = _extract_text(msg.get("content", ""))
                break
    return build_prompt_with_tools(system_prompt, messages, tools), tools
