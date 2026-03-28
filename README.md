# 🤖 AI Trading Bot - Discord + Tradier Edition

**Hybrid AI + Algorithmic Trading System**

## 🎯 Overview

This bot combines Claude/ChatGPT AI decisions with algorithmic execution:
- **AI Layer**: Stock screening via Claude + Tradier MCP
- **Execution**: Automated trade execution with safety checks
- **Interface**: Discord for notifications and control
- **Broker**: Tradier (real-time data + commission-free trading)

## 💰 Monthly Cost

- **Development**: $0 (Tradier sandbox + Discord free)
- **Production**: $10 (Tradier Pro only)

## ⚡ Quick Start

### 1. Prerequisites
- Python 3.11+
- Docker Desktop
- Discord account
- Tradier account

### 2. Setup Discord (10 min)
See `GET_STARTED.md` for detailed instructions:
- Create server + #trading-decisions channel
- Create bot + webhook
- Get tokens and IDs

### 3. Setup Tradier (10 min)
- Create account at tradier.com
- Get API key and account ID
- Start with sandbox mode

### 4. Configure & Run (5 min)
```bash
cp .env.example .env
# Edit .env with your tokens
docker-compose up -d
```

### 5. Connect Claude to Tradier MCP
- Settings → Connectors → Add Custom
- URL: https://mcp.tradier.com/mcp
- Add API_KEY header

## 📊 Daily Workflow

**Morning (9:45 AM) - 2 minutes:**
```
You → Claude: "Run morning screening via Tradier"
Claude → Posts decision to Discord
Bot → Executes trades (30s cancel window)
```

**During Day - Fully Automated:**
```
Bot → Monitors via WebSocket
Bot → Auto-exits on stops/targets
You → Get alerts only if needed
```

**Evening (4:30 PM) - 2 minutes:**
```
You → Claude: "Daily summary"
Claude → Posts performance review
```

**Total time: 4-5 minutes/day**

## 🛡️ Safety Features

- ✅ Max 3 trades/day
- ✅ Max 25% position size
- ✅ Daily loss limit ($500)
- ✅ VIX threshold check
- ✅ 30-second cancel window
- ✅ Auto stop-loss (1.5%)
- ✅ Auto profit-target (2.5%)

## 📁 Project Structure

```
AI_Bot/
├── backend/
│   ├── app/
│   │   ├── core/          # Config, database
│   │   ├── models/        # SQLAlchemy models
│   │   ├── services/      # Discord, Tradier, Safety
│   │   └── main.py        # Entry point
│   ├── alembic/           # Database migrations
│   └── requirements.txt
├── docs/                  # Documentation
├── docker-compose.yml
├── .env.example
└── README.md
```

## 🔧 Technology Stack

- **Backend**: Python, FastAPI
- **Discord**: discord.py
- **Database**: PostgreSQL
- **Cache**: Redis
- **Deployment**: Docker

## 📖 Documentation

- `GET_STARTED.md` - Complete setup guide (30 min)
- `docs/DISCORD_SETUP.md` - Discord configuration
- `docs/TRADIER_SETUP.md` - Tradier setup
- `docs/DEPLOYMENT.md` - Production deployment

## ⚠️ Disclaimer

Trading involves substantial risk. Start with paper trading. Never invest more than you can afford to lose.

## 📄 License

MIT License

---

**Version**: 2.0.0 (Discord + Tradier Edition)
**Built with**: Python, FastAPI, Discord.py, Tradier API
