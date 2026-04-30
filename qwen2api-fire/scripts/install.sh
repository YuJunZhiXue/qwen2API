#!/bin/bash

# Qwen2API Fire - Script de Instalação Automática
# Este script configura tanto o backend PHP quanto o serviço Node.js

set -e

echo "🔥 Qwen2API Fire - Instalação Automática"
echo "========================================"

# Cores
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Função para log
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Verifica se está na pasta correta
if [ ! -f "backend-php/composer.json" ] || [ ! -f "service-node/package.json" ]; then
    log_error "Execute este script a partir da raiz do projeto qwen2api-fire"
    exit 1
fi

# Cria arquivo .env se não existir
if [ ! -f ".env" ]; then
    log_info "Criando arquivo .env..."
    cat > .env << 'EOF'
# ===========================================
# Qwen2API Fire - Configurações
# ===========================================

# --- Backend PHP ---
PHP_DB_HOST=localhost
PHP_DB_NAME=qwen2api_fire
PHP_DB_USER=qwen_user
PHP_DB_PASS=change_this_password_123

# --- Serviço Node.js ---
NODE_SERVICE_URL=http://localhost:3001
NODE_SERVICE_KEY=internal_secure_key_change_me

# --- Configurações Gerais ---
APP_ENV=production
APP_DEBUG=false
LOG_LEVEL=info

# --- Rate Limiting ---
RATE_LIMIT_MAX=60
RATE_LIMIT_WINDOW=60

# --- Fila ---
QUEUE_MAX_CONCURRENT=5
QUEUE_MAX_SIZE=100

# --- Browser Pool (Node) ---
BROWSER_POOL_SIZE=2
MAX_INFLIGHT=1
ACCOUNT_MIN_INTERVAL_MS=1200

# --- Contas Qwen (Adicione suas contas aqui) ---
QWEN_ACCOUNTS=[]
EOF
    log_info ".env criado. Edite com suas credenciais!"
else
    log_warn ".env já existe. Pulando criação."
fi

# ============================================
# Backend PHP
# ============================================
log_info "Configurando Backend PHP..."

if command -v composer &> /dev/null; then
    cd backend-php
    log_info "Instalando dependências PHP..."
    composer install --no-interaction --optimize-autoloader
    cd ..
    log_info "Backend PHP configurado!"
else
    log_error "Composer não encontrado. Instale composer primeiro."
    exit 1
fi

# ============================================
# Serviço Node.js
# ============================================
log_info "Configurando Serviço Node.js..."

if command -v node &> /dev/null && command -v npm &> /dev/null; then
    cd service-node
    log_info "Instalando dependências Node.js..."
    npm install --production
    cd ..
    log_info "Serviço Node.js configurado!"
else
    log_error "Node.js ou npm não encontrados. Instale Node.js 18+ primeiro."
    exit 1
fi

# ============================================
# Banco de Dados
# ============================================
log_info "Configurando Banco de Dados..."

DB_HOST=${PHP_DB_HOST:-localhost}
DB_NAME=${PHP_DB_NAME:-qwen2api_fire}
DB_USER=${PHP_DB_USER:-qwen_user}
DB_PASS=${PHP_DB_PASS:-change_this_password_123}

# Cria script SQL
cat > /tmp/qwen2api_schema.sql << 'EOF'
CREATE DATABASE IF NOT EXISTS qwen2api_fire CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE qwen2api_fire;

-- Tabela de API Keys
CREATE TABLE IF NOT EXISTS api_keys (
    id INT AUTO_INCREMENT PRIMARY KEY,
    key_hash VARCHAR(255) NOT NULL UNIQUE,
    user_id VARCHAR(255),
    name VARCHAR(255),
    quota_daily INT DEFAULT 1000,
    quota_monthly INT DEFAULT 10000,
    usage_daily INT DEFAULT 0,
    usage_monthly INT DEFAULT 0,
    reset_daily_at TIMESTAMP NULL,
    reset_monthly_at TIMESTAMP NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_key (key_hash),
    INDEX idx_user (user_id)
);

-- Tabela de Contas Qwen
CREATE TABLE IF NOT EXISTS qwen_accounts (
    id INT AUTO_INCREMENT PRIMARY KEY,
    email VARCHAR(255) NOT NULL,
    password_encrypted VARCHAR(512),
    access_token TEXT,
    refresh_token TEXT,
    token_expires_at TIMESTAMP NULL,
    status ENUM('active', 'inactive', 'banned', 'needs_auth') DEFAULT 'active',
    last_used_at TIMESTAMP NULL,
    total_requests INT DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_email (email),
    INDEX idx_status (status)
);

-- Tabela de Rate Limits
CREATE TABLE IF NOT EXISTS rate_limits (
    id INT AUTO_INCREMENT PRIMARY KEY,
    ip_hash VARCHAR(255) NOT NULL,
    timestamp INT NOT NULL,
    INDEX idx_ip (ip_hash),
    INDEX idx_timestamp (timestamp)
);

-- Tabela de Request Queue
CREATE TABLE IF NOT EXISTS request_queue (
    id INT AUTO_INCREMENT PRIMARY KEY,
    api_key VARCHAR(255) NOT NULL,
    payload TEXT NOT NULL,
    status ENUM('pending', 'processing', 'completed', 'failed') DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP NULL,
    completed_at TIMESTAMP NULL,
    result TEXT,
    error_message TEXT,
    retry_count INT DEFAULT 0,
    INDEX idx_status (status),
    INDEX idx_created (created_at)
);

-- Tabela de Logs
CREATE TABLE IF NOT EXISTS system_logs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    level VARCHAR(20) DEFAULT 'info',
    message TEXT,
    context JSON,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_level (level),
    INDEX idx_created (created_at)
);
EOF

# Tenta criar banco se MySQL estiver disponível
if command -v mysql &> /dev/null; then
    if mysql -h"$DB_HOST" -u"$DB_USER" -p"$DB_PASS" -e "SELECT 1" &> /dev/null; then
        log_info "Conectando ao MySQL..."
        mysql -h"$DB_HOST" -u"$DB_USER" -p"$DB_PASS" < /tmp/qwen2api_schema.sql
        log_info "Banco de dados criado com sucesso!"
    else
        log_warn "Não foi possível conectar ao MySQL. Execute o schema manualmente."
        log_warn "Arquivo SQL: /tmp/qwen2api_schema.sql"
    fi
else
    log_warn "MySQL client não encontrado. Execute o schema manualmente."
    log_warn "Arquivo SQL: /tmp/qwen2api_schema.sql"
fi

rm -f /tmp/qwen2api_schema.sql

# ============================================
# Permissões
# ============================================
log_info "Configurando permissões..."

mkdir -p backend-php/uploads
mkdir -p logs
chmod 755 backend-php/uploads
chmod 777 logs

# ============================================
# Systemd Services (opcional)
# ============================================
if [ "$EUID" -eq 0 ]; then
    log_info "Criando serviços systemd..."
    
    # Serviço Node.js
    cat > /etc/systemd/system/qwen2api-node.service << EOF
[Unit]
Description=Qwen2API Fire Node.js Service
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=$(pwd)/service-node
ExecStart=/usr/bin/node dist/index.js
Restart=always
RestartSec=10
Environment=NODE_ENV=production

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable qwen2api-node
    log_info "Serviço systemd criado: qwen2api-node"
    log_warn "Inicie com: sudo systemctl start qwen2api-node"
else
    log_warn "Execute como root para criar serviços systemd automaticamente"
fi

# ============================================
# Resumo Final
# ============================================
echo ""
echo "========================================"
echo -e "${GREEN}✅ Instalação concluída!${NC}"
echo "========================================"
echo ""
echo "Próximos passos:"
echo "1. Edite .env com suas credenciais"
echo "2. Configure o banco de dados MySQL"
echo "3. Inicie o serviço Node.js:"
echo "   cd service-node && npm run build && npm start"
echo "   OU use: sudo systemctl start qwen2api-node"
echo "4. Configure Apache/Nginx para apontar para backend-php/public"
echo "5. Teste a API!"
echo ""
echo "Documentação completa em: docs/"
echo ""
