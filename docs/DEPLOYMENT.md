# Deployment Guide

## Local Development

```bash
# Clone/extract project
cd AI_Bot

# Configure
cp .env.example .env
nano .env

# Start
docker-compose up -d

# View logs
docker-compose logs -f backend

# Stop
docker-compose down
```

## Production (DigitalOcean)

### Step 1: Create Droplet
1. DigitalOcean → Create → Droplets
2. Choose: Ubuntu 22.04
3. Size: Basic $6/month
4. Add SSH key

### Step 2: Setup Server
```bash
# SSH into droplet
ssh root@your-droplet-ip

# Install Docker
curl -fsSL https://get.docker.com | sh

# Install Docker Compose
apt install docker-compose

# Clone project
cd /opt
git clone your-repo
cd AI_Bot
```

### Step 3: Configure
```bash
cp .env.example .env
nano .env

# Edit with production values:
# - Set APP_ENV=production
# - Use live Tradier URL if ready
# - Strong SECRET_KEY
```

### Step 4: Deploy
```bash
docker-compose up -d

# Check status
docker-compose ps

# View logs
docker-compose logs -f
```

### Step 5: Enable Auto-Restart
```bash
# Docker containers restart automatically
# To start on boot:
systemctl enable docker
```

## Monitoring

### Check Logs
```bash
docker-compose logs -f backend
```

### Check Discord
Bot should post "online" message on startup

### Check Database
```bash
docker-compose exec postgres psql -U bot_user trading_bot
\dt  # List tables
SELECT * FROM accounts;
```

## Backups

### Database Backup
```bash
docker-compose exec postgres pg_dump -U bot_user trading_bot > backup.sql
```

### Restore
```bash
docker-compose exec -T postgres psql -U bot_user trading_bot < backup.sql
```

## Updates

```bash
# Pull latest code
git pull

# Rebuild
docker-compose down
docker-compose up -d --build
```

## Security

- Use strong SECRET_KEY
- Keep .env file secure
- Enable firewall (ufw)
- Use SSL/TLS for production
- Regular backups
- Monitor logs

## Cost Estimate

**DigitalOcean:**
- Droplet: $6/month
- Database: $15/month (managed) OR included in droplet
- Total: $6-21/month

**Tradier:**
- $10/month (Pro plan)

**Total:** $16-31/month
