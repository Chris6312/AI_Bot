# Kraken ChatGPT MCP Setup

This project can already consume AI decisions through Discord/webhook intake. To make ChatGPT drive the crypto side in the same style as the stock side, use a small MCP bridge that wraps Kraken tools and forwards approved trade payloads to the bot webhook.

## Important constraint

Do **not** point ChatGPT directly at a local Kraken CLI binary. ChatGPT connects to **remote HTTPS MCP servers**, so the CLI needs to sit behind your own MCP service.

## Recommended architecture

1. **Kraken MCP bridge**
   - Runs on your machine or server
   - Exposes an HTTPS `/mcp` endpoint
   - Calls `kraken` in WSL or talks to Kraken REST/CLI internally

2. **Tools to expose**
   - `list_pairs`
   - `get_ticker`
   - `get_ohlc`
   - `preview_order`
   - `submit_trade_signal`

3. **Webhook handoff**
   - `submit_trade_signal` should post a JSON payload to the trading bot webhook
   - Keep the JSON contract parallel to the stock-side screening contract so downstream validation is consistent

## Suggested webhook contract for crypto

```json
{
  "type": "CRYPTO_SCREENING",
  "candidates": [
    {
      "pair": "BTC/USD",
      "ohlcvPair": "XBTUSD",
      "amount": 0.05,
      "side": "BUY"
    }
  ],
  "reasoning": "Momentum continuation with strong volume support",
  "source": "chatgpt-kraken-mcp"
}
```

## Minimal server flow

1. ChatGPT calls `get_ticker` / `get_ohlc`
2. ChatGPT decides on a crypto trade setup
3. ChatGPT calls `submit_trade_signal`
4. MCP server posts the JSON payload to your trading bot webhook
5. The bot applies the same safety validation + execution pipeline used for Discord-delivered decisions

## Local development notes

- Use `wsl bash -lc "kraken ..."` inside the MCP bridge if your CLI lives in WSL.
- Expose the MCP service through a tunnel such as Cloudflare Tunnel or ngrok during development.
- Refresh the ChatGPT app metadata after any MCP tool changes.

## Security recommendations

- Require authentication on the MCP service.
- Keep live-order placement behind a separate write tool or approval gate.
- Log every inbound tool call and every outbound webhook payload.
- Use preview tools first, then add submit tools after you trust the flow.

## Suggested next implementation slice

- Add a dedicated `/api/webhooks/ai-decisions` endpoint in the bot
- Store inbound payloads in PostgreSQL for audit history
- Add a crypto execution service that can translate webhook decisions into paper-ledger or authenticated Kraken actions depending on mode
