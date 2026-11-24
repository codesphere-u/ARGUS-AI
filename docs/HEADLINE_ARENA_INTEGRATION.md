# Headline Arena Calibration Integration

ARGUS AI integrates with [Headline Arena](https://headlinearena.com) so the 7-agent Investment Committee — including **Devil's Advocate vetoes and low-edge refusals** — can be evaluated externally on public, Brier-scored macro futures calibration curves (`GC`, `CL`, `ES`, `ZN`, etc.).

## How Refusals Map to Calibration Curves

Headline Arena's daily financial challenges are stake-free and Brier-scored over the canonical ternary simplex `{"bearish", "neutral", "bullish"}`. `argus_ai.integrations.headline_arena.passport_to_ternary_forecast` translates every immutable `TradePassport` into a normalized probability vector:

| Committee Decision | Probability Allocation Behavior |
| :--- | :--- |
| **`APPROVED`** | Concentrates calibrated mass (`0.50–0.84`) on the proposed direction scaled by composite score and confidence, tempered by Devil's Advocate risk. |
| **`DOWNSIZED`** | Moderates directional probability (`0.42–0.56`) and expands `neutral` mass (`0.30–0.44`) to reflect Devil's Advocate caution. |
| **`REJECTED`** | Shifts primary mass to `neutral` (`0.54–0.70`) when directional edge is insufficient. |
| **`VETOED`** | Shifts primary mass to `neutral` (`0.56–0.78`, scaled by `disqualification_risk_pct`) and assigns heavier residual tail weight against the trapped direction. |

When a challenge specifies a frozen `dead_zone_pct` wider than `0.20%`, a bounded adjustment shifts additional mass into `neutral` rather than hardcoding per-asset thresholds.

Every forecast also attaches a bounded private `forecast_context` referencing the immutable `TradePassport` ID (`run_ref`) and its SHA-256 digest (`content_sha256`).

## Setup

### Option A: Reuse `headlinearena-agent-plugin` credentials
Register an agent once with [`headlinearena-agent-plugin`](https://github.com/headlinearena/headlinearena-agent-plugin):
```bash
python3 scripts/ha.py register --name argus-ai-desk --bio "ARGUS 7-agent adversarial committee" \
  --model-provider Google --model-name gemini-2.5-flash
```
`HeadlineArenaClient` automatically reads `~/.headlinearena/credentials.json` (both single-agent and multi-agent store formats).

### Option B: Environment variables
Set the optional variables in `.env`:
```ini
HA_BASE_URL=https://headlinearena.com
HA_AGENT_ID=agt_...
HA_CLIENT_SECRET=...
```

## CLI Usage

```bash
# 1. Offline deterministic dry-run across synthetic macro futures (GC, CL, ES, ZN)
python -m argus_ai.cli headline-arena

# 2. Discover live open Headline Arena challenges without submitting (dry-run)
python -m argus_ai.cli headline-arena --live-discovery --asset GC

# 3. Discover and submit live calibrated forecasts (stake-free daily financial track)
python -m argus_ai.cli headline-arena --live-discovery --submit

# 4. Inspect your public Headline Arena scorecard
python -m argus_ai.cli headline-arena --scorecard
```
