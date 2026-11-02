# ADR 001: Unilateral Devil's Advocate Veto & Non-Bypassable ATR Risk Gate

- **Status:** Accepted
- **Date:** 2026-11-02
- **Component:** `argus_ai/committee.py`, `argus_ai/agents/devils_advocate.py`, `argus_ai/agents/risk_commander.py`

---

## Context

Most LLM-based and single-model algorithmic trading bots suffer from **sycophantic consensus and confirmation bias**:
1. Momentum indicators (`EMA 20 > EMA 50`, positive price breakout) and bullish news headlines frequently peak simultaneously right at the top of an overextended blow-off move (`RSI > 68` amid rising implied volatility `VIX > 24`).
2. In standard weighted-voting ensembles, 3 bullish thesis agents (`Quant`, `News`, `Options`) easily outvote 1 cautious `Macro` agent, causing the system to enter full-sized long positions directly into liquidity air-pockets and bull traps.

---

## Decision

We decoupled **Thesis Generation** from **Adversarial Stress-Testing** and **Capital Risk Budgeting**:

1. **Four Thesis Specialists (`Quant`, `News`, `Macro`, `Options Flow`)** vote with dynamic Bayesian weights (`w_i`) to produce a directional `composite_score` in `[-100, +100]`.
2. **The Devil's Advocate Agent (`DevilsAdvocateAgent`)** does not cast a normal diluted vote. Instead, it receives the proposed direction and all supporting theses, explicitly searches for structural traps (e.g., overbought exhaustion + elevated VIX + choppy regime), and outputs a `disqualification_risk_pct` in `[0, 100]`:
   - **`disqualification_risk_pct >= 75.0%` (or `veto_recommended = True`)**: Unilateral **VETO** (`status = "VETOED"`, `downsize_factor = 0.0`). Even if all 4 thesis agents vote `BUY`, the trade is blocked before execution.
   - **`45.0% <= disqualification_risk_pct < 75.0%`**: Mandatory **50% Downsize** (`status = "DOWNSIZED"`, `downsize_factor = 0.50`).
   - **`disqualification_risk_pct < 45.0%`**: Full approval (`status = "APPROVED"`, `downsize_factor = 1.0`).
3. **Deterministic Risk Commander (`RiskCommander`)**: Computes `1.5x ATR` stop-loss and `>= 2:1` take-profit brackets and caps dollar risk at `1.0% * downsize_factor` of account equity.
4. **Tamper-Evident Auditability (`TradePassport` + `replay_passport`)**: Every decision—including vetoed proposals—mints a SHA-256 verifiable `TradePassport` that can be replayed offline to verify that neither the Devil's Advocate veto nor the Risk Commander cap was bypassed.

---

## Consequences

- **Positive**: Walk-forward counterfactual testing (`docs/BACKTEST_VERIFICATION_REPORT.md`) confirms that unilateral Devil's Advocate vetoes filter out crowded bull-trap tops, reducing maximum drawdown from `1.89%` to `0.41%` and improving Sharpe ratio from `-0.38` to `6.54` after realistic spread, commission, and ATR slippage costs.
- **Trade-off**: In rare parabolic "melt-up" regimes where `RSI > 70` persists while `VIX > 24`, the Devil's Advocate will conservatively veto or downsize continuation entries, prioritizing capital preservation over tail-momentum capture.
