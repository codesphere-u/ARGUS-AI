# 🤝 ARGUS AI: Team Contribution & Development Guide

Welcome to the **ARGUS AI** development team! This guide provides everything needed to contribute new agents, integrate additional market data streams, extend Alpaca broker features, and maintain code quality.

---

## 🛠️ Development Setup

### 1. Prerequisites
- Python 3.10+
- Git
- PowerShell (Windows) or Bash (macOS/Linux)

### 2. Local Environment
```bash
# Clone the repository
git clone https://github.com/codesphereug/ARGUS-AI.git
cd "ria forex bot"

# Create virtual environment
python -m venv .venv

# Activate environment
# On Windows PowerShell:
.venv\Scripts\Activate.ps1
# On macOS/Linux:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Running Tests
Ensure all unit and integration tests pass before making changes:
```bash
python -m pytest
```

---

## 🏗️ How to Add a New Agent to the Committee

All agents inherit from `BaseAgent` located at `argus_ai/agents/base_agent.py`.

### Step 1: Create the Agent File
Create a new file in `argus_ai/agents/`, e.g., `options_agent.py`:

```python
from typing import List, Dict, Any
from argus_ai.agents.base_agent import BaseAgent
from argus_ai.core.models import AgentVote, MarketContext

class OptionsFlowAgent(BaseAgent):
    """
    Options Flow Agent: Evaluates institutional unusual options activity (UOA),
    put/call ratios, and dealer gamma positioning.
    """

    def __init__(self):
        super().__init__("Options Flow Agent")

    def analyze(self, ctx: MarketContext) -> AgentVote:
        # 1. Prepare LLM Prompts
        system_prompt = (
            "You are the Lead Derivatives & Options Flow Analyst for ARGUS AI. "
            "Evaluate options sentiment. Output JSON with fields: "
            "recommendation ('BUY', 'SELL', 'HOLD'), confidence (0.0 to 1.0), "
            "score (-100 to 100), key_arguments (list), concerns_raised (list)."
        )
        user_prompt = f"Ticker: {ctx.ticker}\nCurrent Price: ${ctx.current_price:.2f}"

        # 2. Query LLM with fallback
        llm_res = self.query_llm_json(system_prompt, user_prompt)
        if llm_res:
            return AgentVote(
                agent_name=self.name,
                recommendation=llm_res.get("recommendation", "HOLD"),
                confidence=float(llm_res.get("confidence", 0.7)),
                score=float(llm_res.get("score", 0.0)),
                key_arguments=llm_res.get("key_arguments", []),
                concerns_raised=llm_res.get("concerns_raised", [])
            )

        # 3. Deterministic Heuristic Fallback
        return AgentVote(
            agent_name=self.name,
            recommendation="BUY" if ctx.rsi_14 < 60 else "HOLD",
            confidence=0.75,
            score=40.0 if ctx.rsi_14 < 60 else 0.0,
            key_arguments=["Options call flow accumulation detected"],
            concerns_raised=[]
        )
```

### Step 2: Register in `CommitteeOrchestrator`
Edit `argus_ai/committee.py`:
1. Import your agent:
   ```python
   from argus_ai.agents.options_agent import OptionsFlowAgent
   ```
2. Initialize it in `__init__`:
   ```python
   self.options = OptionsFlowAgent()
   ```
3. Include its vote in `evaluate_opportunity` and update composite weights.

### Step 3: Write Unit Tests
Add tests in `tests/test_argus_committee.py` to verify your agent's scoring and heuristic fallback.

---

## 🦙 Extending Alpaca Broker Capabilities

The Alpaca integration is encapsulated in `argus_ai/core/alpaca_client.py`.

### Available Methods:
- `get_account_summary()`: Retrieves live equity, cash, buying power, and portfolio value.
- `place_bracket_order(symbol, qty, side, take_profit_price, stop_loss_price)`: Submits institutional bracket orders.
- `cancel_all_orders()`: Emergency kill-switch trigger to purge all open working orders.
- `close_all_positions()`: Emergency liquidation trigger.
- `get_historical_bars(symbol, timeframe, limit)`: Fetches OHLCV candlestick data.

---

## 🎨 Modifying the Web Dashboard

The web dashboard is located in `argus_dashboard/`:
- `index.html`: Layout structure, accessibility tags, and modal dialogs.
- `styles.css`: CSS variables, dark-mode gradients, and glassmorphic styling tokens.
- `app.js`: Tab handling, live deliberation animations, and Trade Passport rendering.

To test changes locally:
```bash
python -m http.server 3000 --directory "argus_dashboard"
```

---

## 📋 Code Standards & PR Checklist

- [ ] All new functions and classes have descriptive docstrings and type hints (`typing`).
- [ ] No hardcoded API keys or secrets in source code (use `.env`).
- [ ] All 41+ automated tests pass (`python -m pytest`).
- [ ] Code is formatted cleanly and adheres to PEP 8 standards.
- [ ] Any new risk parameters are deterministic and documented in `docs/ARCHITECTURE.md`.
