# Changelog

All notable changes to **ARGUS AI** are documented in this file.

## [2.1.0] - 2026-11-02

### Added
- **Deterministic Historical Backtesting & Counterfactual Veto Engine (`argus_ai/backtest.py`)**:
  - `PointInTimeGuard` & `LookaheadLeakageError`: Enforces strict `timestamp <= decision_timestamp` and monotonic ordering on every historical slice passed to the 7-agent Investment Committee.
  - `ExecutionCostModel`: Models bid-ask half-spread (`spread_bps`), broker commission (`commission_bps`), and volatility-scaled slippage (`slippage_atr_fraction` of 14-bar ATR).
  - `AdversarialBacktestEngine`: Runs walk-forward counterfactual comparisons between the **ARGUS 7-Agent Investment Committee** and a **Naive Single-Agent Quant Baseline**, tracking `veto_saved_loss_usd`, `veto_accuracy_pct`, `downsized_trades_count`, and `avg_disagreement_entropy`.
  - `replay_passport` & `compute_passport_digest`: Deterministic SHA-256 Trade Passport integrity and risk invariant verification (`BRACKET_GEOMETRY`, `RISK_REWARD_FLOOR`, `DEVILS_ADVOCATE_VETO_AUTHORITY`, `DEVILS_ADVOCATE_DOWNSIZE_RULE`, `ACCOUNT_RISK_BUDGET_CAP`).
- **CLI Commands (`argus_ai/cli.py`)**: Added `backtest` and `replay` subcommands with Rich institutional tables and Markdown report export.
- **Reproducible Verification Report (`docs/BACKTEST_VERIFICATION_REPORT.md`)**: Walk-forward multi-regime benchmark report.
- **Architecture Decision Record (`docs/decisions/001-adversarial-devils-advocate-veto.md`)** and **Known Limitations (`docs/limitations.md`)**.
- **CI/CD & Packaging (`pyproject.toml`, `.github/workflows/ci.yml`)**: Full automated pytest suite (61 tests passing) and optional `gymnasium` fallback in `environment.py`.

## [2.0.0] - 2026-08-28

### Added
- **Options Flow & Derivatives Agent (`argus_ai/agents/options_agent.py`)** and **Adaptive Bayesian Weight Manager (`argus_ai/core/adaptive_weights.py`)**.
- **Model Context Protocol (MCP) Server (`argus_ai/mcp_server.py`)** exposing `argus_convene_committee`, `argus_emergency_kill_switch`, and `alpaca_get_portfolio_telemetry`.
- **24/7 WebSocket Stream Listener (`argus_ai/core/streamer.py`)** and interactive Telegram/Discord alert payloads.

## [1.0.0] - 2026-08-15

### Added
- Core 7-Agent Investment Committee (`Quant`, `News`, `Macro`, `Devil's Advocate`, `Risk Commander`, `Execution`, `Audit`).
- Unilateral Devil's Advocate Veto (`disqualification_risk_pct >= 75%`) and 50% Downsize (`>= 45%`) gates.
- Immutable JSON **Trade Passports** and **Emergency Kill Switch** circuit breakers.
