# `##TOOL_CALL##` 标记会留在 Qwen 服务端吗？

## 1. 简短回答

**会，而且会越积越多。**

`##TOOL_CALL##` 标记作为普通文本永久存储在 Qwen 服务端的对话历史中。项目没有主动清理机制，只通过话题隔离和 Chat ID 过期来间接缓解。

---

## 2. 技术原理

### 2.1 qwen2API 如何与 Qwen 通信

qwen2API 使用的是 chat.qwen.ai 的**网页对话接口**，通过 `chat_id` 复用已有会话。关键配置在 `backend/upstream/payload_builder.py`：

```python
feature_config = {
    "thinking_enabled": True,
    "output_schema": "phase",
    "research_mode": "normal",
    "auto_thinking": True,
    "thinking_mode": "Auto",
    "thinking_format": "summary",
    "auto_search": False,
    "code_interpreter": False,
    "plugins_enabled": False,
    # 关键：关闭原生函数调用
    "function_calling": False,
    "enable_tools": False,
    "tool_choice": "none",
}
```

**这意味着 Qwen 服务端完全不知道工具调用这回事**——它只看到一段包含 `##TOOL_CALL##` 标记的普通文本对话。

### 2.2 历史对话的组装方式

每次多轮对话时，`backend/services/prompt_builder.py` 的 `_extract_text()` 函数会将历史消息组装进 prompt：

```python
# prompt_builder.py:329-338
elif t == "tool_use":
    # 历史工具调用 → 渲染为 ##TOOL_CALL## 格式发给 Qwen
    other_parts.append(_render_history_tool_call(part.get("name", ""), part.get("input", {}), client_profile))
    # 输出: ##TOOL_CALL## {"name":"fs_open_file","input":{...}} ##END_CALL##

elif t == "tool_result":
    # 工具结果 → 包装成 [Tool Result] 发给 Qwen
    inner = part.get("content", "")
    tid = part.get("tool_use_id", "")
    other_parts.append(f"[Tool Result for call {tid}]\n{_compact_tool_result_body(inner)}\n[/Tool Result]")
```

### 2.3 Qwen 服务端存储的内容

Qwen 服务端存储的对话历史会包含：

```
User: 帮我读取 /tmp/test.py 的内容

Assistant: 我来帮你读取文件。
##TOOL_CALL## {"name":"fs_open_file","input":{"file_path":"/tmp/test.py"}} ##END_CALL##

User: [Tool Result for call toolu_abc123]
文件内容如下：
print("hello world")
[/Tool Result]

Assistant: 文件已读取成功，内容是 print("hello world")
```

**`##TOOL_CALL##` 和 `[Tool Result]` 标记会永久留在 Qwen 的对话历史中。**

---

## 3. 积累效应

### 3.1 每次工具调用产生的垃圾文本

| 标记类型 | 单次大小 | 说明 |
|----------|----------|------|
| `##TOOL_CALL##...##END_CALL##` | ~200-500 bytes | 工具调用请求 |
| `[Tool Result]...[/Tool Result]` | ~500-2000 bytes | 工具执行结果 |
| **单次工具调用合计** | **~700-2500 bytes** | 双向标记 |

### 3.2 典型任务的积累量

一个典型的 Claude Code 任务可能调用 20+ 个工具：

| 工具调用次数 | 标记积累量 | 影响 |
|-------------|-----------|------|
| 5 次 | ~3.5-12.5 KB | 轻微 |
| 20 次 | ~14-50 KB | 中等 |
| 50 次 | ~35-125 KB | 严重 |
| 100 次 | ~70-250 KB | 极严重 |

### 3.3 对模型输出的影响

Qwen 的对话历史越长，模型需要处理的上下文越多。积累的 `##TOOL_CALL##` 标记会：

1. **消耗 token 预算**：每次请求的 prompt 中包含大量历史标记
2. **干扰模型判断**：模型可能尝试"理解"这些标记的含义
3. **增加截断风险**：历史过长时，早期内容会被截断
4. **降低输出质量**：模型可能模仿历史中的标记格式

---

## 4. 项目的缓解措施

### 4.1 已有缓解

| 缓解措施 | 文件 | 机制 | 效果 |
|----------|------|------|------|
| **话题隔离** | `services/topic_isolation.py` | 检测到新任务时丢弃旧历史（Jaccard 相似度 < 0.1） | 间接清除标记 |
| **历史参数压缩** | `services/prompt_builder.py:40-66` | 工具参数中的长文本截断为 `[N chars]`，长路径截断为 `.../最后两级` | 减少标记体积 |
| **拒绝清洗** | `services/refusal_cleaner.py` | 替换历史中的"Tool X does not exists"为占位工具调用 | 减少干扰 |
| **Chat ID 10 分钟过期** | `services/chat_id_pool.py` | 过期后新建会话，历史清零 | 定期重置 |
| **历史工具调用压缩** | `services/prompt_builder.py:69-77` | `_compact_history_tool_input()` 压缩历史参数 | 减少体积 |

### 4.2 没有做的（潜在问题）

| 缺失功能 | 影响 | 实现难度 |
|----------|------|----------|
| **主动清理历史中的 `##TOOL_CALL##`** | 标记持续积累 | 需要 Qwen API 支持历史编辑 |
| **定期重置 chat_id** | 同一会话中标记越积越多 | 已有 Chat ID 过期机制，但 TTL 较长 |
| **标记混淆/加密** | 固定标记容易被检测 | 需要模型配合输出 |
| **`[Tool Result]` 标记清理** | 同样是代理特征 | 同上 |

---

## 5. 实际影响评估

### 5.1 对 Qwen 官方检测的影响

**这是最严重的问题。**

Qwen 官方只需在服务端日志中搜索 `##TOOL_CALL##` 字符串，即可**100% 确定**该会话来自代理：

```python
# 伪代码：Qwen 服务端的检测逻辑
if "##TOOL_CALL##" in conversation_history:
    mark_as_proxy(account_id)  # 确定性检测，零误报
```

**检测率**：接近 100%
**误报率**：接近 0%

### 5.2 对模型输出的影响

| 影响 | 严重程度 | 说明 |
|------|----------|------|
| Token 消耗 | 🟡 中 | 每轮对话额外消耗 14KB+ 的 prompt token |
| 模型混淆 | 🟡 中 | 模型可能尝试模仿历史中的标记格式 |
| 截断风险 | 🟡 中 | 历史过长时早期内容被截断 |
| 输出质量 | 🟠 中高 | 模型可能看到 `[Tool Result]` 后产生幻觉 |

### 5.3 对账号安全的影响

| 影响 | 严重程度 | 说明 |
|------|----------|------|
| 账号封禁 | 🔴 高 | 历史中的标记是确定性证据 |
| 限流 | 🟡 中 | 如果官方做行为分析 |
| 会话劫持 | 🟢 低 | 标记本身不含敏感信息 |

---

## 6. 代码证据

### 6.1 工具调用渲染（发送给 Qwen）

```python
# backend/services/prompt_builder.py:69-77
def _render_history_tool_call(name: str, input_data: dict, client_profile: str) -> str:
    # 出站混淆：把工具名替换成 Qwen-safe 别名
    payload = json.dumps({
        "name": to_qwen_name(name),  # Read → fs_open_file
        "input": _compact_history_tool_input(name, input_data, client_profile)
    }, ensure_ascii=False)
    # 所有 profile 都使用 ##TOOL_CALL## 格式
    return f"##TOOL_CALL##\n{payload}\n##END_CALL##"
```

### 6.2 工具结果渲染（发送给 Qwen）

```python
# backend/services/prompt_builder.py:331-338
elif t == "tool_result":
    inner = part.get("content", "")
    tid = part.get("tool_use_id", "")
    if isinstance(inner, str):
        other_parts.append(
            f"[Tool Result for call {tid}]\n"
            f"{_compact_tool_result_body(inner)}\n"
            f"[/Tool Result]"
        )
```

### 6.3 原生函数调用关闭

```python
# backend/upstream/payload_builder.py:32-36
"function_calling": False,    # 关闭原生函数调用
"enable_tools": False,        # 禁用 Qwen 内置工具
"tool_choice": "none",        # 不选择任何工具
```

### 6.4 日志确认

```python
# backend/upstream/qwen_executor.py:121-124
if "##TOOL_CALL##" in prompt_content:
    log.info(f"[上游] prompt 包含 ##TOOL_CALL## 标记（正常）")
else:
    log.warning(f"[上游] prompt 缺少 ##TOOL_CALL## 标记 — 可能导致上游拦截")
```

代码注释 `（正常）` 表明开发者**明确知道** `##TOOL_CALL##` 会出现在发送给 Qwen 的 prompt 中，且认为这是正常行为。

---

## 7. 总结

### 7.1 核心结论

| 问题 | 回答 |
|------|------|
| `##TOOL_CALL##` 会留在 Qwen 服务端吗？ | **会**，作为普通文本永久存储 |
| 会被清理吗？ | **不会**，项目没有主动清理机制 |
| 会越积越多吗？ | **会**，每次工具调用增加 ~700-2500 bytes |
| 是检测特征吗？ | **是**，100% 确定的代理证据 |
| 对模型有影响吗？ | **有**，消耗 token 预算，可能干扰输出 |

### 7.2 改进建议

1. **动态标记**：不使用固定的 `##TOOL_CALL##`，每次随机生成标记对
2. **缩短 Chat ID TTL**：从 10 分钟缩短到 2-3 分钟
3. **主动重置**：每 N 次工具调用后主动创建新 chat_id
4. **标记混淆**：将 `##TOOL_CALL##` 拆分为多个不明显的片段
5. **监控积累量**：在 prompt 中统计标记占比，超过阈值时重置会话

---

*分析时间：2026-05-21*
*基于项目 v2.0.0 版本分析*
