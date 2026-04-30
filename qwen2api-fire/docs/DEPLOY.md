# Guia de Deploy - Qwen2API Fire

## Opções de Deploy

### 1. Deploy Tradicional (Recomendado para Produção)

#### Backend PHP - Hospedagem Compartilhada
**Provedores sugeridos:** Hostgator, Hostinger, Locaweb

```bash
# 1. Acessar via SSH ou FTP
cd /public_html/qwen-api

# 2. Clonar repositório (se tiver acesso Git)
git clone https://github.com/seu-user/qwen2api-fire.git .

# 3. Instalar dependências
composer install --no-dev --optimize-autoloader

# 4. Configurar .env
cp .env.example .env
nano .env  # Editar com credenciais DB e URL do Node

# 5. Criar banco de dados (via phpMyAdmin ou CLI)
mysql -u usuario -p
> CREATE DATABASE qwen2api;
> source database/schema.sql

# 6. Configurar .htaccess (Apache) ou nginx.conf
# Ver docs/INSTALL.md para exemplos

# 7. Testar
curl https://sua-api.com/health
```

#### Service Node - VPS
**Provedores sugeridos:** Contabo, Hetzner, DigitalOcean, Linode

```bash
# 1. Acessar VPS via SSH
ssh root@ip-da-vps

# 2. Instalar Node.js 20
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

# 3. Instalar Docker (opcional mas recomendado)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# 4. Clone do projeto
cd /opt
git clone https://github.com/seu-user/qwen2api-fire.git
cd qwen2api-fire/service-node

# 5. Instalar dependências
npm install
npm run build
npx playwright install chromium --with-deps

# 6. Configurar .env
cp .env.example .env
nano .env

# 7. Instalar PM2
npm install -g pm2

# 8. Iniciar serviço
pm2 start dist/index.js --name qwen-node
pm2 save
pm2 startup systemd

# 9. Configurar firewall
ufw allow 3000/tcp
ufw allow from IP_HOSPEDAGEM_PHP to any port 3000
ufw enable

# 10. Testar
curl http://localhost:3000/health
```

---

### 2. Deploy com Docker (Mais Simples)

#### Pré-requisitos
- Docker 20+
- Docker Compose 2+

#### Passos

```bash
# 1. Clonar repositório na VPS
cd /opt
git clone https://github.com/seu-user/qwen2api-fire.git
cd qwen2api-fire

# 2. Copiar .env
cp service-node/.env.example service-node/.env
nano service-node/.env

# 3. Build e start
docker-compose up -d --build

# 4. Verificar logs
docker-compose logs -f node-service

# 5. Testar
curl http://localhost:3000/health
```

**Vantagens:**
- ✅ Isolamento completo
- ✅ Reproduzível em qualquer ambiente
- ✅ Atualizações fáceis (docker-compose pull && docker-compose up -d)
- ✅ Health check automático

**Desvantagens:**
- ❌ Requer VPS (não roda em hospedagem compartilhada)
- ❌ Consumo maior de RAM (~500MB vs ~300MB)

---

### 3. Deploy Híbrido (PHP Shared + Node Docker)

Ideal para custo-benefício máximo.

```
┌─────────────────────────┐
│  Hostinger (R$ 20/mês)  │
│  - Backend PHP          │
│  - MySQL                │
│  - Painel Admin         │
└───────────┬─────────────┘
            │ HTTP
┌───────────▼─────────────┐
│  Contabo VPS (R$ 30/mês)│
│  - Docker               │
│  - Node Service         │
│  - Playwright           │
└─────────────────────────┘
```

**Configuração:**

1. **Hospedagem PHP:**
   - Upload via FTP/cPanel
   - Configurar `NODE_SERVICE_URL=http://IP_VPS:3000`

2. **VPS:**
   ```bash
   docker-compose up -d
   
   # Expor porta apenas para IP da hospedagem
   ufw allow from IP_HOSPEDAGEM to any port 3000
   ```

---

### 4. Deploy em Cloud (AWS/GCP/Azure)

#### AWS Example

**Backend PHP:**
- EC2 t3.small ou Lightsail
- RDS MySQL (ou Aurora Serverless)
- Application Load Balancer
- ElastiCache Redis (opcional)

**Service Node:**
- EC2 t3.medium (2GB+ RAM)
- Auto Scaling Group
- Security Group restrito

**Custo estimado:** $50-80/mês

#### GCP Example

**Backend PHP:**
- Compute Engine e2-small
- Cloud SQL MySQL
- Cloud Load Balancing

**Service Node:**
- Compute Engine e2-medium
- Container Registry
- Cloud Run (alternativa serverless)

**Custo estimado:** $45-70/mês

---

## SSL/HTTPS

### Let's Encrypt (Gratuito)

#### Backend PHP (Apache)
```bash
sudo apt-get install certbot python3-certbot-apache
sudo certbot --apache -d sua-api.com
```

#### Backend PHP (Nginx)
```bash
sudo apt-get install certbot python3-certbot-nginx
sudo certbot --nginx -d sua-api.com
```

#### Service Node (Docker com Nginx Proxy)
```yaml
# docker-compose.yml adicional
services:
  nginx-proxy:
    image: nginxproxy/nginx-proxy
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - /var/run/docker.sock:/tmp/docker.sock:ro
      - ./certs:/etc/nginx/certs
      
  letsencrypt:
    image: nginxproxy/acme-companion
    environment:
      DEFAULT_EMAIL: seu@email.com
    volumes_from:
      - nginx-proxy
```

---

## Monitoramento

### PM2 (Node Service)
```bash
# Dashboard em tempo real
pm2 monit

# Logs
pm2 logs qwen-node --lines 100

# Métricas
pm2 show qwen-node

# Restart automático em crash
pm2 update
```

### MySQL Slow Queries
```sql
-- Habilitar slow query log
SET GLOBAL slow_query_log = 'ON';
SET GLOBAL long_query_time = 2;

-- Analisar queries lentas
mysqldumpslow /var/log/mysql/slow.log
```

### Uptime Monitoring
- **UptimeRobot:** Gratuito, checks a cada 5min
- **Pingdom:** Pago, mais recursos
- **Custom:** Script PHP que testa endpoint e envia alerta

---

## Backup

### Banco de Dados (Diário)
```bash
#!/bin/bash
# /opt/scripts/backup-db.sh

DATE=$(date +%Y%m%d_%H%M%S)
mysqldump -u usuario -p'senha' qwen2api > /backups/qwen2api_$DATE.sql
gzip /backups/qwen2api_$DATE.sql

# Manter últimos 7 dias
find /backups -name "*.sql.gz" -mtime +7 -delete
```

**Cron:**
```bash
0 2 * * * /opt/scripts/backup-db.sh
```

### Código Fonte
- GitHub/GitLab com push automático
- Backup manual via rsync

---

## Escalabilidade

### Horizontal (Node Service)

```bash
# Múltiplas instâncias com PM2
pm2 start dist/index.js -i 4 --name qwen-node-cluster

# Ou Docker Swarm/Kubernetes
docker service scale qwen-node=4
```

### Vertical (Aumentar Recursos)

| Componente | Mínimo | Recomendado | Produção |
|------------|--------|-------------|----------|
| RAM Node   | 2GB    | 4GB         | 8GB+     |
| CPU Node   | 2 core | 4 core      | 8 core+  |
| Browsers   | 2      | 4           | 8+       |

---

## Troubleshooting Comum

### Erro: "Cannot allocate memory"
```bash
# Aumentar swap
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile

# Reduzir BROWSER_POOL_SIZE no .env
BROWSER_POOL_SIZE=1
```

### Erro: "Connection refused" do PHP para Node
```bash
# Verificar se Node está rodando
pm2 status
# ou
docker ps

# Testar conexão local
curl http://localhost:3000/health

# Verificar firewall
ufw status
```

### Erro: "Browser crashed"
```bash
# Reinstalar Playwright browsers
npx playwright install chromium --with-deps --force

# Verificar dependências do sistema
ldd node_modules/playwright-core/.local-browsers/chromium-*/chrome-linux/chrome
```

---

## Checklist de Produção

- [ ] SSL configurado em ambos serviços
- [ ] Firewall restrito (apenas portas necessárias)
- [ ] Backups automáticos configurados
- [ ] Monitoramento ativo (logs + uptime)
- [ ] Rate limiting implementado
- [ ] API keys alteradas do padrão
- [ ] Debug desativado (.env: APP_DEBUG=false)
- [ ] Logs rotacionados (logrotate)
- [ ] Health checks passando
- [ ] Documentação de recuperação de desastres

---

## Suporte

Para issues complexas:
1. Verificar logs (`pm2 logs`, `docker logs`, `/var/log/`)
2. Consultar docs/API.md e docs/INSTALL.md
3. Abrir issue no GitHub com:
   - Versões (PHP, Node, Docker)
   - Logs relevantes
   - Passos para reproduzir
