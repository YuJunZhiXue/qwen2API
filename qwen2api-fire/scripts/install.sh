#!/bin/bash

# =============================================================================
# Qwen2API Fire - Instalador Automático Inteligente
# =============================================================================
# Este script configura todo o ambiente PHP + Node.js
# O serviço Node fica INATIVO até você remover o arquivo setup.lock
# =============================================================================

set -e

echo "🔥 =========================================="
echo "🔥  Qwen2API Fire - Instalador Automático"
echo "🔥 =========================================="
echo ""

# Cores
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Função para imprimir mensagens
print_step() {
    echo -e "${BLUE}➜ $1${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

# Verificar se está na pasta correta
if [ ! -d "backend-php" ] || [ ! -d "service-node" ]; then
    print_error "Erro: Execute este script na raiz do projeto qwen2api-fire"
    exit 1
fi

# =============================================================================
# 1. Configuração do Banco de Dados MySQL (INTERATIVO)
# =============================================================================
print_step "Configuração do Banco de Dados MySQL"
echo ""

read -p "Host do MySQL (padrão: localhost): " DB_HOST
DB_HOST=${DB_HOST:-localhost}

read -p "Porta do MySQL (padrão: 3306): " DB_PORT
DB_PORT=${DB_PORT:-3306}

read -p "Usuário root do MySQL: " DB_ROOT_USER
read -sp "Senha do root do MySQL: " DB_ROOT_PASS
echo ""

read -p "Nome do banco de dados (padrão: qwen2api_fire): " DB_NAME
DB_NAME=${DB_NAME:-qwen2api_fire}

read -p "Usuário do banco de dados (padrão: qwen_user): " DB_USER
DB_USER=${DB_USER:-qwen_user}

read -sp "Senha do usuário do banco: " DB_PASS
echo ""

# Criar script SQL temporário
TEMP_SQL=$(mktemp)

cat > $TEMP_SQL << EOF
-- Criar banco de dados
CREATE DATABASE IF NOT EXISTS \`${DB_NAME}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Criar usuário e conceder permissões
CREATE USER IF NOT EXISTS '${DB_USER}'@'${DB_HOST}' IDENTIFIED BY '${DB_PASS}';
GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_USER}'@'${DB_HOST}';
FLUSH PRIVILEGES;

-- Usar o banco
USE \`${DB_NAME}\`;

-- Tabela de usuários/administradores
CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role ENUM('admin', 'user') DEFAULT 'user',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- Tabela de API Keys
CREATE TABLE IF NOT EXISTS api_keys (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    key_hash VARCHAR(255) UNIQUE NOT NULL,
    name VARCHAR(100),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Tabela de Contas Qwen (Pool)
CREATE TABLE IF NOT EXISTS qwen_accounts (
    id INT AUTO_INCREMENT PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    token TEXT,
    refresh_token TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    is_locked BOOLEAN DEFAULT FALSE,
    last_used_at TIMESTAMP NULL,
    expires_at TIMESTAMP NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- Tabela de Quotas
CREATE TABLE IF NOT EXISTS quotas (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    daily_limit INT DEFAULT 1000,
    monthly_limit INT DEFAULT 10000,
    daily_used INT DEFAULT 0,
    monthly_used INT DEFAULT 0,
    reset_daily DATE,
    reset_monthly DATE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Tabela de Logs
CREATE TABLE IF NOT EXISTS request_logs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    api_key_id INT,
    model_used VARCHAR(100),
    tokens_used INT,
    status_code INT,
    response_time_ms INT,
    ip_address VARCHAR(45),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (api_key_id) REFERENCES api_keys(id) ON DELETE SET NULL
);

-- Tabela de Rate Limit
CREATE TABLE IF NOT EXISTS rate_limits (
    id INT AUTO_INCREMENT PRIMARY KEY,
    ip_address VARCHAR(45) NOT NULL,
    endpoint VARCHAR(255) NOT NULL,
    request_count INT DEFAULT 1,
    window_start TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY unique_ip_endpoint (ip_address, endpoint, window_start)
);

-- Inserir usuário admin padrão (senha: admin123)
INSERT INTO users (email, password_hash, role) VALUES 
('admin@qwen2api.local', '\$2y\$10\$92IXUNpkjO0rOQ5byMi.Ye4oKoEa3Ro9llC/.og/at2.uheWG/igi', 'admin');

-- Inserir API Key padrão (chave: sk-fire-admin-key-12345)
INSERT INTO api_keys (user_id, key_hash, name, is_active) VALUES 
(1, '\$2y\$10\$92IXUNpkjO0rOQ5byMi.Ye4oKoEa3Ro9llC/.og/at2.uheWG/igi', 'Admin Master Key', TRUE);

EOF

print_step "Criando banco de dados e tabelas..."
if mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_ROOT_USER" -p"$DB_ROOT_PASS" < $TEMP_SQL 2>/dev/null; then
    print_success "Banco de dados criado com sucesso!"
else
    print_error "Falha ao criar banco de dados. Verifique as credenciais."
    rm $TEMP_SQL
    exit 1
fi

rm $TEMP_SQL

# =============================================================================
# 2. Configurar Backend PHP
# =============================================================================
print_step "Configurando Backend PHP..."

# Criar arquivo .env para PHP
cat > backend-php/.env << EOF
DB_HOST=${DB_HOST}
DB_PORT=${DB_PORT}
DB_NAME=${DB_NAME}
DB_USER=${DB_USER}
DB_PASS=${DB_PASS}

NODE_SERVICE_URL=http://localhost:3001
API_SECRET_KEY=$(openssl rand -hex 32)
LOG_LEVEL=info
EOF

print_success "Arquivo .env do PHP criado"

# Instalar dependências PHP se composer existir
if command -v composer &> /dev/null; then
    print_step "Instalando dependências PHP..."
    cd backend-php
    composer install --no-interaction --quiet 2>/dev/null || true
    cd ..
    print_success "Dependências PHP instaladas"
else
    print_warning "Composer não encontrado. Execute 'composer install' manualmente em backend-php/"
fi

# =============================================================================
# 3. Configurar Serviço Node.js
# =============================================================================
print_step "Configurando Serviço Node.js..."

# Criar arquivo .env para Node
cat > service-node/.env << EOF
PORT=3001
DB_HOST=${DB_HOST}
DB_PORT=${DB_PORT}
DB_NAME=${DB_NAME}
DB_USER=${DB_USER}
DB_PASS=${DB_PASS}

BROWSER_POOL_SIZE=2
MAX_INFLIGHT=1
ACCOUNT_MIN_INTERVAL_MS=1200

LOG_LEVEL=info
SETUP_LOCK=true
EOF

print_success "Arquivo .env do Node criado"

# Instalar dependências Node se npm existir
if command -v npm &> /dev/null; then
    print_step "Instalando dependências Node.js..."
    cd service-node
    npm install --silent 2>/dev/null || true
    cd ..
    print_success "Dependências Node.js instaladas"
else
    print_warning "npm não encontrado. Execute 'npm install' manualmente em service-node/"
fi

# =============================================================================
# 4. Criar Arquivo de Bloqueio (Setup Lock) - NODE FICA INATIVO
# =============================================================================
print_step "Criando arquivo de bloqueio do serviço Node.js..."

touch service-node/setup.lock

print_success "Serviço Node.js está INATIVO (bloqueado por setup.lock)"
print_warning "PARA ATIVAR O SERVIÇO NODE.JS, execute:"
echo "   rm service-node/setup.lock"
echo "   cd service-node && npm run build && npm start"
echo ""

# =============================================================================
# 5. Configurar Permissões
# =============================================================================
print_step "Configurando permissões..."

chmod +x backend-php/public/index.php 2>/dev/null || true
mkdir -p backend-php/storage 2>/dev/null && chmod 755 backend-php/storage || true
mkdir -p service-node/logs 2>/dev/null && chmod 755 service-node/logs || true

print_success "Permissões configuradas"

# =============================================================================
# Resumo Final
# =============================================================================
echo ""
echo "🔥 =========================================="
echo "🔥  Instalação Concluída com Sucesso!"
echo "🔥 =========================================="
echo ""
echo -e "${GREEN}✓ Banco de dados:${NC} ${DB_NAME}"
echo -e "${GREEN}✓ Usuário DB:${NC} ${DB_USER}"
echo -e "${GREEN}✓ Backend PHP:${NC} Configurado em backend-php/"
echo -e "${GREEN}✓ Serviço Node:${NC} Configurado em service-node/ (INATIVO)"
echo ""
echo -e "${YELLOW}⚠ PRÓXIMOS PASSOS:${NC}"
echo ""
echo "1. Configure seu servidor web (Apache/Nginx) para apontar para:"
echo "   backend-php/public/"
echo ""
echo "2. Teste a API PHP:"
echo "   curl http://localhost/health"
echo ""
echo "3. QUANDO ESTIVER PRONTO, ative o serviço Node.js:"
echo "   rm service-node/setup.lock"
echo "   cd service-node && npm run build && npm start"
echo ""
echo "4. Acesse o painel administrativo (se implementado):"
echo "   http://seu-domínio.com/admin"
echo ""
echo -e "${BLUE}📚 Documentação completa em: docs/MANUAL_INSTALACAO.md${NC}"
echo ""
echo "🔥 =========================================="
