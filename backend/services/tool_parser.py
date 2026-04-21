import json
import logging
import re
import uuid
from typing import Any, cast

from backend.adapter.standard_request import CLAUDE_CODE_OPENAI_PROFILE, OPENCLAW_OPENAI_PROFILE
from backend.core.request_logging import get_request_context
from backend.toolcall.formats_json import load_json_with_repair
from backend.toolcall.normalize import build_tool_name_registry, normalize_tool_name
from backend.toolcall.parser import parse_tool_calls_detailed

__all__ = ["parse_tool_calls", "parse_tool_calls_detailed", "inject_format_reminder", "parse_tool_calls_silent", "ToolSieve"]

log = logging.getLogger("qwen2api.tool_parser")


CASE_SENSITIVE_TOOL_NAMES = {"Bash", "Edit", "Write", "Read", "Grep", "Glob", "WebFetch", "WebSearch"}


def _tool_name(tool: dict[str, Any]) -> str:
    if not isinstance(tool, dict):
        return ""
    if isinstance(tool.get("name"), str) and tool.get("name", "").strip():
        return tool.get("name", "").strip()
    function_block = tool.get("function", {})
    if isinstance(function_block, dict):
        return str(function_block.get("name", "")).strip()
    return ""


def _find_tool_definition(name: str, tools: list[dict[str, Any]]) -> dict[str, Any] | None:
    normalized = str(name or "").strip().lower()
    for tool in tools or []:
        candidate = _tool_name(tool).lower()
        if candidate == normalized:
            return tool
    return None


def _tool_properties(tool: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(tool, dict):
        return {}
    params = tool.get("parameters", {}) or {}
    if not params and isinstance(tool.get("function"), dict):
        params = tool["function"].get("parameters", {}) or {}
    if not isinstance(params, dict):
        return {}
    props = params.get("properties", {}) or {}
    return props if isinstance(props, dict) else {}


def _pick_declared_key(props: dict[str, Any], aliases: list[str], fallback: str) -> str:
    if not props:
        return fallback

    alias_lookup = {re.sub(r"[^a-z0-9]+", "", alias.lower()): alias for alias in aliases}
    for declared_key in props:
        normalized_declared = re.sub(r"[^a-z0-9]+", "", declared_key.lower())
        if normalized_declared in alias_lookup:
            return declared_key
    return fallback


def _move_alias_value(payload: dict[str, Any], target_key: str, aliases: list[str]) -> None:
    if target_key in payload and str(payload.get(target_key, "")).strip():
        return
    for alias in aliases:
        if alias == target_key:
            continue
        value = payload.get(alias)
        if isinstance(value, str) and value.strip():
            payload[target_key] = payload.pop(alias).strip()
            return


def _set_default_string(payload: dict[str, Any], target_key: str, value: str) -> None:
    if target_key not in payload or not str(payload.get(target_key, "")).strip():
        payload[target_key] = value


def _first_non_empty_string(payload: dict[str, Any], aliases: list[str]) -> str:
    for alias in aliases:
        value = payload.get(alias)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _normalize_tool_name_case(name: str, tool_names: set[str]) -> str:
    if not isinstance(name, str) or not name:
        return name
    if name in tool_names:
        return name
    lowered = name.lower()
    for candidate in tool_names:
        if candidate.lower() == lowered:
            if candidate in CASE_SENSITIVE_TOOL_NAMES:
                return candidate
            return candidate
    return name


def _find_tool_use_json(text: str, tool_names: set[str]):
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
                            normalized_name = normalize_tool_name(obj.get("name", ""), tool_names)
                            if normalized_name in tool_names:
                                obj = dict(obj)
                                obj["name"] = normalized_name
                                return pos, obj

                    except (json.JSONDecodeError, ValueError):
                        pass
                    break
        i = pos + 1

    return None


def _extract_first_xml_tool_call(text: str) -> str | None:
    wrapped_match = re.search(r"<tool_calls>\s*(<tool_call>[\s\S]*?</tool_call>)\s*</tool_calls>", text, re.IGNORECASE)
    if wrapped_match:
        return wrapped_match.group(1)

    tool_call_match = re.search(r"<tool_call>\s*(\{[\s\S]*?\}|[\s\S]*?)\s*</tool_call>", text, re.IGNORECASE)
    if tool_call_match:
        return tool_call_match.group(0)
    return None


def _extract_first_json_tool_call(text: str) -> str | None:
    normalized = text.strip()

    # 优先查找完整的 JSON 对象
    markers = [
        '<tool_call>{"name"',
        '<tool_calls><tool_call>{"name"',
        '{"name"',
        '"name":',
        '"name="',
        'function.name:',
    ]
    start_positions = [normalized.find(marker) for marker in markers if normalized.find(marker) != -1]
    if not start_positions:
        return None
    start = min(start_positions)
    candidate = normalized[start:]

    wrapped_match = re.search(r"<tool_calls>\s*(<tool_call>[\s\S]*?</tool_call>)\s*</tool_calls>", candidate, re.IGNORECASE)
    if wrapped_match:
        return wrapped_match.group(1)

    tool_call_match = re.search(r"<tool_call>\s*(\{[\s\S]*?\}|[\s\S]*?)\s*</tool_call>", candidate, re.IGNORECASE)
    if tool_call_match:
        return tool_call_match.group(0)

    json_start = candidate.find("{")
    if json_start == -1:
        return None
    depth = 0
    for idx in range(json_start, len(candidate)):
        ch = candidate[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                json_str = candidate[json_start:idx + 1]
                # 验证是否是有效的工具调用 JSON
                try:
                    obj = json.loads(json_str)
                    if isinstance(obj, dict) and "name" in obj:
                        return json_str
                except (json.JSONDecodeError, ValueError):
                    pass
                return json_str
    return candidate[json_start:]


def _normalize_fragmented_tool_call(answer: str) -> str:
    text = answer.strip()
    if "##TOOL_CALL##" in text and "##END_CALL##" in text:
        return text

    extracted_tool_call = _extract_first_xml_tool_call(text) or _extract_first_json_tool_call(text)
    if extracted_tool_call:
        return extracted_tool_call

    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?think>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"Tool\s+[A-Za-z0-9_.:-]*\s*does not exists?\\.?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```[\s\S]*?```", "", text)

    extracted_tool_call = _extract_first_xml_tool_call(text) or _extract_first_json_tool_call(text)
    if extracted_tool_call:
        return extracted_tool_call

    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^[•●·\-*]+\s*", "", line)
        line = line.replace("END_CALL##", "##END_CALL##")
        if line:
            lines.append(line)

    normalized = "\n".join(lines)
    if "TOOL_CALL##" in normalized and "##TOOL_CALL##" not in normalized:
        normalized = normalized.replace("TOOL_CALL##", "##TOOL_CALL##")
    if "##END_CALL##" in normalized and "##TOOL_CALL##" not in normalized and '"name"' in normalized:
        normalized = f"##TOOL_CALL##\n{normalized}"
    return normalized


def _iter_hash_tool_call_matches(answer: str) -> list[re.Match[str]]:
    return list(re.finditer(r'##TOOL_CALL##\s*(.*?)\s*##END_CALL##', answer, re.DOTALL | re.IGNORECASE))


def _coerce_ask_user_question_input(input_data: dict[str, Any], _props: dict[str, Any]) -> dict[str, Any]:
    fixed = dict(input_data)

    if "question" in fixed and "questions" not in fixed:
        question_text = fixed.pop("question")
        fixed["questions"] = [{
            "question": question_text,
            "header": "Question",
            "options": [
                {"label": "Yes", "description": "Confirm"},
                {"label": "No", "description": "Decline"}
            ],
            "multiSelect": False
        }]
        log.info("[ToolCoerce] Fixed AskUserQuestion: converted 'question' to 'questions' array")

    if "questions" in fixed:
        if not isinstance(fixed["questions"], list):
            fixed["questions"] = [fixed["questions"]]

        for j, question in enumerate(fixed["questions"]):
            if not isinstance(question, dict):
                continue
            if "question" not in question:
                question["question"] = "Please provide your input"
            if "header" not in question:
                question["header"] = "Question"
            if "multiSelect" not in question:
                question["multiSelect"] = False
            if "options" not in question:
                question["options"] = [
                    {"label": "Continue", "description": "Proceed"},
                    {"label": "Cancel", "description": "Stop"},
                ]
            elif isinstance(question.get("options"), list):
                for i, opt in enumerate(question["options"]):
                    if isinstance(opt, str):
                        question["options"][i] = {"label": opt, "description": opt}
                    elif isinstance(opt, dict):
                        if "label" not in opt:
                            opt["label"] = opt.get("description", f"Option {j + 1}")
                        if "description" not in opt:
                            opt["description"] = opt.get("label", "")
    return fixed


def _coerce_agent_input(input_data: dict[str, Any], _props: dict[str, Any]) -> dict[str, Any]:
    fixed = dict(input_data)
    if "description" not in fixed:
        fixed["description"] = "Execute sub-task"
    if "prompt" not in fixed:
        fixed["prompt"] = fixed.get("description", "Execute the task")
    return fixed


def _coerce_read_input(input_data: dict[str, Any], props: dict[str, Any]) -> dict[str, Any]:
    fixed = dict(input_data)
    target_key = _pick_declared_key(props, ["file_path", "filePath", "path", "filename"], "file_path")
    _move_alias_value(fixed, target_key, ["file_path", "filePath", "path", "filename", "value"])
    return fixed


def _coerce_shell_input(input_data: dict[str, Any], props: dict[str, Any]) -> dict[str, Any]:
    fixed = dict(input_data)
    command_key = _pick_declared_key(props, ["command", "cmd", "script", "value"], "command")
    workdir_key = _pick_declared_key(props, ["workdir", "cwd", "path", "directory"], "workdir")
    description_key = _pick_declared_key(props, ["description", "summary", "purpose"], "description")

    _move_alias_value(fixed, command_key, ["command", "cmd", "script", "value"])
    _move_alias_value(fixed, workdir_key, ["workdir", "cwd", "path", "directory"])

    command_preview = _first_non_empty_string(fixed, [command_key, "command", "cmd", "script", "value"])
    if description_key in props or "description" in fixed:
        _set_default_string(
            fixed,
            description_key,
            command_preview[:120] if command_preview else "Execute shell command",
        )
    return fixed


def _coerce_search_files_input(input_data: dict[str, Any], _props: dict[str, Any]) -> dict[str, Any]:
    fixed = dict(input_data)
    if "path" not in fixed:
        for key in ("directory", "dir", "cwd"):
            if isinstance(fixed.get(key), str) and fixed[key].strip():
                fixed["path"] = fixed.pop(key).strip()
                break

    if "pattern" not in fixed:
        for key in ("glob", "file_glob", "value"):
            if isinstance(fixed.get(key), str) and fixed[key].strip():
                fixed["pattern"] = fixed.pop(key).strip()
                break

    pattern = fixed.get("pattern")
    if isinstance(pattern, str):
        stripped_pattern = pattern.strip()
        if stripped_pattern:
            fixed["pattern"] = stripped_pattern
            if "target" not in fixed and any(ch in stripped_pattern for ch in ("*", "?", "[")):
                fixed["target"] = "files"

    if "pattern" not in fixed:
        fixed["pattern"] = "*"
    return fixed


TOOL_INPUT_COERCERS_BY_NAME = {
    "AskUserQuestion": _coerce_ask_user_question_input,
    "Agent": _coerce_agent_input,
}


TOOL_INPUT_COERCERS_BY_NORMALIZED_NAME = {
    "read": _coerce_read_input,
    "bash": _coerce_shell_input,
    "execcommand": _coerce_shell_input,
    "exec_command": _coerce_shell_input,
    "shellcommand": _coerce_shell_input,
    "shell_command": _coerce_shell_input,
    "runshellcommand": _coerce_shell_input,
    "run_shell_command": _coerce_shell_input,
    "search_files": _coerce_search_files_input,
}


def _coerce_query_aliases(input_data: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any]:
    query_value = input_data.get("query")
    queries = input_data.get("queries")
    if query_value or "queries" not in input_data:
        return input_data
    if not any(isinstance(tool, dict) and isinstance(tool.get("parameters"), dict) and isinstance(tool["parameters"].get("properties"), dict) and "query" in tool["parameters"]["properties"] for tool in tools):
        return input_data

    if isinstance(queries, list):
        merged = "\n".join(str(item).strip() for item in queries if str(item).strip())
        if merged:
            coerced = dict(input_data)
            coerced.pop("queries", None)
            coerced["query"] = merged
            return coerced
    if isinstance(queries, str) and queries.strip():
        coerced = dict(input_data)
        coerced.pop("queries", None)
        coerced["query"] = queries.strip()
        return coerced

    return input_data


def _coerce_tool_input(name: str, input_data: Any, tools: list[dict[str, Any]]) -> Any:
    if not isinstance(input_data, dict):
        return input_data

    tool_def = _find_tool_definition(name, tools)
    props = _tool_properties(tool_def)
    normalized_name = str(name or "").strip().lower()

    fixed = dict(input_data)
    coercer = TOOL_INPUT_COERCERS_BY_NAME.get(name) or TOOL_INPUT_COERCERS_BY_NORMALIZED_NAME.get(normalized_name)
    if coercer is not None:
        fixed = coercer(fixed, props)

    return _coerce_query_aliases(fixed, tools)


def parse_tool_calls(answer: str, tools: list):
    return _parse_tool_calls(answer, tools, emit_logs=True)


def parse_tool_calls_silent(answer: str, tools: list):
    return _parse_tool_calls(answer, tools, emit_logs=False)


def _parse_tool_calls(answer: str, tools: list, *, emit_logs: bool):
    answer = _normalize_fragmented_tool_call(answer)
    ctx = get_request_context()
    req_tag = f"req={ctx.get('req_id', '-')} chat={ctx.get('chat_id', '-')}"
    if not tools:
        return [{"type": "text", "text": answer}], "end_turn"
    tool_names = {_tool_name(t) for t in tools if _tool_name(t)}
    tool_registry = build_tool_name_registry(tool_names)

    def _log_debug(message: str) -> None:
        if emit_logs:
            log.debug(message)

    def _log_info(message: str) -> None:
        if emit_logs:
            log.info(message)

    def _log_warning(message: str) -> None:
        if emit_logs:
            log.warning(message)

    # 强制记录原始输入用于调试
    log.info(f"[ToolParse] [{req_tag}] 原始回复({len(answer)}字): {answer[:500]!r}")

    def _build_tool_use_block(name, input_data):
        normalized_name = normalize_tool_name(name, tool_registry.values())
        cased_name = _normalize_tool_name_case(normalized_name, tool_names)
        if cased_name not in tool_names:
            _log_warning(f"[ToolParse] 工具名不匹配: name={name!r}, normalized={normalized_name!r}, cased={cased_name!r}, tools={tool_names}")
            return None
        coerced_input = _coerce_tool_input(cased_name, input_data, tools)
        tool_id = f"toolu_{uuid.uuid4().hex[:8]}"
        return {"type": "tool_use", "id": tool_id, "name": cased_name, "input": coerced_input}

    def _make_tool_block(name, input_data, prefix=""):
        tool_block = _build_tool_use_block(name, input_data)
        if tool_block is None:
            _log_warning(f"[ToolParse] 工具名不匹配，回退为普通文本: name={name!r}, tools={tool_names}")
            return [{"type": "text", "text": answer}], "end_turn"
        blocks = []
        if prefix:
            blocks.append({"type": "text", "text": prefix})
        blocks.append(tool_block)
        _log_info(f"[ToolParse] 返回工具块: original={name!r}, final={tool_block['name']!r}, input={json.dumps(tool_block['input'], ensure_ascii=False)[:200]}")
        return blocks, "tool_use"

    detailed = parse_tool_calls_detailed(answer, tool_names)
    detailed_calls = cast(list[dict[str, Any]], detailed["calls"])
    if detailed_calls:
        first_call = detailed_calls[0]
        _log_info(f"[ToolParse] ✓ 详细解析格式: source={detailed['source']}, name={first_call['name']!r}, input={json.dumps(first_call['input'], ensure_ascii=False)[:200]}")
        return _make_tool_block(first_call["name"], first_call["input"])

    tc_matches = _iter_hash_tool_call_matches(answer)
    if tc_matches:
        seq_blocks: list[dict[str, Any]] = []
        cursor = 0
        valid_tool_count = 0

        for tc_m in tc_matches:
            prefix = answer[cursor:tc_m.start()].strip()
            if prefix:
                seq_blocks.append({"type": "text", "text": prefix})

            try:
                obj = load_json_with_repair(tc_m.group(1))
                if not isinstance(obj, dict):
                    raise ValueError("tool call payload is not an object")
                name = obj.get("name", "")
                inp = obj.get("input", obj.get("args", obj.get("arguments", obj.get("parameters", {}))))
                if isinstance(inp, str):
                    try:
                        inp = load_json_with_repair(inp)
                    except Exception:
                        inp = {"value": inp}
                tool_block = _build_tool_use_block(name, inp)
                if tool_block is not None:
                    valid_tool_count += 1
                    seq_blocks.append(tool_block)
                    _log_info(f"[ToolParse] ✓ ##TOOL_CALL## 格式: name={name!r}, input={str(inp)[:120]}")
                else:
                    seq_blocks.append({"type": "text", "text": answer[tc_m.start():tc_m.end()].strip()})
            except (json.JSONDecodeError, ValueError) as e:
                _log_warning(f"[ToolParse] ##TOOL_CALL## 格式解析失败: {e}, content={tc_m.group(1)[:100]!r}")
                seq_blocks.append({"type": "text", "text": answer[tc_m.start():tc_m.end()].strip()})

            cursor = tc_m.end()

        suffix = answer[cursor:].strip()
        if suffix:
            seq_blocks.append({"type": "text", "text": suffix})

        if valid_tool_count:
            return seq_blocks, "tool_use"

    xml_m = re.search(r'<tool_call>\s*(.*?)\s*</tool_call>', answer, re.DOTALL | re.IGNORECASE)
    if xml_m:
        try:
            obj = load_json_with_repair(xml_m.group(1))
            if not isinstance(obj, dict):
                raise ValueError("tool call payload is not an object")
            name = obj.get("name", "")
            inp = obj.get("input", obj.get("args", obj.get("arguments", obj.get("parameters", {}))))
            if isinstance(inp, str):
                try:
                    inp = load_json_with_repair(inp)
                except Exception:
                    inp = {"value": inp}
            prefix = answer[:xml_m.start()].strip()
            _log_info(f"[ToolParse] ✓ XML格式 <tool_call>: name={name!r}, input={str(inp)[:120]}")
            return _make_tool_block(name, inp, prefix)
        except (json.JSONDecodeError, ValueError) as e:
            _log_warning(f"[ToolParse] XML格式解析失败: {e}, content={xml_m.group(1)[:100]!r}")

    cb_m = re.search(r'```tool_call\s*\n(.*?)\n```', answer, re.DOTALL)
    if cb_m:
        try:
            obj = load_json_with_repair(cb_m.group(1).strip())
            if not isinstance(obj, dict):
                raise ValueError("tool call payload is not an object")
            name = obj.get("name", "")
            inp = obj.get("input", obj.get("args", {}))
            if isinstance(inp, str):
                try:
                    inp = load_json_with_repair(inp)
                except Exception:
                    inp = {"value": inp}
            prefix = answer[:cb_m.start()].strip()
            _log_info(f"[ToolParse] ✓ 代码块格式 tool_call: name={name!r}, input={str(inp)[:120]}")
            return _make_tool_block(name, inp, prefix)
        except (json.JSONDecodeError, ValueError) as e:
            _log_warning(f"[ToolParse] 代码块格式解析失败: {e}")

    stripped = re.sub(r'```json\s*\n?', '', answer)
    stripped = re.sub(r'\n?```', '', stripped)
    result = _find_tool_use_json(stripped, tool_names)
    if result:
        pos, tool_call = result
        prefix = stripped[:pos].strip()
        tool_id = tool_call.get("id") or f"toolu_{uuid.uuid4().hex[:8]}"
        _log_info(f"[ToolParse] ✓ 旧JSON格式 tool_call: name={tool_call['name']!r}")
        blocks = []
        if prefix:
            blocks.append({"type": "text", "text": prefix})
        blocks.append({
            "type": "tool_use",
            "id": tool_id,
            "name": tool_call["name"],
            "input": _coerce_tool_input(tool_call["name"], tool_call.get("input", {}), tools),
        })
        return blocks, "tool_use"

    # 尝试解析纯 JSON 格式: {"name": "...", "input": {...}}
    stripped_clean = stripped.strip()
    try:
        if stripped_clean.startswith('{') and stripped_clean.endswith('}'):
            obj = load_json_with_repair(stripped_clean)
            if isinstance(obj, dict) and "name" in obj:
                name = obj.get("name", "")
                inp = obj.get("input", obj.get("args", obj.get("arguments", obj.get("parameters", {}))))
                if isinstance(inp, str):
                    try:
                        inp = load_json_with_repair(inp)
                    except Exception:
                        inp = {"value": inp}
                _log_info(f"[ToolParse] ✓ 纯JSON格式: name={name!r}, input={str(inp)[:120]}")
                return _make_tool_block(name, inp)
    except (json.JSONDecodeError, ValueError) as e:
        _log_debug(f"[ToolParse] 纯JSON格式解析失败: {e}, content={stripped_clean[:200]!r}")

    _log_warning(f"[ToolParse] ✗ 未检测到工具调用，作为普通文本返回。工具列表: {tool_names}")
    return [{"type": "text", "text": answer}], "end_turn"


class ToolSieve:
    """工具调用流式检测器 - 实时检测并分离工具调用"""

    def __init__(self, tool_names: list[str]):
        self.tool_names = set(tool_names) if tool_names else set()
        self.pending = ""
        self.capture = ""
        self.capturing = False
        self.pending_tool_calls = []
        self.tool_calls_detected = False

    def process_chunk(self, chunk: str) -> list[dict]:
        """
        处理一个chunk，返回事件列表
        事件类型：
        - {"type": "content", "text": "..."}  # 普通文本
        - {"type": "tool_calls", "calls": [...]}  # 工具调用
        """
        if not chunk:
            return []

        self.pending += chunk
        events = []

        # 如果正在捕获工具调用
        if self.capturing:
            self.capture += self.pending
            self.pending = ""

            # 尝试解析
            prefix, calls, suffix, ready = self._consume_tool_capture()

            if ready and calls:
                # 解析成功
                if prefix:
                    events.append({"type": "content", "text": prefix})

                self.pending_tool_calls = calls
                self.tool_calls_detected = True
                self.pending = suffix
                self.capture = ""
                self.capturing = False

            return events

        # 检测工具调用开始
        start = self._find_tool_start(self.pending)

        if start >= 0:
            # 找到工具调用开始
            prefix = self.pending[:start]
            if prefix:
                events.append({"type": "content", "text": prefix})

            self.capture = self.pending[start:]
            self.pending = ""
            self.capturing = True
            capture_prefix, calls, suffix, ready = self._consume_tool_capture()
            if ready and calls:
                if capture_prefix:
                    events.append({"type": "content", "text": capture_prefix})
                self.pending_tool_calls = calls
                self.tool_calls_detected = True
                self.pending = suffix
                self.capture = ""
                self.capturing = False
                if self.pending_tool_calls:
                    events.append({"type": "tool_calls", "calls": self.pending_tool_calls})
                    self.pending_tool_calls = []
        else:
            # 没找到，输出安全部分
            safe, hold = self._split_safe_content(self.pending)
            if safe:
                events.append({"type": "content", "text": safe})
            self.pending = hold

        return events

    def _find_tool_start(self, text: str) -> int:
        """查找工具调用开始位置"""
        lowered = text.lower()
        markers = [
            '{"name":',
            '<tool_call>',
            '##tool_call##',
            'tool_call##',
            'function.name:',
        ]

        positions = []
        for marker in markers:
            pos = lowered.find(marker)
            if pos >= 0:
                positions.append(pos)

        return min(positions) if positions else -1

    def _consume_tool_capture(self) -> tuple[str, list, str, bool]:
        """尝试解析捕获的工具调用"""
        if not self.capture:
            return "", [], "", False

        # 尝试解析工具调用
        try:
            # 使用现有的解析逻辑
            blocks, stop_reason = parse_tool_calls_silent(self.capture,
                [{"name": name} for name in self.tool_names])

            if stop_reason == "tool_use":
                # 找到工具��用
                tool_blocks = [b for b in blocks if b.get("type") == "tool_use"]
                if tool_blocks:
                    # 转换为标准格式
                    calls = [{
                        "name": tb["name"],
                        "input": tb["input"]
                    } for tb in tool_blocks]

                    # 提取前缀文本
                    text_blocks = [b for b in blocks if b.get("type") == "text"]
                    prefix = text_blocks[0]["text"] if text_blocks else ""

                    return prefix, calls, "", True
        except Exception as e:
            log.debug(f"[ToolSieve] 解析失败: {e}")

        # 还不完整或解析失败
        return "", [], "", False

    def _split_safe_content(self, text: str) -> tuple[str, str]:
        """分离安全内容和需要保留的部分"""
        # 保留最后几个字符，防止工具调用标记被截断
        if len(text) < 20:
            return "", text

        return text[:-10], text[-10:]

    def flush(self) -> list[dict]:
        """刷新剩余内容"""
        events = []

        if self.pending_tool_calls:
            events.append({"type": "tool_calls", "calls": self.pending_tool_calls})
            self.pending_tool_calls = []

        if self.capturing and self.capture:
            # 尝试最后一次解析
            prefix, calls, suffix, ready = self._consume_tool_capture()
            if ready and calls:
                if prefix:
                    events.append({"type": "content", "text": prefix})
                events.append({"type": "tool_calls", "calls": calls})
                self.tool_calls_detected = True
                if suffix:
                    events.append({"type": "content", "text": suffix})
            else:
                # 解析失败，检查是否看起来像工具调用
                if not self._looks_like_incomplete_tool_call(self.capture):
                    events.append({"type": "content", "text": self.capture})

        if self.pending:
            events.append({"type": "content", "text": self.pending})

        return events

    def _looks_like_incomplete_tool_call(self, text: str) -> bool:
        """检查文本是否看起来像不完整的工具调用"""
        lowered = text.lower()
        markers = ['{"name":', '<tool_call>', '##tool_call##', 'tool_call##', 'function.name:']
        return any(marker in lowered for marker in markers)

    def has_tool_calls(self) -> bool:
        """是否检测到工具调用"""
        return self.tool_calls_detected or bool(self.pending_tool_calls)


def inject_format_reminder(prompt: str, tool_name: str, *, client_profile: str = OPENCLAW_OPENAI_PROFILE) -> str:
    """Inject a format correction reminder into the prompt before the final 'Assistant:' tag.
    Used when Qwen server returns 'Tool X does not exists.' (native call was intercepted)."""
    if client_profile == CLAUDE_CODE_OPENAI_PROFILE:
        reminder = (
            f"[严重错误/CRITICAL ERROR]: 你的输出中出现了 'Tool {tool_name} does not exists.' 这说明你试图描述工具调用而不是真正调用它。\n"
            f"The text 'Tool {tool_name} does not exists.' appeared in your output. "
            f"This means you tried to describe a tool call instead of actually calling it.\n\n"
            f"要调用 {tool_name}，只输出这个精确的JSON格式，不要有其他文本：\n"
            f"To call {tool_name}, output ONLY this exact JSON format with NO other text:\n"
            f'{{"name": "{tool_name}", "input": {{"arg1": "value1", "arg2": "value2"}}}}\n\n'
            f"规则/RULES:\n"
            f"- 只输出JSON对象，不要有其他内容 / Output ONLY the JSON object, nothing else\n"
            f"- 前后不要有解释性文字 / NO explanatory text before or after\n"
            f"- 不要用markdown代码块 / NO markdown code blocks\n"
            f"- 不要用XML标签如<tool_call> / NO XML tags like <tool_call>\n"
            f"- 不要用##TOOL_CALL##标记 / NO ##TOOL_CALL## markers\n"
            f"- 只要纯JSON对象 / Just the raw JSON object\n\n"
            f"Read工具示例 / Example for Read tool:\n"
            f'{{"name": "Read", "input": {{"file_path": "/path/to/file"}}}}\n'
        )
    else:
        reminder = (
            f"[纠正/CORRECTION]: 你用错误的格式调用了 '{tool_name}' — "
            f"服务器用 'Tool {tool_name} does not exists.' 阻止了它。\n"
            f"You called '{tool_name}' using the WRONG format — "
            f"the server BLOCKED it with 'Tool {tool_name} does not exists.'. "
            f"你必须使用##TOOL_CALL##格式，不能用其他格式：\n"
            f"You MUST use ##TOOL_CALL## format and NOTHING ELSE:\n"
            f"##TOOL_CALL##\n"
            f'{{"name": {json.dumps(tool_name)}, "input": {{...your args here...}}}}\n'
            f"##END_CALL##\n"
            f"不要用没有分隔符的JSON。不要用任何XML标签。只能用##TOOL_CALL##。\n"
            f"DO NOT use JSON without delimiters. DO NOT use any XML tags. ONLY ##TOOL_CALL##.\n"
        )
    prompt = prompt.rstrip()
    if prompt.endswith("Assistant:"):
        return prompt[: -len("Assistant:")] + reminder + "\nAssistant:"
    return prompt + "\n\n" + reminder + "\nAssistant:"
