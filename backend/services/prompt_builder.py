import json
import logging
from dataclasses import dataclass

log = logging.getLogger("qwen2api.prompt")


@dataclass(slots=True)
class PromptBuildResult:
    prompt: str
    tools: list[dict]
    tool_enabled: bool


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


def _tool_param_hint(tool: dict) -> str:
    params = tool.get("parameters", {}) or {}
    if not isinstance(params, dict):
        return ""

    props = params.get("properties", {}) or {}
    if not isinstance(props, dict) or not props:
        return ""

    required = params.get("required", []) or []
    ordered_keys: list[str] = []
    for key in required:
        if key in props and key not in ordered_keys:
            ordered_keys.append(key)
    for key in props:
        if key not in ordered_keys:
            ordered_keys.append(key)

    shown = ordered_keys[:3]
    if not shown:
        return ""
    suffix = ", ..." if len(ordered_keys) > len(shown) else ""
    return f" input keys: {', '.join(shown)}{suffix}"


def _safe_preview(text: str, limit: int = 240) -> str:
    if not text:
        return ""
    compact = " ".join(text.split())
    return compact[:limit] + ("...[truncated]" if len(compact) > limit else "")


def build_prompt_with_tools(system_prompt: str, messages: list, tools: list) -> str:
    MAX_CHARS = 18000 if tools else 120000
    sys_part = f"<system>\n{system_prompt[:2000]}\n</system>" if system_prompt else ""
    tools_part = ""
    if tools:
        names = [t.get("name", "") for t in tools if t.get("name")]
        lines = [
            "=== TOOL USAGE INSTRUCTIONS ===",
            "When a tool is needed, output ONLY one XML-wrapped tool call and nothing else.",
            "Required format:",
            '<tool_call>{"name": "EXACT_TOOL_NAME", "input": {"param1": "value1"}}</tool_call>',
            "Rules:",
            "- Output only the single <tool_call>...</tool_call> block.",
            "- No prose before or after the block.",
            "- No markdown fences.",
            "- Use the exact tool name from the list below.",
            "- Put arguments inside the input object.",
            "- Do not invent tool names.",
            "- If no tool is needed, answer normally.",
            "Available tools:",
        ]
        if len(names) <= 12:
            for tool in tools:
                name = tool.get("name", "")
                desc = (tool.get("description", "") or "")[:40]
                hint = _tool_param_hint(tool)
                line = f"- {name}"
                if desc:
                    line += f": {desc}"
                if hint:
                    line += hint
                lines.append(line)
        else:
            priority_tools = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "TaskCreate", "TaskUpdate"]
            priority_lines = []
            seen = set()
            for priority_name in priority_tools:
                tool = next((item for item in tools if item.get("name") == priority_name), None)
                if tool is None:
                    continue
                seen.add(priority_name)
                hint = _tool_param_hint(tool)
                priority_lines.append(f"- {priority_name}{hint}")
            remaining_count = len([name for name in names if name not in seen])
            lines.extend(priority_lines)
            if remaining_count > 0:
                lines.append(f"- Other available tools: {remaining_count} more")
        lines.append("=== END TOOL INSTRUCTIONS ===")
        tools_part = "\n".join(lines)

    overhead = len(sys_part) + len(tools_part) + 50
    budget = MAX_CHARS - overhead
    history_parts = []
    used = 0
    # Keep system-role messages unless they duplicate the top-level system prompt.
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
        if role == "system" and system_prompt and _extract_text(msg.get("content", "")).strip() == system_prompt.strip():
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
                    json.dumps({"name": name, "input": args}, ensure_ascii=False)
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
        tool_names = [tool.get("name", "") for tool in tools if tool.get("name")]
        tool_instruction_preview = _safe_preview(tools_part, 360)
        latest_user_preview = _safe_preview(latest_user_line, 220)
        first_user_preview = ""
        if messages:
            first_user = next((m for m in messages if m.get("role") == "user"), None)
            if first_user:
                first_user_preview = _safe_preview(
                    _extract_text(first_user.get("content", ""), user_tool_mode=True),
                    220,
                )
        log.info(
            "[Prompt] 工具模式: history_msgs=%s history_chars=%s tool_count=%s tool_names=%s first_user=%r latest_user=%r tool_instr=%r",
            len(history_parts),
            used,
            len(tool_names),
            tool_names[:12],
            first_user_preview,
            latest_user_preview,
            tool_instruction_preview,
        )
    parts = []
    if sys_part: parts.append(sys_part)
    parts.extend(history_parts)
    # Tool instructions go LAST — right before "Assistant:" so they have highest priority
    if tools_part: parts.append(tools_part)
    if latest_user_line: parts.append(latest_user_line)
    parts.append("Assistant:")
    return "\n\n".join(parts)


def messages_to_prompt(req_data: dict) -> PromptBuildResult:
    messages = req_data.get("messages", [])
    tools = _normalize_tools(req_data.get("tools", []))
    tool_enabled = bool(tools)
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
    return PromptBuildResult(
        prompt=build_prompt_with_tools(system_prompt, messages, tools),
        tools=tools,
        tool_enabled=tool_enabled,
    )
