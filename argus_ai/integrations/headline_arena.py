"""
Headline Arena Calibration Integration for ARGUS AI.

Connects the ARGUS 7-agent Investment Committee (including Devil's Advocate
vetoes and refusals) to Headline Arena's Brier-scored daily financial forecast
challenges on macro futures (GC, CL, ES, ZN, etc.), supporting:

1. Calibrated TradePassport -> canonical ternary probability vector mapping
   (``{"bearish", "neutral", "bullish"}``) so approvals, downsizes, rejections,
   and Devil's Advocate vetoes all register cleanly on public calibration curves.
2. Cryptographic traceability via optional private ``forecast_context``
   carrying the SHA-256 ``TradePassport`` digest as an external evidence ref.
3. Zero-extra-dependency HTTPS REST client compatible with
   ``~/.headlinearena/credentials.json`` from ``headlinearena-agent-plugin``.
4. Offline deterministic dry-run mode and local Brier / Headline Arena scoring.
"""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, model_validator

from argus_ai.agents.audit_agent import AuditAgent
from argus_ai.backtest import compute_passport_digest
from argus_ai.committee import CommitteeOrchestrator
from argus_ai.config.settings import CONFIG, HeadlineArenaSettings
from argus_ai.core.models import MarketContext, TradePassport

logger = logging.getLogger("argus.headline_arena")

TernaryOutcome = Literal["bearish", "neutral", "bullish"]
TOKEN_REFRESH_MARGIN_SECONDS = 60
MODEL_PROVIDER_MAX = 64
MODEL_NAME_MAX = 128
MODEL_VERSION_MAX = 64


class HeadlineArenaError(RuntimeError):
    """Raised when Headline Arena configuration, validation, or HTTP requests fail."""


class HeadlineArenaChallenge(BaseModel):
    """Normalized representation of an open Headline Arena financial challenge."""

    challenge_id: str
    asset: str
    question: str = ""
    status: str = "open"
    deadline: str = ""
    open_price: float = Field(gt=0.0)
    dead_zone_pct: float | None = Field(default=None, ge=0.0)
    resolution_criteria: str = ""
    scope_key: str | None = None
    contract_symbol: str | None = None
    contract_month: str | None = None
    ohlc_bars: list[dict[str, Any]] = Field(default_factory=list)
    recent_news: list[str] = Field(default_factory=list)
    vix_level: float = 18.0


class TernaryForecast(BaseModel):
    """Canonical Headline Arena ternary probability submission derived from a TradePassport."""

    challenge_id: str
    asset: str
    probabilities: dict[TernaryOutcome, float]
    direction: TernaryOutcome
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    summary: str = Field(max_length=500)
    passport_id: str
    passport_digest: str
    consensus_status: Literal["APPROVED", "DOWNSIZED", "REJECTED", "VETOED"]
    disqualification_risk_pct: float = Field(ge=0.0, le=100.0)
    forecast_context: dict[str, Any]

    @model_validator(mode="after")
    def _validate_probability_simplex(self) -> TernaryForecast:
        required_keys = {"bearish", "neutral", "bullish"}
        if set(self.probabilities.keys()) != required_keys:
            raise ValueError("probabilities must have exact keys: bearish, neutral, bullish")
        for key, value in self.probabilities.items():
            if not math.isfinite(value) or value < 0.0 or value > 1.0:
                raise ValueError(f"probability for {key} must be finite and in [0, 1]")
        total = sum(self.probabilities.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"probabilities must sum to 1.0 within 1e-6, got {total}")
        return self


class HeadlineArenaRunResult(BaseModel):
    """End-to-end result of evaluating one challenge through the ARGUS committee."""

    challenge: HeadlineArenaChallenge
    passport: TradePassport
    forecast: TernaryForecast
    submitted: bool = False
    dry_run: bool = True
    response: dict[str, Any] = Field(default_factory=dict)


class CalibrationSummary(BaseModel):
    """Local calibration and refusal-quality metrics over resolved ternary forecasts."""

    count: int
    brier_score: float
    headline_arena_avg_score: float
    directional_accuracy: float
    refusal_count: int
    refusal_calibration_rate: float


def normalize_origin(raw: str) -> str:
    """Normalize a Headline Arena origin/base URL and enforce HTTPS outside localhost."""
    cleaned = raw.strip().rstrip("/").removesuffix("/api/v1")
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1"):
        raise HeadlineArenaError(
            f"Insecure Headline Arena base URL '{raw}': HTTPS is required except for localhost."
        )
    if not parsed.netloc:
        raise HeadlineArenaError(f"Invalid Headline Arena base URL '{raw}'.")
    return cleaned


def build_model_attribution_fields(
    provider: str | None = None,
    name: str | None = None,
    version: str | None = None,
) -> dict[str, str]:
    """Validate optional per-prediction model attribution fields matching Headline Arena's gate."""
    p = provider.strip() if isinstance(provider, str) and provider.strip() else None
    n = name.strip() if isinstance(name, str) and name.strip() else None
    v = version.strip() if isinstance(version, str) and version.strip() else None

    if (p is None) != (n is None):
        raise HeadlineArenaError("model_provider and model_name must be provided together or both omitted.")
    if v is not None and p is None:
        raise HeadlineArenaError("model_version requires both model_provider and model_name.")

    for label, value, limit in (
        ("model_provider", p, MODEL_PROVIDER_MAX),
        ("model_name", n, MODEL_NAME_MAX),
        ("model_version", v, MODEL_VERSION_MAX),
    ):
        if value is not None and len(value) > limit:
            raise HeadlineArenaError(f"{label} must be at most {limit} characters.")

    if p is None or n is None:
        return {}
    fields: dict[str, str] = {"model_provider": p, "model_name": n}
    if v is not None:
        fields["model_version"] = v
    return fields


def _normalize_ternary(bearish: float, neutral: float, bullish: float) -> dict[TernaryOutcome, float]:
    """Clamp, normalize, and round a 3-way distribution to sum strictly to 1.0."""
    raw = {
        "bearish": max(0.01, float(bearish)),
        "neutral": max(0.01, float(neutral)),
        "bullish": max(0.01, float(bullish)),
    }
    total = sum(raw.values())
    rounded: dict[TernaryOutcome, float] = {
        "bearish": round(raw["bearish"] / total, 4),
        "neutral": round(raw["neutral"] / total, 4),
        "bullish": round(raw["bullish"] / total, 4),
    }
    drift = round(1.0 - sum(rounded.values()), 4)
    if drift != 0.0:
        anchor: TernaryOutcome = max(rounded, key=lambda k: rounded[k])
        rounded[anchor] = round(rounded[anchor] + drift, 4)
    return rounded


def passport_to_ternary_forecast(
    passport: TradePassport,
    challenge: HeadlineArenaChallenge | None = None,
) -> TernaryForecast:
    """
    Map an ARGUS ``TradePassport`` into a calibrated Headline Arena ``TernaryForecast``.

    Key design property: ARGUS refusals (``VETOED`` / ``REJECTED``) are expressed
    as calibrated probability mass on ``neutral`` (with asymmetric trap tail risk
    on vetoes) rather than dropped silently, so over-refusal and under-refusal
    both surface directly on Headline Arena's Brier calibration curve.
    """
    status = passport.consensus.status
    score_strength = min(abs(passport.consensus.composite_score) / 100.0, 1.0)
    confidence = min(max(passport.consensus.overall_confidence, 0.0), 1.0)
    da_risk = min(max(passport.devils_advocate.disqualification_risk_pct / 100.0, 0.0), 1.0)
    is_bullish = passport.direction == "LONG"

    if status == "VETOED":
        # Devil's Advocate veto flags an active bull/bear trap: shift primary mass
        # to neutral and assign slightly more residual probability against the trapped side.
        p_neutral = 0.56 + 0.22 * da_risk
        residual = 1.0 - p_neutral
        p_proposed = residual * 0.35
        p_opposite = residual * 0.65
    elif status == "REJECTED":
        # Insufficient directional edge: center on neutral with gentle directional lean.
        p_neutral = 0.54 + 0.16 * (1.0 - score_strength)
        residual = 1.0 - p_neutral
        lean = 0.50 + 0.20 * score_strength
        p_proposed = residual * lean
        p_opposite = residual * (1.0 - lean)
    elif status == "DOWNSIZED":
        # Cautious approval: moderate directional probability tempered by DA risk.
        p_proposed = 0.42 + 0.14 * (0.5 * confidence + 0.5 * score_strength) * (1.0 - 0.35 * da_risk)
        p_neutral = 0.30 + 0.14 * da_risk
        p_opposite = max(0.05, 1.0 - p_proposed - p_neutral)
    else:
        # Full committee approval: strong calibrated directional probability, never over-certain.
        conviction = 0.55 * confidence + 0.45 * score_strength
        p_proposed = 0.50 + 0.34 * conviction * (1.0 - 0.30 * da_risk)
        p_neutral = max(0.08, (1.0 - p_proposed) * (0.58 + 0.18 * da_risk))
        p_opposite = max(0.04, 1.0 - p_proposed - p_neutral)

    # Honor challenge's frozen dead_zone_pct (wider neutral band shifts small bounded mass to neutral).
    if challenge is not None and challenge.dead_zone_pct is not None and challenge.dead_zone_pct > 0.20:
        band_shift = min(0.08, (challenge.dead_zone_pct - 0.20) * 0.12)
        p_neutral += band_shift
        p_proposed = max(0.05, p_proposed - band_shift * 0.65)
        p_opposite = max(0.03, p_opposite - band_shift * 0.35)

    p_bull = p_proposed if is_bullish else p_opposite
    p_bear = p_opposite if is_bullish else p_proposed
    probs = _normalize_ternary(bearish=p_bear, neutral=p_neutral, bullish=p_bull)

    direction: TernaryOutcome = max(probs, key=lambda k: probs[k])
    calibrated_confidence = probs[direction]

    digest = compute_passport_digest(passport)
    vote_summary = ", ".join(
        f"{v.agent_name}={v.recommendation}({v.score:+.0f}, {v.confidence * 100:.0f}%)"
        for v in passport.agent_votes
    )
    traps = "; ".join(passport.devils_advocate.identified_traps[:2]) or "none"
    criteria_note = (
        f" Resolution criteria reviewed: {challenge.resolution_criteria}"
        if challenge and challenge.resolution_criteria
        else ""
    )
    dead_zone_note = (
        f" Frozen dead-zone band: +/-{challenge.dead_zone_pct:.2f}%."
        if challenge and challenge.dead_zone_pct is not None
        else ""
    )

    reasoning = (
        f"ARGUS 7-agent committee status={status} (proposed={passport.direction}, "
        f"composite={passport.consensus.composite_score:+.1f}, "
        f"confidence={passport.consensus.overall_confidence * 100:.0f}%). "
        f"Votes: [{vote_summary}]. "
        f"Devil's Advocate disqualification risk={passport.devils_advocate.disqualification_risk_pct:.1f}% "
        f"(traps: {traps}). "
        f"Calibrated ternary distribution: bearish={probs['bearish']:.4f}, "
        f"neutral={probs['neutral']:.4f}, bullish={probs['bullish']:.4f}."
        f"{dead_zone_note}{criteria_note}"
    )

    raw_summary = (
        f"ARGUS {status} ({passport.ticker}): {direction.upper()} {calibrated_confidence * 100:.0f}% "
        f"| score {passport.consensus.composite_score:+.1f}, DA risk "
        f"{passport.devils_advocate.disqualification_risk_pct:.0f}% | passport {passport.passport_id}"
    )
    summary = raw_summary[:500]

    observed_at = passport.timestamp
    if observed_at.endswith("+00:00"):
        observed_at = observed_at[:-6] + "Z"
    elif "Z" not in observed_at and "+" not in observed_at:
        observed_at = observed_at + "Z"

    forecast_context: dict[str, Any] = {
        "schema_version": "1",
        "decision": "initial",
        "observed_at": observed_at,
        "run_ref": passport.passport_id[:128],
        "evidence_refs": [
            {
                "kind": "external",
                "namespace": "argus-trade-passport",
                "id": passport.passport_id[:128],
                "content_sha256": digest,
            }
        ],
    }

    challenge_id = challenge.challenge_id if challenge else f"offline-{passport.ticker.lower()}"
    asset = challenge.asset if challenge else passport.ticker

    return TernaryForecast(
        challenge_id=challenge_id,
        asset=asset,
        probabilities=probs,
        direction=direction,
        confidence=calibrated_confidence,
        reasoning=reasoning,
        summary=summary,
        passport_id=passport.passport_id,
        passport_digest=digest,
        consensus_status=status,
        disqualification_risk_pct=passport.devils_advocate.disqualification_risk_pct,
        forecast_context=forecast_context,
    )


def compute_calibration_summary(
    resolved_pairs: Sequence[tuple[TernaryForecast, TernaryOutcome]],
) -> CalibrationSummary:
    """
    Compute multi-class Brier score, Headline Arena leaderboard score, and
    refusal calibration metrics across resolved ``(TernaryForecast, outcome)`` pairs.
    """
    if not resolved_pairs:
        return CalibrationSummary(
            count=0,
            brier_score=0.0,
            headline_arena_avg_score=0.0,
            directional_accuracy=0.0,
            refusal_count=0,
            refusal_calibration_rate=0.0,
        )

    brier_total = 0.0
    ha_score_total = 0.0
    correct_count = 0
    refusal_count = 0
    refusal_well_calibrated = 0

    for forecast, actual in resolved_pairs:
        probs = forecast.probabilities
        brier = sum((probs[k] - (1.0 if k == actual else 0.0)) ** 2 for k in ("bearish", "neutral", "bullish"))
        brier_total += brier

        if forecast.direction == actual:
            correct_count += 1

        if forecast.direction == "neutral":
            ha_score_total += 60.0 if actual == "neutral" else 40.0
        else:
            if forecast.direction == actual:
                ha_score_total += 50.0 + forecast.confidence * 50.0
            else:
                ha_score_total += 50.0 - forecast.confidence * 50.0

        if forecast.consensus_status in ("VETOED", "REJECTED"):
            refusal_count += 1
            # A refusal is well-calibrated when the outcome resolved neutral OR the
            # directional argmax did not fail badly (neutral probability beat uniform).
            if actual == "neutral" or probs[actual] >= 0.20:
                refusal_well_calibrated += 1

    n = len(resolved_pairs)
    return CalibrationSummary(
        count=n,
        brier_score=round(brier_total / n, 4),
        headline_arena_avg_score=round(ha_score_total / n, 2),
        directional_accuracy=round(correct_count / n, 4),
        refusal_count=refusal_count,
        refusal_calibration_rate=round(refusal_well_calibrated / refusal_count, 4) if refusal_count else 0.0,
    )


def challenge_to_market_inputs(challenge: HeadlineArenaChallenge) -> tuple[MarketContext, pd.DataFrame]:
    """
    Convert a Headline Arena challenge (and its OHLC bars / headlines) into
    ARGUS's ``(MarketContext, bars_df)`` inputs.
    """
    bars_df = _build_ohlc_dataframe(challenge)
    closes = bars_df["close"]
    highs = bars_df["high"]
    lows = bars_df["low"]
    current_price = float(closes.iloc[-1])

    delta = closes.diff()
    gain = delta.clip(lower=0.0).rolling(14, min_periods=1).mean()
    loss = (-delta.clip(upper=0.0)).rolling(14, min_periods=1).mean()
    last_gain = float(gain.iloc[-1])
    last_loss = float(loss.iloc[-1])
    if last_loss == 0.0:
        rsi_14 = 100.0 if last_gain > 0.0 else 50.0
    else:
        rs = last_gain / last_loss
        rsi_14 = 100.0 - (100.0 / (1.0 + rs))

    tr = pd.concat(
        [
            highs - lows,
            (highs - closes.shift(1)).abs(),
            (lows - closes.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr_14 = float(tr.rolling(14, min_periods=1).mean().iloc[-1])
    atr_14 = max(atr_14, current_price * 0.002)

    sma_50 = float(closes.rolling(min(50, len(closes)), min_periods=1).mean().iloc[-1])
    pct_vs_sma = ((current_price - sma_50) / sma_50) * 100.0 if sma_50 > 0 else 0.0
    vol_pct = (atr_14 / current_price) * 100.0 if current_price > 0 else 1.0

    if challenge.vix_level >= 28.0 or vol_pct >= 2.2:
        regime = "CHOPPY"
    elif pct_vs_sma >= 0.45:
        regime = "BULLISH"
    elif pct_vs_sma <= -0.45:
        regime = "BEARISH"
    else:
        regime = "NEUTRAL"

    news = list(challenge.recent_news)
    if not news:
        news = [
            f"{challenge.asset} trading near {current_price:.2f} ahead of challenge settlement window",
        ]
        if challenge.question:
            news.append(challenge.question)

    ctx = MarketContext(
        ticker=challenge.asset,
        current_price=round(current_price, 4),
        high_24h=round(float(highs.tail(24).max()), 4),
        low_24h=round(float(lows.tail(24).min()), 4),
        volume_24h=round(float(bars_df["volume"].tail(24).sum()), 2),
        rsi_14=round(rsi_14, 2),
        atr_14=round(atr_14, 4),
        sma_50=round(sma_50, 4),
        market_regime=regime,
        vix_level=challenge.vix_level,
        recent_news=news,
    )
    return ctx, bars_df


def _build_ohlc_dataframe(challenge: HeadlineArenaChallenge) -> pd.DataFrame:
    """Parse challenge OHLC bars into a UTC-indexed DataFrame or synthesize deterministic bars."""
    if len(challenge.ohlc_bars) >= 20:
        rows: list[dict[str, Any]] = []
        timestamps: list[pd.Timestamp] = []
        for idx, bar in enumerate(challenge.ohlc_bars):
            ts_raw = bar.get("timestamp") or bar.get("time") or bar.get("t")
            ts = (
                pd.Timestamp(ts_raw, tz="UTC")
                if ts_raw and pd.Timestamp(ts_raw).tzinfo is None
                else (
                    pd.Timestamp(ts_raw).tz_convert("UTC")
                    if ts_raw
                    else pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(minutes=5 * idx)
                )
            )
            c = float(bar.get("close", bar.get("c", challenge.open_price)))
            o = float(bar.get("open", bar.get("o", c)))
            h = float(bar.get("high", bar.get("h", max(o, c))))
            lo = float(bar.get("low", bar.get("l", min(o, c))))
            vol = float(bar.get("volume", bar.get("v", 10000.0)) or 10000.0)
            timestamps.append(ts)
            rows.append({"open": o, "high": h, "low": lo, "close": c, "volume": vol})
        df = pd.DataFrame(rows, index=pd.DatetimeIndex(timestamps)).sort_index()
        return df

    # Deterministic fallback series anchored to challenge.open_price
    seed = sum(ord(ch) for ch in f"{challenge.challenge_id}:{challenge.asset}") % (2**31)
    rng = np.random.default_rng(seed)
    n_bars = 50
    dates = pd.date_range("2026-09-01T00:00:00Z", periods=n_bars, freq="h")
    returns = rng.normal(loc=0.0005, scale=0.003, size=n_bars)
    prices = challenge.open_price * np.exp(np.cumsum(returns))
    prices = prices * (challenge.open_price / prices[-1])
    spread = challenge.open_price * 0.0025
    return pd.DataFrame(
        {
            "open": prices - spread * 0.25,
            "high": prices + spread,
            "low": prices - spread,
            "close": prices,
            "volume": np.full(n_bars, 25000.0),
        },
        index=dates,
    )


def generate_offline_macro_challenges() -> list[HeadlineArenaChallenge]:
    """
    Create four deterministic macro-futures challenges (GC, CL, ES, ZN) representing
    distinct market regimes (constructive uptrend, overbought bull-trap veto,
    choppy low-edge range, and bearish breakdown) for offline dry-run verification.
    """
    base_ts = pd.date_range("2026-09-04T00:00:00Z", periods=50, freq="h")

    def _series_to_bars(closes: list[float], half_range: float, vol_base: float) -> list[dict[str, Any]]:
        bars: list[dict[str, Any]] = []
        for idx, close_px in enumerate(closes):
            open_px = closes[idx - 1] if idx > 0 else close_px
            bars.append(
                {
                    "timestamp": base_ts[idx].isoformat(),
                    "open": round(open_px, 4),
                    "high": round(max(open_px, close_px) + half_range, 4),
                    "low": round(min(open_px, close_px) - half_range, 4),
                    "close": round(close_px, 4),
                    "volume": vol_base * (1.4 if idx >= 45 else 1.0),
                }
            )
        return bars

    # 1. Gold futures (GC): Constructive bullish trend with healthy pullbacks (RSI ~58-64) -> APPROVED LONG
    gc_closes: list[float] = []
    px = 2480.0
    for i in range(50):
        step = 3.2 if (i % 3 != 2) else -3.4
        px += step
        gc_closes.append(round(px, 2))

    # 2. Crude oil futures (CL): Parabolic overbought spike + high VIX + euphoric headlines -> VETOED by Devil's Advocate
    cl_closes = [72.0 + 0.08 * i for i in range(36)] + [74.9 + 0.65 * j for j in range(14)]

    # 3. S&P 500 E-mini (ES): Flat choppy oscillation -> REJECTED / neutral calibration
    es_closes = [5520.0 + (8.0 if i % 2 == 0 else -8.0) for i in range(50)]

    # 4. 10Y Treasury Note (ZN): Clean bearish drift with relief bounces (RSI ~40-46) -> APPROVED SHORT
    zn_closes: list[float] = []
    zpx = 116.5
    for i in range(50):
        step = -0.14 if (i % 3 != 2) else 0.13
        zpx += step
        zn_closes.append(round(zpx, 3))

    return [
        HeadlineArenaChallenge(
            challenge_id="offline-gc-20260905",
            asset="GC",
            question="Will COMEX Gold futures (GC) close bullish, neutral, or bearish over the next 24h?",
            status="open",
            deadline="2026-09-06T12:00:00Z",
            open_price=round(gc_closes[-1], 2),
            dead_zone_pct=0.15,
            resolution_criteria="Resolves bullish above +0.15%, bearish below -0.15%, else neutral.",
            scope_key="GC",
            contract_symbol="GCZ26",
            contract_month="2026-12",
            ohlc_bars=_series_to_bars(gc_closes, half_range=4.5, vol_base=32000.0),
            recent_news=[
                "Gold futures see strong central-bank inflow and bullish breakout momentum",
                "Real yields ease following growth expansion and sovereign reserve upgrades",
            ],
            vix_level=16.2,
        ),
        HeadlineArenaChallenge(
            challenge_id="offline-cl-20260905",
            asset="CL",
            question="Will WTI Crude Oil futures (CL) close bullish, neutral, or bearish over the next 24h?",
            status="open",
            deadline="2026-09-06T12:00:00Z",
            open_price=round(cl_closes[-1], 2),
            dead_zone_pct=0.30,
            resolution_criteria="Resolves bullish above +0.30%, bearish below -0.30%, else neutral.",
            scope_key="CL",
            contract_symbol="CLV26",
            contract_month="2026-10",
            ohlc_bars=_series_to_bars(cl_closes, half_range=0.85, vol_base=45000.0),
            recent_news=[
                "Crude futures surge on bullish breakout rally with strong momentum fund inflow",
                "Analysts issue target upgrade after refinery throughput beat",
                "Energy complex sees expansion in speculative long positioning",
            ],
            vix_level=29.5,
        ),
        HeadlineArenaChallenge(
            challenge_id="offline-es-20260905",
            asset="ES",
            question="Will S&P 500 E-mini futures (ES) close bullish, neutral, or bearish over the next 24h?",
            status="open",
            deadline="2026-09-06T12:00:00Z",
            open_price=round(es_closes[-1], 2),
            dead_zone_pct=0.10,
            resolution_criteria="Resolves bullish above +0.10%, bearish below -0.10%, else neutral.",
            scope_key="ES",
            contract_symbol="ESU26",
            contract_month="2026-09",
            ohlc_bars=_series_to_bars(es_closes, half_range=6.0, vol_base=50000.0),
            recent_news=[
                "Equity index futures trade sideways ahead of payrolls release",
            ],
            vix_level=21.0,
        ),
        HeadlineArenaChallenge(
            challenge_id="offline-zn-20260905",
            asset="ZN",
            question="Will 10-Year Treasury Note futures (ZN) close bullish, neutral, or bearish over the next 24h?",
            status="open",
            deadline="2026-09-06T12:00:00Z",
            open_price=round(zn_closes[-1], 3),
            dead_zone_pct=0.12,
            resolution_criteria="Resolves bullish above +0.12%, bearish below -0.12%, else neutral.",
            scope_key="ZN",
            contract_symbol="ZNZ26",
            contract_month="2026-12",
            ohlc_bars=_series_to_bars(zn_closes, half_range=0.15, vol_base=40000.0),
            recent_news=[
                "Treasury futures turn bearish on sustained duration fund outflow",
                "Auction demand miss triggers fixed-income rating downgrade",
            ],
            vix_level=18.5,
        ),
    ]


class HeadlineArenaClient:
    """
    HTTPS REST client for Headline Arena (``https://headlinearena.com/api/v1``).

    Interoperates seamlessly with credentials saved by ``headlinearena-agent-plugin``
    (``~/.headlinearena/credentials.json``) or environment variables ``HA_AGENT_ID`` /
    ``HA_CLIENT_SECRET``.
    """

    def __init__(self, settings: HeadlineArenaSettings | None = None) -> None:
        self.settings = settings or CONFIG.headline_arena
        self.origin = normalize_origin(self.settings.base_url)
        self._agent_id: str | None = None
        self._client_secret: str | None = None
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._load_credentials()

    def _load_credentials(self) -> None:
        if self.settings.agent_id and self.settings.client_secret:
            self._agent_id = self.settings.agent_id.strip()
            self._client_secret = self.settings.client_secret.strip()
            return

        cred_path = Path(self.settings.credentials_path).expanduser()
        if not cred_path.exists():
            return

        try:
            data = json.loads(cred_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("[HEADLINE ARENA] Could not read credentials file %s: %s", cred_path, exc)
            return

        if not isinstance(data, dict):
            return

        entry = data.get(self.origin) or data.get(f"{self.origin}/api/v1")
        if not isinstance(entry, dict):
            return

        # Support both single-agent and multi-agent credential store formats from ha.py
        if "agent_id" in entry and "client_secret" in entry:
            self._agent_id = str(entry["agent_id"])
            self._client_secret = str(entry["client_secret"])
            return

        agents = entry.get("agents")
        default_id = entry.get("default")
        if isinstance(agents, dict) and agents:
            target = (
                agents.get(self.settings.agent_id)
                if self.settings.agent_id
                else (agents.get(default_id) if default_id else next(iter(agents.values())))
            )
            if isinstance(target, dict) and target.get("agent_id") and target.get("client_secret"):
                self._agent_id = str(target["agent_id"])
                self._client_secret = str(target["client_secret"])

    @property
    def has_credentials(self) -> bool:
        return bool(self._agent_id and self._client_secret)

    @property
    def agent_id(self) -> str | None:
        return self._agent_id

    def _api_url(self, path: str) -> str:
        return f"{self.origin}/api/v1{path}"

    def _http_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        authenticated: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "argus-ai/2.2.0",
            "X-Request-Id": str(uuid.uuid4()),
        }
        if authenticated:
            token = self.get_access_token()
            headers["Authorization"] = f"Bearer {token}"
            if self._agent_id:
                headers["X-Agent-Id"] = self._agent_id
        if extra_headers:
            headers.update(extra_headers)

        payload_bytes = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self._api_url(path),
            data=payload_bytes,
            headers=headers,
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(req, timeout=self.settings.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8") or "{}"
                parsed: Any = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {"items": parsed}
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            raise HeadlineArenaError(f"Headline Arena HTTP {exc.code} on {path}: {err_body}") from exc
        except urllib.error.URLError as exc:
            raise HeadlineArenaError(f"Cannot reach Headline Arena at {self.origin}{path}: {exc.reason}") from exc

    def get_access_token(self, force_refresh: bool = False) -> str:
        """Obtain or reuse a short-lived Bearer token via client_credentials."""
        if not self.has_credentials:
            raise HeadlineArenaError(
                "Headline Arena credentials not configured. Set HA_AGENT_ID and HA_CLIENT_SECRET "
                "or register with headlinearena-agent-plugin (`ha.py register`)."
            )
        now = time.time()
        if not force_refresh and self._access_token and (self._token_expires_at - TOKEN_REFRESH_MARGIN_SECONDS) > now:
            return self._access_token

        resp = self._http_json(
            "POST",
            "/agent/auth/token",
            body={
                "grant_type": "client_credentials",
                "agent_id": self._agent_id,
                "client_secret": self._client_secret,
            },
            authenticated=False,
        )
        token = resp.get("access_token")
        if not isinstance(token, str) or not token:
            raise HeadlineArenaError(f"Headline Arena token response missing access_token: {resp}")
        expires_in = int(resp.get("expires_in", 900))
        self._access_token = token
        self._token_expires_at = now + expires_in
        return token

    def list_open_challenges(
        self,
        asset: str | None = None,
        authenticated: bool = False,
    ) -> list[HeadlineArenaChallenge]:
        """Discover open financial challenges (public or authenticated active scopes)."""
        if authenticated and self.has_credentials:
            resp = self._http_json("GET", "/eval/challenges/active", authenticated=True)
            raw_items = resp.get("challenges", [])
        else:
            resp = self._http_json("GET", "/eval/challenges?status=open", authenticated=False)
            raw_items = resp.get("items", resp.get("challenges", []))

        challenges: list[HeadlineArenaChallenge] = []
        asset_filter = asset.strip().upper() if asset else None

        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            raw_challenge = entry.get("challenge")
            item: dict[str, Any] = raw_challenge if isinstance(raw_challenge, dict) else entry
            raw_ctx = entry.get("context")
            ctx_bundle: dict[str, Any] = raw_ctx if isinstance(raw_ctx, dict) else {}
            if str(item.get("status", "open")).lower() != "open":
                continue
            item_asset = str(item.get("asset") or item.get("scope_key") or "").upper()
            if not item_asset:
                continue
            if asset_filter and item_asset != asset_filter:
                continue

            open_price_raw = item.get("open_price") or ctx_bundle.get("current_price") or ctx_bundle.get("price")
            try:
                open_price = float(open_price_raw) if open_price_raw is not None else 0.0
            except (TypeError, ValueError):
                open_price = 0.0
            if open_price <= 0.0:
                continue

            raw_ohlc = ctx_bundle.get("ohlc")
            ohlc_obj: dict[str, Any] = raw_ohlc if isinstance(raw_ohlc, dict) else {}
            raw_bars = ohlc_obj.get("bars")
            bars: list[dict[str, Any]] = (
                [b for b in raw_bars if isinstance(b, dict)] if isinstance(raw_bars, list) else []
            )

            dead_zone_raw = item.get("dead_zone_pct")
            dead_zone_pct = float(dead_zone_raw) if dead_zone_raw is not None else None
            raw_resolution = item.get("resolution")
            resolution: dict[str, Any] = raw_resolution if isinstance(raw_resolution, dict) else {}
            criteria = str(item.get("resolution_criteria") or resolution.get("criteria") or "")

            challenges.append(
                HeadlineArenaChallenge(
                    challenge_id=str(item.get("id") or item.get("challenge_id")),
                    asset=item_asset,
                    question=str(item.get("question") or ""),
                    status=str(item.get("status") or "open"),
                    deadline=str(item.get("deadline") or ""),
                    open_price=open_price,
                    dead_zone_pct=dead_zone_pct,
                    resolution_criteria=criteria,
                    scope_key=str(item.get("scope_key") or item_asset),
                    contract_symbol=item.get("contract_symbol"),
                    contract_month=item.get("contract_month"),
                    ohlc_bars=bars,
                )
            )
        return challenges

    def fetch_market_context(self, asset: str, hours: int = 24, bar_limit: int = 60) -> dict[str, Any]:
        """Fetch live quote, OHLC bars, and linked headlines for a single asset."""
        encoded = urllib.parse.quote(asset.strip().upper(), safe="")
        bounded_hours = max(1, min(int(hours), 168))
        bounded_bars = max(1, min(int(bar_limit), 2016))
        return self._http_json(
            "GET",
            f"/eval/context/{encoded}?hours={bounded_hours}&bar_limit={bounded_bars}",
            authenticated=False,
        )

    def build_prediction_payload(
        self,
        forecast: TernaryForecast,
        *,
        idempotency_key: str | None = None,
        model_provider: str | None = None,
        model_name: str | None = None,
        model_version: str | None = None,
        is_revision: bool = False,
    ) -> dict[str, Any]:
        """Construct the exact JSON payload expected by ``POST /eval/challenges/{id}/predict``."""
        payload: dict[str, Any] = {
            "probabilities": dict(forecast.probabilities),
            "direction": forecast.direction,
            "confidence": forecast.confidence,
            "reasoning": forecast.reasoning,
            "summary": forecast.summary,
            "is_revision": is_revision,
            "forecast_context": forecast.forecast_context,
        }
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key

        provider = model_provider if model_provider is not None else self.settings.model_provider
        name = model_name if model_name is not None else self.settings.model_name
        version = model_version if model_version is not None else self.settings.model_version
        payload.update(build_model_attribution_fields(provider, name, version))
        return payload

    def submit_forecast(
        self,
        forecast: TernaryForecast,
        *,
        idempotency_key: str | None = None,
        model_provider: str | None = None,
        model_name: str | None = None,
        model_version: str | None = None,
        is_revision: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Submit a calibrated ternary forecast (stake-free financial track)."""
        idem_key = idempotency_key or f"argus-{forecast.passport_id}"
        payload = self.build_prediction_payload(
            forecast,
            idempotency_key=idem_key,
            model_provider=model_provider,
            model_name=model_name,
            model_version=model_version,
            is_revision=is_revision,
        )
        if dry_run:
            return {
                "status": "dry_run",
                "endpoint": self._api_url(f"/eval/challenges/{forecast.challenge_id}/predict"),
                "idempotency_key": idem_key,
                "payload": payload,
            }

        encoded_id = urllib.parse.quote(forecast.challenge_id, safe="")
        return self._http_json(
            "POST",
            f"/eval/challenges/{encoded_id}/predict",
            body=payload,
            authenticated=True,
            extra_headers={"Idempotency-Key": idem_key},
        )

    def get_scorecard(self, agent_id: str | None = None) -> dict[str, Any]:
        """Fetch an agent's public Headline Arena scorecard and calibration stats."""
        target = (agent_id or self._agent_id or "").strip()
        if not target:
            raise HeadlineArenaError("agent_id is required to fetch a Headline Arena scorecard.")
        encoded = urllib.parse.quote(target, safe="")
        return self._http_json("GET", f"/eval/agents/{encoded}/scorecard", authenticated=False)

    def get_challenge_results(self, challenge_id: str) -> dict[str, Any]:
        """Fetch public settlement results for a resolved challenge."""
        encoded = urllib.parse.quote(challenge_id.strip(), safe="")
        return self._http_json("GET", f"/eval/challenges/{encoded}/results", authenticated=False)


class HeadlineArenaDesk:
    """
    Orchestrates ARGUS committee deliberation on Headline Arena macro-futures
    challenges and produces calibrated ternary forecasts with Trade Passport receipts.
    """

    def __init__(
        self,
        client: HeadlineArenaClient | None = None,
        committee: CommitteeOrchestrator | None = None,
        audit: AuditAgent | None = None,
        use_llm: bool = False,
    ) -> None:
        self.client = client or HeadlineArenaClient()
        self.committee = committee or CommitteeOrchestrator(use_llm=use_llm)
        self.audit = audit or AuditAgent()

    def evaluate_challenge(
        self,
        challenge: HeadlineArenaChallenge,
        *,
        submit: bool = False,
        account_equity: float = 100000.0,
    ) -> HeadlineArenaRunResult:
        """Run the ARGUS committee on one challenge and optionally submit to Headline Arena."""
        ctx, bars_df = challenge_to_market_inputs(challenge)
        passport = self.committee.evaluate_opportunity(ctx=ctx, bars_df=bars_df, account_equity=account_equity)
        self.audit.record_passport(passport)
        forecast = passport_to_ternary_forecast(passport, challenge=challenge)

        response = self.client.submit_forecast(forecast, dry_run=not submit)
        return HeadlineArenaRunResult(
            challenge=challenge,
            passport=passport,
            forecast=forecast,
            submitted=submit,
            dry_run=not submit,
            response=response,
        )

    def run_cycle(
        self,
        *,
        asset: str | None = None,
        live_discovery: bool = False,
        submit: bool = False,
    ) -> list[HeadlineArenaRunResult]:
        """Discover challenges (offline deterministic or live Headline Arena) and evaluate each."""
        if live_discovery:
            challenges = self.client.list_open_challenges(
                asset=asset,
                authenticated=self.client.has_credentials,
            )
        else:
            challenges = generate_offline_macro_challenges()
            if asset:
                target = asset.strip().upper()
                challenges = [c for c in challenges if c.asset.upper() == target]

        return [self.evaluate_challenge(ch, submit=submit) for ch in challenges]
