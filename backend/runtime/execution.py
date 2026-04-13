from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from backend.adapter.standard_request import StandardRequest
from backend.core.config import settings
from backend.core.request_logging import update_request_context
from backend.runtime.stream_metrics import StreamMetrics
from backend.services import tool_parser
from backend.toolcall.normalize import normalize_tool_name
from backend.toolcall.stream_state import StreamingToolCallState

log = logging.getLogger("qwen2api.runtime")


@dataclass(slots=True)
class RuntimeAttemptState:
    answer_text: str = ""
    reasoning_text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    blocked_tool_names: list[str] = field(default_factory=list)
    finish_reason: str = "stop"
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    emitted_visible_output: bool = False
    stage_metrics: dict[str, float] = field(default_factory=dict)
    first_answer_preview: str = ""
    first_reasoning_preview: str = ""
    first_tool_call_preview: str = ""


@dataclass(slots=True)
class RuntimeExecutionResult:
    state: RuntimeAttemptState
    chat_id: str | None
    acc: Any | None


@dataclass(slots=True)
class RuntimeToolDirective:
    tool_blocks: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = "end_turn"


@dataclass(slots=True)
class RuntimeRetryDirective:
    retry: bool
    next_prompt: str


@dataclass(slots=True)
class RuntimeRetryContinuation:
    should_continue: bool
    next_prompt: str


@dataclass(slots=True)
class RuntimeRetryLoop:
    prompt: str
    max_attempts: int


@dataclass(slots=True)
class RuntimeAttemptPlan:
    loop: RuntimeRetryLoop
    prompt: str


@dataclass(slots=True)
class AnthropicStreamCompletionResult:
    chunks: list[str]


@dataclass(slots=True)
class AnthropicStreamSuccessResult:
    chunks: list[str]
    usage_delta: int


@dataclass(slots=True)
class RuntimeAttemptOutcome:
    execution: RuntimeExecutionResult
    continuation: RuntimeRetryContinuation


@dataclass(slots=True)
class RuntimeAttemptCursor:
    index: int
    number: int


TRAILING_IDLE_AFTER_TOOL_SECONDS = 2.0


def _preview_text(text: str, limit: int = 240) -> str:
    if not text:
        return ""
    compact = " ".join(str(text).split())
    return compact[:limit] + ("...[truncated]" if len(compact) > limit else "")


__all__ = [
    "RuntimeAttemptState",
    "RuntimeExecutionResult",
    "RuntimeToolDirective",
    "RuntimeRetryDirective",
    "RuntimeRetryContinuation",
    "RuntimeRetryLoop",
    "RuntimeAttemptPlan",
    "AnthropicStreamCompletionResult",
    "AnthropicStreamSuccessResult",
    "RuntimeAttemptOutcome",
    "RuntimeAttemptCursor",
    "anthropic_stream_stop_reason",
    "anthropic_stream_usage_delta",
    "build_retry_loop",
    "build_tool_directive",
    "begin_runtime_attempt",
    "cleanup_runtime_resources",
    "collect_completion_run",
    "continue_after_retry_directive",
    "evaluate_retry_directive",
    "extract_blocked_tool_names",
    "finalize_anthropic_stream_success",
    "complete_anthropic_stream_success",
    "has_recent_search_no_results",
    "has_recent_unchanged_read_result",
    "inject_assistant_message",
    "native_tool_calls_to_markup",
    "parse_tool_directive_once",
    "plan_runtime_attempts",
    "recent_same_tool_identity_count",
    "retryable_usage_delta",
    "should_force_finish_after_tool_use",
    "tool_identity",
]


def begin_runtime_attempt(attempt_index: int) -> RuntimeAttemptCursor:
    cursor = RuntimeAttemptCursor(index=attempt_index, number=attempt_index + 1)
    update_request_context(stream_attempt=cursor.number)
    return cursor


def should_force_finish_after_tool_use(stop_reason: str, trailing_idle_seconds: float, visible_output_after_tool: bool) -> bool:
    return stop_reason == "tool_use" and trailing_idle_seconds >= TRAILING_IDLE_AFTER_TOOL_SECONDS and not visible_output_after_tool


def extract_blocked_tool_names(text: str, allowed_tool_names: list[str] | None = None) -> list[str]:
    if not text:
        return []
    blocked = re.findall(r"Tool\s+([A-Za-z0-9_.:-]+)\s+does not exists?\.?", text)
    if not blocked:
        return []
    if not allowed_tool_names:
        return blocked
    return [normalize_tool_name(name, allowed_tool_names) for name in blocked]


def normalize_tool_name_for_retry(tool_name: str, allowed_tool_names: list[str] | None) -> str:
    return normalize_tool_name(tool_name, allowed_tool_names or [])


def _recent_message_texts(messages: list[dict[str, Any]] | None, *, limit: int = 10) -> list[str]:
    texts: list[str] = []
    checked = 0
    for msg in reversed(messages or []):
        checked += 1
        content = msg.get("content", "")
        parts: list[str] = []
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        parts.append(part.get("text", ""))
                    elif part.get("type") == "tool_result":
                        inner = part.get("content", "")
                        if isinstance(inner, str):
                            parts.append(inner)
                        elif isinstance(inner, list):
                            for inner_part in inner:
                                if isinstance(inner_part, dict) and inner_part.get("type") == "text":
                                    parts.append(inner_part.get("text", ""))
                elif isinstance(part, str):
                    parts.append(part)
        merged = "\n".join(text for text in parts if text)
        if merged:
            texts.append(merged)
        if checked >= limit:
            break
    return texts


def has_recent_unchanged_read_result(messages: list[dict[str, Any]] | None) -> bool:
    return any("Unchanged since last read" in text for text in _recent_message_texts(messages))


def has_recent_search_no_results(messages: list[dict[str, Any]] | None) -> bool:
    for text in _recent_message_texts(messages):
        lowered = text.lower()
        if "websearch" not in lowered:
            continue
        if "did 0 searches" in lowered or '"results": []' in lowered or '"matches": []' in lowered:
            return True
    return False


def tool_identity(tool_name: str, tool_input: Any = None) -> str:
    try:
        if tool_name == "Read" and isinstance(tool_input, dict):
            return f"Read::{tool_input.get('file_path', '').strip()}"
        return f"{tool_name}::{json.dumps(tool_input or {}, ensure_ascii=False, sort_keys=True)}"
    except Exception:
        return tool_name or ""


def recent_same_tool_identity_count(messages: list[dict[str, Any]] | None, tool_name: str, tool_input: Any = None) -> int:
    target = tool_identity(tool_name, tool_input)
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
        if len(tools) == 1 and tool_identity(tools[0].get("name", ""), tools[0].get("input", {})) == target:
            count += 1
            continue
        break
    return count


def native_tool_calls_to_markup(tool_calls: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for tool_call in tool_calls:
        parts.append(
            f'<tool_call>{{"name": {json.dumps(tool_call["name"])}, "input": {json.dumps(tool_call.get("input", {}), ensure_ascii=False)}}}</tool_call>'
        )
    return "\n".join(parts)


async def collect_completion_run(
    client,
    request: StandardRequest,
    prompt: str,
    *,
    capture_events: bool = True,
    on_delta: Callable[[dict[str, Any], str | None, list[dict[str, Any]] | None], Awaitable[None]] | None = None,
) -> RuntimeExecutionResult:
    chat_id = None
    acc = None
    answer_fragments: list[str] = []
    reasoning_fragments: list[str] = []
    native_tool_calls: list[dict[str, Any]] = []
    first_answer_preview = ""
    first_reasoning_preview = ""
    first_tool_call_preview = ""
    tool_state = StreamingToolCallState()
    emitted_visible_output = False
    raw_events: list[dict[str, Any]] = []
    metrics = StreamMetrics()
    first_event_marked = False

    async for item in client.chat_stream_events_with_retry(
        request.resolved_model,
        prompt,
        has_custom_tools=bool(request.tools),
    ):
        if item.get("type") == "meta":
            chat_id = item.get("chat_id")
            acc = item.get("acc")
            update_request_context(chat_id=chat_id)
            metrics.mark("chat_created", float(len(raw_events)))
            continue
        if item.get("type") != "event":
            continue

        evt = item.get("event", {})
        if capture_events:
            raw_events.append(evt)
        if evt.get("type") != "delta":
            continue

        phase = evt.get("phase", "")
        content = evt.get("content", "")

        if phase in ("think", "thinking_summary") and content:
            reasoning_fragments.append(content)
            if not first_reasoning_preview:
                first_reasoning_preview = _preview_text(content)
                if request.tools:
                    log.info("[Runtime] first_reasoning_delta=%r", first_reasoning_preview)
            emitted_visible_output = True
            if not first_event_marked:
                metrics.mark("first_event", float(len(raw_events)))
                first_event_marked = True
            if on_delta is not None:
                await on_delta(evt, content, None)
            continue

        if phase == "answer" and content:
            answer_fragments.append(content)
            if not first_answer_preview:
                first_answer_preview = _preview_text(content)
                if request.tools:
                    log.info("[Runtime] first_answer_delta=%r", first_answer_preview)
            emitted_visible_output = True
            if not first_event_marked:
                metrics.mark("first_event", float(len(raw_events)))
                first_event_marked = True
            if on_delta is not None:
                await on_delta(evt, content, None)
            continue

        if phase == "tool_call":
            emitted_visible_output = True
            if not first_event_marked:
                metrics.mark("first_event", float(len(raw_events)))
                first_event_marked = True
            completed_calls = tool_state.process_event(evt)
            if completed_calls:
                native_tool_calls.extend(completed_calls)
                if not first_tool_call_preview:
                    first_tool_call_preview = _preview_text(json.dumps(completed_calls[0], ensure_ascii=False), 320)
                    if request.tools:
                        log.info("[Runtime] first_native_tool_call=%r", first_tool_call_preview)
                if on_delta is not None:
                    await on_delta(evt, None, completed_calls)

    answer_text = "".join(answer_fragments)
    reasoning_text = "".join(reasoning_fragments)
    if native_tool_calls and not answer_text:
        answer_text = native_tool_calls_to_markup(native_tool_calls)

    state = RuntimeAttemptState(
        answer_text=answer_text,
        reasoning_text=reasoning_text,
        tool_calls=native_tool_calls,
        blocked_tool_names=extract_blocked_tool_names(answer_text.strip(), request.tool_names),
        finish_reason="tool_calls" if native_tool_calls else "stop",
        raw_events=raw_events,
        emitted_visible_output=emitted_visible_output,
        stage_metrics=metrics.summary(),
        first_answer_preview=first_answer_preview,
        first_reasoning_preview=first_reasoning_preview,
        first_tool_call_preview=first_tool_call_preview,
    )
    if request.tools:
        log.info(
            "[Runtime] finish=%s native_tool_calls=%s blocked=%s first_answer=%r first_reasoning=%r first_tool=%r answer_preview=%r",
            state.finish_reason,
            native_tool_calls,
            state.blocked_tool_names,
            state.first_answer_preview,
            state.first_reasoning_preview,
            state.first_tool_call_preview,
            answer_text[:300],
        )
    return RuntimeExecutionResult(state=state, chat_id=chat_id, acc=acc)


def parse_tool_directive_once(request: StandardRequest, state: RuntimeAttemptState) -> RuntimeToolDirective:
    if request.tools and state.answer_text:
        tool_blocks, stop_reason = tool_parser.parse_tool_calls_silent(state.answer_text, request.tools)
        return RuntimeToolDirective(tool_blocks=tool_blocks, stop_reason=stop_reason)

    if state.tool_calls:
        return RuntimeToolDirective(
            tool_blocks=[
                {
                    "type": "tool_use",
                    "id": tool_call["id"],
                    "name": normalize_tool_name(tool_call["name"], request.tool_names),
                    "input": tool_call.get("input", {}),
                }
                for tool_call in state.tool_calls
            ],
            stop_reason="tool_use",
        )

    return RuntimeToolDirective(tool_blocks=[{"type": "text", "text": state.answer_text}], stop_reason="end_turn")


def build_tool_directive(
    request: StandardRequest,
    state: RuntimeAttemptState,
) -> RuntimeToolDirective:
    directive = parse_tool_directive_once(request, state)
    if request.tools:
        preview = [
            {
                "type": block.get("type"),
                "id": block.get("id"),
                "name": block.get("name"),
                "input": block.get("input"),
            }
            for block in directive.tool_blocks[:3]
        ]
        log.info("[Runtime] directive stop_reason=%s blocks=%s", directive.stop_reason, preview)
    return directive


def anthropic_stream_usage_delta(prompt: str, answer_text: str) -> int:
    return len(answer_text) + len(prompt)


def anthropic_stream_stop_reason(request: StandardRequest, state: RuntimeAttemptState, pending_chunks: list[str], directive: RuntimeToolDirective | None = None) -> str:
    if state.tool_calls or any('"type": "tool_use"' in chunk for chunk in pending_chunks):
        return "tool_use"
    resolved_directive = directive or build_tool_directive(request, state)
    return resolved_directive.stop_reason


def finalize_anthropic_stream_success(*, request: StandardRequest, prompt: str, execution: RuntimeExecutionResult, translator, directive: RuntimeToolDirective | None = None) -> AnthropicStreamSuccessResult:
    stop_reason = anthropic_stream_stop_reason(request, execution.state, translator.pending_chunks, directive)
    chunks = translator.finalize(answer_text=execution.state.answer_text, stop_reason=stop_reason)
    return AnthropicStreamSuccessResult(
        chunks=chunks,
        usage_delta=anthropic_stream_usage_delta(prompt, execution.state.answer_text),
    )


async def complete_anthropic_stream_success(*, users_db, token: str, client, prompt: str, request: StandardRequest, execution: RuntimeExecutionResult, translator) -> AnthropicStreamCompletionResult:
    stream_success = finalize_anthropic_stream_success(
        request=request,
        prompt=prompt,
        execution=execution,
        translator=translator,
    )
    users = await users_db.get()
    for u in users:
        if u["id"] == token:
            u["used_tokens"] += stream_success.usage_delta
            break
    await users_db.save(users)
    await cleanup_runtime_resources(client, execution.acc, execution.chat_id)
    return AnthropicStreamCompletionResult(chunks=stream_success.chunks)


def inject_assistant_message(prompt: str, message: str) -> str:
    next_prompt = prompt.rstrip()
    if next_prompt.endswith("Assistant:"):
        return next_prompt[:-len("Assistant:")] + message + "\nAssistant:"
    return next_prompt + "\n\n" + message + "\nAssistant:"


def retryable_usage_delta(prompt: str):
    return lambda execution, _=None: len(execution.state.answer_text) + len(prompt)


def plan_runtime_attempts(request: StandardRequest, *, initial_prompt: str) -> RuntimeAttemptPlan:
    loop = build_retry_loop(request, initial_prompt=initial_prompt)
    return RuntimeAttemptPlan(loop=loop, prompt=loop.prompt)


def build_retry_loop(request: StandardRequest, *, initial_prompt: str) -> RuntimeRetryLoop:
    return RuntimeRetryLoop(
        prompt=initial_prompt,
        max_attempts=settings.MAX_RETRIES + (1 if request.tools else 0),
    )


def evaluate_retry_directive(
    *,
    request: StandardRequest,
    current_prompt: str,
    history_messages: list[dict[str, Any]] | None,
    attempt_index: int,
    max_attempts: int,
    state: RuntimeAttemptState,
    allow_after_visible_output: bool = False,
) -> RuntimeRetryDirective:
    if attempt_index >= max_attempts - 1:
        return RuntimeRetryDirective(retry=False, next_prompt=current_prompt)

    if state.blocked_tool_names and request.tools:
        if request.surface == "openai":
            return RuntimeRetryDirective(retry=False, next_prompt=current_prompt)
        blocked_name = normalize_tool_name_for_retry(state.blocked_tool_names[0], request.tool_names)
        force_text = (
            f"[MANDATORY NEXT STEP]: The server blocked tool '{blocked_name}' with 'Tool {blocked_name} does not exists.'. "
            f"Retry immediately using ONLY this exact wrapper and no prose before or after it:\n"
            f"<tool_call>{{\"name\": {json.dumps(blocked_name)}, \"input\": {{...your args here...}}}}</tool_call>"
        )
        if state.emitted_visible_output and not allow_after_visible_output:
            next_prompt = inject_assistant_message(current_prompt, force_text)
            return RuntimeRetryDirective(retry=True, next_prompt=next_prompt)
        return RuntimeRetryDirective(
            retry=True,
            next_prompt=tool_parser.inject_format_reminder(current_prompt, blocked_name),
        )

    if request.tools and state.answer_text:
        directive = parse_tool_directive_once(request, state)
        if directive.stop_reason == "tool_use":
            first_tool = next((b for b in directive.tool_blocks if b.get("type") == "tool_use"), None)
            if (
                first_tool
                and first_tool.get("name") == "Read"
                and has_recent_unchanged_read_result(history_messages)
                and not state.emitted_visible_output
            ):
                force_text = (
                    "[MANDATORY NEXT STEP]: You just received 'Unchanged since last read'. "
                    "Do NOT call Read again on the same target. "
                    "Choose another tool now."
                )
                next_prompt = current_prompt.rstrip()
                if next_prompt.endswith("Assistant:"):
                    next_prompt = next_prompt[:-len("Assistant:")] + force_text + "\nAssistant:"
                else:
                    next_prompt += "\n\n" + force_text + "\nAssistant:"
                return RuntimeRetryDirective(retry=True, next_prompt=next_prompt)

            if (
                first_tool
                and first_tool.get("name") == "WebSearch"
                and has_recent_search_no_results(history_messages)
                and not state.emitted_visible_output
            ):
                force_text = (
                    "[MANDATORY NEXT STEP]: The last WebSearch returned no results. "
                    "Do NOT call WebSearch again with similar wording. "
                    "Use another tool or finish with the best available answer."
                )
                next_prompt = current_prompt.rstrip()
                if next_prompt.endswith("Assistant:"):
                    next_prompt = next_prompt[:-len("Assistant:")] + force_text + "\nAssistant:"
                else:
                    next_prompt += "\n\n" + force_text + "\nAssistant:"
                return RuntimeRetryDirective(retry=True, next_prompt=next_prompt)

    return RuntimeRetryDirective(retry=False, next_prompt=current_prompt)


async def cleanup_runtime_resources(client, acc, chat_id: str | None) -> None:
    if acc is None:
        return
    client.account_pool.release(acc)
    if chat_id:
        await client.delete_chat(acc.token, chat_id)
