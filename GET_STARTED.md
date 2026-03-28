# 🚀 GET STARTED - Complete Setup Guide

**Time required: 30 minutes**

This guide takes you from zero to a fully operational AI trading bot with Discord + Tradier integration.

---

## ✅ Prerequisites

Before starting:
- [ ] Discord account (free)
- [ ] Tradier brokerage account (free signup)
- [ ] Python 3.11+ installed
- [ ] Docker Desktop installed
- [ ] Text editor (VS Code recommended)

---

## 📋 Part 1: Discord Setup (10 minutes)

### Step 1: Create Discord Server
1. Open Discord
2. Click "+" in server list
3. "Create My Own" → "For me and my friends"
4. Name: "Trading Bot"

### Step 2: Create #trading-decisions Channel
1. Right-click "general" → "Duplicate Channel"
2. Rename to: `trading-decisions`

### Step 3: Create Bot
1. Go to https://discord.com/developers/applications
2. "New Application" → Name: "Trading Bot"
3. Bot tab → "Add Bot"
4. **Copy token** → Save as `DISCORD_BOT_TOKEN`
5. Enable "MESSAGE CONTENT INTENT"
6. Enable "SERVER MEMBERS INTENT"

### Step 4: Create Webhook
1. Right-click #trading-decisions → "Edit Channel"
2. Integrations → Webhooks → "New Webhook"
3. Name: "AI Decisions"
4. **Copy Webhook URL** → Save as `DISCORD_WEBHOOK_URL`

### Step 5: Get IDs
1. Discord Settings → Advanced → Enable Developer Mode
2. Right-click #trading-decisions → Copy ID → Save as `DISCORD_TRADING_CHANNEL_ID`
3. Right-click your name → Copy ID → Save as `DISCORD_USER_ID`

### Step 6: Invite Bot
1. Developer Portal → OAuth2 → URL Generator
2. Scopes: ✓ bot
3. Permissions: ✓ Read Messages ✓ Send Messages ✓ Add Reactions
4. Copy URL → Open in browser → Add to your server

**✅ Discord Complete!**

---

## 📋 Part 2: Tradier Setup (10 minutes)

### Step 1: Create Account
1. Go to https://tradier.com
2. "Open Account" → Individual Brokerage
3. Complete signup (5 minutes)

### Step 2: Get API Keys
1. Login → Settings → API Access
2. Create Sandbox token
3. **Copy token** → Save as `TRADIER_API_KEY`
4. **Copy Account ID** → Save as `TRADIER_ACCOUNT_ID`

**✅ Tradier Complete!**

---

## 📋 Part 3: Bot Installation (10 minutes)

### Step 1: Clone/Extract Project
```bash
cd ~/AI_Bot
```

### Step 2: Configure Environment
```bash
cp .env.example .env
nano .env  # or: code .env
```

**Edit with your saved values:**
```env
DISCORD_BOT_TOKEN=your_bot_token_here
DISCORD_TRADING_CHANNEL_ID=your_channel_id
DISCORD_WEBHOOK_URL=your_webhook_url
DISCORD_USER_ID=your_user_id

TRADIER_API_KEY=your_tradier_key
TRADIER_ACCOUNT_ID=your_account_id
TRADIER_BASE_URL=https://sandbox.tradier.com/v1

SECRET_KEY=generate-a-random-32-character-key
```

### Step 3: Start Services
```bash
docker-compose up -d
```

### Step 4: Verify
```bash
docker-compose logs -f backend
```

You should see:
```
Discord bot connected as Trading Bot#1234
Trading bot online and listening for decisions
```

**✅ Bot Running!**

---

## 📋 Part 4: Connect Claude to Tradier (5 minutes)

### Step 1: Open Claude.ai
1. Go to https://claude.ai
2. Login with your Claude Pro account

### Step 2: Add Tradier Connector
1. Settings → Connectors → "Add Custom Connector"
2. Fill in:
   - **Name**: Tradier Trading
   - **URL**: https://mcp.tradier.com/mcp
   - **Headers**:
     - Key: `API_KEY`, Value: `your_tradier_key`
     - Key: `PAPER_TRADING`, Value: `true`
3. Click "Save" → "Connect"

### Step 3: Test
In Claude chat:
```
Check my Tradier account via MCP
```

Claude should respond with your account balance!

**✅ Claude Connected!**

---

## 🎉 Your First Trade!

### Test 1: Manual Discord Message
In Discord #trading-decisions:
```
BUY 10 shares AAPL
```

Bot should:
1. React with 👀 (acknowledged)
2. React with ⚡ (executing)
3. Post confirmation with ❌ (cancel option)

### Test 2: AI-Powered Decision
In Claude chat:
```
Run a screening via Tradier MCP.
Find 1-2 momentum stocks.
Post decision to my Discord webhook:
[paste your DISCORD_WEBHOOK_URL]

Use this JSON format:
{
  "type": "SCREENING",
  "candidates": [
    {"ticker": "AAPL", "shares": 5}
  ],
  "vix": 18.5
}
```

Claude will:
1. Query Tradier via MCP
2. Analyze stocks
3. Post to Discord
4. Your bot executes automatically!

---

## 📱 Mobile Notifications (Recommended)

1. Install Discord mobile app
2. Enable notifications for #trading-decisions
3. Turn on sound alerts

Now you get instant notifications on your phone!

---

## 🎯 Your Daily Workflow

### Morning (9:45 AM)
**In Claude:**
```
Run morning screening via Tradier MCP.

Check:
- VIX level
- Market conditions
- My account balance

Screen for 2-3 stocks with:
- Price > $20
- Volume > 500k
- Good momentum

Post decision to: [your_webhook_url]
```

**Bot will:**
- Validate safety
- Execute trades
- Give you 30s to cancel

### During Day
- Bot monitors automatically
- You get alerts only if needed
- No action required!

### Evening (4:30 PM)
**In Claude:**
```
Run end-of-day analysis.
Review today's trades.
```

---

## 🆘 Troubleshooting

### Bot not responding?
```bash
docker-compose logs backend
# Check for "Discord bot connected"
```

### Trades not executing?
- Check Tradier API key in .env
- Verify you have buying power
- Check safety limits (max 3 trades/day)

### "Safety check failed"?
This is GOOD - bot protecting you!
- React with 🔓 to override if needed

---

## 📖 Next Steps

1. **Paper trade for 2 weeks** minimum
2. Review all safety settings
3. Read full README.md
4. Check docs/ folder

---

## ⚠️ Safety Reminders

- ✅ Start with paper trading (sandbox)
- ✅ Complete 100+ paper trades first
- ✅ Never invest more than you can lose
- ✅ Monitor daily for first week

**Emergency Stop:**
```bash
docker-compose down
```

---

**🎉 Congratulations! Your AI trading bot is live!**

Questions? Check docs/ or the README.md
