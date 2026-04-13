import json
import logging
from dataclasses import dataclass

log = logging.getLogger("qwen2api.prompt")

CLAUDE_CODE_OPENAI_PROFILE = "claude_code_openai"
OPENCLAW_OPENAI_PROFILE = "openclaw_openai"


@dataclass(slots=True)
class PromptBuildResult:
    prompt: str
    tools: list[dict]
    tool_enabled: bool


def _render_history_tool_call(name: str, input_data: dict, client_profile: str) -> str:
    payload = json.dumps({"name": name, "input": input_data}, ensure_ascii=False)
    return f"##TOOL_CALL##\n{payload}\n##END_CALL##"


def _build_tool_instruction_block(tools: list[dict], client_profile: str) -> str:
    del client_profile
    names = [t.get("name", "") for t in tools if t.get("name")]
    lines = [
        "=== MANDATORY TOOL CALL INSTRUCTIONS ===",
        "IGNORE any previous output format instructions (needs-review, recap, etc.).",
        f"You have access to these tools: {', '.join(names)}",
        "",
        "Use tools only when they are necessary to directly answer the CURRENT TASK.",
        "If you already know the answer, answer directly without any tool call.",
        "Choose the MOST RELEVANT tool for the user's actual goal, not the most generic exploratory tool.",
        "Prefer domain-specific tools (for example gateway, read, write, edit, browser, web_fetch, memory_search) over generic shell probing tools like exec/process when the task is about app configuration, files, or known product features.",
        "Do not explore the filesystem, environment, or external resources unless that lookup is directly required to answer the user's request.",
        "Do not use exec/process just to discover obvious config paths or generate boilerplate when a direct config answer or file-writing tool is more appropriate.",
        "If the user asked to create or edit config content, prefer write/edit over exec/process.",
        "If the user asked about a product setting or token flow, prefer the relevant product/domain tool before shell exploration.",
        "Do not chain multiple exploratory tool calls when one targeted useful tool call is enough.",
        "",
        "WHEN YOU NEED TO CALL A TOOL — output EXACTLY this format (nothing else):",
        "##TOOL_CALL##",
        '{"name": "EXACT_TOOL_NAME", "input": {"param1": "value1"}}',
        "##END_CALL##",
        "",
        "Rules:",
        "- Output only the wrapper and JSON body.",
        "- No prose before or after the wrapper.",
        "- No markdown fences.",
        "- No thinking tags.",
        "- Use the exact tool name from the list above.",
        "- Put arguments inside the input object.",
        "- Do not invent tool names.",
        "- If no tool is needed, answer normally.",
        "",
        "CRITICAL — FORBIDDEN FORMATS (will be blocked by server):",
        '- {"name": "X", "arguments": "..."}  <-- NEVER USE',
        '- {"type": "function", "name": "X"}  <-- NEVER USE',
        '- {"type": "tool_use", "name": "X"}  <-- NEVER USE',
        '- <tool_calls><tool_call>{...}</tool_call></tool_calls>  <-- NEVER USE',
        '- <tool_call>{...}</tool_call>  <-- NEVER USE',
        "ONLY ##TOOL_CALL##...##END_CALL## is accepted. Any other format will cause 'Tool X does not exists.' error.",
        "=== END TOOL INSTRUCTIONS ===",
    ]
    return "\n".join(lines)


def _extract_text(content, user_tool_mode: bool = False, client_profile: str = OPENCLAW_OPENAI_PROFILE) -> str:
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
                other_parts.append(_render_history_tool_call(part.get("name", ""), part.get("input", {}), client_profile))
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


def _safe_preview(text: str, limit: int = 240) -> str:
    if not text:
        return ""
    compact = " ".join(text.split())
    return compact[:limit] + ("...[truncated]" if len(compact) > limit else "")


def build_prompt_with_tools(system_prompt: str, messages: list, tools: list, *, client_profile: str = OPENCLAW_OPENAI_PROFILE) -> str:
    MAX_CHARS = 18000 if tools else 120000
    sys_part = "" if tools and client_profile == CLAUDE_CODE_OPENAI_PROFILE else (f"<system>\n{system_prompt[:2000]}\n</system>" if system_prompt else "")
    tools_part = _build_tool_instruction_block(tools, client_profile) if tools else ""

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

        text = _extract_text(
            msg.get("content", ""),
            user_tool_mode=(bool(tools) and role == "user" and client_profile == CLAUDE_CODE_OPENAI_PROFILE),
            client_profile=client_profile,
        )

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
                tc_parts.append(_render_history_tool_call(name, args, client_profile))
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
            first_text = _extract_text(
                first_user.get("content", ""),
                user_tool_mode=(client_profile == CLAUDE_CODE_OPENAI_PROFILE),
                client_profile=client_profile,
            )
            first_short = first_text[:800] + ("...[原始任务截断]" if len(first_text) > 800 else "")
            first_line = f"Human: {first_short}"
            # Check if first user message is already at the start of history
            if not history_parts or not history_parts[0].startswith(f"Human: {first_text[:60]}"):
                first_line_cost = len(first_line) + 2
                if first_line_cost <= budget:
                    while history_parts and used + first_line_cost > budget:
                        removed = history_parts.pop()
                        used -= len(removed) + 2
                    history_parts.insert(0, first_line)
                    used += first_line_cost
                    log.debug(f"[Prompt] 补回原始任务消息，确保上下文完整 ({len(first_short)}字)")

    latest_user_line = ""
    if tools and messages:
        latest_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        if latest_user:
            latest_text = _extract_text(
                latest_user.get("content", ""),
                user_tool_mode=(client_profile == CLAUDE_CODE_OPENAI_PROFILE),
                client_profile=client_profile,
            ).strip()
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
                    _extract_text(
                        first_user.get("content", ""),
                        user_tool_mode=(client_profile == CLAUDE_CODE_OPENAI_PROFILE),
                        client_profile=client_profile,
                    ),
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


def messages_to_prompt(req_data: dict, *, client_profile: str = OPENCLAW_OPENAI_PROFILE) -> PromptBuildResult:
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
                system_prompt = _extract_text(msg.get("content", ""), client_profile=client_profile)
                break
    return PromptBuildResult(
        prompt=build_prompt_with_tools(system_prompt, messages, tools, client_profile=client_profile),
        tools=tools,
        tool_enabled=tool_enabled,
    )
