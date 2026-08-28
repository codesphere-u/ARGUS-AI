# Go-Live Checklist — Alvin Hacks FX / RIA Forex Bot

Research-backed protocol for taking this bot from supervised demo to a
real-money account. Sources: FxRobotEasy 12-step go-live, SignalBots forward-test
guide, FXVPS MT5 deployment / watchdog guides, BrokerChampion 2026 algo guide.

Do **not** skip steps. Tiny size first; scale only on live evidence.

---

## 0. Pre-flight (software)

- [ ] `python -m pytest tests/ -q` is green
- [ ] `.env` has `PAPER_TRADING=true` for soak (flip to `false` only at step 5)
- [ ] `.env` is **never** committed (see `.gitignore`)
- [ ] `CIRCUIT_BREAKER_ENABLED=true`, `DAILY_LOSS_LIMIT=0.03`, `CIRCUIT_BREAKER_FLATTEN=true`
- [ ] `MAX_SPREAD_ATR_FRACTION=0.15`, `MAX_CURRENCY_EXPOSURE=2`
- [ ] `ROCKET_SCALP_ATR=2.0`, `STOP_LOSS_ATR_MULT=1.5`, `LOSS_GUARD_SIZE_DOWN_AFTER=2`
- [ ] Single bot process only (`data/telegram_poll.lock` / no 409 in logs)
- [ ] MT5 **Algo Trading** button is green; AutoTrading survives terminal restart

## 0b. After code/env changes (positions + entries)

Always run **one** process only:

1. Stop the old `python main_bot.py run` (Ctrl+C or kill the duplicate)
2. Start: `python main_bot.py run`
3. Confirm startup logs: hydrate count + heartbeat with `open_local` / `open_bot` / `open_external`
4. Telegram `/positions`:
   - Bot-managed = magic `20260728` or Adopted tickets
   - External = manual / other EA (tap **Adopt** to manage)
5. Console when flat should look like:
   `No entry | top=EURUSDm SELL 0.65 < min 0.72 (hot 0.85)`
   (or an actual entry when score clears the bar)
6. Entry retune defaults: `MIN_ENTRY_SCORE=0.72`, `HOT_GRAB_SCORE=0.85`

## 0c. Expanded watchlist (FX + metals + crypto)

Configured `SYMBOLS` spans majors/crosses, XAU/XAG, and BTC/ETH/SOL/XRP/LTC/BNB (`*m` Exness-style).

- [ ] After restart, logs show `Watchlist OK …` and any `Watchlist SKIP (not on broker): …`
- [ ] Telegram startup lists **active** symbols only (skipped names are broker-missing)
- [ ] Confirm each active name appears in MT5 Market Watch
- [ ] Weekend: FX gated; crypto + XAG still scannable
- [ ] `/scan` shows the broader book; open cap still `MAX_OPEN_POSITIONS=4`

---

## 1. Account parity (demo ↔ live)

Open demo settings and the live account form side by side:

- [ ] Same broker server / execution venue (e.g. Exness trial vs live)
- [ ] Same account type (Standard / Raw / …)
- [ ] Same base currency
- [ ] Same leverage
- [ ] Same symbol suffixes (`EURUSDm`, etc.) resolve in Market Watch
- [ ] EAs / algorithmic trading explicitly allowed in T&Cs

Document: broker, licence, account type, server name, measured MT5 ping.

## 2. VPS / machine (operator)

Prefer a VPS in the **same region** as the broker’s execution server.

- [ ] Ping MT5 server (bottom-right of terminal) — target under ~20–50 ms retail
- [ ] Windows sleep = Never; disable auto-reboot during London/NY hours
- [ ] Schedule **weekly reboot** in off-hours (Saturday afternoon broker time)
- [ ] After any MT5 update: re-check Algo Trading is green
- [ ] Install Task Scheduler task for `watchdog.ps1` (every 5 minutes):

```text
Program: powershell.exe
Arguments: -ExecutionPolicy Bypass -File "C:\Users\darka\Desktop\ria forex bot\watchdog.ps1"
```

- [ ] Manually kill `python main_bot.py run` once and confirm watchdog relaunches + Telegram alert

## 3. Risk-rule verification (deliberate triggers)

On **demo**, confirm each rule fires and leaves a log/Telegram trail:

| Rule | How to verify | Expected |
|------|---------------|----------|
| Circuit breaker + flatten | Temporarily set `DAILY_LOSS_LIMIT=0.001` and force a losing day, or unit-test | Halt + flatten alert |
| LossGuard size-down | 2 losses → next entry uses 0.5× lot | Log `risk_scale=0.5` |
| Spread gate | Trade during thin liquidity / widen gate temporarily | `Spread too wide` skip |
| Weekend FX gate | Leave bot running Friday 21:00 UTC | FX entries blocked; crypto may still trade |
| Currency exposure | Open 2 USD-quote longs, try 3rd | Skip unless elite score |
| Magic-only close | Manual non-bot position on same symbol | Bot does not close it |
| Hydrate on restart | Restart with open bot tickets | Positions restored, no double entry |
| Poll lock | Start second `python main_bot.py run` | Second poller refuses to start |

Automated coverage: `tests/` already covers sizing, CB, rocket bars, LossGuard, exposure, weekend gate.

## 4. Supervised soak (48–72 hours)

- [ ] Run on the **target** machine/VPS with `PAPER_TRADING=true` (or tiny demo live)
- [ ] Leave unattended overnight at least once
- [ ] Read `data/logs/bot.log` for `error`, `failed`, `reconnect`, `409`, `Circuit breaker`
- [ ] Confirm heartbeat lines every ~5 minutes
- [ ] Confirm Telegram: status, entries, exits, coach, `/pnl`, `/journal` work
- [ ] Compare fills: spread, slippage, lot size vs risk $ cap

If reconnect storms or silent hangs appear — fix before funding.

## 5. First real-money deploy (tiny)

- [ ] Set `PAPER_TRADING=false`
- [ ] Drop risk via `/setrisk` to the smallest sensible $ (or lower `MAX_RISK_MONEY`)
- [ ] Keep `MAX_OPEN_POSITIONS` ≤ 4; prefer 1–2 until proven
- [ ] Goal for week 1–2: **execution fidelity**, not profit
- [ ] Watchdog + Telegram alerts must be quiet except intentional events

## 6. Scale-up rule

Scale **only** when all are true for 1–2 weeks:

- Live PnL path matches paper/demo behavior (no surprise fill disasters)
- Max adverse excursion / slippage within modeled ATR stops
- No unexplained reconnect loops or missed exits
- Daily loss CB never trips unexpectedly

Then raise `/setrisk` / `MAX_RISK_MONEY` in steps (e.g. +50–100%), never a leap.

## 7. Daily operator habit (5 minutes)

- [ ] Equity vs expectation (`/pnl` or `/status`)
- [ ] Open count local vs broker (heartbeat log)
- [ ] Watchdog log quiet (`data/logs/watchdog.log`)
- [ ] No stale heartbeat / MT5 disconnect alerts
- [ ] If live drifts hard from baseline → `/pause`, investigate, do not “hope”

## 8. Emergency actions

| Situation | Action |
|-----------|--------|
| Uncontrolled losses | `/pause` then `/closeall` |
| MT5 / VPS down | Watchdog should restart; if not, RDP and start manually |
| Suspected bad logic | `/pause`, set `PAPER_TRADING=true`, run pytest, review journal |
| Secrets leaked | Rotate Telegram bot token, Gemini key, MT5 password immediately |

---

## What this repo automates vs what you must do

**Automated here:** reconnect, spread gate, weekend FX gate, currency exposure,
CB flatten, tests, watchdog script, secrets gitignore, pinned deps.

**Manual:** VPS purchase/region, Windows update policy, account parity, capital
allocation, and the final go/no-go decision.
