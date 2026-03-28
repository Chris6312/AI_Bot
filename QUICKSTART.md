# ⚡ QUICKSTART - 5 Minute Setup

If you already have Discord + Tradier accounts configured, start here!

## 1. Configure Environment (2 min)

```bash
cd AI_Bot
cp .env.example .env
nano .env
```

Fill in your tokens:
```env
DISCORD_BOT_TOKEN=...
DISCORD_TRADING_CHANNEL_ID=...
DISCORD_WEBHOOK_URL=...
DISCORD_USER_ID=...

TRADIER_API_KEY=...
TRADIER_ACCOUNT_ID=...
```

## 2. Start Bot (1 min)

```bash
docker-compose up -d
```

## 3. Verify (1 min)

```bash
# Check logs
docker-compose logs -f backend

# Should see:
# "Discord bot connected as Trading Bot"
# "Trading bot online and listening for decisions"
```

## 4. Test (1 min)

In Discord #trading-decisions:
```
BUY 1 share AAPL
```

Bot should execute immediately!

## 5. Connect Claude

Claude.ai → Settings → Connectors → Add Custom:
- URL: https://mcp.tradier.com/mcp
- Header: API_KEY = your_tradier_key
- Header: PAPER_TRADING = true

## 🎉 Done!

See GET_STARTED.md for detailed setup if you need Discord/Tradier accounts.
