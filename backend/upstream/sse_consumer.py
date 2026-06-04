import json
import logging

log = logging.getLogger("qwen2api.sse")


def _flatten_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_flatten_text(item) for item in value)
    if isinstance(value, dict):
        if "content" in value:
            return _flatten_text(value.get("content"))
        if "text" in value:
            return _flatten_text(value.get("text"))
        return "".join(_flatten_text(item) for item in value.values())
    return ""


def _first_text(*values) -> str:
    for value in values:
        text = _flatten_text(value)
        if text:
            return text
    return ""


def _extract_reasoning(delta: dict) -> tuple[str, bool]:
    extra = delta.get("extra") if isinstance(delta.get("extra"), dict) else {}
    direct_reasoning = _first_text(
        delta.get("reasoning_content"),
        delta.get("reasoning"),
        delta.get("reasoning_text"),
        delta.get("thinking"),
        delta.get("thoughts"),
        extra.get("reasoning_content"),
        extra.get("reasoning"),
        extra.get("reasoning_text"),
        extra.get("thinking"),
        extra.get("thoughts"),
    )
    if direct_reasoning:
        return direct_reasoning, False

    snapshot_reasoning = _first_text(
        extra.get("summary_thought"),
        extra.get("summary_title"),
    )
    if snapshot_reasoning:
        return snapshot_reasoning, True
    return "", False


def parse_sse_chunk(chunk: str) -> list[dict]:
    events = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
            events.append(obj)
        except Exception:
            continue

    parsed = []
    for evt in events:
        if evt.get("choices"):
            delta = evt["choices"][0].get("delta", {})
            phase = delta.get("phase", "answer")
            content = delta.get("content", "")
            reasoning, reasoning_is_snapshot = _extract_reasoning(delta)
            if reasoning:
                content = reasoning
                phase = "thinking_summary" if phase == "answer" else phase

            # Log if content contains "Tool" and "does not exist"
            if content and "Tool" in content and "does not exist" in content:
                log.warning(f"[SSE] Detected tool interception: content={content!r} phase={delta.get('phase')} status={delta.get('status')} extra={delta.get('extra')}")

            parsed.append(
                {
                    "type": "delta",
                    "phase": phase,
                    "content": content,
                    "status": delta.get("status", ""),
                    "extra": delta.get("extra", {}),
                    "content_is_snapshot": reasoning_is_snapshot,
                }
            )
    return parsed
