# 🦙 ARGUS AI: Model Context Protocol (MCP) Integration Guide

The **Model Context Protocol (MCP)** enables AI assistants such as **Claude Desktop, Cursor, VS Code, and Gemini** to communicate with ARGUS AI and execute tools directly on your Alpaca trading account.

---

## 🛠️ MCP Server Tools Specification

The ARGUS MCP Server (`argus_ai/mcp_server.py`) provides the following tools:

| Tool Name | Parameters | Description |
| :--- | :--- | :--- |
| `argus_convene_committee` | `ticker`, `current_price`, `market_regime`, `vix_level` | Convenes the 7-agent committee (Quant, News, Macro, Options, Devil's Advocate) to produce a Trade Passport. |
| `argus_get_trade_passport` | `passport_id` | Retrieves the immutable audit record and pre-trade reasoning for any past trade. |
| `argus_emergency_kill_switch` | `action` (`TRIP`, `RESET`, `STATUS`), `justification` | Instantly trips or resets the portfolio circuit breaker and cancels pending Alpaca orders. |
| `alpaca_get_portfolio_telemetry` | *(None)* | Retrieves live account equity, buying power, daily PnL, and active open positions. |
| `alpaca_dispatch_bracket_order` | `symbol`, `qty`, `side`, `take_profit_price`, `stop_loss_price` | Dispatches pre-validated bracket orders directly to Alpaca paper or live accounts. |

---

## 💻 1. Connecting to Claude Desktop

Add ARGUS AI to your Claude Desktop configuration:

### macOS / Linux:
`~/Library/Application Support/Claude/claude_desktop_config.json`

### Windows:
`%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "argus-ai": {
      "command": "python",
      "args": [
        "-m",
        "argus_ai.mcp_server"
      ],
      "cwd": "C:/Users/darka/Desktop/ria forex bot",
      "env": {
        "ALPACA_API_KEY": "YOUR_ALPACA_API_KEY",
        "ALPACA_SECRET_KEY": "YOUR_ALPACA_SECRET_KEY",
        "GEMINI_API_KEY": "YOUR_GEMINI_API_KEY"
      }
    }
  }
}
```

---

## ⚡ 2. Connecting to Cursor IDE

1. Open **Cursor Settings** $\to$ **Features** $\to$ **MCP**.
2. Click **Add New MCP Server**:
   - **Name:** `ARGUS-AI`
   - **Type:** `stdio`
   - **Command:** `python -m argus_ai.mcp_server`
3. Restart Cursor. You can now prompt Composer:
   > *"Evaluate NVDA with the ARGUS committee and show me if Devil's Advocate issued a veto."*

---

## 🧪 3. Manual Testing via CLI

You can test the MCP Server directly in the terminal:
```bash
python -m argus_ai.cli mcp
```

Send a JSON-RPC payload via `stdin`:
```json
{"jsonrpc":"2.0","id":"1","method":"tools/list","params":{}}
```
