# 🤝 ARGUS AI 2.0: Team Contribution & Development Guide

Welcome to the **ARGUS AI 2.0** development team! This guide provides everything needed to contribute new agents, integrate data streams, extend Alpaca broker features, and build MCP tools.

---

## 🛠️ Development Setup

### 1. Prerequisites
- Python 3.10+
- Git
- PowerShell (Windows) or Bash (macOS/Linux)

### 2. Local Environment
```bash
# Clone repository
git clone https://github.com/codesphereug/ARGUS-AI.git
cd "ria forex bot"

# Create virtual environment
python -m venv .venv

# Activate environment
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Running Automated Tests
Run the 49-test automated suite before opening pull requests:
```bash
python -m pytest
```

---

## 🏗️ How to Add a New Agent to the Committee

All committee agents inherit from `BaseAgent` located at `argus_ai/agents/base_agent.py`.

### Step 1: Create the Agent File
Create a new file in `argus_ai/agents/`, e.g., `crypto_onchain_agent.py`:

```python
from typing import List, Dict, Any
from argus_ai.agents.base_agent import BaseAgent
from argus_ai.core.models import AgentVote, MarketContext

class CryptoOnChainAgent(BaseAgent):
    """
    Crypto On-Chain Agent: Evaluates whale wallet movements, exchange net inflows/outflows,
    and funding rates.
    """

    def __init__(self):
        super().__init__("Crypto On-Chain Agent")

    def analyze(self, ctx: MarketContext) -> AgentVote:
        # LLM reasoning with deterministic heuristic fallback
        return AgentVote(
            agent_name=self.name,
            recommendation="BUY",
            confidence=0.82,
            score=45.0,
            key_arguments=["Whale accumulation active; exchange reserves declining"],
            concerns_raised=[]
        )
```

### Step 2: Register in `CommitteeOrchestrator` & `AdaptiveWeightManager`
1. Edit `argus_ai/committee.py` to instantiate and call your agent.
2. Edit `argus_ai/core/adaptive_weights.py` to include your agent in the default Bayesian weighting dictionary.

### Step 3: Write Unit Tests
Add tests in `tests/test_argus_v2.py` to verify your agent's scoring, voting, and fallback behavior.

---

## 🦙 Testing MCP Server & Tools

ARGUS AI implements an official Model Context Protocol (MCP) server in `argus_ai/mcp_server.py`.

To test MCP tools locally:
```bash
python -m argus_ai.cli mcp
```
See **[docs/MCP_INTEGRATION_GUIDE.md](MCP_INTEGRATION_GUIDE.md)** for configuration instructions with Claude Desktop and Cursor.

---

## 🎨 Web Dashboard Development

The web dashboard is located in `argus_dashboard/`:
- `index.html`: Layout structure, accessibility tags, and modal dialogs.
- `styles.css`: CSS variables, dark-mode gradients, and glassmorphic styling tokens.
- `app.js`: Tab handling, live deliberation animations, and Trade Passport rendering.

To test changes locally:
```bash
python -m http.server 3000 --directory "argus_dashboard"
```

---

## 📋 PR Submission Checklist

- [ ] All new functions and classes have type annotations and docstrings.
- [ ] No hardcoded secrets or API keys in source code (use `.env`).
- [ ] All 49+ automated tests pass (`python -m pytest`).
- [ ] Code is formatted cleanly and adheres to PEP 8 standards.
- [ ] New risk parameters are documented in `docs/ARCHITECTURE.md`.
