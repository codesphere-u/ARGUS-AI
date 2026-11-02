# Security Policy

## Supported Versions

| Version | Supported |
| :--- | :--- |
| `2.1.x` | ✅ Supported |
| `2.0.x` | ✅ Supported |
| `< 2.0` | ❌ Unsupported |

## Credential & Execution Safety Invariants

1. **Zero Secret Persistence**: API keys (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `GEMINI_API_KEY`, Telegram/Discord tokens) must **never** be committed to version control. Always use `.env` (which is git-ignored) or environment variables injected at runtime.
2. **Paper-Trading & Simulation by Default**: By default, `ALPUS_BASE_URL` points to `https://paper-api.alpaca.markets`, and when credentials are omitted, `AlpacaClient` runs in offline deterministic simulation mode (`SIMULATION`).
3. **Deterministic Pre-Execution Risk Gate**: No LLM output can bypass the deterministic `RiskCommander` (1.5x ATR stop loss, 1.0% max equity risk per trade) or `EmergencyKillSwitch` circuit breakers.

## Reporting a Vulnerability

If you discover a security vulnerability, credential exposure risk, or risk-gate bypass, please open a private security advisory on GitHub (`https://github.com/codesphere-u/ARGUS-AI/security/advisories`) rather than a public issue. We aim to acknowledge reports within 48 hours.
