# Tradier Setup Guide

## Account Creation

1. Visit https://tradier.com
2. Click "Open Account"
3. Choose "Individual Brokerage"
4. Complete application (5-10 minutes)

## API Access

1. Login to Tradier
2. Settings → API Access
3. Create token:
   - Sandbox token for testing
   - Live token for production

## Sandbox vs Live

### Sandbox (Paper Trading)
- URL: https://sandbox.tradier.com/v1
- Simulated trading
- No real money
- Perfect for testing

### Live Trading
- URL: https://api.tradier.com/v1
- Real money
- Real trades
- Use only after extensive testing

## Claude MCP Integration

1. Claude.ai → Settings → Connectors
2. Add Custom Connector:
   - URL: https://mcp.tradier.com/mcp
   - Headers:
     - API_KEY: your_tradier_key
     - PAPER_TRADING: true (for sandbox)

## Testing Connection

In Claude chat:
```
Check my Tradier account balance via MCP
```

Claude should return your account info.

## Common Issues

### "Invalid API Key"
- Verify key is correct
- Check sandbox vs live URL matches key type

### "No buying power"
- Fund account (live)
- Or check sandbox starting balance

### "Market closed"
- Trading hours: 9:30 AM - 4:00 PM ET
- Bot includes market hours check
