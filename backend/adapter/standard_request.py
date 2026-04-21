from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.runtime.attachment_types import NormalizedAttachment
from backend.services.client_profiles import (
    CLAUDE_CODE_OPENAI_PROFILE,
    OPENCLAW_OPENAI_PROFILE,
    QWEN_CODE_OPENAI_PROFILE,
    detect_openai_client_profile,
    is_qwen_code_openai_request,
)


@dataclass(slots=True)
class ToolChoiceSpec:
    mode: str = "auto"
    required_tool_name: str | None = None
    raw: Any | None = None


def normalize_tool_choice(tool_choice: Any) -> ToolChoiceSpec:
    if tool_choice is None:
        return ToolChoiceSpec(mode="auto", raw=None)

    if isinstance(tool_choice, str):
        lowered = tool_choice.strip().lower()
        if lowered in {"required", "any"}:
            return ToolChoiceSpec(mode="required", raw=tool_choice)
        if lowered == "none":
            return ToolChoiceSpec(mode="none", raw=tool_choice)
        return ToolChoiceSpec(mode="auto", raw=tool_choice)

    if not isinstance(tool_choice, dict):
        return ToolChoiceSpec(mode="auto", raw=tool_choice)

    choice_type = str(tool_choice.get("type") or "").strip().lower()
    if choice_type in {"required", "any"}:
        return ToolChoiceSpec(mode="required", raw=tool_choice)
    if choice_type == "none":
        return ToolChoiceSpec(mode="none", raw=tool_choice)
    if choice_type in {"function", "tool"}:
        raw_function_payload = tool_choice.get("function")
        function_payload: dict[str, Any] = raw_function_payload if isinstance(raw_function_payload, dict) else {}
        required_tool_name = str(
            function_payload.get("name")
            or tool_choice.get("name")
            or ""
        ).strip() or None
        if required_tool_name:
            return ToolChoiceSpec(mode="required", required_tool_name=required_tool_name, raw=tool_choice)
    return ToolChoiceSpec(mode="auto", raw=tool_choice)


@dataclass(slots=True)
class StandardRequest:
    prompt: str
    response_model: str
    resolved_model: str
    surface: str
    client_profile: str = OPENCLAW_OPENAI_PROFILE
    requested_model: str | None = None
    content: str | None = None
    stream: bool = False
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    tool_name_registry: dict[str, str] = field(default_factory=dict)
    tool_enabled: bool = False
    tool_choice_mode: str = "auto"
    required_tool_name: str | None = None
    tool_choice_raw: Any | None = None
    attachments: list[NormalizedAttachment] = field(default_factory=list)
    uploaded_file_ids: list[str] = field(default_factory=list)
    upstream_files: list[dict[str, Any]] = field(default_factory=list)
    session_key: str | None = None
    context_mode: str = "inline"
    bound_account_email: str | None = None
    bound_account: Any | None = None
    stage_labels: dict[str, str] = field(default_factory=dict)
    full_prompt: str | None = None
    upstream_chat_id: str | None = None
    persistent_session: bool = False
    session_message_hashes: list[str] = field(default_factory=list)
    session_chat_invalidated: bool = False
