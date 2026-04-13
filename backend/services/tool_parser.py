import json
import logging
import uuid
import re

log = logging.getLogger("qwen2api.tool_parser")


def _find_tool_use_json(text: str, tool_names: set):
    """Find a tool_use JSON object in text. First tries exact name match, then any tool_use."""
    candidates = []
    i = 0
    while i < len(text):
        pos = text.find('{', i)
        if pos == -1:
            break
        depth = 0
        for j in range(pos, len(text)):
            if text[j] == '{':
                depth += 1
            elif text[j] == '}':
                depth -= 1
                if depth == 0:
                    candidate = text[pos:j + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict) and obj.get("type") == "tool_use" and obj.get("name"):
                            candidates.append((pos, obj))
                    except (json.JSONDecodeError, ValueError):
                        pass
                    break
        i = pos + 1

    if not candidates:
        return None

    for pos, obj in candidates:
        if obj.get("name") in tool_names:
            return pos, obj

    pos, obj = candidates[0]
    model_name = obj.get("name", "")
    best = resolve_tool_name(model_name, tool_names)
    if best:
        obj = dict(obj)
        obj["name"] = best
    return pos, obj



def resolve_tool_name(name: str, tool_names: set):
    if name in tool_names:
        return name
    if not tool_names:
        return name
    best = next((n for n in tool_names if name.lower() in n.lower() or n.lower() in name.lower()), None)
    return best or next(iter(tool_names))



def parse_tool_input(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return {"raw": value}
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    return {"value": value}



def _stable_tool_identity(name: str, input_data) -> str:
    try:
        serialized = json.dumps(input_data or {}, ensure_ascii=False, sort_keys=True)
    except Exception:
        serialized = str(input_data)
    return f"{name}|{serialized}"


def should_block_tool_call(history_messages: list, tool_name: str, input_data) -> tuple[bool, str]:
    target = _stable_tool_identity(tool_name, input_data)
    same_count = 0
    for msg in reversed(history_messages or []):
        if msg.get("role") != "assistant":
            continue
        for blk in msg.get("content", []):
            if blk.get("type") != "tool_use":
                continue
            identity = _stable_tool_identity(blk.get("name", ""), blk.get("input", {}))
            if identity == target:
                same_count += 1
            else:
                return False, ""
        if same_count >= 2:
            return True, f"重复调用相同工具与参数：{tool_name}"
    return False, ""


def make_tool_block(name: str, input_data, tool_names: set, prefix: str = "", tool_id: str | None = None):
    name = resolve_tool_name(name, tool_names)
    tool_id = tool_id or f"toolu_{uuid.uuid4().hex[:8]}"
    blocks = []
    if prefix:
        blocks.append({"type": "text", "text": prefix})
    blocks.append({"type": "tool_use", "id": tool_id, "name": name, "input": input_data})
    return blocks, "tool_use"



def build_tool_blocks_from_native_chunks(native_tc_chunks: dict, tools: list):
    if not native_tc_chunks or not tools:
        return [], "end_turn"
    tool_names = {t.get("name") for t in tools if t.get("name")}
    blocks = []
    for tc_id, tc in native_tc_chunks.items():
        name = resolve_tool_name(tc.get("name", ""), tool_names)
        args = parse_tool_input(tc.get("args", ""))
        blocks.append({
            "type": "tool_use",
            "id": tc_id or f"toolu_{uuid.uuid4().hex[:8]}",
            "name": name,
            "input": args,
        })
    if not blocks:
        return [], "end_turn"
    return blocks, "tool_use"



def parse_tool_calls(answer: str, tools: list):
    if not tools:
        return [{"type": "text", "text": answer}], "end_turn"
    tool_names = {t.get("name") for t in tools if t.get("name")}
    log.debug(f"[ToolParse] 原始回复({len(answer)}字): {answer[:200]!r}")

    tc_m = re.search(r'##TOOL_CALL##\s*(.*?)\s*##END_CALL##', answer, re.DOTALL | re.IGNORECASE)
    if tc_m:
        try:
            obj = json.loads(tc_m.group(1))
            name = obj.get("name", "")
            inp = parse_tool_input(obj.get("input", obj.get("args", obj.get("arguments", obj.get("parameters", {})))))
            prefix = answer[:tc_m.start()].strip()
            log.info(f"[ToolParse] ✓ ##TOOL_CALL## 格式: name={name!r}, input={str(inp)[:120]}")
            return make_tool_block(name, inp, tool_names, prefix)
        except (json.JSONDecodeError, ValueError) as e:
            log.warning(f"[ToolParse] ##TOOL_CALL## 格式解析失败: {e}, content={tc_m.group(1)[:100]!r}")

    xml_m = re.search(r'<tool_call>\s*(.*?)\s*</tool_call>', answer, re.DOTALL | re.IGNORECASE)
    if xml_m:
        try:
            obj = json.loads(xml_m.group(1))
            name = obj.get("name", "")
            inp = parse_tool_input(obj.get("input", obj.get("args", obj.get("arguments", obj.get("parameters", {})))))
            prefix = answer[:xml_m.start()].strip()
            log.info(f"[ToolParse] ✓ XML格式 <tool_call>: name={name!r}, input={str(inp)[:120]}")
            return make_tool_block(name, inp, tool_names, prefix)
        except (json.JSONDecodeError, ValueError) as e:
            log.warning(f"[ToolParse] XML格式解析失败: {e}, content={xml_m.group(1)[:100]!r}")

    cb_m = re.search(r'```tool_call\s*\n(.*?)\n```', answer, re.DOTALL)
    if cb_m:
        try:
            obj = json.loads(cb_m.group(1).strip())
            name = obj.get("name", "")
            inp = parse_tool_input(obj.get("input", obj.get("args", {})))
            prefix = answer[:cb_m.start()].strip()
            log.info(f"[ToolParse] ✓ 代码块格式 tool_call: name={name!r}, input={str(inp)[:120]}")
            return make_tool_block(name, inp, tool_names, prefix)
        except (json.JSONDecodeError, ValueError) as e:
            log.warning(f"[ToolParse] 代码块格式解析失败: {e}")

    try:
        stripped_tmp = re.sub(r'```(?:json)?\s*\n?', '', answer)
        stripped_tmp = re.sub(r'\n?```', '', stripped_tmp).strip()
        if stripped_tmp.startswith('{') and '"name"' in stripped_tmp:
            obj = json.loads(stripped_tmp)
            if "name" in obj and "type" not in obj:
                name = obj.get("name", "")
                args = parse_tool_input(obj.get("arguments", obj.get("input", obj.get("parameters", {}))))
                if name in tool_names or tool_names:
                    log.info(f"[ToolParse] ✓ Qwen原生格式: name={name!r}, args={str(args)[:120]}")
                    return make_tool_block(name, args, tool_names)
    except (json.JSONDecodeError, ValueError):
        pass

    stripped = re.sub(r'```json\s*\n?', '', answer)
    stripped = re.sub(r'\n?```', '', stripped)
    result = _find_tool_use_json(stripped, tool_names)
    if result:
        pos, tool_call = result
        prefix = stripped[:pos].strip()
        tool_id = tool_call.get("id") or f"toolu_{uuid.uuid4().hex[:8]}"
        log.info(f"[ToolParse] ✓ 旧JSON格式 tool_call: name={tool_call['name']!r}")
        blocks = []
        if prefix:
            blocks.append({"type": "text", "text": prefix})
        blocks.append({
            "type": "tool_use",
            "id": tool_id,
            "name": tool_call["name"],
            "input": tool_call.get("input", {}),
        })
        return blocks, "tool_use"

    log.warning(f"[ToolParse] ✗ 未检测到工具调用，作为普通文本返回。工具列表: {tool_names}")
    return [{"type": "text", "text": answer}], "end_turn"



def inject_format_reminder(prompt: str, tool_name: str) -> str:
    """Inject a format correction reminder into the prompt before the final 'Assistant:' tag.
    Used when Qwen server returns 'Tool X does not exists.' (native call was intercepted)."""
    reminder = (
        f"[CORRECTION]: You called '{tool_name}' using the WRONG format — "
        f"the server BLOCKED it with 'Tool {tool_name} does not exists.'. "
        f"You MUST use ##TOOL_CALL## format and NOTHING ELSE:\n"
        f"##TOOL_CALL##\n"
        f'{{\"name\": \"{tool_name}\", \"input\": {{...your args here...}}}}\n'
        f"##END_CALL##\n"
        f"DO NOT use JSON without delimiters. DO NOT use any XML tags. ONLY ##TOOL_CALL##.\n"
    )
    prompt = prompt.rstrip()
    if prompt.endswith("Assistant:"):
        return prompt[:-len("Assistant:")] + reminder + "\nAssistant:"
    return prompt + "\n\n" + reminder + "\nAssistant:"
