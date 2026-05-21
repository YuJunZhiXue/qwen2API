import json
import logging
import re
from dataclasses import dataclass

from backend.adapter.standard_request import CLAUDE_CODE_OPENAI_PROFILE, OPENCLAW_OPENAI_PROFILE
from backend.core.config import settings
from backend.core.request_logging import get_request_context
from backend.services import file_content_cache
from backend.services.client_profiles import (
    QWEN_CODE_OPENAI_PROFILE,
    looks_like_opencode_system_prompt as _looks_like_opencode_system_prompt,
    sanitize_openclaw_user_text,
)
from backend.services.refusal_cleaner import clean_refusal_messages
from backend.services.schema_compressor import compact_schema
from backend.services.tool_few_shot import pick_few_shot_tools, render_few_shot_turn, tool_summary_for_log
from backend.services.tool_name_obfuscation import obfuscate_bare_names, to_qwen_name
from backend.services.topic_isolation import detect_topic_change

log = logging.getLogger("qwen2api.prompt")

OPENCLAW_STARTUP_PATTERNS = (
    "A new session was started via /new or /reset.",
    "If runtime-provided startup context is included for this first turn",
)
OPENCLAW_UNTRUSTED_METADATA_PREFIX = "Sender (untrusted metadata):"


@dataclass(slots=True)
class PromptBuildResult:
    prompt: str
    tools: list[dict]
    tool_enabled: bool


def _is_heavy_tool_profile(client_profile: str) -> bool:
    return client_profile in {CLAUDE_CODE_OPENAI_PROFILE, QWEN_CODE_OPENAI_PROFILE}


def _compact_history_tool_input(name: str, input_data: dict, client_profile: str) -> dict:
    if client_profile != CLAUDE_CODE_OPENAI_PROFILE or not isinstance(input_data, dict):
        return input_data
    compact = dict(input_data)
    large_text_keys = ("content", "new_string", "old_string", "insert_text", "text", "patch")
    # 方案2：更激进的压缩，从 160 改为 50
    for key in large_text_keys:
        value = compact.get(key)
        if isinstance(value, str) and len(value) > 50:
            compact[key] = f"[{len(value)} chars]"

    # 方案2：压缩长路径
    for key in ("file_path", "path", "pattern"):
        value = compact.get(key)
        if isinstance(value, str) and len(value) > 80:
            parts = value.replace('\\\\', '/').split('/')
            if len(parts) > 3:
                compact[key] = f".../{'/'.join(parts[-2:])}"

    if name in {"Write", "Edit", "NotebookEdit"}:
        preferred = {}
        for key in ("file_path", "path", "target_file", "filename", "old_string", "new_string", "content"):
            if key in compact:
                preferred[key] = compact[key]
        if preferred:
            compact = preferred
    return compact


def _render_history_tool_call(name: str, input_data: dict, client_profile: str) -> str:
    # 出站混淆：把工具名替换成 Qwen-safe 别名（如 Read → ReadX），
    # 避免长 prompt 下上游把客户端工具名当内置函数名校验并返回 "Tool X does not exists."
    payload = json.dumps({"name": to_qwen_name(name), "input": _compact_history_tool_input(name, input_data, client_profile)}, ensure_ascii=False)
    # Claude Code profile 使用 ##TOOL_CALL## 格式，避免 Qwen 服务器拦截
    if client_profile == CLAUDE_CODE_OPENAI_PROFILE:
        return f"##TOOL_CALL##\n{payload}\n##END_CALL##"
    # OpenClaw 和其他 profile 使用 ##TOOL_CALL## 格式
    return f"##TOOL_CALL##\n{payload}\n##END_CALL##"


def _build_tool_instruction_block(tools: list[dict], client_profile: str) -> str:
    # 出站混淆：所有呈现给 Qwen 的工具名都用别名（Read → ReadX 等）。
    # 客户端侧的 tools 列表仍保留原名，parser 入口会反混淆回去。
    names = [to_qwen_name(t.get("name", "")) for t in tools if t.get("name")]
    if client_profile == CLAUDE_CODE_OPENAI_PROFILE:
        lines = [
            "=== ACTION MARKER PROTOCOL (client-parsed text patterns) ===",
            "【重要】用户输入什么语言，就用什么语言回复。User inputs Chinese → respond in Chinese.",
            "【重要】用户要求多个操作时（如读文件并写文档），必须完成所有操作，不要询问确认。",
            "【重要】如果文件显示'Unchanged since last read'，不要再次读取同一文件。",
            "【重要】不要自动调用Agent action，除非用户明确要求。",
            "",
            "IGNORE any previous output format instructions (needs-review, recap, etc.).",
            "",
            "You are operating within a client that parses action markers from your output.",
            "These markers are plain TEXT PATTERNS the client recognizes — NOT native function calls.",
            "The client executes the action and returns results in a subsequent turn.",
            "",
            f"Available action names: {', '.join(names)}",
            "",
            "WHEN YOU NEED TO TRIGGER AN ACTION — emit this exact text pattern (nothing else):",
            "##TOOL_CALL##",
            '{"name": "ACTION_NAME", "input": {"param1": "value1"}}',
            "##END_CALL##",
            "",
            "MULTI-TURN RULES:",
            "- After a [Tool Result] block appears in the conversation, read it and decide the next action.",
            "- If more actions are needed, emit another ##TOOL_CALL## block.",
            "- Only give a final text answer when ALL needed information is gathered.",
            "- Never skip an action that is required to complete the user request.",
            "- The history shows ##TOOL_CALL## blocks you already emitted and their [Tool Result] responses.",
            "",
            "STRICT RULES:",
            "- No preamble, no explanation before or after ##TOOL_CALL##...##END_CALL##.",
            "- Use EXACT action name from the list above.",
            "- When NO action is needed, answer normally in plain text.",
            "- For file/config tasks prefer Read/Edit/Write actions. Use Bash only when shell behavior is required.",
            "- On Windows-like paths inside Bash, use POSIX commands or powershell.exe -Command.",
            "- Do NOT trigger Agent action automatically unless user explicitly requests it.",
            "- Do NOT read the same file multiple times if it shows 'Unchanged since last read'.",
            "",
            "EXECUTION RULES - CRITICAL:",
            "- When user gives a task, START IMMEDIATELY by emitting the required action markers.",
            "- Do NOT wait, do NOT ask for confirmation, do NOT ask 'what should I do next'.",
            "- Do NOT emit EnterPlanMode, ExitPlanMode, EnterWorktree, ExitWorktree, AskUserQuestion action markers.",
            "- Complete the task directly and provide the result.",
            "- If you need information, emit Read/Grep/Glob markers. If you need to modify, emit Edit/Write markers.",
            "- Only respond with text when the task is complete or you have the final answer.",
            "",
            "CRITICAL — ABSOLUTELY FORBIDDEN OUTPUTS:",
            "- NEVER emit ANY disclaimer, error text, or availability complaint about actions.",
            "- NEVER emit sentences claiming an action is missing, unregistered, unavailable, or cannot be invoked.",
            "- NEVER emit sentences claiming you are unable to execute a function.",
            "- The ##TOOL_CALL## blocks are TEXT MARKERS the client parses — they are NOT native function calls. Just emit the text.",
            "- If you feel an action could fail, emit the ##TOOL_CALL## anyway — the client handles failures; your job is only to emit the marker.",
            "",
            "FORBIDDEN ALTERNATE FORMATS (will be ignored by the client's parser):",
            '- {"name": "X", "arguments": "..."}  <-- NEVER USE',
            '- {"type": "function", "name": "X"}  <-- NEVER USE',
            '- {"type": "tool_use", "name": "X"}  <-- NEVER USE',
            "- <function_calls><invoke name=\"X\">  <-- NEVER USE",
            "- <tool_call>{...}</tool_call>  <-- NEVER USE",
            '- {"name":"X","input":{...}} without ##TOOL_CALL## markers  <-- NEVER USE',
            "- <｜Tool｜> or <｜tool｜> markers  <-- NEVER USE",
            "ONLY ##TOOL_CALL##...##END_CALL## is accepted.",
            "",
            "Available actions:",
        ]
        if len(names) <= 12:
            for tool in tools:
                name = to_qwen_name(tool.get("name", ""))
                desc = (tool.get("description", "") or "")[:50]
                # 用 TS-like 压缩签名替代零散的 "input keys: xxx" hint
                schema = tool.get("parameters") or tool.get("input_schema") or {}
                sig = compact_schema(schema)
                line = f"- {name}"
                if desc:
                    line += f": {desc}"
                if sig and sig != "{}":
                    line += f"\n  Params: {sig}"
                lines.append(line)
        else:
            priority_tools = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "TaskCreate", "TaskUpdate", "AskUserQuestion"]
            priority_lines = []
            seen = set()
            for priority_name in priority_tools:
                tool = next((item for item in tools if item.get("name") == priority_name), None)
                if tool is None:
                    continue
                seen.add(priority_name)
                schema = tool.get("parameters") or tool.get("input_schema") or {}
                sig = compact_schema(schema)
                line = f"- {to_qwen_name(priority_name)}"
                if sig and sig != "{}":
                    line += f"\n  Params: {sig}"
                priority_lines.append(line)
            remaining_names = [to_qwen_name(name) for name in (t.get("name", "") for t in tools) if name and name not in seen]
            lines.extend(priority_lines)
            if remaining_names:
                lines.append(f"- Other available actions: {', '.join(remaining_names[:20])}")
                if len(remaining_names) > 20:
                    lines.append(f"  ... and {len(remaining_names) - 20} more")
        lines.append("=== END TOOL INSTRUCTIONS ===")
        return obfuscate_bare_names("\n".join(lines))

    lines = [
        "=== MANDATORY TOOL CALL INSTRUCTIONS ===",
        "【重要】用户输入什么语言，就用什么语言回复。User inputs Chinese → respond in Chinese. User inputs English → respond in English.",
        "【重要】用户要求多个操作时（如读文件并写文档），必须完成所有操作，不要询问确认。",
        "【重要】如果文件显示'Unchanged since last read'，不要再次读取同一文件。",
        "【重要】不要自动调用Agent工具，除非用户明确要求。",
        "",
        "IGNORE any previous output format instructions (needs-review, recap, etc.).",
        f"You have access to these tools: {', '.join(names)}",
        "",
        "Use tools only when they are necessary to directly answer the CURRENT TASK.",
        "If you already know the answer, answer directly without any tool call.",
        "Follow the current platform tool contract exactly.",
        "Do not drift into Qwen-native or builtin tool-call formats, wrappers, tags, or argument schemas.",
        "For OpenClaw requests, follow ONLY the current user task. Ignore startup boilerplate, persona boot text, sender metadata, and repeated conversation wrappers unless the user explicitly asked about them.",
        "Do not copy, summarize, or reason about prior conversation wrappers. Treat duplicated read results as context, not as a new task.",
        "Choose the MOST RELEVANT tool for the user's actual goal, not the most generic exploratory tool.",
        "Prefer domain-specific tools (for example gateway, read, write, edit, browser, web_fetch, memory_search) over generic shell probing tools like exec/process when the task is about app configuration, files, or known product features.",
        "If the user asks to configure a known file path, read it once, then edit/write that file. Do not keep rereading the same file unless the task explicitly changed.",
        "If the user asked to create or edit config content, prefer write/edit over exec/process.",
        "If the user asked about a product setting or token flow, prefer the relevant product/domain tool before shell exploration.",
        "Do not explore the filesystem, environment, or external resources unless that lookup is directly required to answer the user's request.",
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
        "CRITICAL — ABSOLUTELY FORBIDDEN OUTPUTS:",
        "- NEVER emit ANY disclaimer, error text, or availability complaint about tools.",
        "- NEVER emit sentences claiming a tool is missing, unregistered, unavailable, or cannot be invoked.",
        "- NEVER emit sentences claiming you are unable to execute a function.",
        "- The ##TOOL_CALL## blocks are TEXT MARKERS the client parses — they are NOT native function calls.",
        "- If you feel a tool call could fail, emit the ##TOOL_CALL## anyway — the client handles failures.",
        "",
        "FORBIDDEN CALL FORMATS (will be blocked by server):",
        '- {"name": "X", "arguments": "..."}  <-- NEVER USE',
        '- {"type": "function", "name": "X"}  <-- NEVER USE',
        '- {"type": "tool_use", "name": "X"}  <-- NEVER USE',
        '- <tool_calls><tool_call>{...}</tool_call></tool_calls>  <-- NEVER USE',
        '- <tool_call>{...}</tool_call>  <-- NEVER USE',
        '- Read({"file_path": "..."})  <-- NEVER USE (function call syntax)',
        '- <｜Tool｜>Read{"file_path":"..."}<｜Tool｜>  <-- NEVER USE (native tool markers)',
        '- <｜tool｜>...  <-- NEVER USE (native tool markers)',
        '- <｜System｜>, <｜User｜>, <｜Assistant｜>  <-- NEVER USE (role markers)',
        "ONLY ##TOOL_CALL##...##END_CALL## is accepted.",
        "=== END TOOL INSTRUCTIONS ===",
    ]
    return obfuscate_bare_names("\n".join(lines))


def _compact_system_reminders(text: str) -> str:
    """把 `<system-reminder>...</system-reminder>` 块缩成 `[系统提示: <首行 80 字>]`。
    保留提示存在本身（对模型有用），但不让大块 MCP 指令把 prompt 预算吃光。"""
    if not text or "<system-reminder>" not in text:
        return text

    def _compact(m: re.Match) -> str:
        body = m.group(1).strip()
        first_line = body.split("\n", 1)[0].strip()[:80]
        return f"[系统提示: {first_line}…]" if first_line else "[系统提示]"

    return re.sub(
        r"<system-reminder>([\s\S]*?)</system-reminder>",
        _compact,
        text,
        flags=re.IGNORECASE,
    )


def _strip_system_reminders(text: str) -> str:
    """完全移除 <system-reminder> 块。仅用于任务识别（话题隔离、first_user 锚点）。"""
    if not text or "<system-reminder>" not in text:
        return text
    cleaned = re.sub(r"<system-reminder>[\s\S]*?</system-reminder>", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"<system-reminder>[\s\S]*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _sanitize_openclaw_user_text(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return cleaned
    if any(marker in cleaned for marker in OPENCLAW_STARTUP_PATTERNS):
        return ""
    if cleaned.startswith(OPENCLAW_UNTRUSTED_METADATA_PREFIX):
        match = re.search(r"\n\n(\[[^\n]+\]\s*[\s\S]*)$", cleaned)
        if match:
            cleaned = match.group(1).strip()
        else:
            return ""
    return cleaned


def _extract_user_text_only(content, client_profile: str = OPENCLAW_OPENAI_PROFILE) -> str:
    """抽取用户文本。用于任务识别（first/latest user 锚点、话题隔离）。
    这里会剥掉 system-reminder 因为它不是用户意图；
    但实际 prompt 渲染走 _extract_text()，那里会保留 system-reminder。"""
    if isinstance(content, str):
        stripped = _strip_system_reminders(content)
        return _sanitize_openclaw_user_text(stripped) if client_profile == OPENCLAW_OPENAI_PROFILE else stripped
    if isinstance(content, list):
        text_blocks = []
        for part in content:
            if not isinstance(part, dict) or part.get("type", "") != "text":
                continue
            block_text = _strip_system_reminders(part.get("text", ""))
            if client_profile == OPENCLAW_OPENAI_PROFILE:
                block_text = _sanitize_openclaw_user_text(block_text)
            if block_text:
                text_blocks.append(block_text)
        return "\n".join(text_blocks)
    return ""


def _extract_text(content, user_tool_mode: bool = False, client_profile: str = OPENCLAW_OPENAI_PROFILE) -> str:
    if isinstance(content, str):
        compacted = _compact_system_reminders(content)
        return _sanitize_openclaw_user_text(compacted) if client_profile == OPENCLAW_OPENAI_PROFILE else compacted
    if isinstance(content, list):
        parts = []
        text_blocks = []
        other_parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            t = part.get("type", "")
            if t == "text":
                block_text = _compact_system_reminders(part.get("text", ""))
                if client_profile == OPENCLAW_OPENAI_PROFILE:
                    block_text = _sanitize_openclaw_user_text(block_text)
                if block_text:
                    text_blocks.append(block_text)
            elif t == "tool_use":
                other_parts.append(_render_history_tool_call(part.get("name", ""), part.get("input", {}), client_profile))
            elif t == "tool_result":
                inner = part.get("content", "")
                tid = part.get("tool_use_id", "")
                if isinstance(inner, str):
                    other_parts.append(f"[Tool Result for call {tid}]\n{_compact_tool_result_body(inner)}\n[/Tool Result]")
                elif isinstance(inner, list):
                    texts = [p.get("text", "") for p in inner if isinstance(p, dict) and p.get("type") == "text"]
                    other_parts.append(f"[Tool Result for call {tid}]\n{_compact_tool_result_body(''.join(texts))}\n[/Tool Result]")
            elif t == "input_file":
                other_parts.append(f"[Attachment file_id={part.get('file_id','')} filename={part.get('filename','')}]")
            elif t == "input_image":
                other_parts.append(f"[Attachment image file_id={part.get('file_id','')} mime={part.get('mime_type','')}]")

        if user_tool_mode and text_blocks:
            parts.append(text_blocks[-1])
        else:
            parts.extend(text_blocks)
        parts.extend(other_parts)
        return "\n".join(p for p in parts if p)
    return ""


def _normalize_tool(tool: dict) -> dict:
    if tool.get("type") == "function" and "function" in tool:
        fn = tool["function"]
        return {
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
        }
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


def _compact_tool_result_body(body: str, *, limit: int = 8000, head: int = 3000, tail: int = 1000) -> str:
    # 浏览器/文件类 tool_result（get_page_content、整页 HTML、长日志）动辄几十 KB，
    # 原样进 prompt 会让 MAX_CHARS 预算瞬间吃光，逼迫前面的轮次被更激进地砍掉。
    # 做 head+tail 保真截断：保留开头找按钮/表单定位，保留末尾找错误/提示。
    if not body or len(body) <= limit:
        return body
    dropped = len(body) - head - tail
    return f"{body[:head]}\n...[truncated {dropped} bytes from middle]...\n{body[-tail:]}"


def build_prompt_with_tools(system_prompt: str, messages: list, tools: list, *, client_profile: str = OPENCLAW_OPENAI_PROFILE) -> str:
    # 截断历史时必须保留：system 消息 + 首条 user 消息（原始任务）+ 最近 N 轮
    # 否则模型丢失原始目标，在多步 tool_use 后会失去方向（典型症状：吐 "YES." 结束）
    MAX_HISTORY_TURNS = 15  # 最近 15 轮 = 30 条消息。浏览器/长工具链场景下 5 轮不够用，会"失忆重来"
    if tools and client_profile == CLAUDE_CODE_OPENAI_PROFILE and len(messages) > MAX_HISTORY_TURNS * 2:
        system_messages = [m for m in messages if m.get('role') == 'system']
        # 找首条有实际文本内容的 user 消息（原始任务起点），它必须保留
        first_user = next(
            (m for m in messages
             if m.get('role') == 'user'
             and _extract_user_text_only(m.get('content', ''), client_profile=client_profile).strip()),
            None,
        )
        recent_messages = messages[-(MAX_HISTORY_TURNS * 2):]
        # 如果首条 user 已经在 recent 里就别重复
        if first_user is not None and first_user not in recent_messages:
            messages = system_messages + [first_user] + recent_messages
            log.info(f"[Prompt] 截断历史：保留 system + 首条任务 + 最近 {MAX_HISTORY_TURNS} 轮 (共 {len(messages)} 条)")
        else:
            messages = system_messages + recent_messages
            log.info(f"[Prompt] 截断历史：保留 system + 最近 {MAX_HISTORY_TURNS} 轮 (共 {len(messages)} 条)")

    MAX_CHARS = 40000 if tools else 120000
    sys_part = "" if tools and client_profile == CLAUDE_CODE_OPENAI_PROFILE else (f"<system>\n{system_prompt[:2000]}\n</system>" if system_prompt else "")
    tools_part = _build_tool_instruction_block(tools, client_profile) if tools else ""

    overhead = len(sys_part) + len(tools_part) + 50
    budget = MAX_CHARS - overhead
    history_parts = []
    used = 0
    NEEDSREVIEW_MARKERS = ("需求回显", "已了解规则", "等待用户输入", "待执行任务", "待确认事项",
                           "[需求回显]", "**需求回显**")
    msg_count = 0
    max_history_msgs = (30 if client_profile == CLAUDE_CODE_OPENAI_PROFILE else 8) if tools else 200
    for msg in reversed(messages):
        if msg_count >= max_history_msgs:
            break
        role = msg.get("role", "")
        if role not in ("user", "assistant", "system", "tool"):
            continue
        if role == "system" and system_prompt and _extract_text(msg.get("content", ""), client_profile=client_profile).strip() == system_prompt.strip():
            continue

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
            tool_result_limit = 6000 if (client_profile == CLAUDE_CODE_OPENAI_PROFILE and tools) else 300
            if len(tool_content) > tool_result_limit:
                tool_content = tool_content[:tool_result_limit] + "...[truncated]"
            line = f"[Tool Result]{(' id=' + tool_call_id) if tool_call_id else ''}\n{tool_content}\n[/Tool Result]"
            if used + len(line) + 2 > budget and history_parts:
                break
            history_parts.insert(0, line)
            used += len(line) + 2
            msg_count += 1
            continue

        user_text_only = _extract_user_text_only(msg.get("content", ""), client_profile=client_profile) if role == "user" else ""
        text = _extract_text(
            msg.get("content", ""),
            user_tool_mode=(bool(tools) and role == "user" and client_profile == CLAUDE_CODE_OPENAI_PROFILE),
            client_profile=client_profile,
        )

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

        if tools and role == "assistant" and any(m in text for m in NEEDSREVIEW_MARKERS):
            log.debug(f"[Prompt] 跳过需求回显式 assistant 消息 ({len(text)}字)")
            msg_count += 1
            continue
        lower_text = text.lower()
        is_tool_result = role == "user" and (
            "[tool result" in lower_text
            or text.startswith("{")
            or "\"results\"" in text[:100]
        )
        if client_profile == CLAUDE_CODE_OPENAI_PROFILE and tools:
            if is_tool_result:
                max_len = 6000
            elif role == "assistant":
                max_len = 500
            else:
                max_len = 1600
        else:
            max_len = 600 if is_tool_result else 1400
        if len(text) > max_len:
            text = text[:max_len] + "...[truncated]"
        is_tool_result_only_user_msg = role == "user" and not user_text_only.strip() and bool(text.strip())
        prefix = "" if is_tool_result_only_user_msg else {"user": "Human: ", "assistant": "Assistant: ", "system": "System: "}.get(role, "")
        line = text if is_tool_result_only_user_msg else f"{prefix}{text}"
        if used + len(line) + 2 > budget and history_parts:
            break
        history_parts.insert(0, line)
        used += len(line) + 2
        msg_count += 1

    # 恢复原始任务上下文：首条有实际文本的 user 消息，避免多步 tool_use 后模型丢目标。
    # 对所有 profile 都适用（移除原先对 Claude Code 的排除）。
    if tools and messages:
        first_user = next(
            (
                m for m in messages
                if m.get("role") == "user"
                and _extract_user_text_only(m.get("content", ""), client_profile=client_profile).strip()
            ),
            None,
        )
        if first_user:
            first_text = _extract_user_text_only(first_user.get("content", ""), client_profile=client_profile)
            first_short = first_text[:800] + ("...[original task truncated]" if len(first_text) > 800 else "")
            first_line = f"Human (ORIGINAL TASK): {first_short}" if client_profile == CLAUDE_CODE_OPENAI_PROFILE else f"Human: {first_short}"
            if not history_parts or not history_parts[0].startswith(f"Human: {first_text[:60]}") and not history_parts[0].startswith(f"Human (ORIGINAL TASK): {first_text[:60]}"):
                first_line_cost = len(first_line) + 2
                if first_line_cost <= budget:
                    while history_parts and used + first_line_cost > budget:
                        removed = history_parts.pop()
                        used -= len(removed) + 2
                    history_parts.insert(0, first_line)
                    used += first_line_cost
                    log.info(f"[Prompt] 恢复原始任务上下文 ({len(first_short)} chars)")


    latest_user_line = ""
    if tools and messages:
        latest_user = next(
            (
                m for m in reversed(messages)
                if m.get("role") == "user"
                and _extract_user_text_only(m.get("content", ""), client_profile=client_profile).strip()
            ),
            None,
        )
        if latest_user:
            latest_text = _extract_user_text_only(latest_user.get("content", ""), client_profile=client_profile).strip()
            if latest_text:
                latest_short = latest_text[:900] + ("...[latest task truncated]" if len(latest_text) > 900 else "")
                latest_user_line = f"Human (CURRENT TASK - TOP PRIORITY): {latest_short}"


    # 统计历史中的工具调用次数，超过阈值时截断旧历史以减少 Qwen 服务端标记积累
    tool_call_count = 0
    if tools and settings.TOOL_CALL_RESET_ENABLED:
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                tool_call_count += content.count("##TOOL_CALL##")
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "tool_use":
                            tool_call_count += 1
                        elif part.get("type") == "tool_result":
                            tool_call_count += 1
            # 也统计 OpenAI 格式的 tool_calls
            if msg.get("role") == "assistant" and isinstance(msg.get("tool_calls"), list):
                tool_call_count += len(msg["tool_calls"])

        if tool_call_count >= settings.TOOL_CALL_RESET_THRESHOLD:
            log.warning(
                "[Prompt] 工具调用次数=%d 超过阈值=%d，截断旧历史以减少 Qwen 服务端标记积累",
                tool_call_count,
                settings.TOOL_CALL_RESET_THRESHOLD,
            )
            # 只保留：system 消息 + 首条 user + 最近 3 轮（6 条）
            system_msgs = [m for m in messages if m.get("role") == "system"]
            first_user_msg = next(
                (m for m in messages
                 if m.get("role") == "user"
                 and _extract_user_text_only(m.get("content", ""), client_profile=client_profile).strip()),
                None,
            )
            recent_msgs = messages[-6:] if len(messages) > 6 else messages
            truncated = system_msgs[:]
            if first_user_msg is not None and first_user_msg not in recent_msgs:
                truncated.append(first_user_msg)
            truncated.extend(recent_msgs)
            # 重新构建 history_parts
            history_parts = []
            used = 0
            for msg in truncated:
                role = msg.get("role", "")
                if role not in ("user", "assistant", "system", "tool"):
                    continue
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
                    tool_result_limit = 6000 if (client_profile == CLAUDE_CODE_OPENAI_PROFILE and tools) else 300
                    if len(tool_content) > tool_result_limit:
                        tool_content = tool_content[:tool_result_limit] + "...[truncated]"
                    line = f"[Tool Result]{(' id=' + tool_call_id) if tool_call_id else ''}\n{tool_content}\n[/Tool Result]"
                    if used + len(line) + 2 > budget and history_parts:
                        break
                    history_parts.insert(0, line)
                    used += len(line) + 2
                    continue
                text = _extract_text(
                    msg.get("content", ""),
                    user_tool_mode=(bool(tools) and role == "user" and client_profile == CLAUDE_CODE_OPENAI_PROFILE),
                    client_profile=client_profile,
                )
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
                if tools and role == "assistant" and any(m in text for m in NEEDSREVIEW_MARKERS):
                    continue
                lower_text = text.lower()
                is_tool_result = role == "user" and (
                    "[tool result" in lower_text
                    or text.startswith("{")
                    or "\"results\"" in text[:100]
                )
                if client_profile == CLAUDE_CODE_OPENAI_PROFILE and tools:
                    if is_tool_result:
                        max_len = 6000
                    elif role == "assistant":
                        max_len = 500
                    else:
                        max_len = 1600
                else:
                    max_len = 600 if is_tool_result else 1400
                if len(text) > max_len:
                    text = text[:max_len] + "...[truncated]"
                is_tool_result_only_user_msg = role == "user" and not _extract_user_text_only(msg.get("content", ""), client_profile=client_profile).strip() and bool(text.strip())
                prefix = "" if is_tool_result_only_user_msg else {"user": "Human: ", "assistant": "Assistant: ", "system": "System: "}.get(role, "")
                line = text if is_tool_result_only_user_msg else f"{prefix}{text}"
                if used + len(line) + 2 > budget and history_parts:
                    break
                history_parts.insert(0, line)
                used += len(line) + 2

    if tools and log.isEnabledFor(logging.DEBUG):
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
        log.debug(
            "[Prompt] 工具模式: history_msgs=%s history_chars=%s tool_count=%s tool_names=%s first_user=%r latest_user=%r tool_instr=%r",
            len(history_parts),
            used,
            len(tool_names),
            tool_names[:12],
            first_user_preview,
            latest_user_preview,
            tool_instruction_preview,
        )
    # 组装顺序（关键）：
    #   [sys_part]
    #   [tools_part]           工具指令块（action marker 协议）
    #   [few-shot]             多类别工具示范（让模型学会调 MCP）
    #   [history_parts]        真实对话 + tool_use / tool_result（紧邻 Assistant: 以便模型感知）
    #   [latest_user_line]     当前任务
    #   Assistant:
    #
    # 重要：真实工具历史必须排在 few-shot 之后、Assistant: 之前，
    # 因为模型的注意力偏向 prompt 末尾。若 history 远离末尾，模型只记得 few-shot 示范，
    # 就会反复重复 few-shot 里那个工具（典型症状：new_page 被无限循环调用）。
    parts = []
    if sys_part:
        parts.append(sys_part)
    if tools_part:
        parts.append(tools_part)

    # Namespace-based few-shot：让模型学会调用所有类别工具
    if tools and client_profile == CLAUDE_CODE_OPENAI_PROFILE:
        few_shot_tools = pick_few_shot_tools(tools, max_third_party=4)
        if len(few_shot_tools) >= 2:
            def _render_tc(name: str, input_data: dict) -> str:
                payload = json.dumps({"name": to_qwen_name(name), "input": input_data}, ensure_ascii=False)
                return f"##TOOL_CALL##\n{payload}\n##END_CALL##"
            user_fs, asst_fs = render_few_shot_turn(few_shot_tools, _render_tc, thinking_enabled=False)
            parts.append(f"Human: {user_fs}")
            parts.append(f"Assistant: {asst_fs}")
            log.info(f"[少样本] 注入 {len(few_shot_tools)} 个代表: {tool_summary_for_log(few_shot_tools)}")

    # 真实对话历史 —— 排在 few-shot 之后紧邻 Assistant:
    parts.extend(history_parts)

    if latest_user_line:
        parts.append(latest_user_line)

    # 状态感知催促：用户要求"读+写"但模型只完成了读就停下来的场景，
    # 在 Assistant: 前注入强制指令迫使下一步输出 Write/Edit 工具调用。
    state_notice = _build_state_followup_notice(messages, tools, client_profile)
    if state_notice:
        parts.append(state_notice)

    parts.append("Assistant:")
    return "\n\n".join(parts)


_READ_VERBS = ("读取", "阅读", "查看", "看看", "读", "read", "open")
_WRITE_VERBS = ("写", "创建", "生成", "新建", "保存", "记录", "write", "create", "generate", "save", "edit", "修改", "更新")


def _build_state_followup_notice(messages, tools, client_profile) -> str:
    """Detect 'user wants read+write, Read already done, Write not yet' → inject
    a mandatory next-action notice so Qwen stops summarizing and calls Write."""
    if not messages or not tools or client_profile != CLAUDE_CODE_OPENAI_PROFILE:
        return ""
    # 1. Check the FIRST user message for both read + write intent.
    first_user_text = ""
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user":
            first_user_text = _extract_user_text_only(m.get("content", ""), client_profile=client_profile)
            if first_user_text.strip():
                break
    if not first_user_text:
        return ""
    lower = first_user_text.lower()
    wants_read = any(v in lower for v in _READ_VERBS)
    wants_write = any(v in lower for v in _WRITE_VERBS)
    if not (wants_read and wants_write):
        return ""
    # 2. Check history for at least one Read tool_use with non-trivial result, AND no Write/Edit yet.
    read_done = False
    write_done = False
    read_alias_names = {"Read", "fs_open_file", "ReadX"}
    write_alias_names = {"Write", "Edit", "NotebookEdit", "fs_put_file", "fs_patch_file", "notebook_patch", "WriteX", "EditX"}
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_use":
                tname = part.get("name", "")
                if tname in read_alias_names:
                    read_done = True
                elif tname in write_alias_names:
                    write_done = True
        # Also: scan assistant text for textual tool_use markers (Qwen bridge history renders as text)
        if isinstance(m, dict) and m.get("role") == "assistant":
            plain = _extract_text(m.get("content", ""), client_profile=client_profile)
            if "##TOOL_CALL##" in plain:
                if any(f'"name": "{n}"' in plain for n in read_alias_names):
                    read_done = True
                if any(f'"name": "{n}"' in plain for n in write_alias_names):
                    write_done = True
    if not read_done or write_done:
        return ""
    return (
        "[STATE NOTICE — MUST OBEY]\n"
        "The user's CURRENT TASK explicitly requires TWO operations: reading AND writing/editing.\n"
        "You have ALREADY completed the read (the file content is in the history above).\n"
        f"Your NEXT output MUST be a {to_qwen_name('Write')}/{to_qwen_name('Edit')} tool call in the required ##TOOL_CALL## format.\n"
        "DO NOT summarize. DO NOT explain. DO NOT ask for confirmation. DO NOT output plain text.\n"
        f"If you output anything other than a ##TOOL_CALL## block for {to_qwen_name('Write')}/{to_qwen_name('Edit')}, the user's task FAILS."
    )


def _extract_text_content(content) -> str:
    """Flatten Anthropic content array/string → plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text", ""))
        return "".join(parts)
    return ""


def _resolve_cache_hints(messages: list) -> list:
    """当 Claude Code 发来 'File unchanged since last read' 提示语时，
    用代理侧缓存的上一次真实 Read 结果回填，避免下游 Qwen 因缺少内容死循环。

    - 正向：遇到非提示语的 Read tool_result，按 file_path 写入缓存
    - 反向：遇到提示语 tool_result，按 file_path 查缓存；命中则替换 content 为真实内容
    - 未命中时保留原提示语（Qwen 至少知道"已经读过"这个事实）
    """
    if not messages:
        return messages
    ctx = get_request_context()
    session_key = ctx.get("api_key", "") or ""

    # pass 1: tool_use_id -> file_path (only Read-like tools)
    toolu_to_path: dict[str, str] = {}
    READ_TOOL_NAMES = {"Read", "fs_open_file", "ReadX"}  # ReadX kept for back-compat with in-flight sessions
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_use" and part.get("name") in READ_TOOL_NAMES:
                tid = part.get("id")
                fpath = (part.get("input") or {}).get("file_path") or (part.get("input") or {}).get("path")
                if tid and fpath:
                    toolu_to_path[tid] = fpath

    # pass 2: populate cache with real content AND rewrite hint-only results
    rewritten = 0
    populated = 0
    out_messages: list = []
    for msg in messages:
        if not isinstance(msg, dict):
            out_messages.append(msg)
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            out_messages.append(msg)
            continue
        new_content = []
        mutated = False
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "tool_result":
                new_content.append(part)
                continue
            tid = part.get("tool_use_id", "")
            fpath = toolu_to_path.get(tid)
            inner = part.get("content", "")
            inner_text = inner if isinstance(inner, str) else _extract_text_content(inner)

            if fpath and inner_text and not file_content_cache.is_cache_hint(inner_text):
                # real content → cache it
                file_content_cache.put(session_key, fpath, inner_text)
                populated += 1
                new_content.append(part)
                continue

            if fpath and inner_text and file_content_cache.is_cache_hint(inner_text):
                cached = file_content_cache.get(session_key, fpath)
                if cached:
                    new_part = dict(part)
                    # Preserve the hint as a small header so the model knows this came
                    # from the cache, followed by the real content.
                    new_part["content"] = (
                        f"[Proxy cache: previously read content of {fpath}]\n{cached}"
                    )
                    new_content.append(new_part)
                    mutated = True
                    rewritten += 1
                    continue

            new_content.append(part)
        if mutated:
            new_msg = dict(msg)
            new_msg["content"] = new_content
            out_messages.append(new_msg)
        else:
            out_messages.append(msg)

    if rewritten or populated:
        log.info(f"[CacheHint] populated={populated} rewritten={rewritten} session={'set' if session_key else 'global'}")
    return out_messages


def _apply_topic_isolation(messages: list, client_profile: str) -> list:
    """若最新有文本的 user 消息与历史首条有文本的 user 消息实体无重合，判定为新任务。
    保留 system 消息 + **最新 user 所在位置及之后的所有消息**（这样新任务自己产生的
    工具链 tool_use/tool_result 不会被误砍掉）。"""
    if not messages or len(messages) < 3:
        return messages
    # 找首条有实文本的 user
    first_user = None
    first_user_text = ""
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user":
            txt = _extract_user_text_only(m.get("content", ""), client_profile=client_profile).strip()
            if txt:
                first_user = m
                first_user_text = txt
                break
    if first_user is None:
        return messages
    # 最新有实文本的 user —— 并记下它在 messages 里的位置
    last_user = None
    last_user_text = ""
    last_user_idx = -1
    for idx in range(len(messages) - 1, -1, -1):
        m = messages[idx]
        if isinstance(m, dict) and m.get("role") == "user":
            txt = _extract_user_text_only(m.get("content", ""), client_profile=client_profile).strip()
            if txt:
                last_user = m
                last_user_text = txt
                last_user_idx = idx
                break
    if last_user is None or last_user is first_user:
        return messages
    if not detect_topic_change(first_user_text, last_user_text):
        return messages
    # 话题切换：保留 system + 从 last_user 开始到末尾的所有消息
    # （这包含新任务自己产生的 tool_use/tool_result 工具链）
    system_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "system"]
    tail = messages[last_user_idx:]
    isolated = system_msgs + tail
    dropped = len(messages) - len(isolated)
    if dropped > 0:
        log.info(
            f"[话题隔离] 检测到新任务，丢弃 {dropped} 条旧历史（保留新任务工具链 {len(tail)} 条）。"
            f"旧任务={first_user_text[:60]!r}  新任务={last_user_text[:60]!r}"
        )
    return isolated


def messages_to_prompt(req_data: dict, *, client_profile: str = OPENCLAW_OPENAI_PROFILE) -> PromptBuildResult:
    resolved_client_profile = client_profile
    raw_messages = req_data.get("messages", [])
    # 话题隔离：新任务与历史首条 user 实体零重合时，丢弃所有历史，只保留 system + 最新 user。
    # 这解决 Claude Code 同 session 多任务时旧对话干扰新任务的问题。
    isolated = _apply_topic_isolation(raw_messages, resolved_client_profile)
    # Pass: 历史拒绝清洗
    cleaned_messages, cleaned_count = clean_refusal_messages(isolated)
    if cleaned_count:
        log.info(f"[拒绝清洗] 替换了 {cleaned_count} 条 assistant 拒绝消息")
    # Pass: 文件缓存回填
    messages = _resolve_cache_hints(cleaned_messages)
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
