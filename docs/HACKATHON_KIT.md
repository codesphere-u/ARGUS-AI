# 🏆 ARGUS AI: Hackathon Presentation & Demo Kit

> **Event:** Alpaca AI Trading Agents Hackathon (Lablab.ai)  
> **Project Name:** ARGUS AI — Autonomous Multi-Agent Trading Desk  
> **Tagline:** *"An AI Trading Desk that knows when to question itself, when to refuse a trade, and when to shut itself down."*

---

## 🎯 The Core Value Proposition (The Hook)

Most AI trading agents blindly predict direction and execute trades without questioning their own biases. When market conditions shift into low-liquidity traps or overbought blow-offs, these bots suffer catastrophic drawdowns.

**ARGUS AI solves this with Institutional Self-Dissent**:
- We built an autonomous **7-agent investment committee** where every trade is actively stress-tested by a dedicated **Devil's Advocate**.
- If the trade setup contains structural flaws, bull/bear traps, or unfavorable risk-reward, the Devil's Advocate exercises **unilateral VETO power** or **downsizes the position by 50%**.
- Every approved order is governed by deterministic **1.5x ATR stops** and dispatched to **Alpaca** with an immutable **Trade Passport**.
- An **Emergency Kill Switch** halts the entire desk if drawdown limits are breached.

---

## 🎬 3-Minute Demo Video Script

### **[0:00 - 0:40] The Problem & The Pitch**
> *"Hey everyone, this is ARGUS AI. Almost every AI trading bot built today suffers from confirmation bias. A model sees momentum, gets excited, and blindly places an order right into an institutional liquidity trap.*
> 
> *Instead of building another blind predictor, we built ARGUS AI — an autonomous AI trading desk that knows how to disagree with itself."*

### **[0:40 - 1:30] Live Committee Deliberation in Action**
> *(Show Web UI / CLI convening on NVDA)*  
> *"Watch what happens when we scan NVDA.  
> Our Quant Specialist sees strong momentum and votes BUY (+45 score).  
> Our News Agent detects positive enterprise demand headlines and votes BUY (+50 score).  
> In a conventional bot, this trade would immediately execute.  
> 
> But look at our Devil's Advocate agent. It intervenes, identifies that RSI is overextended at 76.2 with VIX at 26.4, detects an overbought liquidity flush trap, and calculates a 84% Disqualification Risk.  
> **The Devil's Advocate issues an adversarial VETO.** The trade is discarded, preserving $1,000 USD of risk capital."*

### **[1:30 - 2:15] Trade Passports & Alpaca Execution**
> *(Show BTC/USD healthy trend & Trade Passport modal)*  
> *"Now look at Bitcoin (BTC/USD).  
> Quant, News, and Macro are in healthy alignment. The Devil's Advocate stress-tests the trade and scores only 18% trap risk.  
> The trade is APPROVED.  
> 
> Risk Commander immediately calculates dynamic 1.5x ATR bracket prices — entry, stop-loss, and a 2.4:1 profit target — and routes the bracket order directly to the Alpaca Trading API.  
> An immutable Trade Passport is minted with full audit provenance."*

### **[2:15 - 3:00] Circuit Breakers & Conclusion**
> *(Click 'Trip Kill Switch' on UI)*  
> *"Finally, if market conditions turn catastrophic or daily drawdown hits 3%, our Emergency Kill Switch trips instantly, freezing the portfolio and canceling all working Alpaca orders.  
> 
> ARGUS AI: Autonomous, self-questioning, explainable, and institutionally safe. Powered by Alpaca and multi-agent AI."*

---

## 💡 Judge Q&A Cheat Sheet

**Q: Why have a Devil's Advocate instead of just fine-tuning a single model?**  
**A:** Multi-agent adversarial separation prevents catastrophic groupthink. A single prompt often averages out contradictory evidence; dedicated thesis agents debate independently, and the Devil's Advocate has a singular goal — capital preservation through rigorous counter-theses.

**Q: How does this integrate with Alpaca?**  
**A:** We use the Alpaca Trading API for real-time account telemetry, historical market data fetching, and bracket order execution (combining entry, limit take-profit, and stop-loss into single atomic orders).

**Q: What prevents LLM hallucinations from making dangerous trade decisions?**  
**A:** All risk parameters (stop-loss distances, position sizing, exposure ceilings, drawdown thresholds) are computed deterministically using standard mathematical formulas (ATR, fixed risk % budgeting) and validated by hard Python circuit breakers before hitting the broker.
