from argus_ai.core.models import TradePassport


class TradePassportFormatter:
    """Renders Trade Passports into high-impact Markdown and terminal visual representations."""

    @staticmethod
    def to_markdown(p: TradePassport) -> str:
        status_badge = {
            "APPROVED": "🟢 **APPROVED**",
            "DOWNSIZED": "🟡 **DOWNSIZED (50% RISK)**",
            "REJECTED": "🔴 **REJECTED (INSUFFICIENT EDGE)**",
            "VETOED": "⛔ **VETOED BY DEVIL'S ADVOCATE**",
        }.get(p.consensus.status, p.consensus.status)

        exec_badge = {
            "EXECUTED": "🚀 **EXECUTED ON ALPACA**",
            "SIMULATED": "🧪 **SIMULATION COMPLETED**",
            "REJECTED_BY_RISK": "🛑 **BLOCKED BY RISK RULES**",
            "ABORTED_BY_KILL_SWITCH": "🚨 **HALTED BY KILL SWITCH**",
            "PENDING": "⏳ **PENDING EXECUTION**"
        }.get(p.execution_status, p.execution_status)

        md = f"""# 🛡️ ARGUS AI — TRADE PASSPORT: `{p.passport_id}`

> **Generated:** `{p.timestamp}` | **Asset:** `{p.ticker}` | **Direction:** `{p.direction}`  
> **Committee Consensus:** {status_badge} | **Execution:** {exec_badge}

---

## 📊 Trade Specification & Bracket Strategy
| Parameter | Value | Description |
| :--- | :--- | :--- |
| **Asset / Ticker** | `{p.ticker}` | Asset Class: `{p.asset_class}` |
| **Target Entry** | `${p.target_entry:.2f}` | Market Price at Decision: `${p.current_price:.2f}` |
| **Stop Loss** | `${p.stop_loss:.2f}` | Hard stop based on 1.5x ATR |
| **Take Profit** | `${p.take_profit:.2f}` | Minimum Risk/Reward Ratio: `{p.risk_reward_ratio:.2f}:1` |
| **Position Size** | `{p.shares_or_contracts}` shares | Total Capital Allocated: `${p.position_size_usd:,.2f}` |
| **Account Risk** | `${p.account_risk_usd:,.2f}` | `{p.account_risk_pct:.2f}%` of Total Account Equity |

---

## 🏛️ Investment Committee Deliberation

### 1. Composite Consensus Summary
* **Decision:** {status_badge} (Composite Score: `{p.consensus.composite_score:+.1f}` / Confidence: `{p.consensus.overall_confidence * 100:.1f}%`)
* **Primary Catalyst:** {p.consensus.primary_catalyst}
* **Main Opposition:** {p.consensus.main_opposition}
* **Executive Summary:** *{p.consensus.executive_summary}*

### 2. Committee Agent Scorecard
"""
        for vote in p.agent_votes:
            badge = "🟢 BUY" if vote.recommendation == "BUY" else ("🔴 SELL" if vote.recommendation == "SELL" else "⚪ HOLD")
            md += f"""
#### 👤 **{vote.agent_name}** — {badge} (Score: `{vote.score:+.1f}` | Conf: `{vote.confidence * 100:.0f}%`)
* **Core Theses:** {', '.join(vote.key_arguments) if vote.key_arguments else 'None'}
* **Identified Concerns:** {', '.join(vote.concerns_raised) if vote.concerns_raised else 'None'}
"""

        md += f"""
---

## 😈 The Dissent: Devil's Advocate Stress-Test (USP)
* **Severity Level:** `{p.devils_advocate.severity_level}` (Disqualification Risk: `{p.devils_advocate.disqualification_risk_pct:.1f}%`)
* **Veto Triggered:** `{'YES ⛔' if p.devils_advocate.veto_recommended else 'NO ✅'}`
* **Adversarial Counter-Thesis:** *{p.devils_advocate.adversarial_thesis}*
* **Identified Market Traps:**
"""
        for trap in p.devils_advocate.identified_traps:
            md += f"  - ⚠️ {trap}\n"

        md += f"""* **Mandated Contingency:** {p.devils_advocate.recommended_contingency}

---

## ⚡ Alpaca Execution Audit
* **Alpaca Order ID:** `{p.alpaca_order_id or 'N/A'}`
* **Client Order ID:** `{p.alpaca_client_order_id or 'N/A'}`
* **Fill Price:** `${p.fill_price:.2f}` if p.fill_price else 'N/A'
"""

        if p.post_trade_reflection:
            md += f"""
---

## 🔍 Post-Trade Reflection
*{p.post_trade_reflection}*
"""
        return md
