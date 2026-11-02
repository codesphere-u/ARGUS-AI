# ⚠️ Known Limitations, Assumptions & Risk Disclosures

> **Important Disclaimer:** ARGUS AI is an open-source research and engineering framework for autonomous multi-agent investment deliberation and risk governance. It operates in **Simulation / Paper-Trading Mode by default** and does not constitute financial or investment advice.

---

## 1. Execution & Microstructure Limitations

1. **Bar-Resolution Intrabar Approximation**:
   - In walk-forward backtests (`argus_ai/backtest.py`), bracket orders (`stop_loss` and `take_profit`) are resolved against hourly OHLC bars using a conservative **Stop-Loss-First** rule when both bounds are touched within the same bar, plus half-spread (`spread_bps`), commission (`commission_bps`), and ATR-proportional slippage (`slippage_atr_fraction`).
   - True L2/L3 order-book queue priority, partial fills, and sub-millisecond latency spikes during FOMC/CPI prints cannot be fully captured by OHLCV bar simulation.

2. **Overnight & Weekend Gap Risk**:
   - While `RiskCommander` enforces a `1.5x ATR` hard stop loss and `1.0%` maximum account equity risk at order placement, overnight earnings gaps or weekend geopolitical shocks in US Equities can cause an opening print beyond the stop level, resulting in realized losses exceeding the nominal `1.0%` risk budget.

---

## 2. LLM vs. Deterministic Heuristic Modes

1. **Deterministic Heuristic Fallback**:
   - When `GEMINI_API_KEY` is not configured (or during deterministic walk-forward backtesting and CI test runs), all committee agents execute their deterministic mathematical and rule-based engines. This guarantees `100%` reproducibility and zero network flakiness in tests and benchmarks.
2. **Live LLM Non-Determinism & Latency**:
   - When `GEMINI_API_KEY` is enabled in live/paper scanning, LLM reasoning introduces stochasticity (`temperature > 0`) and network latency (`~1.5s - 4.0s` per committee deliberation). Consequently, live LLM deliberations are suited for swing and intraday bar closures (`5m`, `15m`, `1h`, `1d`) rather than high-frequency tick scalping.

---

## 3. Adaptive Bayesian Weighting Scope

- `AdaptiveWeightManager` updates agent weights across the four thesis-generating specialists (`Quant`, `News`, `Macro`, `Options Flow`) via exponential moving performance attribution.
- By architectural design, the **Devil's Advocate Agent** and **Risk Commander** are **never down-weighted** by online learning—their veto and risk-cap powers remain non-negotiable circuit breakers regardless of recent winning streaks.
