from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

CLAUDE_CODE_OPENAI_PROFILE = "claude_code_openai"
OPENCLAW_OPENAI_PROFILE = "openclaw_openai"
QWEN_CODE_OPENAI_PROFILE = "qwen_code_openai"

OPENCLAW_STARTUP_PATTERNS = (
    "A new session was started via /new or /reset.",
    "If runtime-provided startup context is included for this first turn",
)
OPENCLAW_UNTRUSTED_METADATA_PREFIX = "Sender (untrusted metadata):"
OPENCODE_SYSTEM_PREFIX = "you are opencode"

QWEN_CODE_SYSTEM_HINTS = ("qwen code", "qwen-code", "you are qwen code", "you are qwen-code")
QWEN_CODE_OPENAI_TOOL_NAMES = frozenset({
    "read_file",
    "list_directory",
    "write_file",
    "run_shell_command",
})
QWEN_CODE_PROFILE_TOOL_HINTS = {
    "readfile",
    "writefile",
    "editfile",
    "listdirectory",
    "listdir",
    "listfiles",
    "runshellcommand",
    "runcommand",
}
QWEN_CODE_TOOL_HINTS = {
    "read",
    "write",
    "edit",
    "multiedit",
    "notebookedit",
    "grep",
    "glob",
    "bash",
    "readfile",
    "writefile",
    "editfile",
    "notebookeditcell",
    "runcommand",
    "execcommand",
    "listdirectory",
    "listdir",
    "listfiles",
    "searchfiles",
}
QWEN_CODE_FILE_TOOL_HINTS = {
    "read",
    "write",
    "edit",
    "multiedit",
    "notebookedit",
    "readfile",
    "writefile",
    "editfile",
}
QWEN_CODE_NAV_TOOL_HINTS = {
    "grep",
    "glob",
    "bash",
    "runcommand",
    "execcommand",
    "listdirectory",
    "listdir",
    "listfiles",
    "searchfiles",
}
QWEN_CODE_TASK_REGEX = re.compile(
    r"(code|coding|program|repo|repository|refactor|debug|fix|implement|patch|file|files|terminal|shell|bash|command|test|build|编程|代码|仓库|文件|脚本|命令|调试|修复|实现|重构|测试)",
    re.IGNORECASE,
)
QWEN_CODE_OPENAI_HINT_HEADERS = (
    "user-agent",
    "x-openai-client-user-agent",
    "x-client-user-agent",
)
OPENAI_SDK_FINGERPRINT_HEADERS = (
    "x-openai-client-user-agent",
    "x-stainless-lang",
    "x-stainless-package-version",
    "x-stainless-runtime",
)
EXPLICIT_PROFILE_HEADERS = (
    "x-agent-profile",
    "x-client-profile",
    "x-codex-client-profile",
)
PROFILE_ALIASES = {
    "claude_code": CLAUDE_CODE_OPENAI_PROFILE,
    "claude_code_openai": CLAUDE_CODE_OPENAI_PROFILE,
    "openclaw": OPENCLAW_OPENAI_PROFILE,
    "openclaw_openai": OPENCLAW_OPENAI_PROFILE,
    "opencode": OPENCLAW_OPENAI_PROFILE,
    "qwen_code": QWEN_CODE_OPENAI_PROFILE,
    "qwen_code_openai": QWEN_CODE_OPENAI_PROFILE,
    "qwen-code": QWEN_CODE_OPENAI_PROFILE,
}

log = logging.getLogger("qwen2api.client_profiles")


@dataclass(slots=True)
class ClientProfileDecision:
    profile: str
    reason: str
    explicit: bool = False
    override_source: str | None = None


def header_value(headers: Mapping[str, Any] | Any, header_name: str) -> str:
    value = ""
    if hasattr(headers, "get"):
        value = headers.get(header_name, "")
    elif isinstance(headers, Mapping):
        value = headers.get(header_name, "")
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def normalize_client_profile_name(value: str | None) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return PROFILE_ALIASES.get(normalized, normalized)


def normalized_tool_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def normalize_tool(tool: dict[str, Any]) -> dict[str, Any]:
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


def normalize_tools(tools: list[Any] | None) -> list[dict[str, Any]]:
    return [normalize_tool(tool) for tool in (tools or []) if isinstance(tool, dict)]


def extract_declared_tool_names(req_data: dict[str, Any] | None) -> set[str]:
    if not isinstance(req_data, dict):
        return set()

    tool_names: set[str] = set()
    for tool in req_data.get("tools", []) or []:
        if not isinstance(tool, dict):
            continue
        candidate = tool.get("name")
        if not isinstance(candidate, str) or not candidate.strip():
            function_payload = tool.get("function")
            if isinstance(function_payload, dict):
                candidate = function_payload.get("name")
        if isinstance(candidate, str) and candidate.strip():
            tool_names.add(candidate.strip().lower())
    return tool_names


def has_qwen_code_header_hint(headers: Mapping[str, Any] | Any) -> bool:
    for header_name in QWEN_CODE_OPENAI_HINT_HEADERS:
        value = header_value(headers, header_name).lower()
        if "qwen" in value and "code" in value:
            return True
    return False


def has_openai_sdk_fingerprint(headers: Mapping[str, Any] | Any) -> bool:
    return any(header_value(headers, header_name) for header_name in OPENAI_SDK_FINGERPRINT_HEADERS)


def is_qwen_code_openai_request(headers: Mapping[str, Any] | Any, req_data: dict[str, Any] | None) -> bool:
    tool_names = extract_declared_tool_names(req_data)
    qwen_tool_matches = len(tool_names & QWEN_CODE_OPENAI_TOOL_NAMES)
    if qwen_tool_matches >= len(QWEN_CODE_OPENAI_TOOL_NAMES):
        return True
    if qwen_tool_matches >= 3 and (has_qwen_code_header_hint(headers) or has_openai_sdk_fingerprint(headers)):
        return True
    return has_qwen_code_header_hint(headers)


def resolve_explicit_client_profile(headers: Mapping[str, Any] | Any, req_data: dict[str, Any] | None) -> ClientProfileDecision | None:
    for header_name in EXPLICIT_PROFILE_HEADERS:
        candidate = normalize_client_profile_name(header_value(headers, header_name))
        if candidate in {CLAUDE_CODE_OPENAI_PROFILE, OPENCLAW_OPENAI_PROFILE, QWEN_CODE_OPENAI_PROFILE}:
            return ClientProfileDecision(
                profile=candidate,
                reason=f"explicit_header:{header_name}",
                explicit=True,
                override_source=header_name,
            )

    metadata = req_data.get("metadata") if isinstance(req_data, dict) else None
    if isinstance(metadata, dict):
        for key in ("client_profile", "agent_profile"):
            candidate = normalize_client_profile_name(metadata.get(key))
            if candidate in {CLAUDE_CODE_OPENAI_PROFILE, OPENCLAW_OPENAI_PROFILE, QWEN_CODE_OPENAI_PROFILE}:
                return ClientProfileDecision(
                    profile=candidate,
                    reason=f"explicit_metadata:{key}",
                    explicit=True,
                    override_source=key,
                )
    return None


def sanitize_openclaw_user_text(text: str) -> str:
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


def extract_user_text_only(content: Any, client_profile: str = OPENCLAW_OPENAI_PROFILE) -> str:
    if isinstance(content, str):
        return sanitize_openclaw_user_text(content) if client_profile == OPENCLAW_OPENAI_PROFILE else content
    if not isinstance(content, list):
        return ""

    text_blocks: list[str] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type", "") != "text":
            continue
        block_text = part.get("text", "")
        if client_profile == OPENCLAW_OPENAI_PROFILE:
            block_text = sanitize_openclaw_user_text(block_text)
        if block_text:
            text_blocks.append(block_text)
    return "\n".join(text_blocks)


def extract_system_prompt(req_data: dict[str, Any], *, client_profile: str = OPENCLAW_OPENAI_PROFILE) -> str:
    system_prompt = ""
    sys_field = req_data.get("system", "")
    if isinstance(sys_field, list):
        system_prompt = " ".join(
            part.get("text", "")
            for part in sys_field
            if isinstance(part, dict)
        )
    elif isinstance(sys_field, str):
        system_prompt = sys_field

    if system_prompt:
        return system_prompt

    for msg in req_data.get("messages", []) or []:
        if msg.get("role") == "system":
            return extract_user_text_only(msg.get("content", ""), client_profile=client_profile)
    return ""


def looks_like_opencode_system_prompt(system_prompt: str) -> bool:
    if not isinstance(system_prompt, str):
        return False
    return system_prompt.strip().lower().startswith(OPENCODE_SYSTEM_PREFIX)


def extract_latest_user_text(
    messages: list[dict[str, Any]] | None,
    *,
    client_profile: str = OPENCLAW_OPENAI_PROFILE,
) -> str:
    for msg in reversed(messages or []):
        if msg.get("role") != "user":
            continue
        text = extract_user_text_only(msg.get("content", ""), client_profile=client_profile).strip()
        if text:
            return text
    return ""


def request_looks_like_coding_task(
    req_data: dict[str, Any],
    *,
    client_profile: str = OPENCLAW_OPENAI_PROFILE,
) -> bool:
    normalized_tools = normalize_tools(req_data.get("tools", []))
    normalized_names = {
        normalized_tool_name(tool.get("name", ""))
        for tool in normalized_tools
        if tool.get("name")
    }
    latest_user_text = extract_latest_user_text(
        req_data.get("messages", []),
        client_profile=client_profile,
    )
    if len(normalized_names & QWEN_CODE_FILE_TOOL_HINTS) >= 1 and len(normalized_names & QWEN_CODE_NAV_TOOL_HINTS) >= 1:
        return True
    if len(normalized_names & QWEN_CODE_TOOL_HINTS) >= 3:
        return True
    return bool(latest_user_text and QWEN_CODE_TASK_REGEX.search(latest_user_text))


def infer_client_profile(
    req_data: dict[str, Any],
    *,
    fallback_profile: str = OPENCLAW_OPENAI_PROFILE,
) -> str:
    if fallback_profile in {CLAUDE_CODE_OPENAI_PROFILE, QWEN_CODE_OPENAI_PROFILE}:
        return fallback_profile

    system_prompt = extract_system_prompt(req_data, client_profile=fallback_profile)
    system_lower = system_prompt.strip().lower()
    if looks_like_opencode_system_prompt(system_prompt):
        return fallback_profile
    if any(hint in system_lower for hint in QWEN_CODE_SYSTEM_HINTS):
        return QWEN_CODE_OPENAI_PROFILE

    normalized_names = {
        normalized_tool_name(tool.get("name", ""))
        for tool in normalize_tools(req_data.get("tools", []))
        if tool.get("name")
    }
    qwen_code_tool_matches = normalized_names & QWEN_CODE_PROFILE_TOOL_HINTS
    if len(qwen_code_tool_matches) >= 3:
        return QWEN_CODE_OPENAI_PROFILE
    if len(qwen_code_tool_matches) >= 2 and request_looks_like_coding_task(req_data, client_profile=fallback_profile):
        return QWEN_CODE_OPENAI_PROFILE
    return fallback_profile


def detect_openai_client_profile_decision(headers: Mapping[str, Any] | Any, req_data: dict[str, Any] | None) -> ClientProfileDecision:
    if header_value(headers, "x-anthropic-billing-header"):
        return ClientProfileDecision(
            profile=CLAUDE_CODE_OPENAI_PROFILE,
            reason="anthropic_billing_header",
        )
    explicit = resolve_explicit_client_profile(headers, req_data)
    if explicit is not None:
        return explicit
    if is_qwen_code_openai_request(headers, req_data):
        reason = "qwen_code_header_hint" if has_qwen_code_header_hint(headers) else "qwen_code_tool_signature"
        return ClientProfileDecision(profile=QWEN_CODE_OPENAI_PROFILE, reason=reason)
    return ClientProfileDecision(profile=OPENCLAW_OPENAI_PROFILE, reason="default_openclaw")


def detect_openai_client_profile(headers: Mapping[str, Any] | Any, req_data: dict[str, Any] | None) -> str:
    return detect_openai_client_profile_decision(headers, req_data).profile


def log_client_profile_decision(
    decision: ClientProfileDecision,
    *,
    headers: Mapping[str, Any] | Any,
    req_data: dict[str, Any] | None,
    surface: str,
) -> None:
    tool_names = sorted(extract_declared_tool_names(req_data))
    log.info(
        "[Profile] surface=%s profile=%s reason=%s explicit=%s source=%s header_hint=%s sdk_fingerprint=%s tools=%s",
        surface,
        decision.profile,
        decision.reason,
        decision.explicit,
        decision.override_source or "-",
        has_qwen_code_header_hint(headers),
        has_openai_sdk_fingerprint(headers),
        tool_names[:8],
    )


__all__ = [
    "CLAUDE_CODE_OPENAI_PROFILE",
    "OPENCLAW_OPENAI_PROFILE",
    "QWEN_CODE_OPENAI_PROFILE",
    "detect_openai_client_profile",
    "detect_openai_client_profile_decision",
    "extract_declared_tool_names",
    "extract_latest_user_text",
    "extract_system_prompt",
    "extract_user_text_only",
    "has_openai_sdk_fingerprint",
    "has_qwen_code_header_hint",
    "header_value",
    "infer_client_profile",
    "is_qwen_code_openai_request",
    "log_client_profile_decision",
    "looks_like_opencode_system_prompt",
    "normalize_client_profile_name",
    "normalize_tool",
    "normalize_tools",
    "normalized_tool_name",
    "request_looks_like_coding_task",
    "sanitize_openclaw_user_text",
]
