from __future__ import annotations

import copy
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from backend.adapter.standard_request import StandardRequest
from backend.runtime.execution import build_tool_directive

log = logging.getLogger("qwen2api.responses")
TOOL_ARGUMENT_CHUNK_SIZE = 128


@dataclass(slots=True)
class PreparedResponsesRequest:
    transformed_payload: dict[str, Any]
    current_messages: list[dict[str, Any]]
    combined_messages: list[dict[str, Any]]
    previous_response_id: str | None


def normalize_response_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return content

    normalized: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            normalized.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in {"input_text", "output_text", "text"}:
            normalized.append({"type": "text", "text": part.get("text", "")})
        elif part_type == "input_file":
            normalized.append(
                {
                    "type": "input_file",
                    "file_id": part.get("file_id", ""),
                    "filename": part.get("filename", ""),
                    "mime_type": part.get("mime_type", ""),
                }
            )
        elif part_type == "input_image":
            normalized.append(
                {
                    "type": "input_image",
                    "file_id": part.get("file_id", ""),
                    "image_url": part.get("image_url"),
                    "mime_type": part.get("mime_type", ""),
                }
            )
    return normalized


def is_textual_tool_wrapper(content: Any) -> bool:
    normalized = normalize_response_content(content)
    if isinstance(normalized, str):
        text = normalized
    elif isinstance(normalized, list):
        text = "\n".join(
            part.get("text", "")
            for part in normalized
            if isinstance(part, dict) and part.get("type") == "text"
        )
    else:
        return False
    stripped = text.strip()
    return stripped.startswith("##TOOL_CALL##") and (
        "##END_CALL##" in stripped or stripped.endswith("##END")
    )


def response_input_item_to_messages(item: Any) -> list[dict[str, Any]]:
    if isinstance(item, str):
        return [{"role": "user", "content": item}]
    if not isinstance(item, dict):
        return []

    item_type = item.get("type")
    if item_type == "function_call_output":
        output = item.get("output", "")
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False)
        return [
            {
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "content": output,
            }
        ]

    if item_type == "function_call":
        arguments = item.get("arguments", "{}")
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, ensure_ascii=False)
        return [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": item.get("call_id")
                        or item.get("id")
                        or f"call_{uuid.uuid4().hex[:12]}",
                        "type": "function",
                        "function": {
                            "name": item.get("name", ""),
                            "arguments": arguments or "{}",
                        },
                    }
                ],
            }
        ]

    if item_type in {"input_text", "output_text", "text"}:
        role = item.get("role", "user")
        text = item.get("text", "")
        return [{"role": role, "content": text}]

    if item_type == "message" or "role" in item:
        role = item.get("role", "user")
        content = normalize_response_content(item.get("content", item.get("text", "")))
        if role == "assistant" and is_textual_tool_wrapper(content):
            log.info("[Responses] skipping assistant textual tool wrapper echo from input history")
            return []
        message: dict[str, Any] = {"role": role, "content": content}
        if role == "assistant" and item.get("tool_calls"):
            message["tool_calls"] = item["tool_calls"]
        return [message]

    return []


def coerce_input_to_messages(input_value: Any) -> list[dict[str, Any]]:
    if input_value is None:
        return []
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]
    if isinstance(input_value, dict):
        return response_input_item_to_messages(input_value)

    messages: list[dict[str, Any]] = []
    if isinstance(input_value, list):
        for item in input_value:
            messages.extend(response_input_item_to_messages(item))
    return messages


def prepend_instructions(messages: list[dict[str, Any]], instructions: Any) -> list[dict[str, Any]]:
    if not instructions:
        return messages
    if isinstance(instructions, list):
        text = "\n".join(
            part.get("text", "")
            for part in instructions
            if isinstance(part, dict) and part.get("type") in {"input_text", "output_text", "text"}
        )
    else:
        text = str(instructions)
    if not text.strip():
        return messages
    return [{"role": "system", "content": text}] + messages


def sse_event(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def sse_chunk_to_payload(chunk: str) -> dict[str, Any]:
    body = chunk.strip()
    if body.startswith("data: "):
        body = body[6:]
    return json.loads(body)


class ResponsesStreamTranslator:
    def __init__(self, *, response_id: str, created: int, model_name: str) -> None:
        self.response_id = response_id
        self.created = created
        self.model_name = model_name
        self.sequence_number = 0
        self.pending_chunks: list[str] = []
        self.text_item_id = f"msg_{uuid.uuid4().hex[:24]}"
        self.text_item_started = False
        self.tool_item_ids: set[str] = set()

    def _next_sequence(self) -> int:
        self.sequence_number += 1
        return self.sequence_number

    def start(self) -> None:
        response = {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created,
            "status": "in_progress",
            "model": self.model_name,
            "output": [],
            "parallel_tool_calls": False,
        }
        self.pending_chunks.append(
            sse_event(
                {
                    "type": "response.created",
                    "sequence_number": self._next_sequence(),
                    "response": response,
                }
            )
        )
        self.pending_chunks.append(
            sse_event(
                {
                    "type": "response.in_progress",
                    "sequence_number": self._next_sequence(),
                    "response": response,
                }
            )
        )

    def on_text_delta(self, delta: str) -> None:
        if not delta:
            return
        if not self.text_item_started:
            item = {
                "id": self.text_item_id,
                "type": "message",
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            }
            self.pending_chunks.append(
                sse_event(
                    {
                        "type": "response.output_item.added",
                        "sequence_number": self._next_sequence(),
                        "output_index": 0,
                        "item": item,
                    }
                )
            )
            self.pending_chunks.append(
                sse_event(
                    {
                        "type": "response.content_part.added",
                        "sequence_number": self._next_sequence(),
                        "output_index": 0,
                        "content_index": 0,
                        "item_id": self.text_item_id,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    }
                )
            )
            self.text_item_started = True

        self.pending_chunks.append(
            sse_event(
                {
                    "type": "response.output_text.delta",
                    "sequence_number": self._next_sequence(),
                    "item_id": self.text_item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": delta,
                }
            )
        )

    def finalize(
        self,
        *,
        response_payload: dict[str, Any],
        standard_request: StandardRequest,
        execution: Any,
    ) -> list[str]:
        chunks = list(self.pending_chunks)
        answer_text = execution.state.answer_text or ""
        directive = build_tool_directive(standard_request, execution.state)

        if directive.stop_reason == "tool_use":
            output_index = 0
            if answer_text:
                if not self.text_item_started:
                    item = {
                        "id": self.text_item_id,
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": answer_text,
                                "annotations": [],
                            }
                        ],
                    }
                    chunks.append(
                        sse_event(
                            {
                                "type": "response.output_item.added",
                                "sequence_number": self._next_sequence(),
                                "output_index": output_index,
                                "item": item,
                            }
                        )
                    )
                chunks.append(
                    sse_event(
                        {
                            "type": "response.output_text.done",
                            "sequence_number": self._next_sequence(),
                            "item_id": self.text_item_id,
                            "output_index": output_index,
                            "content_index": 0,
                            "text": answer_text,
                        }
                    )
                )
                chunks.append(
                    sse_event(
                        {
                            "type": "response.content_part.done",
                            "sequence_number": self._next_sequence(),
                            "item_id": self.text_item_id,
                            "output_index": output_index,
                            "content_index": 0,
                            "part": {
                                "type": "output_text",
                                "text": answer_text,
                                "annotations": [],
                            },
                        }
                    )
                )
                chunks.append(
                    sse_event(
                        {
                            "type": "response.output_item.done",
                            "sequence_number": self._next_sequence(),
                            "output_index": output_index,
                            "item": response_payload["output"][0],
                        }
                    )
                )
                output_index += 1

            tool_items = [item for item in response_payload["output"] if item.get("type") == "function_call"]
            for tool_item in tool_items:
                arguments = str(tool_item.get("arguments", "") or "")
                chunks.append(
                    sse_event(
                        {
                            "type": "response.output_item.added",
                            "sequence_number": self._next_sequence(),
                            "output_index": output_index,
                            "item": tool_item,
                        }
                    )
                )
                chunks.append(
                    sse_event(
                        {
                            "type": "response.function_call_arguments.delta",
                            "sequence_number": self._next_sequence(),
                            "item_id": tool_item["id"],
                            "output_index": output_index,
                            "delta": "",
                        }
                    )
                )
                for start in range(0, len(arguments), TOOL_ARGUMENT_CHUNK_SIZE):
                    chunks.append(
                        sse_event(
                            {
                                "type": "response.function_call_arguments.delta",
                                "sequence_number": self._next_sequence(),
                                "item_id": tool_item["id"],
                                "output_index": output_index,
                                "delta": arguments[start:start + TOOL_ARGUMENT_CHUNK_SIZE],
                            }
                        )
                    )
                chunks.append(
                    sse_event(
                        {
                            "type": "response.function_call_arguments.done",
                            "sequence_number": self._next_sequence(),
                            "item_id": tool_item["id"],
                            "output_index": output_index,
                            "arguments": arguments,
                        }
                    )
                )
                chunks.append(
                    sse_event(
                        {
                            "type": "response.output_item.done",
                            "sequence_number": self._next_sequence(),
                            "output_index": output_index,
                            "item": tool_item,
                        }
                    )
                )
                output_index += 1
        else:
            if not self.text_item_started:
                item = response_payload["output"][0]
                chunks.append(
                    sse_event(
                        {
                            "type": "response.output_item.added",
                            "sequence_number": self._next_sequence(),
                            "output_index": 0,
                            "item": item,
                        }
                    )
                )
                chunks.append(
                    sse_event(
                        {
                            "type": "response.content_part.added",
                            "sequence_number": self._next_sequence(),
                            "output_index": 0,
                            "content_index": 0,
                            "item_id": item["id"],
                            "part": item["content"][0],
                        }
                    )
                )
                self.text_item_id = item["id"]
            chunks.append(
                sse_event(
                    {
                        "type": "response.output_text.done",
                        "sequence_number": self._next_sequence(),
                        "item_id": self.text_item_id,
                        "output_index": 0,
                        "content_index": 0,
                        "text": answer_text,
                    }
                )
            )
            chunks.append(
                sse_event(
                    {
                        "type": "response.content_part.done",
                        "sequence_number": self._next_sequence(),
                        "item_id": self.text_item_id,
                        "output_index": 0,
                        "content_index": 0,
                        "part": response_payload["output"][0]["content"][0],
                    }
                )
            )
            chunks.append(
                sse_event(
                    {
                        "type": "response.output_item.done",
                        "sequence_number": self._next_sequence(),
                        "output_index": 0,
                        "item": response_payload["output"][0],
                    }
                )
            )

        chunks.append(
            sse_event(
                {
                    "type": "response.completed",
                    "sequence_number": self._next_sequence(),
                    "response": response_payload,
                }
            )
        )
        return chunks


async def prepare_responses_request(*, response_store, req_data: dict[str, Any]) -> PreparedResponsesRequest:
    previous_response_id = req_data.get("previous_response_id")
    previous_history: list[dict[str, Any]] = []
    if previous_response_id:
        stored = await response_store.get(previous_response_id)
        if stored is None:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "message": f"Response '{previous_response_id}' not found",
                        "type": "invalid_request_error",
                    }
                },
            )
        previous_history = stored.history_messages

    current_messages = coerce_input_to_messages(req_data.get("input"))
    current_messages = prepend_instructions(current_messages, req_data.get("instructions"))
    combined_messages = copy.deepcopy(previous_history) + current_messages

    return PreparedResponsesRequest(
        transformed_payload={
            "model": req_data.get("model", "gpt-4.1"),
            "stream": bool(req_data.get("stream", False)),
            "tools": req_data.get("tools", []),
            "tool_choice": req_data.get("tool_choice"),
            "messages": combined_messages,
        },
        current_messages=current_messages,
        combined_messages=combined_messages,
        previous_response_id=previous_response_id,
    )


__all__ = [
    "PreparedResponsesRequest",
    "ResponsesStreamTranslator",
    "coerce_input_to_messages",
    "is_textual_tool_wrapper",
    "normalize_response_content",
    "prepare_responses_request",
    "prepend_instructions",
    "response_input_item_to_messages",
    "sse_chunk_to_payload",
    "sse_event",
]
