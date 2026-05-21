# qwen2API 项目深度分析报告

## 1. 项目概述

**项目名称**：qwen2API Enterprise Gateway  
**版本**：v2.0.0  
**作者**：YuJunZhiXue  
**仓库**：https://github.com/YuJunZhiXue/qwen2API  
**许可证**：MIT License  
**Docker 镜像**：`yujunzhixue/qwen2api:latest`

### 一句话定位

qwen2API 是一个**企业级 AI API 网关**，将**通义千问（chat.qwen.ai）网页版**的对话能力转换为 **OpenAI、Anthropic Claude、Gemini** 三种主流 AI API 兼容接口，使 Claude Code、Cursor、OpenClaw 等客户端工具能够无缝接入通义千问模型。

### 核心价值

| 痛点 | 解决方案 |
|------|----------|
| Claude Code 等工具只能调用 OpenAI/Claude API | 通过协议转换，让这些工具调用通义千问 |
| 通义千问官方 API 功能有限 | 利用网页版完整能力（工具调用、图片生成等） |
| 单账号容易限流封禁 | 多账号池轮询 + 智能限流冷却 |
| 工具调用格式不兼容 | 自动解析和转换多种工具调用协议 |

---

## 2. 技术架构

### 2.1 技术栈

| 层级 | 技术选型 | 说明 |
|------|----------|------|
| **后端框架** | Python FastAPI + Uvicorn | 高性能异步 Web 框架 |
| **前端框架** | React 19 + Vite 6 + Tailwind CSS 4 | 现代化管理台 UI |
| **浏览器自动化** | Camoufox | 基于 Firefox 的反检测浏览器 |
| **HTTP 客户端** | httpx (HTTP/2) + curl_cffi | 双引擎请求 |
| **数据格式** | Pydantic Settings | 配置验证与管理 |
| **部署** | Docker + Docker Compose | 支持 amd64/arm64 多平台 |

### 2.2 系统架构图

```
┌─────────────────────────────────────────────────────────┐
│                    客户端 / SDK                          │
│         (Claude Code / Cursor / OpenClaw / curl)        │
└──────────┬──────────────┬──────────────┬────────────────┘
           │              │              │
    ┌──────▼──────┐ ┌─────▼──────┐ ┌────▼──────┐
    │  OpenAI     │ │  Claude    │ │  Gemini   │
    │  /v1/chat/  │ │  /anthropic│ │  /v1beta/ │
    │  completions│ │  /v1/msgs  │ │  models/* │
    └──────┬──────┘ └─────┬──────┘ └────┬──────┘
           │              │              │
    ┌──────▼──────────────▼──────────────▼──────┐
    │           CLIProxy 协议转换层              │
    │  (多协议 → StandardRequest 统一格式)       │
    └──────────────────┬───────────────────────┘
                       │
    ┌──────────────────▼───────────────────────┐
    │           Qwen Executor                   │
    │  (会话管理 + 流式处理 + 重试决策)          │
    └──────────────────┬───────────────────────┘
                       │
    ┌──────────────────▼───────────────────────┐
    │           核心服务层                       │
    │  ┌─────────────┐  ┌──────────────────┐   │
    │  │ Account Pool│  │ Prompt Builder   │   │
    │  │ (账号池)    │  │ (Prompt 组装)    │   │
    │  └─────────────┘  └──────────────────┘   │
    │  ┌─────────────┐  ┌──────────────────┐   │
    │  │ Tool Parser │  │ Schema Compressor│   │
    │  │ (工具解析)  │  │ (Schema 压缩)    │   │
    │  └─────────────┘  └──────────────────┘   │
    │  ┌─────────────┐  ┌──────────────────┐   │
    │  │ ChatId Pool │  │ Tool Name        │   │
    │  │ (会话预热)  │  │ Obfuscation      │   │
    │  └─────────────┘  └──────────────────┘   │
    └──────────────────┬───────────────────────┘
                       │
    ┌──────────────────▼───────────────────────┐
    │           传输引擎层                       │
    │  ┌──────────┐ ┌──────────┐ ┌──────────┐  │
    │  │ Browser  │ │  Httpx   │ │  Hybrid  │  │
    │  │ Engine   │ │  Engine  │ │  Engine  │  │
    │  │(Camoufox)│ │(httpx)   │ │(推荐)    │  │
    │  └──────────┘ └──────────┘ └──────────┘  │
    └──────────────────┬───────────────────────┘
                       │
                       ▼
              ┌─────────────────┐
              │  chat.qwen.ai   │
              │  (通义千问网页版) │
              └─────────────────┘
```

### 2.3 请求处理流程

```
客户端请求
  → FastAPI Router (路由分发)
    → 协议适配层 (OpenAI/Claude/Gemini → StandardRequest)
      → Auth Resolver (API Key → 上游账号)
        → Account Pool (获取可用账号)
          → ChatId Pool (获取预热会话)
            → Prompt Builder (组装 prompt + tools)
              → Qwen Executor (流式执行)
                → Hybrid Engine (浏览器/HTTP 双引擎)
                  → chat.qwen.ai (上游)
                ← 流式响应
              ← 工具调用解析 (ToolParser)
            ← 协议转换 (StandardResponse → OpenAI/Claude/Gemini)
          ← SSE 流式返回给客户端
```

---

## 3. 项目目录结构

```
qwen2API/
├── backend/                        # Python FastAPI 后端
│   ├── main.py                     # 应用入口，注册路由/中间件/生命周期
│   ├── requirements.txt            # Python 依赖 (9个包)
│   ├── api/                        # 协议入口层 (7个路由模块)
│   │   ├── v1_chat.py              # OpenAI Chat Completions
│   │   ├── anthropic.py            # Anthropic Messages
│   │   ├── gemini.py               # Gemini GenerateContent
│   │   ├── images.py               # 图片生成
│   │   ├── files_api.py            # 文件上传/管理
│   │   ├── embeddings.py           # 向量嵌入
│   │   ├── models.py               # 模型列表
│   │   ├── probes.py               # 健康检查/就绪检查
│   │   └── admin.py                # 管理台 API
│   ├── adapter/                    # 协议适配
│   │   ├── cli_proxy.py            # 多协议 → StandardRequest 转换
│   │   └── standard_request.py     # 统一请求结构 + 客户端 profile
│   ├── core/                       # 核心配置与基础设施
│   │   ├── config.py               # 全局配置/模型映射/环境变量
│   │   ├── database.py             # 异步 JSON 数据库
│   │   ├── auth.py                 # API Key / Bearer 鉴权
│   │   ├── request_logging.py      # 请求上下文 + 链路日志
│   │   ├── browser_engine.py       # 浏览器引擎
│   │   ├── httpx_engine.py         # HTTP 引擎
│   │   ├── hybrid_engine.py        # 混合引擎
│   │   ├── session_affinity.py     # 会话亲和性
│   │   ├── session_lock.py         # 会话锁
│   │   ├── upstream_file_cache.py  # 上游文件缓存
│   │   ├── tool_cache.py           # 工具缓存
│   │   ├── log_filter.py           # 日志过滤
│   │   └── account_pool/           # 账号池子模块
│   │       ├── pool_core.py        # 池状态/冷却/淘汰
│   │       └── pool_acquire.py     # 账号获取/并发控制
│   ├── runtime/                    # 运行时执行层
│   │   ├── execution.py            # 流式执行 + 重试指令 (43KB)
│   │   ├── stream_metrics.py       # 流式指标埋点
│   │   ├── stream_presenter.py     # 流式呈现
│   │   └── stream_runtime.py       # 流式运行时
│   ├── services/                   # 业务服务层 (30个模块)
│   │   ├── qwen_client.py          # HTTP + 浏览器双引擎客户端
│   │   ├── prompt_builder.py       # 消息 → prompt + tools 组装 (44KB)
│   │   ├── tool_parser.py          # ##TOOL_CALL## 文本协议解析 (25KB)
│   │   ├── tool_validator.py       # 工具入参校验
│   │   ├── tool_arg_fixer.py       # 智能引号/模糊 Edit 修复
│   │   ├── tool_few_shot.py        # 按命名空间注入少样本
│   │   ├── tool_name_obfuscation.py# 工具名混淆避免 Qwen 函数校验
│   │   ├── schema_compressor.py    # JSON Schema → TS-like 签名
│   │   ├── chat_id_pool.py         # chat_id 预热池
│   │   ├── file_content_cache.py   # Read 结果缓存
│   │   ├── incremental_text_streamer.py # 流式 warmup/guard
│   │   ├── refusal_cleaner.py      # 历史拒绝文本清洗
│   │   ├── topic_isolation.py      # 新任务检测/历史切分
│   │   ├── truncation_recovery.py  # ##TOOL_CALL## 截断续写
│   │   ├── openai_stream_translator.py # 文本协议 → OpenAI SSE
│   │   ├── context_attachment_manager.py # 长上下文 → 附件
│   │   ├── context_offload.py      # 上下文卸载策略
│   │   ├── context_cleanup.py      # TTL 清理
│   │   ├── auth_resolver.py        # API Key → 上游账号 (35KB)
│   │   ├── auth_quota.py           # 下游 Key 配额
│   │   ├── completion_bridge.py    # 多协议响应桥
│   │   ├── file_store.py           # 本地文件暂存
│   │   ├── upstream_file_uploader.py # 文件上传至千问
│   │   ├── garbage_collector.py    # 会话/临时文件 GC
│   │   ├── response_formatters.py  # 各协议响应格式化
│   │   ├── standard_request_builder.py
│   │   ├── attachment_preprocessor.py
│   │   ├── task_session.py         # 任务会话管理 (16KB)
│   │   ├── token_calc.py           # Token 计算
│   │   └── client_profiles.py      # 客户端 profile 定义
│   ├── toolcall/                   # 工具调用处理
│   │   ├── normalize.py            # 工具名规范化
│   │   ├── stream_state.py         # 流式工具调用状态机
│   │   ├── formats_json.py         # JSON 工具格式
│   │   ├── formats_xml.py          # XML 工具格式
│   │   ├── parser.py               # 工具调用解析器
│   │   └── fallback_textkv.py      # 回退文本键值
│   ├── upstream/                   # 上游通信
│   │   ├── qwen_executor.py        # 会话生命周期 + 流式分发
│   │   ├── payload_builder.py      # 请求载荷构建
│   │   └── sse_consumer.py         # SSE 流解析
│   └── data/                       # 运行期数据目录
│       └── accounts.json           # 账号数据
├── frontend/                       # React + Vite 管理台
│   ├── src/
│   │   ├── pages/                  # Dashboard/Settings/Test/Images
│   │   ├── layouts/                # 布局组件
│   │   └── components/             # 通用组件
│   ├── package.json                # 前端依赖
│   ├── vite.config.ts              # Vite 配置 (API 代理到 7860)
│   └── Dockerfile                  # 前端独立部署 (nginx)
├── data/                           # 数据目录
│   └── .keep
├── scripts/
│   └── buildx-push.sh              # 多架构 Docker 推送脚本
├── .github/workflows/
│   └── docker-publish.yml          # CI/CD: 自动构建推送 Docker 镜像
├── Dockerfile                      # 主 Dockerfile (多阶段构建)
├── docker-compose.yml              # 生产部署配置
├── docker-compose.build.yml        # 本地构建配置
├── start.py                        # 一键启动脚本
├── .env.example                    # 环境变量模板
├── .gitignore
├── .dockerignore
├── .editorconfig
└── README.md                       # 完整项目文档 (46KB)
```

---

## 4. 核心功能详解

### 4.1 多协议兼容

qwen2API 支持三种主流 AI API 协议，客户端可以像调用 OpenAI/Claude/Gemini 一样调用通义千问：

| 协议 | 端点 | 兼容客户端 |
|------|------|------------|
| **OpenAI** | `POST /v1/chat/completions` | OpenAI SDK、Cursor、Cline |
| **Anthropic** | `POST /anthropic/v1/messages` | Claude Code、Anthropic SDK |
| **Gemini** | `POST /v1beta/models/{model}:generateContent` | Google Gen AI SDK |

**协议转换原理**：
- 所有协议入口统一通过 `CLIProxy` 转换为 `StandardRequest` 内部格式
- 执行完成后，再将 `StandardResponse` 转换回客户端期望的协议格式
- 避免三个入口各自维护 prompt 组装/工具装配逻辑导致行为漂移

### 4.2 模型映射

项目将主流 AI 客户端的模型名称统一映射到通义千问实际模型：

| 客户端传入模型 | 实际调用模型 |
|----------------|-------------|
| gpt-4o, gpt-4-turbo, gpt-4.1, o1, o3 | `qwen3.6-plus` |
| gpt-4o-mini, gpt-3.5-turbo | `qwen3.5-flash` |
| claude-opus-4-6, claude-sonnet-4-6, claude-3-5-sonnet | `qwen3.6-plus` |
| claude-3-haiku, claude-haiku-4-5 | `qwen3.5-flash` |
| gemini-2.5-pro, gemini-2.5-flash | `qwen3.6-plus` / `qwen3.5-flash` |
| deepseek-chat, deepseek-reasoner | `qwen3.6-plus` |

### 4.3 三种执行引擎

| 引擎 | 模式 | 特点 | 适用场景 |
|------|------|------|----------|
| **Hybrid** (推荐) | 浏览器优先 + HTTP 兜底 | 稳定性最高，限流风险最低 | 生产环境 |
| **Httpx** | 纯 HTTP 请求 | 速度最快，资源占用最低 | 速度优先测试 |
| **Browser** | 纯浏览器自动化 | 最接近真实网页环境 | 高拟态需求 |

### 4.4 账号池管理

```
账号池核心机制：
├── 多账号轮询：自动在多个通义千问账号间分配请求
├── 并发控制：MAX_INFLIGHT 限制每账号最大并发
├── 限流冷却：429 触发指数退避 (600s → 3600s)
├── 请求抖动：随机延迟 120-360ms 模拟真实用户
├── 故障重试：最多 3 次重试，自动排除故障账号
└── 会话亲和：同一 API Key 优先复用同一账号
```

### 4.5 工具调用系统

qwen2API 实现了一套完整的工具调用链路，核心创新在于**文本标记协议**——因为通义千问网页版本身不支持原生 function calling，项目通过 prompt 工程让模型输出特定格式的纯文本，再由代理层解析为标准工具调用协议。

#### 4.5.1 完整工具调用流水线（6 阶段）

```
客户端请求 (OpenAI/Claude/Gemini tools 定义)
  │
  ▼
① Prompt Builder（services/prompt_builder.py，44KB）
  ├── Schema 压缩：JSON Schema → TS-like 签名（~90% 体积缩减）
  ├── 工具名混淆：Read→fs_open_file、Bash→shell_run（其余加 u_ 前缀）
  ├── 少样本注入：按命名空间选代表工具构造合成对话
  └── 指令块注入：在 prompt 中嵌入 ##TOOL_CALL## 格式说明
  │
  ▼
② Qwen Executor（runtime/execution.py，43KB）
  ├── 发送组装好的 prompt 到 chat.qwen.ai
  ├── 流式收集响应，累积到 answer_text
  ├── 实时毒性拒绝检测（"Tool X does not exists." / "I cannot help"）
  └── 流式 warmup：累积 96 字符再输出给客户端
  │
  ▼
③ 文本标记协议解析（services/tool_parser.py，25KB + toolcall/ 包）
  ├── 多格式候选解析链（6 种格式按优先级依次尝试）：
  │   ├── 1. JSON 格式：{"tool_calls": [{"function": {...}}]}
  │   ├── 2. 文本标记：##TOOL_CALL## {...} ##END_CALL##
  │   ├── 3. XML 格式：<tool_call>...</tool_call>
  │   ├── 4. 代码块：```tool_call ... ```
  │   ├── 5. 旧 JSON：{"type": "tool_use", ...}
  │   └── 6. 纯 JSON：{"name": "...", "input": {...}}
  ├── 入站反混淆：fs_open_file → Read、u_TaskCreate → TaskCreate
  ├── 参数修正：path → file_path、cmd → command、智能引号修复
  └── 输出标准格式：{"type": "tool_use", "id": "...", "name": "...", "input": {...}}
  │
  ▼
④ 流式工具检测（ToolSieve 类）
  ├── 每个 chunk 实时检测工具调用开始标记
  ├── 进入捕获模式直到 ##END_CALL## 闭合
  └── 安全内容立即输出，疑似工具内容暂存
  │
  ▼
⑤ 协议转换（services/openai_stream_translator.py）
  ├── 内部格式 → OpenAI SSE tool_calls 格式
  ├── 内部格式 → Claude tool_use 格式
  └── 内部格式 → Gemini functionCall 格式
  │
  ▼
⑥ SSE 流式返回给客户端
  └── data: {"choices": [{"delta": {"tool_calls": [{"function": {...}}]}}]}
```

#### 4.5.2 数据流全景示例

```
Claude Code 发送：
  {"tools": [{"name": "Read", "parameters": {"type":"object",...}}]}

        ↓ CLIProxy 协议转换（adapter/cli_proxy.py）

StandardRequest（内部统一格式）

        ↓ Prompt Builder
        ├── Schema 压缩：{file_path!: string, encoding?: utf-8|base64}
        ├── 工具名混淆：Read → fs_open_file
        ├── 少样本注入：合成对话示例
        └── 指令块注入：##TOOL_CALL## 格式说明

        ↓ Hybrid Engine → chat.qwen.ai

Qwen 输出（纯文本）：
  "我来帮你读取文件。
   ##TOOL_CALL## {"name":"fs_open_file","input":{"file_path":"/tmp/test.py"}} ##END_CALL##"

        ↓ Tool Parser（_parse_tool_calls）
        ├── 匹配 ##TOOL_CALL##...##END_CALL##
        ├── JSON 提取：name=fs_open_file, input={file_path: /tmp/test.py}
        ├── 入站反混淆：fs_open_file → Read
        └── 输出：{"type":"tool_use", "id":"toolu_xxx", "name":"Read", ...}

        ↓ OpenAI Stream Translator

Claude Code 收到（OpenAI SSE）：
  data: {"choices":[{"delta":{"tool_calls":[{"function":
    {"name":"Read","arguments":"{\"file_path\":\"/tmp/test.py\"}"}}]}}]}
```

#### 4.5.3 各模块详解

**Prompt Builder（`backend/services/prompt_builder.py`）**

核心任务：让 Qwen 模型知道有哪些工具可用，并教会它输出 `##TOOL_CALL##` 格式。

- `_build_tool_instruction_block()`：生成 ACTION MARKER PROTOCOL 指令块，包含完整的格式说明、可用工具列表、严格规则
- `_render_history_tool_call()`：将历史工具调用渲染为 `##TOOL_CALL##` 格式，工具名自动混淆
- `_compact_history_tool_input()`：压缩历史工具参数（长文本截断为 `[N chars]`，长路径截断为 `.../最后两级`）
- `_is_heavy_tool_profile()`：判断是否为 Claude Code 等重工具客户端

**Schema 压缩（`backend/services/schema_compressor.py`）**

将 JSON Schema 压缩为 TypeScript-like 签名：
```
输入: {"type":"object","properties":{"file_path":{"type":"string"},
        "encoding":{"type":"string","enum":["utf-8","base64"]}},
        "required":["file_path"]}
输出: {file_path!: string, encoding?: utf-8|base64}
```
- `!` = required，`?` = optional
- 单工具从 ~1.5KB 降到 ~150-250 bytes，90 个工具从 ~135KB → ~15KB

**工具名混淆（`backend/services/tool_name_obfuscation.py`）**

Qwen 上游会把常见短名当内置函数校验并返回 "Tool X does not exists."：
- 显式别名：`Read→fs_open_file`、`Write→fs_put_file`、`Edit→fs_patch_file`、`Bash→shell_run`、`Grep→text_search`、`Glob→path_find` 等
- 通用兜底：其余所有工具自动加 `u_` 前缀
- 出站自动转换（`to_qwen_name`），入站反向还原（`from_qwen_name`）

**工具调用解析器（`backend/services/tool_parser.py` + `backend/toolcall/`）**

`toolcall/` 子包提供多格式解析基础设施：

| 模块 | 功能 |
|------|------|
| `parser.py` | 调度器：按优先级尝试 JSON/XML/TextKV 三种格式 |
| `formats_json.py` | JSON 格式解析，支持宽松修复（`name=` → `name":`） |
| `formats_xml.py` | XML 格式解析，支持 `<tool_call>` 和 `<invoke>` 两种风格 |
| `fallback_textkv.py` | 回退文本键值解析（`function.name: Read`） |
| `normalize.py` | 工具名归一化注册表，支持大小写不敏感匹配 |
| `stream_state.py` | 流式工具调用状态机（`StreamingToolCallState`） |

解析后的统一处理（`_make_tool_block`）：
1. `from_qwen_name()` 反混淆工具名
2. `normalize_tool_name()` 大小写归一化
3. `_normalize_tool_name_case()` 精确匹配客户端工具名
4. `_coerce_tool_input()` 修正参数名差异
5. `fix_tool_call_arguments()` 智能引号 + Edit 模糊匹配修复

**流式检测器（`ToolSieve` 类）**

实时检测 chunk 中的工具调用标记：
- `_find_tool_start()`：检测 `##TOOL_CALL##`、`{"name":`、`<tool_call>` 等 5 种开始标记
- `_consume_tool_capture()`：累积到完整标记后尝试解析
- `_split_safe_content()`：安全内容立即输出，末尾 10 字符保留防止标记被截断

**协议转换（`backend/services/openai_stream_translator.py`）**

两种模式：
- `BUFFERED_TOOL_CALLS_ONLY`（Claude Code profile）：缓冲所有疑似工具输出，最终确认后才发出
- `DIRECTIVE_DRIVEN_TOOL_CALLS`（其他 profile）：由 RuntimeToolDirective 驱动
- 检测到工具输出特征时先缓冲，确认后通过 `emit_tool_calls()` 发出；确认不是工具则作为普通文本发出

#### 4.5.4 关键优化策略汇总

| 优化 | 文件 | 效果 |
|------|------|------|
| **Schema 压缩** | `schema_compressor.py` | 90 个工具 ~135KB → ~15KB，省 90% token |
| **工具名混淆** | `tool_name_obfuscation.py` | 避免 Qwen 把 Read/Bash 当内置函数拦截 |
| **少样本注入** | `tool_few_shot.py` | 按命名空间选代表工具构造合成对话，提高 MCP/Skill 命中率 |
| **截断续写** | `truncation_recovery.py` | `##TOOL_CALL##` 未闭合时自动续写并去重拼接 |
| **话题隔离** | `topic_isolation.py` | Jaccard 相似度 < 0.1 判定新任务，丢弃无关旧历史 |
| **拒绝清洗** | `refusal_cleaner.py` | 替换历史中拒绝文本，防止级联复现 |
| **流式 warmup** | `incremental_text_streamer.py` | 累积 96 字符再输出，期间做拒绝检测 |
| **运行期重试** | `execution.py` | 8 种重试原因（空响应、工具名拦截、重复调用等） |
| **文件内容缓存** | `file_content_cache.py` | 代理侧缓存 "Unchanged since last read" 回填 |
| **参数模糊修复** | `tool_arg_fixer.py` | 智能引号替换 + Edit/StrReplace fuzzy 匹配 |
| **历史参数压缩** | `prompt_builder.py` | 长文本截断为 `[N chars]`，长路径保留末尾两级 |

### 4.6 图片生成

- 接口：`POST /v1/images/generations`
- 底层：通过 `qwen3.6-plus` + 千问网页 `image_gen` 工具
- 支持比例：1:1, 16:9, 9:16, 4:3, 3:4
- Chat 接口自动识别图片生成意图（"帮我画一张……"）

### 4.7 WebUI 管理台

内置 React 管理台，提供：
- **运行状态**：服务状态、引擎状态、统计信息
- **账号管理**：添加/测试/禁用上游账号
- **API Key 管理**：管理下游调用密钥
- **接口测试**：直接测试 OpenAI 对话接口
- **图片生成**：图形化图片生成页面
- **系统设置**：查看并修改运行时参数

---

## 5. API 接口参考

### 5.1 完整端点列表

| 方法       | 路径                                             | 说明                      |
| -------- | ---------------------------------------------- | ----------------------- |
| `POST`   | `/v1/chat/completions`                         | OpenAI Chat Completions |
| `GET`    | `/v1/models`                                   | 可用模型列表                  |
| `POST`   | `/v1/images/generations`                       | 图片生成                    |
| `POST`   | `/v1/files`                                    | 文件上传                    |
| `GET`    | `/v1/files`                                    | 文件列表                    |
| `GET`    | `/v1/files/{id}`                               | 文件详情                    |
| `DELETE` | `/v1/files/{id}`                               | 删除文件                    |
| `POST`   | `/v1/embeddings`                               | 向量嵌入                    |
| `POST`   | `/anthropic/v1/messages`                       | Anthropic Messages      |
| `POST`   | `/v1beta/models/{model}:generateContent`       | Gemini 非流式              |
| `POST`   | `/v1beta/models/{model}:streamGenerateContent` | Gemini 流式               |
| `GET`    | `/api/admin/*`                                 | 管理 API                  |
| `GET`    | `/healthz`                                     | 存活探针                    |
| `GET`    | `/readyz`                                      | 就绪探针                    |
| `GET`    | `/api`                                         | 服务信息                    |
| `GET`    | `/`                                            | 前端管理台                   |

### 5.2 认证方式

所有 API 请求通过 Bearer Token 认证：
```
Authorization: Bearer YOUR_API_KEY
```

---

## 6. 部署指南

### 6.1 Docker 部署（推荐）

```bash
# 1. 准备目录
mkdir qwen2api && cd qwen2api
mkdir -p data logs

# 2. 创建 .env
cat > .env << 'EOF'
ADMIN_KEY=your-strong-password
PORT=7860
ENGINE_MODE=hybrid
MAX_INFLIGHT=2
EOF

# 3. 启动
docker compose up -d
```

**docker-compose.yml 关键配置**：
- 镜像：`yujunzhixue/qwen2api:latest`
- 端口：`7860:7860`
- 数据卷：`./data:/workspace/data`、`./logs:/workspace/logs`
- 共享内存：`shm_size: '256m'`（浏览器需要）
- 健康检查：`/healthz` 每 30s 检查一次

### 6.2 本地源码运行

```bash
git clone https://github.com/YuJunZhiXue/qwen2API.git
cd qwen2API
python start.py
```

`start.py` 自动完成：
1. 安装后端依赖 (`pip install -r requirements.txt`)
2. 下载 Camoufox 浏览器内核
3. 安装前端依赖 (`npm install`)
4. 构建前端 (`npm run build`)
5. 启动后端服务 (Uvicorn)

### 6.3 环境变量参考

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ADMIN_KEY` | `change-me-now` | 管理台密码（**必须修改**） |
| `PORT` | `7860` | 服务端口 |
| `WORKERS` | `1` | Worker 数量（**必须保持 1**） |
| `ENGINE_MODE` | `hybrid` | 引擎模式: hybrid/httpx/browser |
| `MAX_INFLIGHT` | `2` | 每账号最大并发 |
| `MAX_RETRIES` | `3` | 请求失败重试次数 |
| `ACCOUNT_MIN_INTERVAL_MS` | `0` | 同账号请求最小间隔 |
| `REQUEST_JITTER_MIN_MS` | `0` | 请求抖动最小值 |
| `REQUEST_JITTER_MAX_MS` | `0` | 请求抖动最大值 |
| `RATE_LIMIT_BASE_COOLDOWN` | `600` | 限流基础冷却（秒） |
| `RATE_LIMIT_MAX_COOLDOWN` | `3600` | 限流最大冷却（秒） |
| `LOG_LEVEL` | `INFO` | 日志级别 |

---

## 7. 高级特性

### 7.1 Chat ID 预热池

- **问题**：每次新建会话 (`/api/v2/chats/new`) 握手耗时 500ms~6s
- **方案**：服务启动后为每个账号预建 5 个 chat_id，请求到来直接取用
- **TTL**：单条 chat_id 默认 10 分钟过期

### 7.2 Schema 压缩

将 JSON Schema 压缩为 TypeScript-like 签名：
```
输入: {"type":"object","properties":{"file_path":{"type":"string"},...}}
输出: {file_path!: string, encoding?: utf-8|base64}
```
- 单工具定义从 ~1.5KB 降到 ~150-250 bytes
- 90 个工具从 ~135KB → ~15KB

### 7.3 工具名混淆

Qwen 上游会把常见短名（Read/Write/Bash/Edit）当内置函数校验并返回 "Tool X does not exists."：
- 显式别名：`Read→fs_open_file`、`Bash→shell_run`
- 通用兜底：其余工具自动加 `u_` 前缀
- 出站自动转换，入站反向还原

### 7.4 话题隔离

- 抽取每条 user 消息的关键实体（文件路径、URL、专名等）
- 计算 Jaccard 相似度，< 0.1 判定为新任务
- 新任务自动丢弃无关历史，避免旧工具调用误导模型

### 7.5 历史拒绝清洗

扫描历史 assistant 消息中的拒绝/自我限制文本（"I'm sorry, I cannot help..."），替换为占位工具调用，防止模型看到自己的拒绝模式并级联复现。

### 7.6 增量流式 Warmup

- 累积 96 字符再开始输出
- 期间可做拒绝检测/格式判断
- 保留末尾 256 字符暂不输出，给跨 chunk 检测留空间

---

## 8. 依赖分析

### 8.1 后端依赖 (requirements.txt)

| 包 | 用途 |
|-----|------|
| `fastapi` | Web 框架 |
| `uvicorn[standard]` | ASGI 服务器 |
| `httpx[http2]` | 异步 HTTP 客户端 (支持 HTTP/2) |
| `pydantic-settings` | 配置管理 |
| `python-dotenv` | 环境变量加载 |
| `python-multipart` | 文件上传解析 |
| `tiktoken` | Token 计算 |
| `curl_cffi` | TLS 指纹伪造 HTTP 客户端 |
| `camoufox` | 反检测浏览器自动化 |
| `oss2` | 阿里云 OSS SDK |

### 8.2 前端依赖 (package.json)

| 包 | 用途 |
|-----|------|
| `react` 19.2 | UI 框架 |
| `react-dom` 19.2 | DOM 渲染 |
| `react-router-dom` 7.14 | 路由 |
| `tailwindcss` 4.2 | CSS 框架 |
| `@radix-ui/react-slot` | 无障碍组件基元 |
| `lucide-react` | 图标库 |
| `sonner` | Toast 通知 |
| `class-variance-authority` | 组件变体 |
| `clsx` + `tailwind-merge` | CSS 类名工具 |

---

## 9. CI/CD 与 DevOps

### 9.1 GitHub Actions 自动发布

`.github/workflows/docker-publish.yml` 配置：
- **触发条件**：push 到 main 分支 或打 `v*.*.*` tag
- **支持平台**：`linux/amd64`, `linux/arm64`
- **镜像标签**：`latest`、semver 版本号、SHA 短哈希
- **缓存**：GitHub Actions 缓存加速构建

### 9.2 多架构构建脚本

`scripts/buildx-push.sh` 支持本地构建多架构镜像：
```bash
./scripts/buildx-push.sh myrepo/qwen2api:tag linux/amd64,linux/arm64
```

---

## 10. 数据持久化

| 文件 | 内容 |
|------|------|
| `data/accounts.json` | 上游通义千问账号信息 |
| `data/users.json` | 下游 API Key / 用户数据 |
| `data/captures.json` | 抓取结果 |
| `data/config.json` | 运行时配置 |
| `data/context_cache.json` | 上下文缓存 |
| `data/uploaded_files.json` | 上传文件记录 |
| `data/session_affinity.json` | 会话亲和性 |
| `logs/` | 运行日志 |

---

## 11. 项目亮点总结

1. **多协议统一网关**：同时兼容 OpenAI、Claude、Gemini 三种协议，业界少见
2. **浏览器+HTTP 双引擎**：Hybrid 模式兼顾稳定性和速度
3. **企业级账号池**：多账号轮询、智能限流冷却、故障自动切换
4. **工具调用深度优化**：Schema 压缩、工具名混淆、少样本注入、截断续写等 10+ 项优化
5. **生产级稳定性**：Chat ID 预热、话题隔离、拒绝清洗、增量流式 warmup
6. **完整管理台**：WebUI 提供账号管理、API Key 管理、接口测试、图片生成
7. **一键部署**：Docker 镜像包含前后端，一条命令启动
8. **多平台支持**：amd64/arm64，支持 Mac (Apple Silicon) 和 Linux 服务器

---

## 12. 同类项目对比

| 项目 | 协议支持 | 引擎 | 账号池 | 工具调用 | 部署 |
|------|----------|------|--------|----------|------|
| **qwen2API** (本项目) | OpenAI+Claude+Gemini | 浏览器+HTTP 混合 | 多账号智能轮询 | 完整支持 | Docker/Vercel/Zeabur |
| smanx/qwen2api | OpenAI | HTTP | 基础 | 基础 | Docker/Vercel |
| tarun-re/qwen-api | OpenAI | HTTP | 无 | 无 | 本地 |

---

## 13. 注意事项与风险

1. **账号安全**：项目依赖通义千问网页账号，存在被限流/封禁风险
2. **合规性**：项目明确声明与阿里云无商业合作，使用者需自行评估合规风险
3. **WORKERS 必须为 1**：多 worker 会导致 JSON 文件写冲突
4. **浏览器资源**：Camoufox 需要至少 256MB 共享内存
5. **上游依赖**：项目依赖 chat.qwen.ai 网页接口，接口变更可能导致功能失效

---

*文档生成时间：2026-05-20*  
*基于项目 v2.0.0 版本分析*
