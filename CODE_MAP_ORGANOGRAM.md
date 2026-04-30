# 🗺️ MAPA/ORGANOGRAMA DO CÓDIGO - qwen2API Enterprise Gateway

## 📊 VISÃO GERAL DA ARQUITETURA

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         CLIENTES EXTERNOS                                │
│         (OpenAI SDK / Anthropic SDK / Gemini SDK / HTTP Direct)         │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      FASTAPI APPLICATION (main.py)                      │
│  • Lifespan Management (startup/shutdown)                               │
│  • CORS Middleware                                                      │
│  • Static Files (Frontend React)                                        │
│  • Inicialização de Componentes Core                                    │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        │                           │                           │
        ▼                           ▼                           ▼
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────────┐
│   API ROUTERS    │    │   CORE LAYER     │    │   SERVICES LAYER     │
│                  │    │                  │    │                      │
│ • v1_chat.py     │    │ • account_pool   │    │ • qwen_client        │
│ • anthropic.py   │    │ • browser_engine │    │ • auth_resolver      │
│ • gemini.py      │    │ • httpx_engine   │    │ • tool_parser        │
│ • images.py      │    │ • hybrid_engine  │    │ • token_calc         │
│ • embeddings.py  │    │ • database       │    │ • prompt_builder     │
│ • admin.py       │    │ • config         │    │ • garbage_collector  │
│ • probes.py      │    │                  │    │                      │
└──────────────────┘    └──────────────────┘    └──────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    QWEN.AI API (https://chat.qwen.ai)                   │
│                        (通义千问 - Alibaba Cloud)                        │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 🏗️ DETALHAMENTO POR CAMADA

### 1️⃣ ENTRY POINT & LIFESPAN
**Arquivo:** `backend/main.py` (103 linhas)

**Responsabilidades:**
- Configurar logging UTF-8
- Inicializar FastAPI com lifespan context
- Criar instâncias de:
  - `AsyncJsonDB` (accounts, users, captures)
  - `BrowserEngine` (pool de browsers Camoufox)
  - `HttpxEngine` (HTTP direto)
  - `HybridEngine` (combina os dois acima)
  - `AccountPool` (gerenciamento de contas)
  - `QwenClient` (cliente principal)
- Iniciar garbage collector de chats antigos
- Montar rotas da API
- Servir frontend estático (produção)

**Fluxo de Startup:**
```
main.py → lifespan() 
   ├─ AsyncJsonDB (3 arquivos JSON)
   ├─ BrowserEngine (pool_size=2)
   ├─ HttpxEngine (base_url=chat.qwen.ai)
   ├─ HybridEngine (browser + httpx)
   ├─ AccountPool (max_inflight=1)
   ├─ QwenClient (engine + pool)
   ├─ account_pool.load()
   ├─ engine.start()
   └─ garbage_collect_chats (background task)
```

---

### 2️⃣ API ROUTERS (7 endpoints principais)

#### 🔹 OpenAI Compatible (`api/v1_chat.py` - 644 linhas)
**Endpoints:**
- `POST /v1/chat/completions`
- `POST /chat/completions`
- `POST /completions`

**Funcionalidades:**
- Parse de mensagens (texto, imagens, tool_results)
- Detecção automática T2I (text-to-image) e T2V (text-to-video)
- Tool calling avançado com parse de `##TOOL_CALL##`
- Stream SSE com keepalive
- Cálculo de tokens (prompt + completion + total)
- Retry com failover entre contas
- Bloqueio de tool calls duplicados

**Fluxo:**
```
Request → Auth Check → Quota Validation → Model Resolve 
   → Detect Media Intent (t2i/t2v/t2t)
   → Build Prompt (messages_to_prompt)
   → Inject Tool Format Reminder
   → Acquire Account from Pool
   → QwenClient.chat_stream_events_with_retry()
   → Parse Tool Calls (se houver)
   → Stream Response (SSE)
   → Update Quota & Release Account
```

#### 🔹 Anthropic Compatible (`api/anthropic.py`)
**Endpoints:**
- `POST /anthropic/v1/messages`
- `GET /anthropic/v1/models`

**Características:**
- Formato Claude-native (system, messages, tools)
- Tool blocks nativos (`type: tool_use`, `type: tool_result`)
- Stream com eventos `content_block_delta`

#### 🔹 Gemini Compatible (`api/gemini.py`)
**Endpoints:**
- `POST /v1beta/models/{model}:streamGenerateContent`

**Características:**
- Formato Gemini-native (contents, generationConfig)
- Suporte a partes múltiplas (texto + imagem)

#### 🔹 Images (`api/images.py`)
**Endpoints:**
- `POST /v1/images/generations`

**Modelo:** `wanx2.1-t2i-plus` (Wanx Alibaba)

#### 🔹 Embeddings (`api/embeddings.py`)
**Endpoints:**
- `POST /v1/embeddings`

#### 🔹 Admin (`api/admin.py`)
**Endpoints:**
- `/api/admin/*` (dashboard, accounts, tokens, stats)

#### 🔹 Probes (`api/probes.py`)
**Endpoints:**
- `/health`, `/ready` (health checks)

---

### 3️⃣ CORE LAYER (Coração do Sistema)

#### 🔸 Account Pool (`core/account_pool.py` - 239 linhas)
**Classes:**
- `Account`: Representa uma conta Qwen
  - Campos: email, password, token, cookies, username
  - Estado: valid, activation_pending, rate_limited_until
  - Métricas: inflight, last_used, consecutive_failures
  - Métodos: `is_available()`, `is_rate_limited()`, `next_available_at()`

- `AccountPool`: Gerencia pool de contas
  - Load/Save assíncrono (JSON DB)
  - `acquire()`: Pega conta disponível (menor inflight, mais antiga)
  - `acquire_wait(timeout)`: Aguarda conta ficar disponível
  - `release()`: Libera conta após uso
  - `mark_invalid()`, `mark_success()`, `mark_rate_limited()`
  - Sticky email para sessões consecutivas

**Algoritmo de Seleção:**
```python
1. Filtra contas válidas e não rate-limited
2. Filtra contas com inflight < max_inflight
3. Filtra contas que passaram do min_interval
4. Ordena por: (inflight, last_request_started, last_used)
5. Retorna a primeira (menor carga)
```

#### 🔸 Browser Engine (`core/browser_engine.py` - 274 linhas)
**Tecnologia:** Camoufox (Firefox fingerprinting)

**Configurações Anti-Detecção:**
- OS: Windows
- Locale: zh-CN
- WebRender software
- Humanize delays
- No hardware video decoding

**Métodos Principais:**
- `start()`: Inicia pool de browsers
- `stop()`: Fecha todos browsers
- `api_call(method, path, token, body)`: Request HTTP via browser fetch()
- `fetch_chat(token, chat_id, payload, buffered)`: Stream completo

**JS Injection:**
- `JS_FETCH`: Request síncrono
- `JS_STREAM_CHUNKED`: Stream com chunks (envia para `window.send_chunk`)
- `JS_STREAM_FULL`: Stream bufferizado completo

#### 🔸 HTTPX Engine (`core/httpx_engine.py`)
**Propósito:** Requests HTTP diretos (mais rápido, menos stealth)

**Uso:**
- Primary para `api_call` no modo hybrid
- Fallback para `fetch_chat` quando browser falha

#### 🔸 Hybrid Engine (`core/hybrid_engine.py` - 123 linhas)
**Estratégia Inteligente:**
- `api_call`: **httpx primeiro** → fallback browser se (401, 403, 429, WAF)
- `fetch_chat`: **browser primeiro** → fallback httpx se erro

**Detecção de Falha:**
```python
should_fallback = (
    status == 0 or
    status in (401, 403, 429) or
    "waf" in body_text or
    "<!doctype" in body_text or
    "forbidden" in body_text
)
```

#### 🔸 Database (`core/database.py`)
**Tipo:** AsyncJsonDB (JSON files assíncronos)

**Arquivos:**
- `data/accounts.json`: Contas upstream Qwen
- `data/users.json`: Usuários da API
- `data/captures.json`: Capturas de login

**Operações:**
- `load()`: Lê JSON do disco
- `save(data)`: Escreve JSON no disco
- Thread-safe com asyncio.Lock

#### 🔸 Config (`core/config.py` - 116 linhas)
**Settings (via .env):**
- `PORT=7860`, `WORKERS=3`, `ADMIN_KEY=admin`
- `ENGINE_MODE=hybrid` (httpx|browser|hybrid)
- `BROWSER_POOL_SIZE=2`, `MAX_INFLIGHT=1`
- `ACCOUNT_MIN_INTERVAL_MS=1200`
- `REQUEST_JITTER_MIN_MS=120`, `REQUEST_JITTER_MAX_MS=360`
- `RATE_LIMIT_BASE_COOLDOWN=600`, `MAX=3600`

**Model Map:**
- Mapeia **todos** modelos OpenAI/Claude/Gemini → `qwen3.6-plus`
- Ex: `gpt-4o`, `claude-3-5-sonnet`, `gemini-2.5-pro` → `qwen3.6-plus`

---

### 4️⃣ SERVICES LAYER

#### ⚙️ Qwen Client (`services/qwen_client.py`)
**Responsabilidade:** Orquestrar chamadas à API Qwen

**Métodos Chave:**
- `chat_stream_events_with_retry(model, prompt, has_custom_tools, exclude_accounts)`
  - Tenta com conta atual
  - Se falhar: mark_invalid + acquire nova conta + retry
  - Max retries configurável
- `check_quota(user_api_key)`: Valida quota do usuário
- `deduct_quota(user_api_key, amount)`: Deduz tokens usados

#### ⚙️ Auth Resolver (`services/auth_resolver.py` - 806 linhas)
**Funcionalidade CRÍTICA:** Auto-cura de tokens expirados

**Fluxo de Recuperação:**
```
1. Detecta token inválido (401/403)
2. Tenta login com email/password (browser)
   - Preenche formulário
   - Resolve captcha (se houver)
3. Se activation_pending:
   - Acessa email temporário (mail.chatgpt.org.uk)
   - Extrai link de ativação do iframe
   - Clica no link
4. Extrai token novo do localStorage
5. Atualiza conta no AccountPool
6. Retry da requisição original
```

**Funções Principais:**
- `_verify_qwen_token(token)`: Valida token via API /auths/
- `get_fresh_token(email, password)`: Login completo
- `_extract_verify_link_from_page(page)`: Scraping de email
- `_activate_account(verify_link)`: Ativa conta

#### ⚙️ Tool Parser (`services/tool_parser.py`)
**Desafio:** Qwen retorna tool calls em formato texto `##TOOL_CALL##`

**Solução:**
1. Regex para encontrar blocos `##TOOL_CALL##...##END_TOOL_CALL##`
2. Parse JSON interno (name, input)
3. Conversão para formato OpenAI/Claude
4. Correção automática de JSON inválido
5. Detecção de tool calls duplicados (loop prevention)

**Exemplo de Output:**
```json
{
  "id": "call_abc123",
  "type": "function",
  "function": {
    "name": "search_web",
    "arguments": "{\"query\": \"preço bitcoin\"}"
  }
}
```

#### ⚙️ Token Calculator (`services/token_calc.py`)
**Cálculo:**
- Prompt tokens: soma de mensagens (tiktoken-style)
- Completion tokens: resposta do modelo
- Total: prompt + completion

#### ⚙️ Prompt Builder (`services/prompt_builder.py`)
**Conversão:**
- OpenAI messages → Prompt format Qwen
- Injeção de system prompts
- Formatação de tool definitions

#### ⚙️ Garbage Collector (`services/garbage_collector.py`)
**Background Task:**
- Roda periodicamente
- Deleta chats antigos (> threshold)
- Libera recursos no Qwen

---

## 🔄 FLUXO COMPLETO DE UMA REQUISIÇÃO

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. CLIENT REQUEST                                               │
│    POST /v1/chat/completions                                    │
│    Body: {model: "gpt-4o", messages: [...], stream: true}       │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 2. AUTH & QUOTA (v1_chat.py)                                    │
│    - Validate API Key (Bearer token)                            │
│    - Check user quota remaining                                 │
│    - Reject if insufficient                                     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 3. MODEL RESOLVE (config.py)                                    │
│    gpt-4o → qwen3.6-plus                                        │
│    Detect T2I/T2V intent from messages                          │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 4. PROMPT BUILDING (prompt_builder.py)                          │
│    - Convert messages to Qwen format                            │
│    - Inject tool definitions (if any)                           │
│    - Add format reminder for tool calling                       │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 5. ACCOUNT ACQUISITION (account_pool.py)                        │
│    - Lock pool                                                    │
│    - Find available account (valid, not rate-limited)           │
│    - Sort by (inflight, last_used)                              │
│    - Increment inflight counter                                 │
│    - Set sticky_email if only one available                     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 6. ENGINE DISPATCH (hybrid_engine.py)                           │
│    api_call: httpx first → fallback browser                     │
│    fetch_chat: browser first → fallback httpx                   │
│                                                                  │
│    Detection: 401/403/429/WAF → trigger fallback                │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 7. QWEN API CALL (browser_engine.py / httpx_engine.py)          │
│    - Browser: page.evaluate(JS_STREAM_CHUNKED)                  │
│    - HTTPX: client.post() with streaming                        │
│    - Timeout: 30min (1800000ms)                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 8. STREAM PROCESSING                                            │
│    - Receive chunks from Qwen                                   │
│    - Buffer and parse for tool calls                            │
│    - Keepalive every 5s (prevent timeout)                       │
│    - Yield SSE events to client                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 9. TOOL PARSING (tool_parser.py) [if applicable]                │
│    - Detect ##TOOL_CALL## blocks                                │
│    - Parse JSON (handle malformed)                              │
│    - Resolve tool name mapping                                  │
│    - Check for duplicate calls (loop prevention)                │
│    - Convert to OpenAI/Claude format                            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 10. RESPONSE ASSEMBLY                                           │
│     - Build OpenAI-compatible SSE stream                        │
│     - Include usage: {prompt_tokens, completion_tokens, total}  │
│     - Send finish_reason: stop/tool_calls                       │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 11. POST-PROCESSING                                             │
│     - Deduct user quota                                         │
│     - Mark account success (reset failure counters)             │
│     - Release account (decrement inflight)                      │
│     - Notify waiters in queue                                   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 12. ERROR HANDLING [if failed]                                  │
│     - Catch exception                                           │
│     - Mark account invalid/rate_limited                         │
│     - Acquire new account (exclude failed)                      │
│     - Retry (max_retries=2)                                     │
│     - If all retries fail → return error to client              │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🛡️ MECANISMOS DE RESILIÊNCIA

### 1. **Failover Automático**
- Hybrid Engine detecta erros (401, 403, 429, WAF)
- Troca automaticamente de httpx ↔ browser
- Troca de conta no Account Pool

### 2. **Rate Limiting Inteligente**
- Base cooldown: 600s (10min)
- Exponencial: 600s → 1200s → 2400s → 3600s (max)
- Jitter aleatório para evitar padrões

### 3. **Request Throttling**
- Min interval entre requests: 1200ms
- Jitter: 120-360ms aleatório
- Max inflight por conta: 1 (configurável)

### 4. **Auto-Healing de Auth**
- Detecta token expirado
- Re-login automático com credenciais
- Leitura de email de ativação
- Renovação sem intervenção manual

### 5. **Keepalive em Streams**
- Envia evento `{type: "keepalive"}` a cada 5s
- Previne timeout de proxies/load balancers

### 6. **Tool Call Loop Prevention**
- Detecta chamadas idênticas repetidas
- Bloqueia após 2 ocorrências
- Mensagem de erro clara

---

## 📁 ESTRUTURA DE ARQUIVOS

```
/workspace
├── backend/
│   ├── main.py                      # Entry point FastAPI
│   ├── requirements.txt             # Dependencies
│   ├── accounts.json                # (legacy)
│   │
│   ├── api/                         # API Routers
│   │   ├── v1_chat.py               # OpenAI compatible (644 lines)
│   │   ├── anthropic.py             # Claude compatible
│   │   ├── gemini.py                # Gemini compatible
│   │   ├── images.py                # Image generation
│   │   ├── embeddings.py            # Embeddings
│   │   ├── admin.py                 # Dashboard API
│   │   └── probes.py                # Health checks
│   │
│   ├── core/                        # Core Engine
│   │   ├── config.py                # Settings & Model Map
│   │   ├── database.py              # AsyncJsonDB
│   │   ├── account_pool.py          # Account management
│   │   ├── browser_engine.py        # Camoufox browser
│   │   ├── httpx_engine.py          # HTTP direct
│   │   └── hybrid_engine.py         # Smart routing
│   │
│   └── services/                    # Business Logic
│       ├── qwen_client.py           # Main orchestrator
│       ├── auth_resolver.py         # Auto-healing (806 lines)
│       ├── tool_parser.py           ##TOOL_CALL## parsing
│       ├── token_calc.py            # Token counting
│       ├── prompt_builder.py        # Message conversion
│       └── garbage_collector.py     # Chat cleanup
│
├── data/                            # Persistent Storage
│   ├── accounts.json                # Upstream accounts
│   ├── users.json                   # API users
│   ├── captures.json                # Login captures
│   ├── config.json                  # App config
│   └── api_keys.json                # Managed keys
│
├── frontend/                        # React Dashboard
│   ├── src/
│   │   ├── pages/
│   │   │   ├── Dashboard.tsx
│   │   │   ├── Accounts.tsx
│   │   │   ├── Tokens.tsx
│   │   │   ├── Settings.tsx
│   │   │   └── Test.tsx
│   │   └── components/              # Shadcn UI
│   └── dist/                        # Production build
│
├── start.py                         # Launcher script
└── docker-compose.yml               # Docker deployment
```

---

## 📊 MÉTRICAS DE CÓDIGO

| Componente | Arquivo | Linhas | Complexidade |
|------------|---------|--------|--------------|
| **Entry Point** | main.py | 103 | Baixa |
| **API Routers** | v1_chat.py | 644 | Alta |
| | anthropic.py | ~150 | Média |
| | gemini.py | ~120 | Média |
| | images.py | ~80 | Baixa |
| | embeddings.py | ~60 | Baixa |
| | admin.py | ~200 | Média |
| **Core** | account_pool.py | 239 | Média-Alta |
| | browser_engine.py | 274 | Alta |
| | hybrid_engine.py | 123 | Média |
| | httpx_engine.py | ~100 | Baixa |
| | database.py | ~80 | Baixa |
| | config.py | 116 | Baixa |
| **Services** | auth_resolver.py | 806 | Muito Alta |
| | tool_parser.py | ~250 | Alta |
| | qwen_client.py | ~200 | Média-Alta |
| | token_calc.py | ~50 | Baixa |
| | prompt_builder.py | ~100 | Média |
| **Total Backend** | **~3.500+** | **Complexidade Geral: Alta** |

---

## 🔑 PONTOS CRÍTICOS DE IMPLEMENTAÇÃO

### 1. **Browser Automation (Camoufox)**
- **Desafio:** Manter sessions reais de browser
- **Solução:** Pool de browsers Firefox com fingerprinting
- **Alternativa PHP:** Puppeteer via bridge ou Guzzle com headers cuidadosos

### 2. **Async Streaming**
- **Python:** asyncio + async generators
- **Complexidade:** Alta (manage queues, timeouts, cancellation)
- **PHP:** ReactPHP ou Swoole necessário

### 3. **Auth Auto-Healing**
- **Mais complexo:** 806 linhas de lógica
- Envolve: login, captcha, email scraping, activation
- **PHP:** Viável mas exigiria mais código boilerplate

### 4. **Tool Call Parsing**
- **Desafio:** Qwen retorna texto, não JSON estruturado
- **Solução:** Regex + JSON repair + fuzzy matching
- **PHP:** Similar complexidade (regex + json_decode)

### 5. **Account Pool Concurrency**
- **Python:** asyncio.Lock + Queue
- **PHP:** Swoole channels ou Redis locks

---

## 🎯 CONCLUSÃO DA ANÁLISE

Este é um sistema **enterprise-grade** com:
- ✅ Multi-protocolo (OpenAI, Claude, Gemini)
- ✅ Resiliência extrema (failover, retry, healing)
- ✅ Anti-detecção sofisticada (browser fingerprinting)
- ✅ Gestão inteligente de quotas e rate limiting
- ✅ Tool calling avançado com correção automática
- ✅ Dashboard administrativo completo

**Nível de Maturidade:** Produção-ready para múltiplos usuários simultâneos.

**Pontos Fortes:**
1. Arquitetura em camadas bem definida
2. Separação clara de responsabilidades
3. Tratamento de erros abrangente
4. Configuração flexível via environment variables
5. Monitoramento via health probes e admin API

**Possíveis Melhorias:**
1. Adicionar métricas Prometheus
2. Cache de respostas frequentes
3. Rate limiting por IP além de por conta
4. Webhooks para eventos críticos
5. Backup automático dos JSON files
