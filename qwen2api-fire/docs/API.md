# API Documentation - Qwen2API Fire

## Endpoints Compatíveis

### OpenAI API (`/v1/*`)

#### POST /v1/chat/completions
**Compatível com:** SDK OpenAI, LangChain, Vercel AI, etc.

**Request:**
```json
{
  "model": "gpt-4o",
  "messages": [
    {"role": "system", "content": "Você é um assistente útil."},
    {"role": "user", "content": "Olá, como vai?"}
  ],
  "stream": false,
  "temperature": 0.7,
  "max_tokens": 2048
}
```

**Response (non-streaming):**
```json
{
  "id": "chatcmpl-123456",
  "object": "chat.completion",
  "created": 1704067200,
  "model": "qwen3.6-plus",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "Olá! Estou bem, obrigado por perguntar..."
    },
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 25,
    "completion_tokens": 42,
    "total_tokens": 67
  }
}
```

**Response (streaming):**
```
data: {"id":"chatcmpl-123","choices":[{"delta":{"content":"Olá"}}]}

data: {"id":"chatcmpl-123","choices":[{"delta":{"content":"!"}}]}

data: [DONE]
```

---

### Model Aliases Suportados

| Alias | Modelo Real |
|-------|-------------|
| `gpt-4o` | qwen3.6-plus |
| `gpt-4-turbo` | qwen3.6-plus |
| `gpt-3.5-turbo` | qwen3.6-plus |
| `claude-3-5-sonnet` | qwen3.6-plus |
| `claude-3-opus` | qwen3.6-plus |
| `gemini-pro` | qwen3.6-plus |
| `qwen3.6-plus` | qwen3.6-plus |
| `qwen-max` | qwen3.6-plus |

---

### GET /v1/models
Lista modelos disponíveis.

**Response:**
```json
{
  "data": [
    {"id": "qwen3.6-plus", "object": "model", "owned_by": "qwen"},
    {"id": "gpt-4o", "object": "model", "owned_by": "openai"},
    {"id": "claude-3-5-sonnet", "object": "model", "owned_by": "anthropic"}
  ]
}
```

---

### Anthropic API (`/anthropic/v1/*`)

#### POST /anthropic/v1/messages
**Compatível com:** SDK Anthropic/Claude

**Request:**
```json
{
  "model": "claude-3-5-sonnet",
  "messages": [
    {"role": "user", "content": "Explique quantum computing"}
  ],
  "max_tokens": 1024
}
```

---

### Gemini API (`/v1beta/*`)

#### POST /v1beta/models/{model}:generateContent
**Compatível com:** SDK Google Gemini

---

## Autenticação

Todas as requisições requerem header de autenticação:

```http
Authorization: Bearer sua-api-key
```

Ou para comunicação interna PHP↔Node:

```http
X-API-Key: node-service-secret-key
```

---

## Rate Limiting

Limites configuráveis por usuário no banco de dados:

| Tipo | Padrão |
|------|--------|
| Diário | 100.000 tokens |
| Mensal | 2.000.000 tokens |

**Resposta 429 (Quota Exceeded):**
```json
{
  "error": {
    "message": "Quota exceeded",
    "type": "quota_error",
    "daily_remaining": 0,
    "monthly_remaining": 150000
  }
}
```

---

## Tool Calling

Suporte a funções/tools via padrão `##TOOL_CALL##`:

**Request com tools:**
```json
{
  "model": "gpt-4o",
  "messages": [{"role": "user", "content": "Qual a previsão do tempo em SP?"}],
  "tools": [{
    "type": "function",
    "function": {
      "name": "get_weather",
      "parameters": {
        "type": "object",
        "properties": {
          "location": {"type": "string"}
        },
        "required": ["location"]
      }
    }
  }]
}
```

**Response com tool call:**
```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [{
        "id": "call_abc123",
        "type": "function",
        "function": {
          "name": "get_weather",
          "arguments": "{\"location\":\"São Paulo\"}"
        }
      }]
    },
    "finish_reason": "tool_calls"
  }]
}
```

---

## Health Check

### GET /health

**Response:**
```json
{
  "status": "ok",
  "node_service": "connected",
  "timestamp": "2024-01-01T12:00:00Z"
}
```

---

## Códigos de Erro

| Código | Significado |
|--------|-------------|
| 400 | Bad Request - JSON inválido |
| 401 | Unauthorized - API key inválida |
| 429 | Too Many Requests - Quota excedida |
| 500 | Internal Server Error |
| 503 | Service Unavailable - Node offline |

---

## Exemplos de Uso

### Python (OpenAI SDK)
```python
from openai import OpenAI

client = OpenAI(
    api_key="sua-api-key",
    base_url="https://sua-api.com/v1"
)

response = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Olá!"}]
)

print(response.choices[0].message.content)
```

### JavaScript (Vercel AI SDK)
```javascript
import { streamText } from 'ai';

const result = await streamText({
  model: customProvider('gpt-4o', {
    baseURL: 'https://sua-api.com/v1',
    apiKey: 'sua-api-key',
  }),
  messages: [{ role: 'user', content: 'Olá!' }],
});

for await (const chunk of result.textStream) {
  process.stdout.write(chunk);
}
```

### cURL
```bash
curl https://sua-api.com/v1/chat/completions \
  -H "Authorization: Bearer sua-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

---

## Streaming com Server-Sent Events (SSE)

O Qwen2API Fire suporta streaming nativo via SSE:

```javascript
const response = await fetch('https://sua-api.com/v1/chat/completions', {
  method: 'POST',
  headers: {
    'Authorization': 'Bearer sua-api-key',
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    model: 'gpt-4o',
    messages: [{ role: 'user', content: 'Conte uma história' }],
    stream: true,
  }),
});

const reader = response.body.getReader();
const decoder = new TextDecoder();

while (true) {
  const { done, value } = await reader.read();
  if (done) break;
  
  const chunk = decoder.decode(value);
  for (const line of chunk.split('\n')) {
    if (line.startsWith('data: ')) {
      const data = JSON.parse(line.slice(6));
      console.log(data.choices[0]?.delta?.content || '');
    }
  }
}
```

---

## Considerações de Performance

- **Latência típica:** 200-800ms (primeiro token)
- **Throughput:** ~50 tokens/segundo
- **Concorrência:** Configurar `BROWSER_POOL_SIZE` conforme RAM disponível
- **Timeout:** 120 segundos por requisição

Para alta concorrência, aumentar pool de browsers e usar múltiplas instâncias do Node service.
