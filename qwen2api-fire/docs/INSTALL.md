# Guia de Instalação - Qwen2API Fire

## Pré-requisitos

### Backend PHP (Hospedagem Compartilhada)
- PHP 8.1+
- Composer
- MySQL/MariaDB
- Extensões: PDO, curl, json, mbstring

### Service Node (VPS)
- Node.js 20+
- npm ou yarn
- 2GB RAM mínimo
- Docker (opcional para deploy containerizado)

---

## 1. Instalação do Backend PHP

### Passo 1: Upload para hospedagem
```bash
# Copiar arquivos para hospedagem compartilhada
cd backend-php
scp -r * usuario@servidor:/public_html/qwen-api/
```

### Passo 2: Instalar dependências
```bash
cd /public_html/qwen-api
composer install --no-dev --optimize-autoloader
```

### Passo 3: Configurar banco de dados
```sql
CREATE DATABASE qwen2api CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE qwen2api;

CREATE TABLE users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    api_key VARCHAR(255) UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE user_quotas (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    model VARCHAR(100) NOT NULL,
    daily_limit INT DEFAULT 100000,
    daily_used INT DEFAULT 0,
    monthly_limit INT DEFAULT 2000000,
    monthly_used INT DEFAULT 0,
    reset_daily_at TIMESTAMP,
    reset_monthly_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE request_logs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    model VARCHAR(100),
    input_tokens INT,
    output_tokens INT,
    status VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX idx_user_model ON user_quotas(user_id, model);
CREATE INDEX idx_created_at ON request_logs(created_at);
```

### Passo 4: Configurar variáveis de ambiente
```bash
cp .env.example .env
nano .env
```

**.env:**
```env
DB_DSN=mysql:host=localhost;dbname=qwen2api;charset=utf8mb4
DB_USER=usuario_db
DB_PASS=senha_db

NODE_SERVICE_URL=http://ip-da-vps:3000
NODE_API_KEY=sua-chave-secreta-muito-forte

APP_DEBUG=false
APP_ENV=production
```

### Passo 5: Configurar web server

**Apache (.htaccess):**
```apache
RewriteEngine On
RewriteCond %{REQUEST_FILENAME} !-f
RewriteCond %{REQUEST_FILENAME} !-d
RewriteRule ^(.*)$ index.php [QSA,L]

# Headers para SSE
<IfModule mod_headers.c>
    Header set Access-Control-Allow-Origin "*"
    Header set Access-Control-Allow-Methods "GET, POST, OPTIONS"
    Header set Access-Control-Allow-Headers "Content-Type, Authorization"
</IfModule>
```

**Nginx:**
```nginx
server {
    listen 80;
    server_name sua-api.com;
    root /var/www/qwen-api/public;
    index index.php;

    location / {
        try_files $uri $uri/ /index.php?$query_string;
    }

    location ~ \.php$ {
        fastcgi_pass unix:/var/run/php/php8.1-fpm.sock;
        fastcgi_index index.php;
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
        
        # Importante para SSE streaming
        fastcgi_buffering off;
        fastcgi_cache off;
    }

    # CORS headers
    add_header Access-Control-Allow-Origin * always;
    add_header Access-Control-Allow-Methods "GET, POST, OPTIONS" always;
    add_header Access-Control-Allow-Headers "Content-Type, Authorization" always;
}
```

---

## 2. Instalação do Service Node (VPS)

### Opção A: Instalação Direta

#### Passo 1: Instalar Node.js
```bash
# Ubuntu/Debian
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

# Verificar instalação
node -v  # v20.x.x
npm -v   # 10.x.x
```

#### Passo 2: Instalar dependências do sistema (Playwright)
```bash
sudo apt-get update
sudo apt-get install -y \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2
```

#### Passo 3: Configurar aplicação
```bash
cd /opt/qwen2api-fire/service-node
npm install
cp .env.example .env
nano .env
```

**.env:**
```env
NODE_API_KEY=sua-chave-secreta-muito-forte
PORT=3000
BROWSER_POOL_SIZE=2
MAX_INFLIGHT_PER_BROWSER=1
BROWSER_TIMEOUT_MS=300000
QWEN_BASE_URL=https://chat.qwen.ai
DEFAULT_MODEL=qwen3.6-plus
LOG_LEVEL=info
```

#### Passo 4: Build e instalar Playwright
```bash
npm run build
npx playwright install chromium --with-deps
```

#### Passo 5: Usar PM2 para gerenciar processo
```bash
npm install -g pm2

pm2 start dist/index.js --name qwen-node
pm2 save
pm2 startup systemd
```

### Opção B: Docker (Recomendado)

```bash
cd /opt/qwen2api-fire
docker-compose up -d --build

# Verificar logs
docker-compose logs -f node-service

# Verificar saúde
curl http://localhost:3000/health
```

---

## 3. Configurar Firewall

### VPS (Node Service)
```bash
# Permitir apenas IP da hospedagem PHP
sudo ufw allow from IP_HOSPEDAGEM_PHP to any port 3000
sudo ufw enable
```

### Hospedagem PHP
```bash
# Permitir saída para VPS
# (Geralmente já permitido em hospedagens compartilhadas)
```

---

## 4. Testar Instalação

### Health Check
```bash
# Testar Node
curl http://IP_VPS:3000/health

# Testar PHP
curl https://sua-api.com/health
```

### Testar Chat API
```bash
curl -X POST https://sua-api.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sua-api-key" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "Olá!"}],
    "stream": false
  }'
```

### Testar Streaming
```bash
curl -X POST https://sua-api.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sua-api-key" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "Conte uma história"}],
    "stream": true
  }'
```

---

## 5. Monitoramento

### Logs PHP
```bash
# Apache
tail -f /var/log/apache2/error.log

# Nginx + PHP-FPM
tail -f /var/log/nginx/error.log
tail -f /var/log/php8.1-fpm.log
```

### Logs Node
```bash
# PM2
pm2 logs qwen-node

# Docker
docker-compose logs -f node-service
```

### Métricas
```bash
# Status PM2
pm2 status

# Uso de memória
pm2 monit
```

---

## 6. Troubleshooting

### Erro: "Cannot connect to Node service"
- Verificar se Node está rodando: `pm2 status` ou `docker ps`
- Verificar firewall: `ufw status`
- Testar conexão direta: `curl http://localhost:3000/health`

### Erro: "Browser pool full"
- Aumentar `BROWSER_POOL_SIZE` no .env
- Diminuir `MAX_INFLIGHT_PER_BROWSER`
- Verificar vazamento de sessões

### Erro: "Quota exceeded"
- Verificar tabela `user_quotas` no MySQL
- Ajustar limites conforme necessário

---

## 7. Atualização

### Backend PHP
```bash
cd /public_html/qwen-api
git pull
composer install --no-dev --optimize-autoloader
```

### Service Node
```bash
cd /opt/qwen2api-fire/service-node
git pull
npm install
npm run build
pm2 restart qwen-node
```

---

## Suporte

Para issues, consulte a documentação ou abra um ticket no repositório.
