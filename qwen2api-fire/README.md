# Qwen2API Fire - Solução Híbrida PHP + Node.js

## 🚀 Visão Geral
Arquitetura híbrida que combina a **facilidade de hospedagem do PHP** (hospedagem compartilhada) com o **poder de automação do Node.js** (VPS barata).

### Arquitetura
```
┌─────────────────────────────────────────────────────────────┐
│  HOSPEDAGEM COMPARTILHADA (R$ 20/mês)                       │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  BACKEND PHP (Laravel/Slim)                           │  │
│  │  • Painel Administrativo                              │  │
│  │  • Gestão de Usuários & Auth                          │  │
│  │  • Banco de Dados MySQL                               │  │
│  │  • APIs OpenAI/Anthropic/Gemini (proxy)               │  │
│  │  • Gestão de Contas Qwen                              │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                            ↕ HTTP/JSON (cURL)
┌─────────────────────────────────────────────────────────────┐
│  VPS BARATA (R$ 30/mês)                                     │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  SERVICE NODE.JS (Express/Fastify)                    │  │
│  │  • Controle de Navegador (Playwright)                 │  │
│  │  • Pool de Sessões Qwen                               │  │
│  │  • Auto-Login & Token Refresh                         │  │
│  │  • SSE Streaming para PHP                             │  │
│  │  • Tool Calling Parser                                │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## 📁 Estrutura de Arquivos

```
qwen2api-fire/
├── backend-php/              # Hospedagem Compartilhada
│   ├── public/
│   │   └── index.php        # Entry point
│   ├── src/
│   │   ├── Controllers/
│   │   │   ├── ChatController.php
│   │   │   ├── AccountController.php
│   │   │   └── AdminController.php
│   │   ├── Services/
│   │   │   ├── NodeService.php      # Comunicação com Node
│   │   │   ├── AuthService.php
│   │   │   └── QuotaService.php
│   │   ├── Models/
│   │   │   ├── User.php
│   │   │   └── Account.php
│   │   └── Middleware/
│   │       └── AuthMiddleware.php
│   ├── config/
│   │   └── database.php
│   ├── vendor/              # Composer dependencies
│   └── composer.json
│
├── service-node/            # VPS Barata
│   ├── src/
│   │   ├── index.ts         # Entry point
│   │   ├── server.ts        # Fastify server
│   │   ├── services/
│   │   │   ├── BrowserPool.ts
│   │   │   ├── QwenClient.ts
│   │   │   ├── ToolParser.ts
│   │   │   └── StreamHandler.ts
│   │   ├── utils/
│   │   │   └── logger.ts
│   │   └── types/
│   │       └── index.ts
│   ├── package.json
│   ├── tsconfig.json
│   └── .env.example
│
├── docker/
│   ├── docker-compose.yml   # Deploy opcional do Node
│   └── node.Dockerfile
│
└── docs/
    ├── INSTALL.md           # Guia de instalação
    ├── API.md               # Documentação das APIs
    └── DEPLOY.md            # Guia de deploy
```

## 💰 Custo Mensal Estimado

| Componente | Serviço | Custo |
|------------|---------|-------|
| Backend PHP | Hostgator/Hostinger (Compartilhada) | R$ 20-30 |
| Service Node | Contabo/Hetzner (VPS 2GB) | R$ 25-35 |
| Domínio | Registro.br | R$ 5 |
| **Total** | | **R$ 50-70/mês** |

## 🔧 Tecnologias

### Backend PHP
- **Framework**: Slim 4 (leve) ou Laravel (completo)
- **HTTP Client**: Guzzle
- **Banco de Dados**: MySQL/MariaDB
- **Cache**: Redis (opcional)
- **Autenticação**: JWT

### Service Node.js
- **Runtime**: Node.js 20+
- **Framework**: Fastify (performance) ou Express
- **Browser**: Playwright (Chromium)
- **Streaming**: Server-Sent Events (SSE)
- **TypeScript**: 5+
- **Logger**: Pino

## 🔄 Fluxo de Requisição

1. **Cliente** → API PHP (`POST /v1/chat/completions`)
2. **PHP** → Valida auth, verifica quota, log no DB
3. **PHP** → Encaminha request para Node via `cURL POST http://node:3000/chat`
4. **Node** → Seleciona conta do pool (least-used)
5. **Node** → Controla navegador via Playwright
6. **Node** → Envia prompt para Qwen (chat.qwen.ai)
7. **Qwen** → Responde com SSE stream
8. **Node** → Parseia stream, detecta tool calls
9. **Node** → Re-envia stream para PHP via chunked transfer
10. **PHP** → Repassa stream para cliente final
11. **Cliente** → Recebe resposta em tempo real
12. **PHP** → Atualiza quota após conclusão

## 📦 Instalação Rápida

### Backend PHP (Hospedagem Compartilhada)
```bash
cd backend-php
composer install
cp .env.example .env
# Configurar .env com URL do Node e DB
```

### Service Node (VPS)
```bash
cd service-node
npm install
npm run build
npm start
# Ou usar PM2: pm2 start dist/index.js --name qwen-node
```

## 🔐 Segurança

- **API Key**: PHP valida antes de encaminhar para Node
- **Rate Limiting**: Implementado no PHP (Redis/MySQL)
- **CORS**: Configurado no Node para aceitar apenas PHP
- **SSL**: HTTPS em ambos (Let's Encrypt gratuito)
- **Isolamento**: Node em rede privada (firewall)

## 🎯 Vantagens desta Abordagem

✅ **Hospedagem barata**: PHP em compartilhada (R$ 20)  
✅ **Escalabilidade**: Node pode ser escalado independentemente  
✅ **Manutenção fácil**: PHP atualiza sem reiniciar Node  
✅ **Performance**: Node lida com I/O intensivo (browser)  
✅ **Flexibilidade**: Pode migrar Node para serverless depois  
✅ **Backup simples**: DB MySQL em hospedagem comum  

## ⚠️ Considerações

- **Latência**: +10-50ms devido à comunicação PHP↔Node
- **Complexidade**: Dois serviços para gerenciar
- **Monitoramento**: Logs separados (PHP + Node)
- **Deploy**: Dois ambientes diferentes

## 📞 Suporte

Para dúvidas, consulte a documentação em `/docs/` ou abra issues no repositório.
