from __future__ import annotations

from backend.adapter.standard_request import StandardRequest, normalize_tool_choice
from backend.core.config import resolve_request_model
from backend.services.client_profiles import infer_client_profile, request_looks_like_coding_task
from backend.services.prompt_builder import messages_to_prompt
from backend.toolcall.normalize import build_tool_name_registry, normalize_tool_name


def build_chat_standard_request(req_data: dict, *, default_model: str, surface: str, client_profile: str = "openclaw_openai") -> StandardRequest:
    requested_model = req_data.get("model", default_model)
    effective_client_profile = infer_client_profile(req_data, fallback_profile=client_profile)
    prompt_result = messages_to_prompt(req_data, client_profile=effective_client_profile)
    tools = prompt_result.tools
    tool_names = [tool_name for tool_name in (tool.get("name") for tool in tools) if isinstance(tool_name, str) and tool_name]
    coding_intent = request_looks_like_coding_task(req_data, client_profile=effective_client_profile)
    tool_choice = normalize_tool_choice(req_data.get("tool_choice"))
    if tool_choice.required_tool_name:
        tool_choice.required_tool_name = normalize_tool_name(tool_choice.required_tool_name, tool_names)
    return StandardRequest(
        prompt=prompt_result.prompt,
        response_model=requested_model,
        resolved_model=resolve_request_model(
            requested_model,
            client_profile=effective_client_profile,
            tool_enabled=prompt_result.tool_enabled,
            coding_intent=coding_intent,
        ),
        surface=surface,
        client_profile=effective_client_profile,
        requested_model=requested_model,
        stream=req_data.get("stream", False),
        tools=tools,
        tool_names=tool_names,
        tool_name_registry=build_tool_name_registry(tool_names),
        tool_enabled=prompt_result.tool_enabled,
        tool_choice_mode=tool_choice.mode,
        required_tool_name=tool_choice.required_tool_name,
        tool_choice_raw=tool_choice.raw,
    )
