import json
import logging

log = logging.getLogger("qwen2api.sse")


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
        log.info("[SSE-DEBUG] 上游事件: keys=%s preview=%s", list(evt.keys()), str(evt)[:300])
        if evt.get("choices"):
            choices = evt["choices"]
            if choices:
                delta = choices[0].get("delta", {})
                content = delta.get("content", "")
                log.info("[SSE-DEBUG] choices事件: content=%r phase=%s finish=%s",
                         content, delta.get("phase"), choices[0].get("finish_reason"))
            else:
                content = ""
                delta = {}

            # Log if content contains "Tool" and "does not exist"
            if content and "Tool" in content and "does not exist" in content:
                log.warning(f"[SSE] Detected tool interception: content={content!r} phase={delta.get('phase')} status={delta.get('status')} extra={delta.get('extra')}")

            parsed.append(
                {
                    "type": "delta",
                    "phase": delta.get("phase", "answer"),
                    "content": content,
                    "status": delta.get("status", ""),
                    "extra": delta.get("extra", {}),
                }
            )
        else:
            # 非标准格式 - 可能是 Qwen 原生格式
            log.info("[SSE-DEBUG] 非标准格式事件: %s", str(evt)[:300])
            content = evt.get("content", "") or evt.get("text", "") or evt.get("delta", "")
            if isinstance(content, dict):
                content = content.get("content", "") or content.get("text", "") or ""
            phase = evt.get("phase", evt.get("type", "answer"))
            if content:
                log.info("[SSE-DEBUG] 从非标准格式提取内容: content=%r phase=%s", content, phase)
                parsed.append(
                    {
                        "type": "delta",
                        "phase": phase,
                        "content": content,
                        "status": evt.get("status", ""),
                        "extra": evt.get("extra", {}),
                    }
                )
    return parsed
