"""Strict quality entry gate for higher win-rate targeting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import pandas as pd

from config import QUALITY, QualityConfig
from live_brain import session_ok_for_quality
from macro_filter import MacroFilter, default_macro_filter
from opportunity_scanner import Opportunity, score_symbol

Side = Literal["buy", "sell", "flat"]


@dataclass
class GateResult:
    """Outcome of the quality gate for a proposed side."""

    allowed: bool
    side: Side
    score: float
    reasons: list[str]
    reject_reason: str = ""


def _f(row: pd.Series, col: str, default: float = 0.0) -> float:
    if col not in row.index:
        return default
    try:
        v = float(row[col])
        if v != v:  # NaN
            return default
        return v
    except (TypeError, ValueError):
        return default


def ema_stack_ok(row: pd.Series, side: Side) -> tuple[bool, str]:
    close = _f(row, "close")
    ema20 = _f(row, "ema_20", close)
    ema50 = _f(row, "ema_50", close)
    ema200 = _f(row, "ema_200", close)
    if side == "buy":
        ok = ema20 > ema50 > ema200 and close > ema20
        return ok, "EMA stack bullish" if ok else "EMA stack not bullish"
    if side == "sell":
        ok = ema20 < ema50 < ema200 and close < ema20
        return ok, "EMA stack bearish" if ok else "EMA stack not bearish"
    return False, "flat"


def rsi_ok(row: pd.Series, side: Side, cfg: QualityConfig) -> tuple[bool, str]:
    rsi = _f(row, "rsi_14", 50.0)
    if side == "buy":
        ok = cfg.rsi_buy_low <= rsi <= cfg.rsi_buy_high
        return ok, f"RSI {rsi:.1f} buy-band" if ok else f"RSI {rsi:.1f} outside buy band"
    if side == "sell":
        ok = cfg.rsi_sell_low <= rsi <= cfg.rsi_sell_high
        return ok, f"RSI {rsi:.1f} sell-band" if ok else f"RSI {rsi:.1f} outside sell band"
    return False, "flat"


def extension_ok(row: pd.Series, side: Side, cfg: QualityConfig) -> tuple[bool, str]:
    """Reject chase entries too far from EMA20."""
    close = _f(row, "close")
    ema20 = _f(row, "ema_20", close)
    atr = max(_f(row, "atr_14", 0.0), 1e-12)
    ext = abs(close - ema20) / atr
    if ext > cfg.max_extension_atr:
        return False, f"Extended {ext:.2f} ATR from EMA20"
    return True, f"Extension {ext:.2f} ATR ok"


def h1_trend_ok(row: pd.Series, side: Side) -> tuple[bool, str]:
    """Require higher-timeframe EMA alignment with trade side."""
    close = _f(row, "close")
    ema20_1h = _f(row, "ema_20_1h", close)
    ema50_1h = _f(row, "ema_50_1h", close)
    if side == "buy":
        ok = ema20_1h >= ema50_1h and close >= ema20_1h
        return ok, "H1 bullish" if ok else "H1 not bullish"
    if side == "sell":
        ok = ema20_1h <= ema50_1h and close <= ema20_1h
        return ok, "H1 bearish" if ok else "H1 not bearish"
    return False, "flat"


def soft_direction_ok(row: pd.Series, side: Side) -> tuple[bool, str]:
    """Lighter direction check for hot grabs (EMA20 vs EMA50)."""
    close = _f(row, "close")
    ema20 = _f(row, "ema_20", close)
    ema50 = _f(row, "ema_50", close)
    if side == "buy":
        ok = ema20 >= ema50 and close >= ema50
        return ok, "Soft bullish" if ok else "Not soft-bullish"
    if side == "sell":
        ok = ema20 <= ema50 and close <= ema50
        return ok, "Soft bearish" if ok else "Not soft-bearish"
    return False, "flat"


def smc_confluence_ok(row: pd.Series, side: Side) -> tuple[bool, str, float]:
    """
    Smart Money Concepts (SMC) evaluation:
    Checks Order Block (OB), Fair Value Gap (FVG), and Break of Structure (BOS).
    Returns (has_confluence, detail_str, bonus_score).
    """
    close = _f(row, "close")
    high = _f(row, "high", close)
    low = _f(row, "low", close)
    open_p = _f(row, "open", close)
    atr = max(_f(row, "atr_14", 0.0), 1e-12)
    
    # Custom indicator fields or derived candle structure
    fvg = _f(row, "fvg_score", 0.0)
    ob = _f(row, "order_block", 0.0)
    bos = _f(row, "bos_signal", 0.0)
    
    # Derived candle structure if indicators not present
    body = abs(close - open_p)
    candle_range = max(high - low, 1e-12)
    is_impulse = (body / candle_range) > 0.65 and body > 1.2 * atr
    
    score_bonus = 0.0
    notes: list[str] = []

    if side == "buy":
        if fvg > 0 or (close > open_p and is_impulse):
            score_bonus += 0.05
            notes.append("FVG/Impulse bullish")
        if ob > 0 or (low <= _f(row, "ema_20", close) and close > open_p):
            score_bonus += 0.05
            notes.append("SMC Demand Zone bounce")
        if bos > 0 or (close > _f(row, "high_1h", high)):
            score_bonus += 0.05
            notes.append("BOS bullish break")
    elif side == "sell":
        if fvg < 0 or (close < open_p and is_impulse):
            score_bonus += 0.05
            notes.append("FVG/Impulse bearish")
        if ob < 0 or (high >= _f(row, "ema_20", close) and close < open_p):
            score_bonus += 0.05
            notes.append("SMC Supply Zone reject")
        if bos < 0 or (close < _f(row, "low_1h", low)):
            score_bonus += 0.05
            notes.append("BOS bearish break")

    if notes:
        return True, "SMC: " + " · ".join(notes), score_bonus
    return False, "SMC: Neutral", 0.0


def evaluate_hot_grab(
    symbol: str,
    row: pd.Series,
    side: Side,
    *,
    scanner_score: float,
    now: datetime | None = None,
    macro: MacroFilter | None = None,
    cfg: QualityConfig | None = None,
    policy_action: int | None = None,
) -> GateResult:
    """
    Act immediately on hot scanner setups.

    Soft EMA + blackout + extension guards. Requires H1 agreement OR policy
    not opposing the trade side (tightened vs earlier hot path).
    """
    cfg = cfg or QUALITY
    macro = macro or default_macro_filter
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = [f"HOT grab score={scanner_score:.2f}"]

    if side not in {"buy", "sell"}:
        return GateResult(False, "flat", 0.0, [], "No side")

    if scanner_score < cfg.hot_grab_score:
        return GateResult(
            False,
            side,
            scanner_score,
            reasons,
            f"Score {scanner_score:.2f} < hot {cfg.hot_grab_score:.2f}",
        )

    # Blackout always blocks; session: 24h symbols always ok, else London/NY
    in_blackout, title, _ = macro.blackout_status(now, symbol)
    if in_blackout:
        why = title or "high-impact event"
        return GateResult(False, side, scanner_score, reasons, f"Calendar blackout: {why}")

    feats = macro.macro_features(now, symbol)
    ok_sess, sess_msg = session_ok_for_quality(
        symbol, feats, require_overlap=False  # hot path never waits for overlap
    )
    if not ok_sess:
        return GateResult(False, side, scanner_score, reasons, sess_msg)
    reasons.append(sess_msg)

    ok_dir, dir_msg = soft_direction_ok(row, side)
    reasons.append(dir_msg)
    if not ok_dir:
        return GateResult(False, side, scanner_score, reasons, dir_msg)

    # Require H1 agree OR policy not opposing
    ok_h1, h1_msg = h1_trend_ok(row, side)
    policy_opposes = False
    if policy_action is not None:
        # PPO: 0=hold, 1=buy, 2=sell (TradingEnv convention used live as int hints)
        if side == "buy" and policy_action == 2:
            policy_opposes = True
        elif side == "sell" and policy_action == 1:
            policy_opposes = True
    if ok_h1:
        reasons.append(h1_msg)
    elif not policy_opposes:
        reasons.append(f"{h1_msg} · policy not opposing")
    else:
        return GateResult(
            False,
            side,
            scanner_score,
            reasons,
            f"{h1_msg} and policy opposing",
        )

    # Light RSI exhaustion guard — reject blow-off tops / capitulation bottoms
    rsi = _f(row, "rsi_14", 50.0)
    if side == "buy" and rsi < 20.0:
        return GateResult(
            False, side, scanner_score, reasons, f"RSI {rsi:.1f} exhaustion (buy)"
        )
    if side == "sell" and rsi > 80.0:
        return GateResult(
            False, side, scanner_score, reasons, f"RSI {rsi:.1f} exhaustion (sell)"
        )
    reasons.append(f"RSI {rsi:.1f} ok")

    # Hot path: allow chase up to 1.8 ATR (strict gate stays tighter at 1.0)
    close = _f(row, "close")
    ema20 = _f(row, "ema_20", close)
    atr = max(_f(row, "atr_14", 0.0), 1e-12)
    ext = abs(close - ema20) / atr
    if ext > 1.8:
        return GateResult(
            False, side, scanner_score, reasons, f"Extended {ext:.2f} ATR from EMA20"
        )
    reasons.append(f"Extension {ext:.2f} ATR ok")

    return GateResult(True, side, float(scanner_score), reasons, "")


def evaluate_quality(
    symbol: str,
    row: pd.Series,
    side: Side,
    *,
    now: datetime | None = None,
    macro: MacroFilter | None = None,
    cfg: QualityConfig | None = None,
    policy_action: int | None = None,
) -> GateResult:
    """
    Return whether ``side`` is an A+ entry on this bar.

    Requires: macro allowed, full EMA stack, RSI band, not over-extended,
    and scanner score >= MIN_ENTRY_SCORE.
    """
    cfg = cfg or QUALITY
    macro = macro or default_macro_filter
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []

    if side not in {"buy", "sell"}:
        return GateResult(False, "flat", 0.0, [], "No side")

    allowed_m, gate_reason = macro.is_trading_allowed(now, symbol)
    if not allowed_m:
        return GateResult(False, side, 0.0, [], gate_reason)

    feats = macro.macro_features(now, symbol)
    ok_sess, sess_msg = session_ok_for_quality(
        symbol, feats, require_overlap=cfg.require_overlap
    )
    if not ok_sess:
        return GateResult(False, side, 0.0, [], sess_msg)
    reasons.append(sess_msg)

    ok_ema, ema_msg = ema_stack_ok(row, side)
    reasons.append(ema_msg)
    if not ok_ema:
        return GateResult(False, side, 0.0, reasons, ema_msg)

    ok_rsi, rsi_msg = rsi_ok(row, side, cfg)
    reasons.append(rsi_msg)
    if not ok_rsi:
        return GateResult(False, side, 0.0, reasons, rsi_msg)

    ok_ext, ext_msg = extension_ok(row, side, cfg)
    reasons.append(ext_msg)
    if not ok_ext:
        return GateResult(False, side, 0.0, reasons, ext_msg)

    ok_h1, h1_msg = h1_trend_ok(row, side)
    reasons.append(h1_msg)
    if not ok_h1:
        return GateResult(False, side, 0.0, reasons, h1_msg)

    smc_has, smc_msg, smc_bonus = smc_confluence_ok(row, side)
    if smc_has:
        reasons.append(smc_msg)

    opp: Opportunity = score_symbol(
        symbol,
        row,
        macro=macro,
        policy_action=policy_action,
        now=now,
    )
    # Force side alignment for score interpretation
    score = min(1.0, opp.score + smc_bonus)
    if opp.side != side and opp.side != "flat":
        score *= 0.5
        reasons.append(f"Scanner prefers {opp.side}")
    reasons.extend(opp.reasons[:3])

    if score < cfg.min_entry_score:
        return GateResult(
            False,
            side,
            score,
            reasons,
            f"Score {score:.2f} < {cfg.min_entry_score:.2f}",
        )

    return GateResult(True, side, score, reasons, "")


def best_quality_side(
    symbol: str,
    row: pd.Series,
    *,
    now: datetime | None = None,
    macro: MacroFilter | None = None,
    cfg: QualityConfig | None = None,
    policy_action: int | None = None,
) -> GateResult:
    """Evaluate buy and sell; return the better allowed side (or reject)."""
    buy = evaluate_quality(
        symbol, row, "buy", now=now, macro=macro, cfg=cfg, policy_action=policy_action
    )
    sell = evaluate_quality(
        symbol, row, "sell", now=now, macro=macro, cfg=cfg, policy_action=policy_action
    )
    if buy.allowed and sell.allowed:
        return buy if buy.score >= sell.score else sell
    if buy.allowed:
        return buy
    if sell.allowed:
        return sell
    # Prefer higher score reject for logging
    worse = buy if buy.score >= sell.score else sell
    return GateResult(False, "flat", worse.score, worse.reasons, worse.reject_reason)
