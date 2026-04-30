# 🔥 Qwen2API Fire - Manual Completo de Instalação

## 📋 Visão Geral

Qwen2API Fire é uma solução híbrida **PHP + Node.js** que converte a API do Qwen (通义千问) em endpoints compatíveis com OpenAI, Anthropic e Gemini.

### Arquitetura Híbrida Inteligente

- **Backend PHP**: Roda em hospedagem compartilhada (cPanel, Hostgator, etc.)
- **Serviço Node.js**: Micro-serviço isolado para automação de navegador
- **Banco MySQL**: Armazenamento robusto de dados, quotas e logs
- **Setup Lock**: Sistema que mantém o Node inativo até você estar pronto

---

## 🚀 Instalação Automática (Recomendado)

### Pré-requisitos

- **PHP 8.0+** com extensões: `pdo`, `pdo_mysql`, `curl`, `json`, `mbstring`
- **Composer** (gerenciador de dependências PHP)
- **Node.js 18+** e **npm**
- **MySQL 5.7+** ou **MariaDB 10.3+**
- **Acesso SSH** ao servidor
- **Git** (para clonar o repositório)

### Passo 1: Clonar Repositório

```bash
cd /var/www/html
git clone https://github.com/seu-usuario/qwen2api-fire.git
cd qwen2api-fire
```

### Passo 2: Executar Script de Instalação

```bash
chmod +x scripts/install.sh
./scripts/install.sh
```

O script fará as seguintes perguntas interativamente:

```
🔥 ==========================================
🔥  Qwen2API Fire - Instalador Automático
🔥 ==========================================

➜ Configuração do Banco de Dados MySQL

Host do MySQL (padrão: localhost): 
Porta do MySQL (padrão: 3306): 
Usuário root do MySQL: 
Senha do root do MySQL: 
Nome do banco de dados (padrão: qwen2api_fire): 
Usuário do banco de dados (padrão: qwen_user): 
Senha do usuário do banco: 
```

### Passo 3: Aguardar Conclusão

O script irá automaticamente:

1. ✅ Criar banco de dados e tabelas
2. ✅ Criar usuário do banco com permissões
3. ✅ Inserir usuário admin padrão
4. ✅ Gerar arquivos `.env` para PHP e Node
5. ✅ Instalar dependências PHP (Composer)
6. ✅ Instalar dependências Node (npm)
7. ✅ Criar arquivo `setup.lock` (Node fica INATIVO)
8. ✅ Configurar permissões de diretórios

### Resultado Esperado

```
🔥 ==========================================
🔥  Instalação Concluída com Sucesso!
🔥 ==========================================

✓ Banco de dados: qwen2api_fire
✓ Usuário DB: qwen_user
✓ Backend PHP: Configurado em backend-php/
✓ Serviço Node: Configurado em service-node/ (INATIVO)

⚠ PRÓXIMOS PASSOS:

1. Configure seu servidor web (Apache/Nginx) para apontar para:
   backend-php/public/

2. Teste a API PHP:
   curl http://localhost/health

3. QUANDO ESTIVER PRONTO, ative o serviço Node.js:
   rm service-node/setup.lock
   cd service-node && npm run build && npm start

4. Acesse o painel administrativo (se implementado):
   http://seu-domínio.com/admin

📚 Documentação completa em: docs/MANUAL_INSTALACAO.md

🔥 ==========================================
```

---

## 🔧 Configuração do Servidor Web

### Apache (Hospedagem Compartilhada)

Crie ou edite `.htaccess` em `backend-php/public/`:

```apache
<IfModule mod_rewrite.c>
    RewriteEngine On
    RewriteCond %{REQUEST_FILENAME} !-f
    RewriteCond %{REQUEST_FILENAME} !-d
    RewriteRule ^(.*)$ index.php [QSA,L]
</IfModule>

# Headers de segurança
<IfModule mod_headers.c>
    Header set Access-Control-Allow-Origin "*"
    Header set Access-Control-Allow-Methods "GET, POST, PUT, DELETE, OPTIONS"
    Header set Access-Control-Allow-Headers "Content-Type, Authorization, X-API-Key"
</IfModule>
```

### Nginx (VPS)

Crie um bloco de servidor em `/etc/nginx/sites-available/qwen2api`:

```nginx
server {
    listen 80;
    server_name api.seudominio.com;
    
    root /var/www/html/qwen2api-fire/backend-php/public;
    index index.php;
    
    location / {
        try_files $uri $uri/ /index.php?$query_string;
    }
    
    location ~ \.php$ {
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:/run/php/php8.1-fpm.sock;
        fastcgi_param SCRIPT_FILENAME $realpath_root$fastcgi_script_name;
        include fastcgi_params;
    }
    
    # Proteger arquivos sensíveis
    location ~ /\.env {
        deny all;
    }
    
    location ~ /\.git {
        deny all;
    }
}
```

Ative o site:

```bash
sudo ln -s /etc/nginx/sites-available/qwen2api /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

---

## 🗄️ Estrutura do Banco de Dados

O instalador cria automaticamente as seguintes tabelas:

### `users`
Armazena usuários administrativos.

| Coluna | Tipo | Descrição |
|--------|------|-----------|
| id | INT | Primary Key |
| email | VARCHAR(255) | Email único |
| password_hash | VARCHAR(255) | Senha criptografada |
| role | ENUM | 'admin' ou 'user' |
| created_at | TIMESTAMP | Data de criação |

### `api_keys`
Chaves de API dos clientes.

| Coluna | Tipo | Descrição |
|--------|------|-----------|
| id | INT | Primary Key |
| user_id | INT | FK para users |
| key_hash | VARCHAR(255) | Hash da API key |
| name | VARCHAR(100) | Nome descritivo |
| is_active | BOOLEAN | Status da chave |
| last_used_at | TIMESTAMP | Último uso |

### `qwen_accounts`
Pool de contas Qwen.

| Coluna | Tipo | Descrição |
|--------|------|-----------|
| id | INT | Primary Key |
| email | VARCHAR(255) | Email da conta Qwen |
| token | TEXT | Token de acesso |
| refresh_token | TEXT | Token de renovação |
| is_active | BOOLEAN | Conta ativa |
| is_locked | BOOLEAN | Conta bloqueada |
| expires_at | TIMESTAMP | Expiração do token |

### `quotas`
Limites de uso por usuário.

| Coluna | Tipo | Descrição |
|--------|------|-----------|
| id | INT | Primary Key |
| user_id | INT | FK para users |
| daily_limit | INT | Limite diário |
| monthly_limit | INT | Limite mensal |
| daily_used | INT | Uso diário atual |
| monthly_used | INT | Uso mensal atual |

### `request_logs`
Logs de todas as requisições.

| Coluna | Tipo | Descrição |
|--------|------|-----------|
| id | BIGINT | Primary Key |
| api_key_id | INT | FK para api_keys |
| model_used | VARCHAR(100) | Modelo utilizado |
| tokens_used | INT | Tokens consumidos |
| status_code | INT | Código HTTP |
| response_time_ms | INT | Tempo de resposta |
| ip_address | VARCHAR(45) | IP do cliente |

### `rate_limits`
Controle de rate limiting por IP.

---

## 🔐 Credenciais Padrão

Após instalação, use estas credenciais:

### Admin Panel
- **Email**: `admin@qwen2api.local`
- **Senha**: `admin123`

### API Key Master
- **Chave**: `sk-fire-admin-key-12345`

⚠️ **IMPORTANTE**: Altere estas credenciais imediatamente após o primeiro login!

---

## ⚡ Ativando o Serviço Node.js

O serviço Node.js permanece **INATIVO** após instalação devido ao arquivo `setup.lock`. Isso permite que você configure tudo com calma antes de iniciar a automação.

### Quando Ativar

Ative o Node.js quando:
- ✅ O banco de dados estiver configurado
- ✅ O PHP estiver funcionando (teste com `/health`)
- ✅ Você tiver adicionado pelo menos uma conta Qwen no banco
- ✅ O servidor web estiver apontando corretamente

### Como Ativar

```bash
# 1. Remover arquivo de bloqueio
rm service-node/setup.lock

# 2. Build do TypeScript
cd service-node
npm run build

# 3. Iniciar serviço
npm start

# Ou em produção (background)
npm run prod
```

### Verificar se está rodando

```bash
curl http://localhost:3001/health
# Deve retornar: {"status":"ok","service":"qwen2api-node"}
```

### Manter Rodando (Produção)

Use **PM2** para manter o serviço ativo:

```bash
npm install -g pm2
cd service-node
pm2 start npm --name "qwen2api-node" -- start
pm2 save
pm2 startup
```

---

## 🧪 Testes

### Testar Saúde da API PHP

```bash
curl http://seu-dominio.com/health
# Retorna: {"status":"ok","service":"qwen2api-php"}
```

### Testar Endpoint OpenAI Compatível

```bash
curl -X POST http://seu-dominio.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-fire-admin-key-12345" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "Olá!"}]
  }'
```

### Testar Streaming

```bash
curl -X POST http://seu-dominio.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-fire-admin-key-12345" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "Conte uma história"}],
    "stream": true
  }'
```

---

## 🔒 Segurança

### 1. Alterar Credenciais Padrão

```sql
-- Alterar senha do admin
UPDATE users SET password_hash = '$2y$10$NOVA_HASH_AQUI' WHERE email = 'admin@qwen2api.local';

-- Gerar nova hash: php -r "echo password_hash('sua-nova-senha', PASSWORD_DEFAULT);"
```

### 2. Gerar Nova API Secret Key

Edite `backend-php/.env`:

```env
API_SECRET_KEY=$(openssl rand -hex 32)
# Copie o resultado e cole no .env
```

### 3. Proteger Diretórios

```bash
# Remover acesso a .env
chmod 644 backend-php/.env
chmod 644 service-node/.env

# Proteger diretórios sensíveis
chmod 755 backend-php/storage
chmod 755 service-node/logs
```

### 4. Firewall

```bash
# Permitir apenas portas necessárias
sudo ufw allow 80/tcp    # HTTP
sudo ufw allow 443/tcp   # HTTPS
sudo ufw allow 22/tcp    # SSH
sudo ufw enable
```

---

## 🛠️ Troubleshooting

### Erro: "Não foi possível conectar ao MySQL"

**Solução:**
1. Verifique se MySQL está rodando: `sudo systemctl status mysql`
2. Confirme credenciais no `.env`
3. Teste conexão manual: `mysql -h localhost -u root -p`

### Erro: "Composer não encontrado"

**Solução:**
```bash
curl -sS https://getcomposer.org/installer | php
sudo mv composer.phar /usr/local/bin/composer
```

### Erro: "npm install falhou"

**Solução:**
```bash
# Limpar cache npm
npm cache clean --force

# Reinstalar dependências
cd service-node
rm -rf node_modules package-lock.json
npm install
```

### Serviço Node não inicia

**Solução:**
1. Verifique se `setup.lock` foi removido
2. Confira logs: `cat service-node/logs/error.log`
3. Teste manualmente: `cd service-node && node dist/index.js`

### API retorna erro 500

**Solução:**
1. Ative debug em `backend-php/.env`: `APP_DEBUG=true`
2. Verifique logs: `tail -f backend-php/storage/logs/error.log`
3. Confirme permissões: `chmod -R 755 backend-php/storage`

---

## 📊 Monitoramento

### Logs em Tempo Real

```bash
# Logs PHP
tail -f backend-php/storage/logs/app.log

# Logs Node
tail -f service-node/logs/app.log

# Logs MySQL
sudo tail -f /var/log/mysql/error.log
```

### Métricas Importantes

Consulte o banco para verificar:

```sql
-- Requisições nas últimas 24h
SELECT COUNT(*) FROM request_logs 
WHERE created_at >= NOW() - INTERVAL 1 DAY;

-- Top modelos mais usados
SELECT model_used, COUNT(*) as count 
FROM request_logs 
GROUP BY model_used 
ORDER BY count DESC 
LIMIT 10;

-- Contas Qwen mais ativas
SELECT email, total_requests 
FROM qwen_accounts 
ORDER BY total_requests DESC 
LIMIT 5;
```

---

## 🔄 Atualização

Para atualizar para uma nova versão:

```bash
# 1. Backup
mysqldump -u root -p qwen2api_fire > backup_$(date +%Y%m%d).sql

# 2. Parar serviços
pm2 stop qwen2api-node

# 3. Pull das mudanças
git pull origin main

# 4. Atualizar dependências
cd backend-php && composer install --no-dev --optimize-autoloader
cd ../service-node && npm install --production

# 5. Migrar banco (se houver migrations)
php backend-php/bin/migrate.php

# 6. Reiniciar
pm2 restart qwen2api-node
```

---

## 📞 Suporte

- **Documentação**: `/docs/`
- **Issues**: GitHub Issues
- **Comunidade**: Discord/Telegram (se aplicável)

---

## 📝 Checklist Pós-Instalação

- [ ] Alterar senha do admin
- [ ] Gerar nova API Secret Key
- [ ] Configurar servidor web (Apache/Nginx)
- [ ] Testar endpoint `/health`
- [ ] Adicionar contas Qwen no banco
- [ ] Remover `setup.lock` e iniciar Node.js
- [ ] Testar requisição real
- [ ] Configurar HTTPS (Let's Encrypt)
- [ ] Setup de monitoramento (PM2, logs)
- [ ] Backup automático do banco

---

**Instalado com sucesso! 🎉**

Agora você tem uma API Qwen poderosa rodando com facilidade de hospedagem PHP e performance Node.js!
