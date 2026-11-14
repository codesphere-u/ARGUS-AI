# Getting Started with ARGUS AI (`v2.2.0`)

ARGUS AI is an autonomous multi-agent investment committee and trading desk with adversarial dissent, SHA-256 Trade Passports, and deterministic replay.

## 1. Install

```bash
git clone https://github.com/codesphere-u/ARGUS-AI.git
cd ARGUS-AI
uv sync --extra dev
```

## 2. Run Deterministic Committee & Backtest Commands

```bash
argus adversarial-demo
argus backtest --ticker NVDA --bars 120 --regime CHOPPY --vix 26
argus replay --passport argus_ai/data/passports/PASSPORT-20261109-1F5BB2.json
```

## 3. Start the HTTP Workbench or MCP Server

```bash
argus serve --host 127.0.0.1 --port 8080
argus mcp
```
