# qwen2API Enterprise Gateway

## 加入群聊

Telegram 群聊: [https://t.me/qwen2api](https://t.me/qwen2api)

[![Stars](https://img.shields.io/github/stars/YuJunZhiXue/qwen2API?style=flat-square)](https://github.com/YuJunZhiXue/qwen2API/stargazers)
[![Forks](https://img.shields.io/github/forks/YuJunZhiXue/qwen2API?style=flat-square)](https://github.com/YuJunZhiXue/qwen2API/network/members)
[![Docker Pulls](https://img.shields.io/docker/pulls/yujunzhixue/qwen2api?style=flat-square)](https://hub.docker.com/r/yujunzhixue/qwen2api)

语言 / Language: [中文](./README.md)

qwen2API 将通义千问 Web 端能力转换为 OpenAI、Anthropic Claude 与 Gemini 兼容接口，并提供 Web 管理台、账号池、图片生成、文件上传与多架构 Docker 部署能力。

## 目录

- [项目说明](#项目说明)
- [核心能力](#核心能力)
- [接口支持](#接口支持)
- [模型列表与模式](#模型列表与模式)
- [图片生成](#图片生成)
- [快速开始](#快速开始)
- [环境变量说明](#环境变量说明)
- [WebUI 管理台](#webui-管理台)
- [客户端接入示例](#客户端接入示例)
- [常见问题](#常见问题)
- [故障排查速查表](#故障排查速查表)
- [开发指南](#开发指南)
- [许可证与免责声明](#许可证与免责声明)

## 项目说明

本项目定位为本地或自托管的协议兼容网关。客户端请求进入 qwen2API 后，会被统一转换为内部标准请求，再由账号池选择可用千问账号访问上游。

适合场景：

- 使用 OpenAI 兼容客户端访问千问 Web 能力。
- 使用 Claude Code、Codex 等客户端接入自托管网关。
- 在 WebUI 中管理账号、API Key、测试聊天与图片生成。
- 通过 Docker 在 x86_64 或 arm64 服务器上部署。

## 核心能力

- 兼容 OpenAI Chat Completions、OpenAI Responses、Anthropic Messages 与 Gemini GenerateContent 的常用调用方式。
- `/v1/models` 从上游获取模型列表，并返回模型能力、基础模型与模式变体。
- 支持按请求切换思考模式：`enable_thinking=true` 为思考模式，`enable_thinking=false` 为快速模式。
- 支持 `POST /v1/images/generations` 图片生成接口。
- 支持文件上传与上下文附件链路。
- 支持多账号轮询、单账号并发控制、限流冷却与请求重试。
- 内置 React WebUI 管理台，后端可直接托管前端构建产物。
- 提供 `/healthz` 与 `/readyz` 用于容器健康检查。

更底层的高级内部机制说明放在 [INTERNALS.md](./INTERNALS.md)。

## 接口支持

### 已支持接口

| 协议 | 路径 | 说明 |
|---|---|---|
| OpenAI Chat Completions | `POST /v1/chat/completions`、`POST /chat/completions` | 支持流式与非流式聊天请求。 |
| OpenAI Responses | `POST /v1/responses`、`POST /responses` | 面向 Responses API 的兼容入口，覆盖常用文本与流式场景。 |
| OpenAI Models | `GET /v1/models`、`GET /v1/models/{model_id}` | 返回上游模型列表、能力字段与模式变体。 |
| OpenAI Images | `POST /v1/images/generations`、`POST /images/generations` | 返回 URL 格式图片结果。 |
| OpenAI Files | `POST /v1/files`、`DELETE /v1/files/{file_id}` | 用于本项目附件链路的上传与删除。 |
| OpenAI Embeddings | `POST /v1/embeddings`、`POST /embeddings` | 兼容占位实现，返回确定性模拟向量。 |
| Anthropic Messages | `POST /anthropic/v1/messages`、`POST /v1/messages`、`POST /messages` | Claude / Anthropic SDK 常用入口。 |
| Anthropic Count Tokens | `POST /anthropic/v1/messages/count_tokens`、`POST /v1/messages/count_tokens`、`POST /messages/count_tokens` | Token 估算接口。 |
| Gemini GenerateContent | `POST /v1beta/models/{model}:generateContent`、`POST /v1/models/{model}:generateContent`、`POST /models/{model}:generateContent` | Gemini 兼容非流式入口。 |
| Gemini StreamGenerateContent | `POST /v1beta/models/{model}:streamGenerateContent`、`POST /v1/models/{model}:streamGenerateContent`、`POST /models/{model}:streamGenerateContent` | Gemini 兼容流式入口。 |
| Admin API | `/api/admin/*` | WebUI 管理接口。 |
| Health | `GET /healthz` | 存活探针。 |
| Ready | `GET /readyz` | 就绪探针。 |

### 未实现或非完整协议

| 协议 | 当前状态 |
|---|---|
| OpenAI Assistants / Threads / Runs | 不提供完整协议实现。需要此类状态机的客户端建议改走 Chat Completions、Responses 或 Anthropic Messages。 |
| OpenAI Realtime / Audio / Speech / Transcriptions | 不支持。 |
| OpenAI Batch / Fine-tuning / Vector Stores | 不支持。 |
| OpenAI Files 完整生态 | 仅实现本项目附件所需的上传与删除，不等价于 OpenAI 官方文件生命周期。 |
| OpenAI Images 全参数 | 仅覆盖文本生图、数量、尺寸/比例与 URL 返回，不保证兼容全部高级参数。 |
| 原生 Embeddings | 千问 Web 端没有原生 Embeddings；当前接口为兼容占位的模拟向量。 |

## 模型列表与模式

`/v1/models` 会优先从千问上游 `/api/models` 获取真实模型列表，再按模型能力展开模式变体。公开文档不再维护静态模型转换表，客户端应以接口返回为准。

每个模型项包含 OpenAI 兼容字段和扩展字段：

```json
{
  "id": "qwen3.6-plus",
  "object": "model",
  "created": 1700000000,
  "owned_by": "qwen",
  "capabilities": {
    "thinking": true,
    "search": true,
    "vision": true,
    "deep_research": false,
    "image_gen": true,
    "video_gen": false,
    "web_dev": false,
    "slides": false
  },
  "base_model": "qwen3.6-plus",
  "mode": "chat",
  "display_name": "qwen3.6-plus",
  "family": "qwen3.6"
}
```

支持的模式变体：

| 后缀 | 含义 |
|---|---|
| `-thinking` | 使用同一个基础模型，并强制开启思考模式。 |
| `-deep-research` | 使用深度研究模式，并默认开启搜索。 |
| `-image` | 使用图片模式。 |
| `-video` | 使用视频模式。 |
| `-webdev` | 使用网页/建站模式。 |
| `-slides` | 使用 PPT/幻灯片模式。 |

示例：

```bash
curl http://127.0.0.1:7860/v1/models \
  -H "Authorization: Bearer YOUR_API_KEY"
```

如果上游模型列表暂时不可用，服务会回退到内置兼容列表，保证常见客户端仍能完成模型发现。

### 思考与快速模式

Chat 请求可以显式传入：

```json
{
  "model": "qwen3.6-plus",
  "messages": [{"role": "user", "content": "你好"}],
  "enable_thinking": false
}
```

规则：

- `enable_thinking=true`：开启思考模式。
- `enable_thinking=false`：关闭思考模式，优先更快返回。
- 不传 `enable_thinking` 时，默认保持项目原有行为：开启思考。
- 选择 `*-thinking` 模型变体时，后端会强制开启思考，即使请求里传了 `enable_thinking=false`。
- 图片、视频等非文本模式会自动关闭思考。

## 图片生成

图片接口兼容 OpenAI Images 的常用调用方式。

- 接口：`POST /v1/images/generations`
- 默认模型：`qwen3.6-plus`
- 默认尺寸：`1328x1328`
- 默认比例：`1:1`
- 返回格式：URL

建议请求里直接使用 `qwen3.6-plus`，或省略 `model` 使用默认模型。

```bash
curl http://127.0.0.1:7860/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "qwen3.6-plus",
    "prompt": "一只赛博朋克风格的猫，霓虹灯背景，超写实",
    "n": 1,
    "size": "1328x1328",
    "response_format": "url"
  }'
```

支持尺寸与比例：

| size | ratio |
|---|---|
| `1328x1328` | `1:1` |
| `1664x928` | `16:9` |
| `928x1664` | `9:16` |
| `1472x1140` | `4:3` |
| `1140x1472` | `3:4` |

也可以传 `ratio` 或 `aspect_ratio`，例如：

```json
{
  "prompt": "一张产品海报",
  "ratio": "16:9"
}
```

## 快速开始

### 部署前准备

你需要准备：

- 一个可用的千问 Web 账号。
- 一个服务器或本机 Docker 环境。
- 一个管理台密钥 `ADMIN_KEY`。
- 一个客户端调用用的 API Key，启动后可在 WebUI 中创建。

默认访问地址：

- WebUI：`http://127.0.0.1:7860/`
- API：`http://127.0.0.1:7860/v1`
- 健康检查：`http://127.0.0.1:7860/healthz`

### 方案 A：直接拉取多架构 Docker 镜像部署（推荐）

本仓库已经通过 GitHub Actions 构建并发布 `linux/amd64` 与 `linux/arm64` 多架构镜像。服务器不需要本地构建镜像，也不需要自行构建前端。

#### 1. 创建部署目录

```bash
mkdir -p qwen2api/data qwen2api/logs
cd qwen2api
```

#### 2. 创建 `.env`

```env
ADMIN_KEY=change-me-now
PORT=7860
WORKERS=1
LOG_LEVEL=INFO
MAX_INFLIGHT_PER_ACCOUNT=2
MAX_RETRIES=3
ACCOUNT_MIN_INTERVAL_MS=0
REQUEST_JITTER_MIN_MS=0
REQUEST_JITTER_MAX_MS=0
RATE_LIMIT_BASE_COOLDOWN=600
RATE_LIMIT_MAX_COOLDOWN=3600
ACCOUNTS_FILE=/workspace/data/accounts.json
USERS_FILE=/workspace/data/users.json
CAPTURES_FILE=/workspace/data/captures.json
CONTEXT_GENERATED_DIR=/workspace/data/context_files
CONTEXT_CACHE_FILE=/workspace/data/context_cache.json
UPLOADED_FILES_FILE=/workspace/data/uploaded_files.json
CONTEXT_AFFINITY_FILE=/workspace/data/session_affinity.json
CONTEXT_INLINE_MAX_CHARS=4000
CONTEXT_FORCE_FILE_MAX_CHARS=10000
CONTEXT_ATTACHMENT_TTL_SECONDS=1800
CONTEXT_UPLOAD_PARSE_TIMEOUT_SECONDS=60
```

`ADMIN_KEY` 必须改成自己的强密码。

#### 3. 创建 `docker-compose.yml`

```yaml
services:
  qwen2api:
    image: ${QWEN2API_IMAGE:-yujunzhixue/qwen2api:latest}
    container_name: qwen2api
    restart: unless-stopped
    init: true
    env_file:
      - path: .env
        required: false
    ports:
      - "${HOST_PORT:-7860}:7860"
    volumes:
      - ./data:/workspace/data
      - ./logs:/workspace/logs
    shm_size: "512m"
    environment:
      PYTHONIOENCODING: utf-8
      PORT: "7860"
      WORKERS: "1"
      LOG_LEVEL: "INFO"
      BROWSER_POOL_SIZE: "1"
      MAX_INFLIGHT_PER_ACCOUNT: "2"
      ACCOUNT_MIN_INTERVAL_MS: "0"
      REQUEST_JITTER_MIN_MS: "0"
      REQUEST_JITTER_MAX_MS: "0"
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS http://127.0.0.1:7860/healthz || exit 1"]
      interval: 30s
      timeout: 10s
      start_period: 120s
      retries: 3
```

#### 4. 拉取并启动

```bash
docker compose pull
docker compose up -d
```

#### 5. 查看状态

```bash
docker compose ps
docker compose logs -f
curl http://127.0.0.1:7860/healthz
```

#### 6. 初始化账号与 API Key

打开 `http://服务器IP:7860/`，使用 `.env` 里的 `ADMIN_KEY` 登录管理台。

建议初始化顺序：

1. 在账号管理中添加或注册千问账号。
2. 确认账号状态可用。
3. 在 API Key 管理中创建客户端调用用的 API Key。
4. 在聊天测试页调用一次普通文本请求。
5. 如需图片生成，在图片页面测试一次生图。

#### 7. 更新镜像

```bash
docker compose pull
docker compose up -d
```

#### 8. 停止服务

```bash
docker compose down
```

如果要保留账号、API Key、上传文件和缓存数据，不要删除 `data/`。

### 方案 B：本地 buildx 推送多架构镜像

适合没有 CI、需要推送到自己镜像仓库的场景。此方案会在本地构建 `linux/amd64` 与 `linux/arm64` 镜像，并直接推送到镜像仓库。

#### 1. 登录镜像仓库

```bash
docker login
```

如果使用 GHCR 或私有仓库，请按对应仓库要求登录。

#### 2. 创建并启用 buildx builder

```bash
docker buildx create --name qwen2api-builder --use
docker buildx inspect --bootstrap
```

如果 builder 已存在，可以改用：

```bash
docker buildx use qwen2api-builder
docker buildx inspect --bootstrap
```

#### 3. 构建并推送多架构镜像

把 `YOUR_DOCKERHUB_NAME` 和 `v0.0.0` 改成自己的仓库名与版本号。

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t YOUR_DOCKERHUB_NAME/qwen2api:v0.0.0 \
  -t YOUR_DOCKERHUB_NAME/qwen2api:latest \
  --push \
  .
```

PowerShell 可以写成：

```powershell
docker buildx build `
  --platform linux/amd64,linux/arm64 `
  -t YOUR_DOCKERHUB_NAME/qwen2api:v0.0.0 `
  -t YOUR_DOCKERHUB_NAME/qwen2api:latest `
  --push `
  .
```

#### 4. 服务器使用自定义镜像

方式一：修改 `docker-compose.yml` 的 `image`。

```yaml
image: YOUR_DOCKERHUB_NAME/qwen2api:latest
```

方式二：保留仓库默认 Compose 文件，通过 `.env` 或命令行覆盖镜像。

```env
QWEN2API_IMAGE=YOUR_DOCKERHUB_NAME/qwen2api:latest
```

然后在服务器执行：

```bash
docker compose pull
docker compose up -d
```

### 方案 C：本地源码开发运行

适合开发、调试和改前端页面，不建议作为生产部署方式。

环境要求：

- Python 3.10+
- Node.js 20+
- 可访问 Python 与 npm 依赖源
- 可访问 Camoufox 浏览器内核下载源

一键开发启动：

```bash
git clone https://github.com/YuJunZhiXue/qwen2API.git
cd qwen2API
python start.py
```

`start.py` 会安装后端依赖、检查 Camoufox、启动后端，并启动前端 Vite 开发服务器。

如果需要手动分开启动，见 [开发指南](#开发指南)。

## 环境变量说明

项目提供 `.env.example`，部署时建议复制为 `.env` 后修改。

| 变量 | 推荐值 / 默认值 | 说明 |
|---|---|---|
| `ADMIN_KEY` | `change-me-now` | 管理台登录密钥，生产环境必须修改。 |
| `PORT` | `7860` | 容器内服务端口。Compose 默认映射到宿主机 `7860`。 |
| `HOST_PORT` | `7860` | 仅 Compose 使用，控制宿主机端口映射。 |
| `QWEN2API_IMAGE` | `yujunzhixue/qwen2api:latest` | 仅 Compose 使用，覆盖要拉取的镜像。 |
| `WORKERS` | `1` | Uvicorn worker 数量。建议保持 `1`，避免 JSON 数据文件并发写冲突。 |
| `LOG_LEVEL` | `INFO` | 日志级别，可选 `DEBUG`、`INFO`、`WARNING`、`ERROR`。 |
| `BROWSER_POOL_SIZE` | `1` | 浏览器页面池大小。内存有限时保持 `1`。 |
| `MAX_INFLIGHT_PER_ACCOUNT` | `2` | 每个千问账号允许同时处理的请求数。 |
| `MAX_RETRIES` | `3` | 上游失败后的最大重试次数。 |
| `ACCOUNT_MIN_INTERVAL_MS` | `0` | 同一账号两次请求之间的最小间隔。 |
| `REQUEST_JITTER_MIN_MS` | `0` | 请求前随机抖动最小值，单位毫秒。 |
| `REQUEST_JITTER_MAX_MS` | `0` | 请求前随机抖动最大值，单位毫秒。 |
| `RATE_LIMIT_BASE_COOLDOWN` | `600` | 账号限流后的基础冷却秒数。 |
| `RATE_LIMIT_MAX_COOLDOWN` | `3600` | 账号限流后的最大冷却秒数。 |
| `ACCOUNTS_FILE` | `/workspace/data/accounts.json` | 千问账号数据文件路径。 |
| `USERS_FILE` | `/workspace/data/users.json` | API Key / 用户数据文件路径。 |
| `CAPTURES_FILE` | `/workspace/data/captures.json` | 调试抓取数据文件路径。 |
| `CONTEXT_GENERATED_DIR` | `/workspace/data/context_files` | 上下文附件与生成文件目录。 |
| `CONTEXT_CACHE_FILE` | `/workspace/data/context_cache.json` | 上下文缓存数据文件。 |
| `UPLOADED_FILES_FILE` | `/workspace/data/uploaded_files.json` | 上传文件元数据文件。 |
| `CONTEXT_AFFINITY_FILE` | `/workspace/data/session_affinity.json` | 会话亲和数据文件。 |
| `CONTEXT_INLINE_MAX_CHARS` | `4000` | 小文件内联进上下文的字符上限。 |
| `CONTEXT_FORCE_FILE_MAX_CHARS` | `10000` | 强制转附件前允许处理的字符上限。 |
| `CONTEXT_ATTACHMENT_TTL_SECONDS` | `1800` | 附件缓存过期时间。 |
| `CONTEXT_UPLOAD_PARSE_TIMEOUT_SECONDS` | `60` | 上传文件解析超时时间。 |

兼容说明：

- `MAX_INFLIGHT` 是旧版兼容别名，不建议继续使用。

## WebUI 管理台

访问 `http://127.0.0.1:7860/`，使用 `ADMIN_KEY` 登录。

主要页面：

- 账号管理：添加、注册、激活、验证和删除千问账号。
- API Key 管理：创建和删除客户端调用用的 Key。
- 聊天测试：选择模型、切换思考/快速模式、测试流式与非流式请求。
- 图片生成：测试图片生成接口和尺寸比例。
- 系统设置：查看运行状态与基础配置。

数据持久化依赖 `data/` 目录。Docker 部署时必须保留：

```yaml
volumes:
  - ./data:/workspace/data
  - ./logs:/workspace/logs
```

## 客户端接入示例

### Claude Code

Claude Code 推荐走 Anthropic 兼容入口。

macOS / Linux：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:7860/anthropic
export ANTHROPIC_API_KEY=YOUR_API_KEY
claude --model claude-sonnet-4-6
```

Windows PowerShell：

```powershell
$env:ANTHROPIC_BASE_URL="http://127.0.0.1:7860/anthropic"
$env:ANTHROPIC_API_KEY="YOUR_API_KEY"
claude --model claude-sonnet-4-6
```

如果你的 Claude Code 版本使用配置文件，请把 API 地址设置为：

```text
http://127.0.0.1:7860/anthropic
```

### Codex

Codex 推荐走 OpenAI 兼容入口。

macOS / Linux：

```bash
export OPENAI_BASE_URL=http://127.0.0.1:7860/v1
export OPENAI_API_KEY=YOUR_API_KEY
codex --model qwen3.6-plus
```

Windows PowerShell：

```powershell
$env:OPENAI_BASE_URL="http://127.0.0.1:7860/v1"
$env:OPENAI_API_KEY="YOUR_API_KEY"
codex --model qwen3.6-plus
```

如果你的 Codex 版本要求显式配置 provider，请选择 OpenAI compatible provider，并填写：

```text
base_url = http://127.0.0.1:7860/v1
api_key = YOUR_API_KEY
model = qwen3.6-plus
```

### curl 冒烟请求

```bash
curl http://127.0.0.1:7860/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "qwen3.6-plus",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": false,
    "enable_thinking": false
  }'
```

## 常见问题

### `.env` 不存在能启动吗

可以。`docker-compose.yml` 中 `.env` 是可选文件，缺失时使用镜像默认值。但生产环境必须设置自己的 `ADMIN_KEY`。

### 登录管理台后应该先做什么

先添加或注册千问账号，再创建客户端 API Key。客户端不要直接使用 `ADMIN_KEY` 作为长期调用 Key。

### `/v1/models` 返回空或回退列表

通常是账号池暂时没有可用千问账号，或者上游模型列表请求失败。先在 WebUI 中确认账号状态，再重新调用 `/v1/models`。

### 图片生成返回 500 或没有 URL

先确认同一个千问账号在网页端可以正常生成图片，再确认请求使用了支持的尺寸或比例。服务端日志里可以搜索 `[T2I]` 和 `[T2I-SSE]`。

### 端口 7860 被占用怎么办

Docker 部署时改宿主机端口即可：

```env
HOST_PORT=8080
```

然后访问 `http://127.0.0.1:8080/`。

### amd64 服务器拉到 arm64 镜像怎么办

使用本仓库发布的 `yujunzhixue/qwen2api:latest` 即可，它是多架构镜像。如果你自己构建镜像，请使用 buildx 同时推送 `linux/amd64` 和 `linux/arm64`。

### WebUI 白屏或静态资源 404

确认使用的是完整 Docker 镜像，或本地源码运行时已经完成前端构建。浏览器缓存旧资源时可以强制刷新。

### 账号频繁被限流

降低单账号并发，增加请求间隔，或在管理台添加更多账号。可优先调整：

```env
MAX_INFLIGHT_PER_ACCOUNT=1
ACCOUNT_MIN_INTERVAL_MS=1200
RATE_LIMIT_BASE_COOLDOWN=1200
```

## 故障排查速查表

| 现象 | 常见原因 | 处理方式 |
|---|---|---|
| 容器启动后立刻退出 | 端口冲突、环境变量错误、数据目录权限异常 | 查看 `docker compose logs -f`，确认 `HOST_PORT`、`data/` 与 `logs/`。 |
| `/healthz` 不通 | 服务未启动完成或容器异常 | 先看 `docker compose ps`，再看容器日志。 |
| `/readyz` 失败 | 账号池未加载或上游状态不可用 | 登录 WebUI 检查账号状态。 |
| `/v1/chat/completions` 返回 401 | API Key 错误或未创建客户端 Key | 在 WebUI 创建 API Key，并用 `Authorization: Bearer YOUR_API_KEY` 调用。 |
| `/v1/models` 只有少量兼容模型 | 上游模型列表暂时不可用 | 检查账号是否可用，稍后重试。 |
| 图片生成无结果 | 账号不支持图片、上游未返回 URL、尺寸参数不合适 | 先用 WebUI 图片页测试，再检查日志中的 `[T2I]`。 |
| 响应很慢 | 上游排队、账号冷却、服务器资源不足 | 降低并发，增加账号，检查 CPU/内存。 |
| 浏览器相关错误 | 共享内存不足或运行环境缺少依赖 | Docker 部署保持 `shm_size: "512m"`，优先使用预构建镜像。 |
| 重启后账号或 Key 丢失 | 没有挂载 `data/` | 确认 Compose 中存在 `./data:/workspace/data`。 |

## 开发指南

### 后端本地启动

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
python -m camoufox fetch
uvicorn backend.main:app --reload --host 0.0.0.0 --port 7860
```

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
python -m camoufox fetch
uvicorn backend.main:app --reload --host 0.0.0.0 --port 7860
```

### 前端本地启动

```bash
npm --prefix frontend install
npm --prefix frontend run dev
```

前端开发服务器会通过 Vite 配置代理到后端，后端默认运行在 `http://127.0.0.1:7860`。

### 前端构建

```bash
npm --prefix frontend run typecheck
npm --prefix frontend run build
```

构建产物位于 `frontend/dist`。后端启动时如果检测到该目录，会把它作为 WebUI 静态资源托管。

### 后端语法检查

```bash
python -m compileall backend
```

### 路由开发约定

- OpenAI 兼容入口放在 `backend/api/v1_chat.py`、`backend/api/responses.py`、`backend/api/models.py`、`backend/api/images.py`、`backend/api/files_api.py`。
- Anthropic 兼容入口放在 `backend/api/anthropic.py`。
- Gemini 兼容入口放在 `backend/api/gemini.py`。
- 管理台接口放在 `backend/api/admin.py`。
- 复杂业务逻辑放在 `backend/services/` 或 `backend/runtime/`，不要堆在 API 层。
- 配置项统一从 `backend/core/config.py` 读取，并同步更新 `.env.example` 与 README。

### Docker 镜像开发

本地单架构构建可使用：

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

发布多架构镜像请使用 [方案 B](#方案-b本地-buildx-推送多架构镜像) 的 buildx 命令。

## 许可证与免责声明

本项目用于协议兼容、接口转换、自动化测试与个人技术研究。项目本身不提供任何官方授权的通义千问商业接口服务。

免责声明：

- 本项目与阿里云、通义千问及相关官方服务无任何从属、代理或商业合作关系。
- 本项目不是官方产品，也不构成任何官方服务承诺。
- 使用者应自行评估所在地区法律法规、上游服务条款、账号合规性与数据安全要求。
- 因使用本项目导致的账号封禁、请求受限、数据丢失、服务中断或其他风险，由使用者自行承担。
- 如果权利人认为本项目内容侵犯其合法权益，请通过仓库 Issue 提出，维护者将在核实后处理。
