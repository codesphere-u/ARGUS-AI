"""Rank live multi-pair trade opportunities for console + Telegram alerts."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import numpy as np
import pandas as pd

from chart_renderer import levels_from_atr, render_trade_chart
from config import MULTI, RISK, MultiPairConfig
from macro_filter import MacroFilter, default_macro_filter
from telegram_format import format_opportunities_alert, truncate_caption

logger = logging.getLogger(__name__)

Side = Literal["buy", "sell", "flat"]

# Avoid spamming the same hot setup every poll cycle
# key -> (side, score, price, timestamp)
_LAST_OPP_ALERT: dict[str, tuple[str, float, float, float]] = {}


@dataclass
class Opportunity:
    """Scored trade opportunity for one symbol."""

    symbol: str
    side: Side
    score: float
    reasons: list[str] = field(default_factory=list)
    atr: float = 0.0
    price: float = 0.0
    allowed: bool = True
    gate_reason: str = "ok"


def _safe_float(row: pd.Series, col: str, default: float = 0.0) -> float:
    if col not in row.index:
        return default
    val = row[col]
    try:
        f = float(val)
        if np.isnan(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def score_symbol(
    symbol: str,
    row: pd.Series,
    *,
    macro: MacroFilter | None = None,
    policy_action: int | None = None,
    now: datetime | None = None,
    cfg: MultiPairConfig | None = None,
) -> Opportunity:
    """
    Score a symbol from EMA alignment, RSI, ATR expansion, session, H1, and policy.

    Typical good setups land ~0.70–0.88; elite >= 0.90 is rare.
    """
    del cfg  # reserved for future weights
    macro = macro or default_macro_filter
    now = now or datetime.now(timezone.utc)

    close = _safe_float(row, "close")
    ema20 = _safe_float(row, "ema_20", close)
    ema50 = _safe_float(row, "ema_50", close)
    ema200 = _safe_float(row, "ema_200", close)
    rsi = _safe_float(row, "rsi_14", 50.0)
    atr = _safe_float(row, "atr_14", 0.0)
    vol = _safe_float(row, "volatility", 0.0)
    ema20_1h = _safe_float(row, "ema_20_1h", close)
    ema50_1h = _safe_float(row, "ema_50_1h", close)

    reasons: list[str] = []
    score = 0.0
    side: Side = "flat"

    bullish = ema20 > ema50 > ema200 and close > ema20
    bearish = ema20 < ema50 < ema200 and close < ema20

    if bullish:
        side = "buy"
        score += 0.32
        reasons.append("EMA stack bullish")
    elif bearish:
        side = "sell"
        score += 0.32
        reasons.append("EMA stack bearish")
    elif ema20 > ema50 and close > ema50:
        side = "buy"
        score += 0.14
        reasons.append("Soft bullish EMAs")
    elif ema20 < ema50 and close < ema50:
        side = "sell"
        score += 0.14
        reasons.append("Soft bearish EMAs")

    if side == "buy" and 42 <= rsi <= 65:
        score += 0.16
        reasons.append(f"RSI ok for long ({rsi:.1f})")
    elif side == "sell" and 35 <= rsi <= 58:
        score += 0.16
        reasons.append(f"RSI ok for short ({rsi:.1f})")
    elif side != "flat" and 40 <= rsi <= 60:
        score += 0.05
        reasons.append("RSI neutral")

    # ATR / volatility expansion vs price
    if close > 0 and atr / close > 0.0004:
        score += 0.10
        reasons.append("ATR expanded")
    if vol > 0:
        score += 0.03
        reasons.append("Vol present")

    # H1 soft alignment — bonus when agrees, penalty when fights
    if side == "buy":
        if ema20_1h >= ema50_1h and close >= ema20_1h:
            score += 0.15
            reasons.append("H1 bullish")
        elif ema20_1h < ema50_1h:
            score *= 0.72
            reasons.append("H1 fights long")
    elif side == "sell":
        if ema20_1h <= ema50_1h and close <= ema20_1h:
            score += 0.15
            reasons.append("H1 bearish")
        elif ema20_1h > ema50_1h:
            score *= 0.72
            reasons.append("H1 fights short")

    # MACD & Bollinger Bands Confluence
    macd_hist = _safe_float(row, "macd_hist")
    bb_pct_b = _safe_float(row, "bb_pct_b", 0.5)
    if side == "buy":
        if macd_hist > 0:
            score += 0.08
            reasons.append("MACD hist bullish")
        if bb_pct_b < 0.35:
            score += 0.08
            reasons.append("BB oversold bounce")
    elif side == "sell":
        if macd_hist < 0:
            score += 0.08
            reasons.append("MACD hist bearish")
        if bb_pct_b > 0.65:
            score += 0.08
            reasons.append("BB overbought reject")

    allowed, gate_reason = macro.is_trading_allowed(now, symbol)
    feats = macro.macro_features(now, symbol)
    from config import MACRO as _MACRO

    session_mode = (_MACRO.session_mode or "always").lower()
    # When SESSION_MODE=always, don't inflate every bar with session bonuses
    if session_mode not in {"always", "off", "all"}:
        if feats.get("in_overlap", 0) >= 1.0:
            score += 0.10
            reasons.append("London/NY overlap")
        elif feats.get("in_london", 0) >= 1.0 or feats.get("in_ny", 0) >= 1.0:
            score += 0.05
            reasons.append("In session")
    elif feats.get("in_overlap", 0) >= 1.0:
        score += 0.05
        reasons.append("Overlap bonus")

    if not allowed:
        score *= 0.25
        reasons.append(f"Gated: {gate_reason}")

    # Policy agreement bonus (smaller — avoid saturation)
    if policy_action == 1 and side == "buy":
        score += 0.10
        reasons.append("Policy BUY agrees")
    elif policy_action == 2 and side == "sell":
        score += 0.10
        reasons.append("Policy SELL agrees")
    elif policy_action == 0 and side != "flat":
        score *= 0.90
        reasons.append("Policy HOLD (soft penalty)")

    score = float(max(0.0, min(1.0, score)))
    if side == "flat":
        score = min(score, 0.3)

    return Opportunity(
        symbol=symbol,
        side=side,
        score=score,
        reasons=reasons,
        atr=atr,
        price=close,
        allowed=allowed and side != "flat",
        gate_reason=gate_reason,
    )


def scan_opportunities(
    market_rows: dict[str, pd.Series],
    *,
    policy_actions: dict[str, int] | None = None,
    macro: MacroFilter | None = None,
    cfg: MultiPairConfig | None = None,
    now: datetime | None = None,
) -> list[Opportunity]:
    """Score all symbols and return ranked opportunities (best first)."""
    cfg = cfg or MULTI
    macro = macro or default_macro_filter
    now = now or datetime.now(timezone.utc)
    policy_actions = policy_actions or {}
    opps: list[Opportunity] = []
    for symbol, row in market_rows.items():
        opps.append(
            score_symbol(
                symbol,
                row,
                macro=macro,
                policy_action=policy_actions.get(symbol),
                now=now,
                cfg=cfg,
            )
        )
    opps.sort(key=lambda o: o.score, reverse=True)
    return opps


def format_opportunities(opps: list[Opportunity], limit: int = 4) -> str:
    lines = ["Opportunities:"]
    for opp in opps[:limit]:
        why = "; ".join(opp.reasons[:3]) or "-"
        lines.append(
            f"  {opp.symbol} {opp.side.upper()} score={opp.score:.2f} "
            f"allowed={opp.allowed} | {why}"
        )
    return "\n".join(lines)


def alert_opportunities(
    opps: list[Opportunity],
    *,
    notify: Callable[[str], Any] | None = None,
    notify_photo: Callable[[str, bytes], Any] | None = None,
    frames: dict[str, Any] | None = None,
    cfg: MultiPairConfig | None = None,
    log: logging.Logger | None = None,
) -> list[Opportunity]:
    """
    Log all ranked opps; Telegram-alert those above threshold and allowed.
    Dedupes identical setups for 15 minutes so alerts stay useful.
    Sends one chart photo for the top fresh setup when frames are available.
    """
    cfg = cfg or MULTI
    log = log or logger
    summary = format_opportunities(opps)
    log.info(summary)

    hot = [o for o in opps if o.allowed and o.score >= cfg.opportunity_alert_min]
    if not hot or not notify:
        return []

    now_ts = datetime.now(timezone.utc).timestamp()
    cooldown_sec = float(getattr(cfg, "opportunity_alert_cooldown_min", 30.0)) * 60.0
    fresh: list[Opportunity] = []
    for o in hot:
        side_k = o.side
        score_k = round(o.score, 2)
        price_k = round(float(o.price), 5)
        # Cooldown key: symbol + side so opposite side can still alert
        alert_key = f"{o.symbol}:{side_k}"
        prev = _LAST_OPP_ALERT.get(alert_key) or _LAST_OPP_ALERT.get(o.symbol)
        if prev is not None:
            p_side, p_score, _p_price, p_ts = prev
            same_side = p_side == side_k
            if same_side and (now_ts - p_ts) < cooldown_sec and score_k <= p_score + 0.05:
                # Same side within cooldown — skip unless score jumped materially
                continue
        _LAST_OPP_ALERT[alert_key] = (side_k, score_k, price_k, now_ts)
        _LAST_OPP_ALERT[o.symbol] = (side_k, score_k, price_k, now_ts)
        fresh.append(o)

    if not fresh:
        return []

    top = fresh[0]
    caption = format_opportunities_alert(
        fresh,
        sl_atr_mult=RISK.stop_loss_atr_mult,
        tp_atr_mult=RISK.take_profit_atr_mult,
        limit=3,
    )

    # Prefer a single chart for the strongest fresh setup
    chart_sent = False
    if notify_photo and frames:
        raw = frames.get(top.symbol)
        df = raw[0] if isinstance(raw, tuple) else raw
        if df is not None:
            levels = levels_from_atr(
                side=top.side,
                price=float(top.price),
                atr=float(top.atr),
                sl_atr_mult=RISK.stop_loss_atr_mult,
                tp_atr_mult=RISK.take_profit_atr_mult,
            )
            sl = levels[0] if levels else None
            tp = levels[1] if levels else None
            png = render_trade_chart(
                df,
                symbol=top.symbol,
                side=top.side,
                entry=float(top.price) if top.price else None,
                stop_loss=sl,
                take_profit=tp,
                title_suffix="SETUP",
            )
            if png:
                try:
                    notify_photo(truncate_caption(caption, 1024), png)
                    chart_sent = True
                except Exception as exc:  # noqa: BLE001
                    log.warning("Opportunity photo notify failed: %s", exc)

    if not chart_sent:
        notify(caption)
    return fresh


def best_entry_candidate(
    opps: list[Opportunity],
    open_symbols: set[str],
    *,
    cfg: MultiPairConfig | None = None,
) -> Opportunity | None:
    """Pick highest-score allowed opportunity not already open."""
    picks = entry_candidates(opps, open_symbols, limit=1, cfg=cfg)
    return picks[0] if picks else None


def entry_candidates(
    opps: list[Opportunity],
    open_symbols: set[str],
    *,
    limit: int = 2,
    cfg: MultiPairConfig | None = None,
) -> list[Opportunity]:
    """Pick up to ``limit`` ranked opportunities not already open."""
    cfg = cfg or MULTI
    out: list[Opportunity] = []
    seen: set[str] = set(open_symbols)
    for opp in opps:
        if len(out) >= max(1, limit):
            break
        if not opp.allowed or opp.side == "flat":
            continue
        if opp.symbol in seen:
            continue
        from config import QUALITY as _Q

        if opp.score < _Q.min_entry_score:
            continue
        out.append(opp)
        seen.add(opp.symbol)
    return out
