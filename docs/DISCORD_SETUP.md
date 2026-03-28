# Discord Setup Guide

Complete guide to setting up Discord for the trading bot.

## Bot Creation

1. Go to https://discord.com/developers/applications
2. Create new application
3. Navigate to Bot tab
4. Enable required intents:
   - MESSAGE CONTENT INTENT
   - SERVER MEMBERS INTENT

## Required Permissions

Your bot needs:
- Read Messages/View Channels
- Send Messages
- Add Reactions
- Read Message History

## Webhook Setup

Webhooks allow Claude to post directly to Discord without needing Discord.py.

1. Channel Settings → Integrations → Webhooks
2. Create webhook
3. Copy URL
4. Give URL to Claude for posting decisions

## Message Format Examples

### JSON Format (Recommended)
```json
{
  "type": "SCREENING",
  "candidates": [
    {"ticker": "AAPL", "shares": 10},
    {"ticker": "MSFT", "shares": 15}
  ],
  "vix": 18.5
}
```

### Natural Language Format
```
SCREENING DECISION

BUY 10 shares AAPL
BUY 15 shares MSFT

Reasoning: Strong momentum, favorable VIX
```

Both formats work - bot parses either.

## Mobile Notifications

1. Install Discord mobile app
2. Join your trading server
3. Channel Settings → Notifications:
   - ✓ All Messages
   - ✓ Mobile Push Notifications
   - 🔊 Sound: On

Now you get instant alerts!

## Security

- Keep bot token secret
- Never share webhook URL publicly
- Use private channels for trading
- Enable 2FA on Discord account
