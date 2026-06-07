# qwen2API Enterprise Gateway

## Join the Community

Telegram Group: [https://t.me/qwen2api](https://t.me/qwen2api)

[![Stars](https://img.shields.io/github/stars/YuJunZhiXue/qwen2API?style=flat-square)](https://github.com/YuJunZhiXue/qwen2API/stargazers)
[![Forks](https://img.shields.io/github/forks/YuJunZhiXue/qwen2API?style=flat-square)](https://github.com/YuJunZhiXue/qwen2API/network/members)
[![Docker Pulls](https://img.shields.io/docker/pulls/yujunzhixue/qwen2api?style=flat-square)](https://hub.docker.com/r/yujunzhixue/qwen2api)

语言 / Language: [中文](./README.md)

qwen2API converts the Qwen web-side capabilities into OpenAI, Anthropic Claude, and Gemini compatible interfaces, and provides a web management console, account pool, image generation, file upload, and multi-architecture Docker deployment.

## Table of Contents

- [Project Overview](#project-overview)
- [Core Capabilities](#core-capabilities)
- [API Support](#api-support)
- [Model List and Modes](#model-list-and-modes)
- [Image Generation](#image-generation)
- [Quick Start](#quick-start)
- [Environment Variables](#environment-variables)
- [WebUI Console](#webui-console)
- [Client Integration Examples](#client-integration-examples)
- [FAQ](#faq)
- [Troubleshooting Reference](#troubleshooting-reference)
- [Development Guide](#development-guide)
- [License and Disclaimer](#license-and-disclaimer)

## Project Overview

This project is designed as a local or self-hosted protocol-compatible gateway. After client requests enter qwen2API, they are uniformly converted into internal standard requests, and then an available Qwen account is selected from the account pool to access the upstream.

Suitable scenarios:

- Accessing Qwen web capabilities using OpenAI-compatible clients.
- Connecting Claude Code, Codex, and similar clients to a self-hosted gateway.
- Managing accounts, API keys, and testing chat and image generation in the WebUI.
- Deploying on x86_64 or arm64 servers via Docker.

## Core Capabilities

- Compatible with common calling patterns for OpenAI Chat Completions, OpenAI Responses, Anthropic Messages, and Gemini GenerateContent.
- `/v1/models` fetches the model list from upstream and returns model capabilities, base models, and mode variants.
- Supports per-request switching of thinking mode: `enable_thinking=true` for thinking mode, `enable_thinking=false` for fast mode.
- Supports `POST /v1/images/generations` image generation interface.
- Supports file upload and context attachment pipeline.
- Supports multi-account polling, single-account concurrency control, rate limit cooling, and request retry.
- Built-in React WebUI console; the backend can directly host the frontend build artifacts.
- Provides `/healthz` and `/readyz` for container health checks.

More detailed low-level internal mechanism descriptions are in [INTERNALS.md](./INTERNALS.md).

## API Support

### Supported Interfaces

| Protocol | Path | Description |
|---|---|---|
| OpenAI Chat Completions | `POST /v1/chat/completions`, `POST /chat/completions` | Supports streaming and non-streaming chat requests. |
| OpenAI Responses | `POST /v1/responses`, `POST /responses` | Compatible entry point for the Responses API, covering common text and streaming scenarios. |
| OpenAI Models | `GET /v1/models`, `GET /v1/models/{model_id}` | Returns upstream model list, capability fields, and mode variants. |
| OpenAI Images | `POST /v1/images/generations`, `POST /images/generations` | Returns image results in URL format. |
| OpenAI Files | `POST /v1/files`, `DELETE /v1/files/{file_id}` | Upload and delete for this project's attachment pipeline. |
| OpenAI Embeddings | `POST /v1/embeddings`, `POST /embeddings` | Compatibility placeholder implementation, returns deterministic simulated vectors. |
| Anthropic Messages | `POST /anthropic/v1/messages`, `POST /v1/messages`, `POST /messages` | Common entry points for Claude / Anthropic SDK. |
| Anthropic Count Tokens | `POST /anthropic/v1/messages/count_tokens`, `POST /v1/messages/count_tokens`, `POST /messages/count_tokens` | Token estimation interface. |
| Gemini GenerateContent | `POST /v1beta/models/{model}:generateContent`, `POST /v1/models/{model}:generateContent`, `POST /models/{model}:generateContent` | Gemini-compatible non-streaming entry point. |
| Gemini StreamGenerateContent | `POST /v1beta/models/{model}:streamGenerateContent`, `POST /v1/models/{model}:streamGenerateContent`, `POST /models/{model}:streamGenerateContent` | Gemini-compatible streaming entry point. |
| Admin API | `/api/admin/*` | WebUI management interface. |
| Health | `GET /healthz` | Liveness probe. |
| Ready | `GET /readyz` | Readiness probe. |

### Unimplemented or Incomplete Protocols

| Protocol | Current Status |
|---|---|
| OpenAI Assistants / Threads / Runs | No complete protocol implementation. Clients requiring this type of state machine are recommended to use Chat Completions, Responses, or Anthropic Messages instead. |
| OpenAI Realtime / Audio / Speech / Transcriptions | Not supported. |
| OpenAI Batch / Fine-tuning / Vector Stores | Not supported. |
| OpenAI Files Full Ecosystem | Only upload and delete required for this project's attachments are implemented; not equivalent to the official OpenAI file lifecycle. |
| OpenAI Images Full Parameters | Only covers text-to-image, quantity, size/ratio, and URL return; full compatibility with all advanced parameters is not guaranteed. |
| Native Embeddings | Qwen's web side has no native Embeddings; the current interface is a compatibility placeholder with simulated vectors. |

## Model List and Modes

`/v1/models` will first fetch the real model list from the Qwen upstream `/api/models`, then expand mode variants based on model capabilities. The public documentation no longer maintains a static model conversion table; clients should use the interface return as the authoritative source.

Each model item contains OpenAI-compatible fields and extended fields:

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

Supported mode variants:

| Suffix | Meaning |
|---|---|
| `-thinking` | Uses the same base model with thinking mode forced on. |
| `-deep-research` | Uses deep research mode with search enabled by default. |
| `-image` | Uses image mode. |
| `-video` | Uses video mode. |
| `-webdev` | Uses web/site-building mode. |
| `-slides` | Uses PPT/slides mode. |

Example:

```bash
curl http://127.0.0.1:7860/v1/models \
  -H "Authorization: Bearer YOUR_API_KEY"
```

If the upstream model list is temporarily unavailable, the service will fall back to a built-in compatibility list to ensure common clients can still complete model discovery.

### Thinking and Fast Mode

Chat requests can explicitly pass:

```json
{
  "model": "qwen3.6-plus",
  "messages": [{"role": "user", "content": "Hello"}],
  "enable_thinking": false
}
```

Rules:

- `enable_thinking=true`: Enables thinking mode.
- `enable_thinking=false`: Disables thinking mode, prioritizing faster responses.
- When `enable_thinking` is not passed, thinking mode is enabled by default.
- When selecting a `*-thinking` model variant, the backend will force thinking on, even if `enable_thinking=false` is passed in the request.
- Non-text modes such as image and video will automatically disable thinking.

## Image Generation

The image interface is compatible with the common OpenAI Images calling pattern.

- Interface: `POST /v1/images/generations`
- Default model: `qwen3.6-plus`
- Default size: `1328x1328`
- Default ratio: `1:1`
- Return format: URL

It is recommended to use `qwen3.6-plus` directly in the request, or omit `model` to use the default model.

```bash
curl http://127.0.0.1:7860/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "qwen3.6-plus",
    "prompt": "A cyberpunk-style cat, neon light background, ultra-realistic",
    "n": 1,
    "size": "1328x1328",
    "response_format": "url"
  }'
```

Supported sizes and ratios:

| size | ratio |
|---|---|
| `1328x1328` | `1:1` |
| `1664x928` | `16:9` |
| `928x1664` | `9:16` |
| `1472x1140` | `4:3` |
| `1140x1472` | `3:4` |

You can also pass `ratio` or `aspect_ratio`, for example:

```json
{
  "prompt": "A product poster",
  "ratio": "16:9"
}
```

## Quick Start

### Pre-deployment Preparation

You will need:

- A working Qwen web account.
- A server or local Docker environment.
- An admin console key `ADMIN_KEY`.
- An API key for client calls, which can be created in the WebUI after startup.

Default access addresses:

- WebUI: `http://127.0.0.1:7860/`
- API: `http://127.0.0.1:7860/v1`
- Health check: `http://127.0.0.1:7860/healthz`

### Option A: Pull Multi-Architecture Docker Image (Recommended)

This repository has already built and published `linux/amd64` and `linux/arm64` multi-architecture images via GitHub Actions. The server does not need to build images locally, nor does it need to build the frontend separately.

#### 1. Create Deployment Directory

```bash
mkdir -p qwen2api/data qwen2api/logs
cd qwen2api
```

#### 2. Create `.env`

```env
ADMIN_KEY=change-me-now
# QWEN_API_KEY=sk-your-env-key
# QWEN_API_KEYS=sk-key-1,sk-key-2
# QWEN_API_KEY_1=sk-numbered-key
# QWEN_ACCOUNT_1=your-qwen-token;optional-email;optional-password
# KEEPALIVE_URL=https://your-app.onrender.com/keepalive
# KEEPALIVE_INTERVAL=60
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
CONFIG_FILE=/workspace/data/config.json
CONTEXT_GENERATED_DIR=/workspace/data/context_files
CONTEXT_CACHE_FILE=/workspace/data/context_cache.json
UPLOADED_FILES_FILE=/workspace/data/uploaded_files.json
CONTEXT_AFFINITY_FILE=/workspace/data/session_affinity.json
CONTEXT_INLINE_MAX_CHARS=4000
CONTEXT_FORCE_FILE_MAX_CHARS=10000
CONTEXT_ATTACHMENT_TTL_SECONDS=1800
CONTEXT_UPLOAD_PARSE_TIMEOUT_SECONDS=60
```

`ADMIN_KEY` must be changed to your own strong password.

#### 3. Create `docker-compose.yml`

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

#### 4. Pull and Start

```bash
docker compose pull
docker compose up -d
```

#### 5. Check Status

```bash
docker compose ps
docker compose logs -f
curl http://127.0.0.1:7860/healthz
```

#### 6. Initialize Accounts and API Keys

Open `http://YOUR_SERVER_IP:7860/`, and log in to the management console using the `ADMIN_KEY` from your `.env` file.

Recommended initialization order:

1. Add or register Qwen accounts in account management, or inject them with `QWEN_ACCOUNT_1=token;optional-email;optional-password`.
2. Confirm account status is available.
3. Create an API key for client calls in API key management, or inject it with `QWEN_API_KEY` / `QWEN_API_KEYS` / `QWEN_API_KEY_1`.
4. Make one ordinary text request on the chat test page.
5. If image generation is needed, test image generation on the image page.

#### 7. Update Image

```bash
docker compose pull
docker compose up -d
```

#### 8. Stop Service

```bash
docker compose down
```

If you want to retain account, API key, uploaded file, and cache data, do not delete `data/`.

### Option B: Local buildx Push Multi-Architecture Image

Suitable for scenarios without CI that need to push to their own image registry. This option builds `linux/amd64` and `linux/arm64` images locally and pushes them directly to the image registry.

#### 1. Log in to the Image Registry

```bash
docker login
```

If using GHCR or a private registry, log in according to the corresponding registry requirements.

#### 2. Create and Enable buildx Builder

```bash
docker buildx create --name qwen2api-builder --use
docker buildx inspect --bootstrap
```

If the builder already exists, use:

```bash
docker buildx use qwen2api-builder
docker buildx inspect --bootstrap
```

#### 3. Build and Push Multi-Architecture Image

Replace `YOUR_DOCKERHUB_NAME` and `v0.0.0` with your own repository name and version number.

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t YOUR_DOCKERHUB_NAME/qwen2api:v0.0.0 \
  -t YOUR_DOCKERHUB_NAME/qwen2api:latest \
  --push \
  .
```

PowerShell:

```powershell
docker buildx build `
  --platform linux/amd64,linux/arm64 `
  -t YOUR_DOCKERHUB_NAME/qwen2api:v0.0.0 `
  -t YOUR_DOCKERHUB_NAME/qwen2api:latest `
  --push `
  .
```

#### 4. Use Custom Image on Server

Option 1: Modify `image` in `docker-compose.yml`.

```yaml
image: YOUR_DOCKERHUB_NAME/qwen2api:latest
```

Option 2: Keep the default Compose file and override the image via `.env` or command line.

```env
QWEN2API_IMAGE=YOUR_DOCKERHUB_NAME/qwen2api:latest
```

Then run on the server:

```bash
docker compose pull
docker compose up -d
```

### Option C: Local Source Code Development

Suitable for development, debugging, and modifying the frontend pages. Not recommended as a production deployment method.

Environment requirements:

- Python 3.10+
- Node.js 20+
- Access to Python and npm dependency sources
- Access to Camoufox browser kernel download source

One-click development startup:

```bash
git clone https://github.com/YuJunZhiXue/qwen2API.git
cd qwen2API
python start.py
```

`start.py` will install backend dependencies, check Camoufox, start the backend, and start the frontend Vite development server.

If you need to start them separately, see [Development Guide](#development-guide).

## Environment Variables

The project provides `.env.example`. It is recommended to copy it to `.env` before deployment and modify it accordingly.

| Variable | Recommended Value / Default | Description |
|---|---|---|
| `ADMIN_KEY` | `change-me-now` | Management console login key. Must be changed in production environments. |
| `QWEN_API_KEY` / `QWEN_API_KEYS` / `QWEN_API_KEY_1` | Empty | Inject client API keys from environment variables. Multiple keys can be separated by commas, semicolons, or whitespace, or provided with numbered variables. The WebUI lists environment keys first and labels their source. |
| `QWEN_ACCOUNT_1` / `QWEN_ACCOUNT_2` | Empty | Inject Qwen accounts from environment variables. Format: `token;optional-email;optional-password`; token is required, email and password are optional. |
| `KEEPALIVE_URL` | Empty | Keepalive URL. When set, the service periodically sends GET requests to this URL; empty disables keepalive. Environment variables lock the same WebUI setting. |
| `KEEPALIVE_INTERVAL` | `60` | Keepalive interval in seconds, range `5` - `86400`. Environment variables lock the same WebUI setting. |
| `PORT` | `7860` | Service port inside the container. Compose maps to host port `7860` by default. |
| `HOST_PORT` | `7860` | Compose-only; controls host port mapping. |
| `QWEN2API_IMAGE` | `yujunzhixue/qwen2api:latest` | Compose-only; overrides the image to pull. |
| `WORKERS` | `1` | Number of Uvicorn workers. Recommended to keep at `1` to avoid concurrent write conflicts with JSON data files. |
| `LOG_LEVEL` | `INFO` | Log level. Options: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `BROWSER_POOL_SIZE` | `1` | Browser page pool size. Keep at `1` when memory is limited. |
| `MAX_INFLIGHT_PER_ACCOUNT` | `2` | Maximum number of requests allowed to be processed simultaneously per Qwen account. |
| `MAX_RETRIES` | `3` | Maximum number of retries after upstream failure. |
| `ACCOUNT_MIN_INTERVAL_MS` | `0` | Minimum interval between two requests for the same account. |
| `REQUEST_JITTER_MIN_MS` | `0` | Minimum random jitter before a request, in milliseconds. |
| `REQUEST_JITTER_MAX_MS` | `0` | Maximum random jitter before a request, in milliseconds. |
| `RATE_LIMIT_BASE_COOLDOWN` | `600` | Base cooldown seconds after account rate limiting. |
| `RATE_LIMIT_MAX_COOLDOWN` | `3600` | Maximum cooldown seconds after account rate limiting. |
| `ACCOUNTS_FILE` | `/workspace/data/accounts.json` | Qwen account data file path. |
| `USERS_FILE` | `/workspace/data/users.json` | API key / user data file path. |
| `CAPTURES_FILE` | `/workspace/data/captures.json` | Debug capture data file path. |
| `CONFIG_FILE` | `/workspace/data/config.json` | Runtime config file path, used by WebUI keepalive settings. |
| `CONTEXT_GENERATED_DIR` | `/workspace/data/context_files` | Context attachment and generated file directory. |
| `CONTEXT_CACHE_FILE` | `/workspace/data/context_cache.json` | Context cache data file. |
| `UPLOADED_FILES_FILE` | `/workspace/data/uploaded_files.json` | Uploaded file metadata file. |
| `CONTEXT_AFFINITY_FILE` | `/workspace/data/session_affinity.json` | Session affinity data file. |
| `CONTEXT_INLINE_MAX_CHARS` | `4000` | Character limit for inlining small files into context. |
| `CONTEXT_FORCE_FILE_MAX_CHARS` | `10000` | Character limit allowed before forcing conversion to attachment. |
| `CONTEXT_ATTACHMENT_TTL_SECONDS` | `1800` | Attachment cache expiration time. |
| `CONTEXT_UPLOAD_PARSE_TIMEOUT_SECONDS` | `60` | Upload file parsing timeout. |

Compatibility note:

- `MAX_INFLIGHT` is a legacy compatibility alias and is not recommended for continued use.
- `QWEN_ACCOUNT_1=token` is the simplest account injection form. If email is omitted but password is provided, use `QWEN_ACCOUNT_1=token;;password`.

## WebUI Console

Access `http://127.0.0.1:7860/` and log in with `ADMIN_KEY`.

Main pages:

- Account Management: Add, register, activate, verify, and delete Qwen accounts.
- API Key Management: Create and delete keys for client calls, and show environment-injected keys.
- Chat Test: Select a model, switch thinking/fast mode, test streaming and non-streaming requests.
- Image Generation: Test the image generation interface and size ratios.
- System Settings: View runtime status, basic configuration, and keepalive settings.

Data persistence relies on the `data/` directory. This must be retained for Docker deployments:

```yaml
volumes:
  - ./data:/workspace/data
  - ./logs:/workspace/logs
```

## Client Integration Examples

### Claude Code

Claude Code is recommended to use the Anthropic-compatible entry point.

macOS / Linux:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:7860/anthropic
export ANTHROPIC_API_KEY=YOUR_API_KEY
claude --model claude-sonnet-4-6
```

Windows PowerShell:

```powershell
$env:ANTHROPIC_BASE_URL="http://127.0.0.1:7860/anthropic"
$env:ANTHROPIC_API_KEY="YOUR_API_KEY"
claude --model claude-sonnet-4-6
```

If your Claude Code version uses a configuration file, set the API address to:

```text
http://127.0.0.1:7860/anthropic
```

### Codex

Codex is recommended to use the OpenAI-compatible entry point.

macOS / Linux:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:7860/v1
export OPENAI_API_KEY=YOUR_API_KEY
codex --model qwen3.6-plus
```

Windows PowerShell:

```powershell
$env:OPENAI_BASE_URL="http://127.0.0.1:7860/v1"
$env:OPENAI_API_KEY="YOUR_API_KEY"
codex --model qwen3.6-plus
```

If your Codex version requires explicit provider configuration, select the OpenAI compatible provider and fill in:

```text
base_url = http://127.0.0.1:7860/v1
api_key = YOUR_API_KEY
model = qwen3.6-plus
```

### curl Smoke Test

```bash
curl http://127.0.0.1:7860/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "qwen3.6-plus",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": false,
    "enable_thinking": false
  }'
```

## FAQ

### Can the service start without `.env`?

Yes. In `docker-compose.yml`, `.env` is an optional file; missing it will use the image defaults. However, in production environments, you must set your own `ADMIN_KEY`.

### What should I do first after logging into the management console?

First add or register Qwen accounts, then create a client API key. Clients should not use `ADMIN_KEY` directly as a long-term call key.

### `/v1/models` returns empty or a fallback list

This usually means the account pool temporarily has no available Qwen accounts, or the upstream model list request failed. First confirm the account status in the WebUI, then call `/v1/models` again.

### Image generation returns 500 or no URL

First confirm that the same Qwen account can generate images normally on the web side, then confirm the request uses a supported size or ratio. Search for `[T2I]` and `[T2I-SSE]` in the server-side logs.

### What to do if port 7860 is occupied?

For Docker deployments, simply change the host port:

```env
HOST_PORT=8080
```

Then access `http://127.0.0.1:8080/`.

### What to do if an amd64 server pulls an arm64 image?

Use the `yujunzhixue/qwen2api:latest` published by this repository — it is a multi-architecture image. If you build your own image, use buildx to push both `linux/amd64` and `linux/arm64` simultaneously.

### WebUI shows a blank page or static resources return 404

Confirm that you are using the complete Docker image, or that the frontend build has been completed when running from local source code. Force-refresh the browser if it has cached old resources.

### Accounts are frequently rate-limited

Reduce single-account concurrency, increase request intervals, or add more accounts in the management console. Priority adjustments:

```env
MAX_INFLIGHT_PER_ACCOUNT=1
ACCOUNT_MIN_INTERVAL_MS=1200
RATE_LIMIT_BASE_COOLDOWN=1200
```

## Troubleshooting Reference

| Symptom | Common Cause | Resolution |
|---|---|---|
| Container exits immediately after startup | Port conflict, environment variable error, data directory permission issue | Check `docker compose logs -f`, confirm `HOST_PORT`, `data/`, and `logs/`. |
| `/healthz` unreachable | Service not fully started or container abnormal | Check `docker compose ps` first, then container logs. |
| `/readyz` fails | Account pool not loaded or upstream status unavailable | Log in to WebUI and check account status. |
| `/v1/chat/completions` returns 401 | API key error or client key not created | Create an API key in WebUI and call with `Authorization: Bearer YOUR_API_KEY`. |
| `/v1/models` only shows a few compatibility models | Upstream model list temporarily unavailable | Check whether accounts are available and retry later. |
| Image generation returns no result | Account does not support images, upstream did not return URL, size parameter inappropriate | Test first on WebUI image page, then check logs for `[T2I]`. |
| Very slow responses | Upstream queuing, account cooling, insufficient server resources | Reduce concurrency, add accounts, check CPU/memory. |
| Browser-related errors | Insufficient shared memory or missing runtime dependencies | Keep `shm_size: "512m"` for Docker deployments; prefer pre-built images. |
| Accounts or keys lost after restart | `data/` not mounted | Confirm `./data:/workspace/data` exists in Compose. |

## Development Guide

### Backend Local Startup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
python -m camoufox fetch
uvicorn backend.main:app --reload --host 0.0.0.0 --port 7860
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
python -m camoufox fetch
uvicorn backend.main:app --reload --host 0.0.0.0 --port 7860
```

### Frontend Local Startup

```bash
npm --prefix frontend install
npm --prefix frontend run dev
```

The frontend development server proxies to the backend via Vite configuration; the backend defaults to `http://127.0.0.1:7860`.

### Frontend Build

```bash
npm --prefix frontend run typecheck
npm --prefix frontend run build
```

Build artifacts are located in `frontend/dist`. When the backend starts, if this directory is detected, it will be hosted as WebUI static resources.

### Backend Syntax Check

```bash
python -m compileall backend
```

### Routing Development Conventions

- OpenAI-compatible entry points go in `backend/api/v1_chat.py`, `backend/api/responses.py`, `backend/api/models.py`, `backend/api/images.py`, `backend/api/files_api.py`.
- Anthropic-compatible entry points go in `backend/api/anthropic.py`.
- Gemini-compatible entry points go in `backend/api/gemini.py`.
- Admin console interfaces go in `backend/api/admin.py`.
- Complex business logic goes in `backend/services/` or `backend/runtime/`; do not pile it in the API layer.
- Configuration items are uniformly read from `backend/core/config.py`, and `.env.example` and README should be updated in sync.

### Docker Image Development

For local single-architecture builds:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

For publishing multi-architecture images, use the buildx command in [Option B](#option-b-local-buildx-push-multi-architecture-image).

## License and Disclaimer

This project is intended for protocol compatibility, interface conversion, automated testing, and personal technical research. The project itself does not provide any officially authorized Tongyi Qianwen commercial interface services.

Disclaimer:

- This project has no affiliation, agency, or commercial partnership with Alibaba Cloud, Tongyi Qianwen, or related official services.
- This project is not an official product and does not constitute any official service commitment.
- Users should independently evaluate the laws and regulations of their region, upstream service terms, account compliance, and data security requirements.
- Risks arising from the use of this project, including account bans, request restrictions, data loss, service interruptions, or other risks, are borne by the user.
- If rights holders believe that the content of this project infringes their legitimate rights and interests, please raise the issue via the repository's Issue tracker, and the maintainers will handle it after verification.
