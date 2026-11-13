"""
Production HTTP Workbench & Observability Server for ARGUS AI.

Uses Python standard library ``ThreadingHTTPServer`` so zero new runtime
dependencies are required. Exposes health/readiness probes, Prometheus and JSON
metrics, committee specifications, deterministic walk-forward backtests,
SHA-256 Trade Passport verification, and an interactive dark-mode Portal UI.
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import platform
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from pydantic import ValidationError

from argus_ai import __version__
from argus_ai.backtest import (
    AdversarialBacktestEngine,
    ExecutionCostModel,
    generate_multi_regime_dataset,
    replay_passport,
)
from argus_ai.config.settings import CONFIG
from argus_ai.core.adaptive_weights import AdaptiveWeightManager

logger = logging.getLogger("argus.http_server")

DEFAULT_MAX_BODY_BYTES = 1_048_576  # 1 MiB
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
TICKER_PATTERN = re.compile(r"^[A-Za-z0-9./_-]{1,20}$")

STATUS_REASON: dict[int, tuple[str, str]] = {
    400: ("bad_request", "Bad request."),
    404: ("not_found", "Requested endpoint was not found."),
    405: ("method_not_allowed", "HTTP method is not supported for this endpoint."),
    413: ("payload_too_large", "Request body exceeds maximum allowed size."),
    422: ("validation_error", "Request payload failed validation."),
    500: ("internal_error", "Internal server error."),
}


@dataclass
class ServerState:
    """Thread-safe observability metrics and runtime metadata for the HTTP server."""

    started_monotonic: float = field(default_factory=time.monotonic)
    started_at_utc: str = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    requests_total: int = 0
    errors_total: int = 0
    backtests_run_total: int = 0
    passport_verifications_total: int = 0
    passport_verification_failures_total: int = 0
    last_backtest_ticker: str = ""
    last_backtest_return_pct: float = 0.0
    last_backtest_vetoes: int = 0
    last_backtest_veto_saved_loss_usd: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_request(self, status_code: int) -> None:
        with self._lock:
            self.requests_total += 1
            if status_code >= 400:
                self.errors_total += 1

    def record_backtest(
        self,
        ticker: str,
        return_pct: float,
        vetoes: int,
        veto_saved_loss_usd: float,
    ) -> None:
        with self._lock:
            self.backtests_run_total += 1
            self.last_backtest_ticker = ticker
            self.last_backtest_return_pct = float(return_pct)
            self.last_backtest_vetoes = int(vetoes)
            self.last_backtest_veto_saved_loss_usd = float(veto_saved_loss_usd)

    def record_verification(self, is_valid: bool) -> None:
        with self._lock:
            self.passport_verifications_total += 1
            if not is_valid:
                self.passport_verification_failures_total += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            uptime = round(max(0.0, time.monotonic() - self.started_monotonic), 3)
            return {
                "uptime_seconds": uptime,
                "started_at_utc": self.started_at_utc,
                "requests_total": self.requests_total,
                "errors_total": self.errors_total,
                "backtests_run_total": self.backtests_run_total,
                "passport_verifications_total": self.passport_verifications_total,
                "passport_verification_failures_total": self.passport_verification_failures_total,
                "last_backtest": {
                    "ticker": self.last_backtest_ticker or None,
                    "return_pct": self.last_backtest_return_pct,
                    "vetoed_trades": self.last_backtest_vetoes,
                    "veto_saved_loss_usd": self.last_backtest_veto_saved_loss_usd,
                },
            }

    def render_prometheus(self) -> str:
        snap = self.snapshot()
        last = snap["last_backtest"]
        lines = [
            "# HELP argus_info Static build and version info for ARGUS AI.",
            "# TYPE argus_info gauge",
            f'argus_info{{version="{__version__}",deterministic_mode="true"}} 1',
            "# HELP argus_uptime_seconds HTTP server uptime in seconds.",
            "# TYPE argus_uptime_seconds gauge",
            f"argus_uptime_seconds {snap['uptime_seconds']:.3f}",
            "# HELP argus_http_requests_total Total HTTP requests handled.",
            "# TYPE argus_http_requests_total counter",
            f"argus_http_requests_total {snap['requests_total']}",
            "# HELP argus_http_errors_total Total HTTP 4xx/5xx responses returned.",
            "# TYPE argus_http_errors_total counter",
            f"argus_http_errors_total {snap['errors_total']}",
            "# HELP argus_backtests_run_total Total deterministic walk-forward backtests executed.",
            "# TYPE argus_backtests_run_total counter",
            f"argus_backtests_run_total {snap['backtests_run_total']}",
            "# HELP argus_passport_verifications_total Total Trade Passport integrity verifications.",
            "# TYPE argus_passport_verifications_total counter",
            f"argus_passport_verifications_total {snap['passport_verifications_total']}",
            "# HELP argus_passport_verification_failures_total Total failed Trade Passport verifications.",
            "# TYPE argus_passport_verification_failures_total counter",
            f"argus_passport_verification_failures_total {snap['passport_verification_failures_total']}",
            "# HELP argus_last_backtest_return_pct Net return percentage of the most recent backtest.",
            "# TYPE argus_last_backtest_return_pct gauge",
            f"argus_last_backtest_return_pct {last['return_pct']:.4f}",
            "# HELP argus_last_backtest_vetoes Devil's Advocate veto count of the most recent backtest.",
            "# TYPE argus_last_backtest_vetoes gauge",
            f"argus_last_backtest_vetoes {last['vetoed_trades']}",
            "# HELP argus_last_backtest_veto_saved_loss_usd Counterfactual USD loss prevented in the last backtest.",
            "# TYPE argus_last_backtest_veto_saved_loss_usd gauge",
            f"argus_last_backtest_veto_saved_loss_usd {last['veto_saved_loss_usd']:.2f}",
        ]
        return "\n".join(lines) + "\n"


def build_committee_spec() -> dict[str, Any]:
    """Build sanitized architectural specification of the ARGUS AI Investment Committee."""
    cost_defaults = ExecutionCostModel()
    return {
        "service": "argus-ai",
        "version": __version__,
        "deterministic_mode": True,
        "architecture": "7-Agent Adversarial Investment Committee",
        "agents": [
            {
                "name": "Quant Agent",
                "role": "Technical & Quantitative Analyst",
                "default_weight": AdaptiveWeightManager.DEFAULT_WEIGHTS["Quant Agent"],
                "signals": ["EMA(20)/EMA(50) alignment", "RSI(14) momentum", "ATR(14) volatility"],
            },
            {
                "name": "News & Sentiment Agent",
                "role": "Catalyst & Event Risk Analyst",
                "default_weight": AdaptiveWeightManager.DEFAULT_WEIGHTS["News & Sentiment Agent"],
                "signals": ["Positive catalyst keyword scoring", "Regulatory/headline risk flags"],
            },
            {
                "name": "Macro & Sector Agent",
                "role": "Macroeconomic & Regime Strategist",
                "default_weight": AdaptiveWeightManager.DEFAULT_WEIGHTS["Macro & Sector Agent"],
                "signals": ["VIX implied volatility regimes", "Broad market breadth (SPY/QQQ)"],
            },
            {
                "name": "Options Flow Agent",
                "role": "Derivatives & Positioning Specialist",
                "default_weight": AdaptiveWeightManager.DEFAULT_WEIGHTS["Options Flow Agent"],
                "signals": ["Put/Call ratio skew", "Dealer gamma positioning", "IV cost drag"],
            },
            {
                "name": "Devil's Advocate",
                "role": "Adversarial Risk Auditor & Veto Gate",
                "default_weight": 0.0,
                "authority": "Unilateral VETO at disqualification_risk >= 75%; 50% downsize at 45%-74.9%",
            },
            {
                "name": "Risk Commander",
                "role": "ATR Position Sizing & Bracket Engine",
                "default_weight": 0.0,
                "controls": ["1.5x ATR stop loss", "Minimum 2.0:1 reward-to-risk bracket"],
            },
            {
                "name": "Audit & Reflection Agent",
                "role": "Cryptographic Trade Passport & Replay Ledger",
                "default_weight": 0.0,
                "controls": ["SHA-256 Trade Passport digests", "PointInTimeGuard lookahead prevention"],
            },
        ],
        "decision_rules": {
            "rejection_edge_threshold": 25.0,
            "downsize_risk_threshold_pct": 45.0,
            "veto_risk_threshold_pct": 75.0,
            "downsize_factor": 0.50,
            "consensus_threshold": CONFIG.committee.consensus_threshold,
            "devils_advocate_veto_threshold": CONFIG.committee.devils_advocate_veto_threshold,
        },
        "risk_limits": {
            "max_account_risk_pct": CONFIG.risk.max_account_risk_pct,
            "max_portfolio_exposure_pct": CONFIG.risk.max_portfolio_exposure_pct,
            "max_daily_drawdown_pct": CONFIG.risk.max_daily_drawdown_pct,
            "max_position_size_usd": CONFIG.risk.max_position_size_usd,
            "max_consecutive_losses": CONFIG.risk.max_consecutive_losses,
            "default_risk_reward_ratio": CONFIG.risk.default_risk_reward_ratio,
        },
        "default_cost_model": cost_defaults.model_dump(),
        "watchlist": list(CONFIG.watchlist),
    }


def _require_non_bool_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"Field '{name}' must be an integer.")
    if value < minimum or value > maximum:
        raise ValueError(f"Field '{name}' must be between {minimum} and {maximum}.")
    return value


def _require_number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"Field '{name}' must be a number.")
    num = float(value)
    if not math.isfinite(num) or num < minimum or num > maximum:
        raise ValueError(f"Field '{name}' must be between {minimum} and {maximum}.")
    return num


def _parse_backtest_request(payload: dict[str, Any]) -> tuple[dict[str, Any], ExecutionCostModel]:
    ticker_raw = payload.get("ticker", "NVDA")
    if not isinstance(ticker_raw, str) or not TICKER_PATTERN.fullmatch(ticker_raw.strip()):
        raise ValueError("Field 'ticker' must be 1-20 alphanumeric or ./_- characters.")
    ticker = ticker_raw.strip().upper()

    bars = _require_non_bool_int(payload.get("bars", 240), "bars", 70, 2000)
    seed = _require_non_bool_int(payload.get("seed", 42), "seed", 0, 2_147_483_647)
    warmup_bars = _require_non_bool_int(payload.get("warmup_bars", 50), "warmup_bars", 20, 500)
    eval_stride = _require_non_bool_int(payload.get("eval_stride", 4), "eval_stride", 1, 50)
    holding_horizon = _require_non_bool_int(payload.get("holding_horizon", 12), "holding_horizon", 1, 100)
    initial_equity = _require_number(payload.get("initial_equity", 100_000.0), "initial_equity", 100.0, 1e9)

    if bars <= warmup_bars + holding_horizon:
        raise ValueError("Field 'bars' must exceed 'warmup_bars' + 'holding_horizon'.")

    cost_raw = payload.get("cost_model")
    if cost_raw is None:
        cost_model = ExecutionCostModel()
    elif isinstance(cost_raw, dict):
        spread_bps = _require_number(cost_raw.get("spread_bps", 4.0), "cost_model.spread_bps", 0.0, 500.0)
        commission_bps = _require_number(
            cost_raw.get("commission_bps", 1.5), "cost_model.commission_bps", 0.0, 500.0
        )
        slippage_atr_fraction = _require_number(
            cost_raw.get("slippage_atr_fraction", 0.03), "cost_model.slippage_atr_fraction", 0.0, 1.0
        )
        cost_model = ExecutionCostModel(
            spread_bps=spread_bps,
            commission_bps=commission_bps,
            slippage_atr_fraction=slippage_atr_fraction,
        )
    else:
        raise ValueError("Field 'cost_model' must be a JSON object when provided.")

    return {
        "ticker": ticker,
        "bars": bars,
        "seed": seed,
        "warmup_bars": warmup_bars,
        "eval_stride": eval_stride,
        "holding_horizon": holding_horizon,
        "initial_equity": initial_equity,
    }, cost_model


def render_portal_html() -> str:
    """Render the self-contained dark-mode ARGUS AI Committee & Backtest Portal UI."""
    spec_json = json.dumps(build_committee_spec())
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>ARGUS AI | Investment Committee &amp; Backtest Workbench</title>
  <style>
    :root {{
      --bg: #090d16;
      --card: rgba(15, 23, 42, 0.86);
      --border: rgba(255, 255, 255, 0.09);
      --text: #f8fafc;
      --muted: #94a3b8;
      --cyan: #06b6d4;
      --emerald: #10b981;
      --rose: #ef4444;
      --amber: #f59e0b;
      --purple: #a855f7;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
      background:
        radial-gradient(circle at 12% 10%, rgba(6, 182, 212, 0.14), transparent 36%),
        radial-gradient(circle at 88% 12%, rgba(168, 85, 247, 0.12), transparent 32%),
        var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}
    .wrap {{ max-width: 1200px; margin: 0 auto; padding: 28px 20px 56px; }}
    header {{
      display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
      gap: 16px; padding-bottom: 20px; border-bottom: 1px solid var(--border); margin-bottom: 24px;
    }}
    .brand h1 {{ margin: 0; font-size: 1.5rem; letter-spacing: 0.03em; }}
    .brand p {{ margin: 4px 0 0; color: var(--muted); font-size: 0.92rem; }}
    .pill-row {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
    .pill {{
      font-size: 0.78rem; padding: 5px 11px; border-radius: 999px;
      border: 1px solid var(--border); background: rgba(15, 23, 42, 0.9); color: var(--text);
      text-decoration: none;
    }}
    .pill.ok {{ border-color: rgba(16, 185, 129, 0.45); color: #6ee7b7; }}
    .pill.cyan {{ border-color: rgba(6, 182, 212, 0.45); color: #67e8f9; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 18px; }}
    .card {{
      background: var(--card); border: 1px solid var(--border); border-radius: 14px;
      padding: 20px; box-shadow: 0 12px 32px rgba(0, 0, 0, 0.35);
    }}
    .card h2 {{ margin: 0 0 6px; font-size: 1.08rem; color: #e2e8f0; }}
    .sub {{ color: var(--muted); font-size: 0.85rem; margin: 0 0 16px; }}
    label {{ display: block; font-size: 0.78rem; color: var(--muted); margin-bottom: 4px; }}
    input, select, textarea {{
      width: 100%; background: #0b1220; border: 1px solid rgba(148, 163, 184, 0.22);
      color: var(--text); border-radius: 8px; padding: 8px 10px; font-size: 0.9rem;
      font-family: inherit;
    }}
    textarea {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.8rem; min-height: 150px; }}
    .form-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
    button {{
      margin-top: 14px; background: linear-gradient(135deg, #0891b2, #06b6d4);
      color: #06212b; font-weight: 700; border: none; border-radius: 9px;
      padding: 10px 16px; cursor: pointer; font-size: 0.9rem;
    }}
    button.secondary {{
      background: rgba(148, 163, 184, 0.14); color: var(--text); border: 1px solid var(--border);
    }}
    .kpi-grid {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); gap: 10px; margin-top: 14px;
    }}
    .kpi {{
      background: rgba(9, 13, 22, 0.72); border: 1px solid var(--border); border-radius: 10px; padding: 11px;
    }}
    .kpi .k {{ font-size: 0.72rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }}
    .kpi .v {{ font-size: 1.15rem; font-weight: 700; margin-top: 2px; }}
    .pos {{ color: var(--emerald); }}
    .neg {{ color: var(--rose); }}
    .warn {{ color: var(--amber); }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 0.83rem; }}
    th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; font-size: 0.76rem; text-transform: uppercase; }}
    pre {{
      background: #060a12; border: 1px solid var(--border); border-radius: 10px; padding: 12px;
      overflow-x: auto; font-size: 0.78rem; color: #cbd5e1; margin-top: 12px;
    }}
    .full {{ margin-top: 18px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div class="brand">
        <h1>ARGUS AI &middot; Committee &amp; Backtest Portal</h1>
        <p>Deterministic Multi-Agent Investment Committee, Devil's Advocate Veto Attribution &amp; Trade Passport Verification</p>
      </div>
      <div class="pill-row">
        <span class="pill ok">READY &middot; DETERMINISTIC MODE</span>
        <span class="pill cyan">v{__version__}</span>
        <a class="pill" href="/health" target="_blank">/health</a>
        <a class="pill" href="/metrics" target="_blank">/metrics</a>
        <a class="pill" href="/api/committee-spec" target="_blank">/api/committee-spec</a>
      </div>
    </header>

    <div class="grid">
      <section class="card">
        <h2>Walk-Forward Backtest Runner</h2>
        <p class="sub">Runs point-in-time committee deliberation with Devil's Advocate counterfactual comparison.</p>
        <form id="bt-form">
          <div class="form-grid">
            <div>
              <label for="ticker">Ticker</label>
              <input id="ticker" name="ticker" value="NVDA" maxlength="20" />
            </div>
            <div>
              <label for="bars">Hourly Bars</label>
              <input id="bars" name="bars" type="number" value="240" min="70" max="2000" />
            </div>
            <div>
              <label for="seed">Deterministic Seed</label>
              <input id="seed" name="seed" type="number" value="42" min="0" />
            </div>
            <div>
              <label for="initial_equity">Initial Equity (USD)</label>
              <input id="initial_equity" name="initial_equity" type="number" value="100000" min="100" />
            </div>
          </div>
          <button type="submit">Run Deterministic Backtest</button>
        </form>
        <div id="bt-kpis" class="kpi-grid"></div>
        <pre id="bt-status">Ready. Click "Run Deterministic Backtest" to simulate POST /api/backtest.</pre>
      </section>

      <section class="card">
        <h2>Trade Passport SHA-256 Verifier</h2>
        <p class="sub">Verifies cryptographic digest integrity, risk limits, and Devil's Advocate veto invariants.</p>
        <label for="passport-json">TradePassport JSON Payload</label>
        <textarea id="passport-json" placeholder='{{"passport": {{...}}, "expected_digest": "..."}}'></textarea>
        <div>
          <label for="expected-digest" style="margin-top:8px;">Expected SHA-256 Digest (optional)</label>
          <input id="expected-digest" placeholder="64-char hex digest (optional)" />
        </div>
        <button id="verify-btn" type="button">Verify Passport Invariants</button>
        <pre id="verify-out">Paste a TradePassport JSON payload or wrapped {{"passport": ..., "expected_digest": ...}}.</pre>
      </section>
    </div>

    <section class="card full">
      <h2>7-Agent Committee Specification</h2>
      <p class="sub">Live quorum structure, adaptive weighting defaults, and Devil's Advocate veto gates.</p>
      <table>
        <thead>
          <tr><th>Agent</th><th>Role</th><th>Default Weight</th><th>Primary Mandate</th></tr>
        </thead>
        <tbody id="agents-body"></tbody>
      </table>
    </section>
  </div>

  <script>
    const INITIAL_SPEC = {spec_json};
    function renderSpec(spec) {{
      const tbody = document.getElementById("agents-body");
      tbody.innerHTML = spec.agents.map(a => {{
        const w = a.default_weight > 0 ? (a.default_weight * 100).toFixed(0) + "%" : "Governance";
        const detail = (a.signals || a.controls || [a.authority || ""]).join(", ");
        return `<tr><td><strong>${{a.name}}</strong></td><td>${{a.role}}</td><td>${{w}}</td><td>${{detail}}</td></tr>`;
      }}).join("");
    }}
    renderSpec(INITIAL_SPEC);

    document.getElementById("bt-form").addEventListener("submit", async (e) => {{
      e.preventDefault();
      const statusEl = document.getElementById("bt-status");
      const kpiEl = document.getElementById("bt-kpis");
      statusEl.textContent = "Running deterministic walk-forward simulation...";
      const body = {{
        ticker: document.getElementById("ticker").value,
        bars: Number(document.getElementById("bars").value),
        seed: Number(document.getElementById("seed").value),
        initial_equity: Number(document.getElementById("initial_equity").value),
      }};
      try {{
        const res = await fetch("/api/backtest", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json", "Accept": "application/json" }},
          body: JSON.stringify(body),
        }});
        const data = await res.json();
        if (!res.ok) {{
          statusEl.textContent = JSON.stringify(data, null, 2);
          return;
        }}
        const a = data.argus_committee;
        const b = data.naive_baseline;
        kpiEl.innerHTML = `
          <div class="kpi"><div class="k">ARGUS Return</div><div class="v ${{a.total_return_pct >= 0 ? 'pos' : 'neg'}}">${{a.total_return_pct.toFixed(2)}}%</div></div>
          <div class="kpi"><div class="k">Baseline Return</div><div class="v">${{b.total_return_pct.toFixed(2)}}%</div></div>
          <div class="kpi"><div class="k">DD Reduction</div><div class="v pos">-${{data.drawdown_reduction_pct.toFixed(2)}}%</div></div>
          <div class="kpi"><div class="k">DA Vetoes</div><div class="v warn">${{a.vetoed_trades_count}}</div></div>
          <div class="kpi"><div class="k">Losses Saved</div><div class="v pos">$${{a.veto_saved_loss_usd.toFixed(2)}}</div></div>
        `;
        statusEl.textContent = JSON.stringify({{
          ticker: data.ticker,
          evaluated_opportunities: data.evaluated_opportunities,
          return_outperformance_pct: data.return_outperformance_pct,
          sharpe_improvement: data.sharpe_improvement,
          veto_accuracy_pct: a.veto_accuracy_pct,
        }}, null, 2);
      }} catch (err) {{
        statusEl.textContent = "Request failed: " + String(err);
      }}
    }});

    document.getElementById("verify-btn").addEventListener("click", async () => {{
      const outEl = document.getElementById("verify-out");
      const raw = document.getElementById("passport-json").value.trim();
      const digest = document.getElementById("expected-digest").value.trim();
      if (!raw) {{
        outEl.textContent = "Enter TradePassport JSON first.";
        return;
      }}
      try {{
        let parsed = JSON.parse(raw);
        if (!parsed.passport) {{
          parsed = {{ passport: parsed }};
        }}
        if (digest) {{
          parsed.expected_digest = digest;
        }}
        const res = await fetch("/api/verify-passport", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json", "Accept": "application/json" }},
          body: JSON.stringify(parsed),
        }});
        const data = await res.json();
        outEl.textContent = JSON.stringify(data, null, 2);
      }} catch (err) {{
        outEl.textContent = "Verification error: " + String(err);
      }}
    }});
  </script>
</body>
</html>
"""


class ArgusHTTPServer(ThreadingHTTPServer):
    """Multi-threaded HTTP server carrying shared ARGUS state and body limits."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        state: ServerState | None = None,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ) -> None:
        self.state = state or ServerState()
        self.max_body_bytes = int(max_body_bytes)
        super().__init__(server_address, ArgusRequestHandler)


class ArgusRequestHandler(BaseHTTPRequestHandler):
    """Hardened HTTP handler for ARGUS AI workbench UI, API, and observability probes."""

    server_version = f"ARGUS-AI/{__version__}"

    def _argus_server(self) -> ArgusHTTPServer:
        return cast(ArgusHTTPServer, self.server)

    def _resolve_request_id(self) -> str:
        incoming = self.headers.get("X-Request-ID", "").strip()
        if REQUEST_ID_PATTERN.fullmatch(incoming):
            return incoming
        return str(uuid.uuid4())

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("%s - %s", self.address_string(), format % args)

    def _write_security_headers(
        self,
        request_id: str,
        *,
        is_api: bool,
        allow_methods: str | None = None,
    ) -> None:
        self.send_header("X-Request-ID", request_id)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept, X-Request-ID")
        self.send_header("Access-Control-Expose-Headers", "X-Request-ID")
        self.send_header("Access-Control-Max-Age", "600")
        if allow_methods:
            self.send_header("Allow", allow_methods)
        if is_api:
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "no-cache")

    def _send_bytes(
        self,
        status_code: int,
        content_type: str,
        body: bytes,
        request_id: str,
        *,
        is_api: bool,
        head_only: bool = False,
        allow_methods: str | None = None,
    ) -> None:
        self._argus_server().state.record_request(status_code)
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._write_security_headers(request_id, is_api=is_api, allow_methods=allow_methods)
        self.end_headers()
        if not head_only and body:
            self.wfile.write(body)

    def _send_json(
        self,
        status_code: int,
        payload: dict[str, Any],
        request_id: str,
        *,
        is_api: bool,
        head_only: bool = False,
        allow_methods: str | None = None,
    ) -> None:
        encoded = json.dumps(payload, indent=2).encode("utf-8")
        self._send_bytes(
            status_code,
            "application/json; charset=utf-8",
            encoded,
            request_id,
            is_api=is_api,
            head_only=head_only,
            allow_methods=allow_methods,
        )

    def _send_error_json(
        self,
        status_code: int,
        request_id: str,
        message: str | None = None,
        error_code: str | None = None,
        *,
        head_only: bool = False,
        allow_methods: str | None = None,
    ) -> None:
        default_code, default_msg = STATUS_REASON.get(status_code, ("error", "Request error."))
        self._send_json(
            status_code,
            {
                "error": error_code or default_code,
                "message": message or default_msg,
                "status": status_code,
                "request_id": request_id,
            },
            request_id,
            is_api=True,
            head_only=head_only,
            allow_methods=allow_methods,
        )

    def _read_json_body(self, *, allow_empty: bool = False) -> dict[str, Any]:
        max_bytes = self._argus_server().max_body_bytes
        raw_cl = self.headers.get("Content-Length")
        if raw_cl is not None:
            try:
                content_length = int(raw_cl)
            except ValueError as exc:
                raise ValueError("Invalid Content-Length header.") from exc
            if content_length < 0:
                raise ValueError("Negative Content-Length header is not allowed.")
            if content_length > max_bytes:
                raise _PayloadTooLargeError
            raw_bytes = self.rfile.read(content_length)
        else:
            raw_bytes = b""

        if len(raw_bytes) > max_bytes:
            raise _PayloadTooLargeError

        if not raw_bytes.strip():
            if allow_empty:
                return {}
            raise ValueError("Request body must contain valid JSON.")

        try:
            decoded = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Malformed JSON request body.") from exc

        if not isinstance(decoded, dict):
            raise TypeError("JSON request body must be a JSON object.")
        return decoded

    def _wants_html(self, query: dict[str, list[str]]) -> bool:
        fmt = query.get("format", [""])[0].lower()
        if fmt == "json":
            return False
        if fmt == "html":
            return True
        accept = self.headers.get("Accept", "").lower()
        return "text/html" in accept

    def _wants_json_metrics(self, query: dict[str, list[str]]) -> bool:
        fmt = query.get("format", [""])[0].lower()
        if fmt == "json":
            return True
        if fmt == "prometheus":
            return False
        accept = self.headers.get("Accept", "").lower()
        return "application/json" in accept and "text/plain" not in accept

    def _handle_read(self, *, head_only: bool) -> None:
        request_id = self._resolve_request_id()
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        state = self._argus_server().state

        try:
            if path == "/":
                if self._wants_html(query):
                    html_bytes = render_portal_html().encode("utf-8")
                    self._send_bytes(
                        200,
                        "text/html; charset=utf-8",
                        html_bytes,
                        request_id,
                        is_api=False,
                        head_only=head_only,
                    )
                    return
                self._send_json(
                    200,
                    {
                        "service": "argus-ai",
                        "version": __version__,
                        "status": "ok",
                        "deterministic_mode": True,
                        "endpoints": [
                            "/",
                            "/health",
                            "/healthz",
                            "/readyz",
                            "/info",
                            "/metrics",
                            "/api/committee-spec",
                            "/api/backtest",
                            "/api/verify-passport",
                        ],
                    },
                    request_id,
                    is_api=False,
                    head_only=head_only,
                )
                return

            if path in ("/health", "/healthz"):
                snap = state.snapshot()
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "service": "argus-ai",
                        "version": __version__,
                        "uptime_seconds": snap["uptime_seconds"],
                    },
                    request_id,
                    is_api=True,
                    head_only=head_only,
                )
                return

            if path == "/readyz":
                self._send_json(
                    200,
                    {
                        "status": "ready",
                        "ready": True,
                        "service": "argus-ai",
                        "version": __version__,
                        "deterministic_mode": True,
                    },
                    request_id,
                    is_api=True,
                    head_only=head_only,
                )
                return

            if path == "/info":
                snap = state.snapshot()
                self._send_json(
                    200,
                    {
                        "service": "argus-ai",
                        "version": __version__,
                        "python_version": platform.python_version(),
                        "deterministic_mode": True,
                        "started_at_utc": snap["started_at_utc"],
                        "uptime_seconds": snap["uptime_seconds"],
                        "watchlist": list(CONFIG.watchlist),
                        "endpoints": {
                            "ui": "GET / (Accept: text/html)",
                            "probes": ["GET /health", "GET /healthz", "GET /readyz", "GET /info"],
                            "metrics": "GET /metrics",
                            "committee_spec": "GET /api/committee-spec",
                            "backtest": "POST /api/backtest",
                            "verify_passport": "POST /api/verify-passport",
                        },
                    },
                    request_id,
                    is_api=True,
                    head_only=head_only,
                )
                return

            if path == "/metrics":
                if head_only:
                    if self._wants_json_metrics(query):
                        self._send_json(
                            200,
                            {"service": "argus-ai", "version": __version__, **state.snapshot()},
                            request_id,
                            is_api=True,
                            head_only=True,
                        )
                    else:
                        prom_bytes = state.render_prometheus().encode("utf-8")
                        self._send_bytes(
                            200,
                            "text/plain; version=0.0.4; charset=utf-8",
                            prom_bytes,
                            request_id,
                            is_api=True,
                            head_only=True,
                        )
                    return

                if self._wants_json_metrics(query):
                    self._send_json(
                        200,
                        {"service": "argus-ai", "version": __version__, **state.snapshot()},
                        request_id,
                        is_api=True,
                    )
                else:
                    prom_bytes = state.render_prometheus().encode("utf-8")
                    self._send_bytes(
                        200,
                        "text/plain; version=0.0.4; charset=utf-8",
                        prom_bytes,
                        request_id,
                        is_api=True,
                    )
                return

            if path == "/api/committee-spec":
                self._send_json(
                    200,
                    build_committee_spec(),
                    request_id,
                    is_api=True,
                    head_only=head_only,
                )
                return

            if path in ("/api/backtest", "/api/verify-passport"):
                self._send_error_json(
                    405,
                    request_id,
                    message="Use POST for this API endpoint.",
                    head_only=head_only,
                    allow_methods="POST, OPTIONS",
                )
                return

            self._send_error_json(404, request_id, head_only=head_only)
        except Exception:
            logger.exception("Unhandled error while serving %s %s [request_id=%s]", self.command, path, request_id)
            self._send_error_json(500, request_id, head_only=head_only)

    def do_GET(self) -> None:
        self._handle_read(head_only=False)

    def do_HEAD(self) -> None:
        self._handle_read(head_only=True)

    def do_OPTIONS(self) -> None:
        request_id = self._resolve_request_id()
        self._argus_server().state.record_request(204)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._write_security_headers(request_id, is_api=True)
        self.end_headers()

    def do_POST(self) -> None:
        request_id = self._resolve_request_id()
        path = urlparse(self.path).path
        state = self._argus_server().state

        try:
            if path in ("/", "/health", "/healthz", "/readyz", "/info", "/metrics", "/api/committee-spec"):
                self._send_error_json(
                    405,
                    request_id,
                    message="POST is not supported for this endpoint.",
                    allow_methods="GET, HEAD, OPTIONS",
                )
                return

            if path not in ("/api/backtest", "/api/verify-passport"):
                self._send_error_json(404, request_id)
                return

            try:
                body = self._read_json_body(allow_empty=(path == "/api/backtest"))
            except _PayloadTooLargeError:
                self._send_error_json(413, request_id)
                return
            except (TypeError, ValueError) as exc:
                self._send_error_json(400, request_id, message=str(exc), error_code="bad_request")
                return

            if path == "/api/backtest":
                try:
                    params, cost_model = _parse_backtest_request(body)
                except (TypeError, ValueError) as exc:
                    self._send_error_json(422, request_id, message=str(exc), error_code="validation_error")
                    return

                bars_df = generate_multi_regime_dataset(
                    ticker=params["ticker"],
                    n_bars=params["bars"],
                    seed=params["seed"],
                )
                engine = AdversarialBacktestEngine(
                    cost_model=cost_model,
                    initial_equity=params["initial_equity"],
                    warmup_bars=params["warmup_bars"],
                    eval_stride=params["eval_stride"],
                    holding_horizon=params["holding_horizon"],
                    use_llm=False,
                )
                report = engine.run_backtest(bars_df, ticker=params["ticker"])
                state.record_backtest(
                    ticker=report.ticker,
                    return_pct=report.argus_committee.total_return_pct,
                    vetoes=report.argus_committee.vetoed_trades_count,
                    veto_saved_loss_usd=report.argus_committee.veto_saved_loss_usd,
                )
                payload = {"deterministic_mode": True, **report.model_dump()}
                self._send_json(200, payload, request_id, is_api=True)
                return

            if path == "/api/verify-passport":
                if "passport" in body:
                    passport_source = body["passport"]
                    expected_digest = body.get("expected_digest")
                    max_risk = body.get("max_account_risk_pct")
                elif {"ticker", "consensus", "devils_advocate"} <= set(body.keys()):
                    passport_source = body
                    expected_digest = None
                    max_risk = None
                else:
                    self._send_error_json(
                        422,
                        request_id,
                        message="Payload must provide 'passport' (dict or JSON string) or a valid TradePassport object.",
                        error_code="validation_error",
                    )
                    return

                if expected_digest is not None and not isinstance(expected_digest, str):
                    self._send_error_json(
                        422,
                        request_id,
                        message="Field 'expected_digest' must be a string when provided.",
                        error_code="validation_error",
                    )
                    return

                max_risk_val: float | None = None
                if max_risk is not None:
                    try:
                        max_risk_val = _require_number(max_risk, "max_account_risk_pct", 0.01, 100.0)
                    except (TypeError, ValueError) as exc:
                        self._send_error_json(422, request_id, message=str(exc), error_code="validation_error")
                        return

                try:
                    verification = replay_passport(
                        passport_source,
                        expected_digest=expected_digest,
                        max_account_risk_pct=max_risk_val,
                    )
                except ValidationError as exc:
                    self._send_error_json(
                        422,
                        request_id,
                        message=f"TradePassport schema validation failed ({exc.error_count()} issue(s)).",
                        error_code="validation_error",
                    )
                    return
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    self._send_error_json(422, request_id, message=str(exc), error_code="validation_error")
                    return

                state.record_verification(verification.is_valid)
                self._send_json(200, verification.model_dump(), request_id, is_api=True)
                return
        except Exception:
            logger.exception("Unhandled error while serving POST %s [request_id=%s]", path, request_id)
            self._send_error_json(500, request_id)

    def _reject_unsupported_method(self) -> None:
        request_id = self._resolve_request_id()
        self._send_error_json(405, request_id, allow_methods="GET, POST, HEAD, OPTIONS")

    def do_PUT(self) -> None:
        self._reject_unsupported_method()

    def do_DELETE(self) -> None:
        self._reject_unsupported_method()

    def do_PATCH(self) -> None:
        self._reject_unsupported_method()


class _PayloadTooLargeError(Exception):
    """Internal sentinel raised when HTTP request body exceeds configured size cap."""


def create_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    state: ServerState | None = None,
) -> ArgusHTTPServer:
    """Instantiate an ``ArgusHTTPServer`` without blocking the caller."""
    return ArgusHTTPServer((host, port), state=state, max_body_bytes=max_body_bytes)


def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> None:
    """Start the blocking HTTP Workbench & Observability server until interrupted."""
    server = create_server(host=host, port=port, max_body_bytes=max_body_bytes)
    bound_host, bound_port = server.server_address[:2]
    logger.info("ARGUS AI HTTP Workbench listening on http://%s:%s", bound_host, bound_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down ARGUS AI HTTP Workbench.")
    finally:
        server.server_close()
