# 📜 ARGUS AI: Trade Passport Specification

The **Trade Passport** is an immutable, cryptographic-style audit record generated for every trade opportunity evaluated by the ARGUS AI Investment Committee.

---

## 📋 Data Schema

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "TradePassport",
  "type": "object",
  "required": [
    "passport_id",
    "timestamp",
    "ticker",
    "direction",
    "current_price",
    "target_entry",
    "stop_loss",
    "take_profit",
    "risk_reward_ratio",
    "shares_or_contracts",
    "position_size_usd",
    "account_risk_usd",
    "consensus",
    "agent_votes",
    "devils_advocate",
    "execution_status"
  ],
  "properties": {
    "passport_id": {
      "type": "string",
      "example": "PASSPORT-20260828-NVDA01"
    },
    "timestamp": {
      "type": "string",
      "format": "date-time"
    },
    "ticker": {
      "type": "string",
      "example": "NVDA"
    },
    "asset_class": {
      "type": "string",
      "enum": ["US_EQUITY", "CRYPTO", "FOREX"]
    },
    "direction": {
      "type": "string",
      "enum": ["LONG", "SHORT"]
    },
    "current_price": {
      "type": "number"
    },
    "target_entry": {
      "type": "number"
    },
    "stop_loss": {
      "type": "number"
    },
    "take_profit": {
      "type": "number"
    },
    "risk_reward_ratio": {
      "type": "number"
    },
    "shares_or_contracts": {
      "type": "number"
    },
    "position_size_usd": {
      "type": "number"
    },
    "account_risk_usd": {
      "type": "number"
    },
    "account_risk_pct": {
      "type": "number"
    },
    "consensus": {
      "type": "object",
      "properties": {
        "status": {
          "type": "string",
          "enum": ["APPROVED", "DOWNSIZED", "REJECTED", "VETOED"]
        },
        "overall_confidence": { "type": "number" },
        "composite_score": { "type": "number" },
        "downsize_factor": { "type": "number" },
        "primary_catalyst": { "type": "string" },
        "main_opposition": { "type": "string" },
        "executive_summary": { "type": "string" },
        "unanimous": { "type": "boolean" }
      }
    },
    "agent_votes": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "agent_name": { "type": "string" },
          "recommendation": { "type": "string", "enum": ["BUY", "SELL", "HOLD", "VETO"] },
          "confidence": { "type": "number" },
          "score": { "type": "number" },
          "key_arguments": { "type": "array", "items": { "type": "string" } },
          "concerns_raised": { "type": "array", "items": { "type": "string" } }
        }
      }
    },
    "devils_advocate": {
      "type": "object",
      "properties": {
        "adversarial_thesis": { "type": "string" },
        "identified_traps": { "type": "array", "items": { "type": "string" } },
        "disqualification_risk_pct": { "type": "number" },
        "veto_recommended": { "type": "boolean" },
        "recommended_contingency": { "type": "string" },
        "severity_level": { "type": "string", "enum": ["LOW", "MODERATE", "SEVERE", "CRITICAL"] }
      }
    },
    "execution_status": {
      "type": "string",
      "enum": ["PENDING", "EXECUTED", "REJECTED_BY_RISK", "ABORTED_BY_KILL_SWITCH", "SIMULATED", "VETOED_BEFORE_DISPATCH"]
    },
    "alpaca_order_id": { "type": ["string", "null"] },
    "post_trade_reflection": { "type": ["string", "null"] }
  }
}
```

---

## 🔄 Trade Passport Lifecycle

1. **Minting**: Generated at the end of deliberation in `CommitteeOrchestrator.evaluate_opportunity`.
2. **Archival**: Stored as JSON files in `argus_ai/data/passports/` by `AuditAgent`.
3. **Execution Tagging**: Updated with Alpaca Order ID upon successful dispatch.
4. **Post-Trade Attribution**: Updated with post-trade reflection after position closure to evaluate prediction fidelity.
