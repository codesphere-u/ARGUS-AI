"""Fast live monitor: background learning, 24h symbols, urgency entry path."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import pandas as pd

from config import BOT, ONLINE, QUALITY

logger = logging.getLogger(__name__)

# Crypto + metals trade nearly 24/5 — don't wait for FX overlap / weekend
# Longest tags first where relevant for substring checks
_ALWAYS_LIQUID = (
    "AVAX",
    "DOGE",
    "LINK",
    "XAU",
    "XAG",
    "BTC",
    "ETH",
    "SOL",
    "XRP",
    "LTC",
    "BNB",
    "ADA",
)

# Asset base tags for leg parsing (longest first so AVAX ≠ AVA)
_ASSET_BASE_TAGS = (
    "AVAX",
    "DOGE",
    "LINK",
    "XAU",
    "XAG",
    "BTC",
    "ETH",
    "SOL",
    "XRP",
    "LTC",
    "BNB",
    "ADA",
)


def is_always_liquid(symbol: str) -> bool:
    u = symbol.upper()
    return any(tag in u for tag in _ALWAYS_LIQUID)


@dataclass
class LearnJob:
    """Queued fine-tune request."""

    df: pd.DataFrame
    feature_cols: list[str]
    force: bool = False
    micro: bool = False
    symbol: str | None = None
    notify: Any = None


class BackgroundLearner:
    """
    Run PPO fine-tunes off the trading thread so entries/exits never stall.

    Only one learn job runs at a time; newer requests coalesce while busy.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False
        self._pending: LearnJob | None = None
        self._closes_since_learn = 0
        self._on_done: Callable[[dict[str, Any]], None] | None = None
        self.last_result: dict[str, Any] = {}

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def set_on_done(self, cb: Callable[[dict[str, Any]], None] | None) -> None:
        self._on_done = cb

    def note_closed_trade(self) -> int:
        with self._lock:
            self._closes_since_learn += 1
            return self._closes_since_learn

    def reset_close_counter(self) -> None:
        with self._lock:
            self._closes_since_learn = 0

    def submit(self, job: LearnJob) -> bool:
        """Queue a learn job. Returns False if already busy (job may still be pending)."""
        with self._lock:
            if self._busy:
                # Keep the freshest market snapshot for when we free up
                self._pending = job
                logger.info("Online learn busy — queued next job (micro=%s)", job.micro)
                return False
            self._busy = True
        thread = threading.Thread(
            target=self._run, args=(job,), name="online-learn", daemon=True
        )
        thread.start()
        return True

    def _run(self, job: LearnJob) -> None:
        from online_learner import run_daily_update

        steps = ONLINE.micro_steps if job.micro else None
        try:
            result = run_daily_update(
                job.df,
                job.feature_cols,
                has_open_position=False,
                notify=job.notify,
                force=job.force or job.micro,
                symbol=job.symbol,
                max_steps=steps,
            )
            self.last_result = result
            if result.get("status") == "updated":
                self.reset_close_counter()
            if self._on_done:
                try:
                    self._on_done(result)
                except Exception:  # noqa: BLE001
                    logger.exception("Learn on_done callback failed")
        except Exception as exc:  # noqa: BLE001
            self.last_result = {"status": "error", "reason": str(exc)}
            logger.exception("Background learn failed: %s", exc)
        finally:
            next_job: LearnJob | None = None
            with self._lock:
                self._busy = False
                next_job = self._pending
                self._pending = None
            if next_job is not None:
                self.submit(next_job)


default_learner = BackgroundLearner()


def should_fast_skip_gemini(score: float, *, threshold: float | None = None) -> bool:
    """A+ scanner scores can skip Gemini latency for speed."""
    thr = threshold if threshold is not None else getattr(QUALITY, "fast_entry_score", 0.85)
    return score >= thr


# ---------------------------------------------------------------------------
# Per-symbol reentry cooldown (anti-revenge, not a global pause)
# ---------------------------------------------------------------------------

_REENTRY_UNTIL: dict[str, datetime] = {}


def record_reentry_cooldown(symbol: str, pnl: float, *, now: datetime | None = None) -> float:
    """
    After a close, block the same symbol for a short window.

    Losing closes: REENTRY_COOLDOWN_MIN (default 20m).
    Winning closes: REENTRY_COOLDOWN_WIN_MIN (default 5m).
    Returns minutes applied.
    """
    from config import MULTI
    from datetime import timedelta, timezone as _tz

    now = now or datetime.now(_tz.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_tz.utc)
    if pnl < 0:
        mins = float(getattr(MULTI, "reentry_cooldown_loss_min", 20.0))
    else:
        mins = float(getattr(MULTI, "reentry_cooldown_win_min", 5.0))
    mins = max(0.0, mins)
    if mins <= 0:
        _REENTRY_UNTIL.pop(symbol, None)
        return 0.0
    _REENTRY_UNTIL[symbol] = now + timedelta(minutes=mins)
    logger.info("Reentry cooldown %s for %.0fm (pnl=%.2f)", symbol, mins, pnl)
    return mins


def reentry_blocked(symbol: str, *, now: datetime | None = None) -> tuple[bool, str]:
    """Return (blocked, reason) if symbol is still in post-close cooldown."""
    from datetime import timezone as _tz

    now = now or datetime.now(_tz.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_tz.utc)
    until = _REENTRY_UNTIL.get(symbol)
    if until is None:
        return False, ""
    if until.tzinfo is None:
        until = until.replace(tzinfo=_tz.utc)
    if now >= until:
        _REENTRY_UNTIL.pop(symbol, None)
        return False, ""
    left = (until - now).total_seconds() / 60.0
    return True, f"Reentry cooldown {left:.0f}m left"


def _strip_broker_suffix(symbol: str) -> str:
    """EURUSDm / XAUUSDm → EURUSD / XAUUSD."""
    u = symbol.upper().replace(".", "")
    if u.endswith("M") and len(u) > 6:
        u = u[:-1]
    return u


def _currency_legs(symbol: str, side: str) -> tuple[str, str] | None:
    """
    Return (base, quote) currency codes for a symbol.

    Handles standard FX (EURUSD), metals (XAUUSD), and crypto (BTCUSD / SOLUSD).
    """
    del side  # side applied in _signed_legs
    u = _strip_broker_suffix(symbol)
    for tag in _ASSET_BASE_TAGS:
        if u.startswith(tag):
            quote = u[len(tag) :] or "USD"
            return tag, quote
    if len(u) >= 6:
        return u[:3], u[3:6]
    return None


def currency_codes_for_symbol(symbol: str) -> set[str]:
    """Currency / asset codes used for calendar + exposure (no MACRO defaults)."""
    legs = _currency_legs(symbol, "buy")
    if legs is None:
        return set()
    return {legs[0], legs[1]}


def _signed_legs(symbol: str, side: str) -> dict[str, int]:
    """Buy = +base/−quote; sell = −base/+quote."""
    legs = _currency_legs(symbol, side)
    if legs is None:
        return {}
    base, quote = legs
    s = side.lower()
    if s == "buy":
        return {base: 1, quote: -1}
    if s == "sell":
        return {base: -1, quote: 1}
    return {}


def currency_exposure_block(
    symbol: str,
    side: str,
    *,
    open_positions: list[dict[str, str]] | None = None,
    open_symbols: set[str] | list[str] | None = None,
    score: float,
    max_exposure: int = 2,
    elite_override: float = 0.92,
) -> tuple[bool, str]:
    """
    Block a new entry that would push any currency's absolute net exposure
    past ``max_exposure`` unless score ≥ elite_override.

    ``open_positions`` is preferred: ``[{symbol, side}, ...]``.
    ``open_symbols`` alone is treated as long-only (legacy).
    """
    if score >= elite_override:
        return False, ""

    exposure: dict[str, int] = {}
    if open_positions:
        for pos in open_positions:
            for ccy, delta in _signed_legs(
                str(pos.get("symbol", "")), str(pos.get("side", ""))
            ).items():
                exposure[ccy] = exposure.get(ccy, 0) + delta
    elif open_symbols:
        for sym in open_symbols:
            for ccy, delta in _signed_legs(str(sym), "buy").items():
                exposure[ccy] = exposure.get(ccy, 0) + delta

    proposed = _signed_legs(symbol, side)
    if not proposed:
        return False, ""

    for ccy, delta in proposed.items():
        new_net = exposure.get(ccy, 0) + delta
        if abs(new_net) > max_exposure:
            return (
                True,
                f"Currency exposure {ccy} would be {new_net} "
                f"(max±{max_exposure}) — need score≥{elite_override:.2f}",
            )
    return False, ""


def usd_fx_long_cluster_block(
    symbol: str,
    side: str,
    *,
    open_symbols: set[str] | list[str],
    score: float,
    elite_override: float = 0.92,
) -> tuple[bool, str]:
    """
    Deprecated thin wrapper — prefer ``currency_exposure_block``.

    Kept for import compatibility; maps open symbols as long-only legs.
    """
    return currency_exposure_block(
        symbol,
        side,
        open_symbols=open_symbols,
        score=score,
        max_exposure=2,
        elite_override=elite_override,
    )


def session_ok_for_quality(
    symbol: str,
    feats: dict[str, float],
    *,
    require_overlap: bool,
) -> tuple[bool, str]:
    """
    Session check for the quality gate.

    - Always-liquid symbols (BTC/ETH/XAU): always pass session filter.
    - SESSION_MODE=always/off/all: always pass.
    - require_overlap: London∩NY only.
    - else: London or NY (matches macro SESSION_MODE=london_ny).
    """
    from config import MACRO

    mode = (MACRO.session_mode or "always").lower()
    if mode in {"off", "always", "all"}:
        return True, "Session always"
    if is_always_liquid(symbol):
        return True, "24h liquid market"
    if require_overlap:
        if feats.get("in_overlap", 0.0) >= 1.0:
            return True, "Overlap session"
        return False, "Outside London/NY overlap"
    if feats.get("in_london", 0.0) >= 1.0 or feats.get("in_ny", 0.0) >= 1.0:
        return True, "London/NY session"
    return False, "Outside London/NY session"


def maybe_schedule_learn(
    *,
    df: pd.DataFrame | None,
    feature_cols: list[str] | None,
    open_count: int,
    closes_trigger: int,
    daily: bool,
    last_learn_day,
    now: datetime,
    notify: Any = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    """
    Non-blocking learn scheduler for the live loop.

    - Daily full update when flat (background).
    - Micro update after ``closes_trigger`` closed trades (even with other positions
      closed recently — only when currently flat to avoid mid-trade policy swap race).
    """
    if df is None or feature_cols is None:
        return {"status": "skipped", "reason": "no data"}
    if not ONLINE.enabled:
        return {"status": "skipped", "reason": "disabled"}
    if open_count > 0:
        return {"status": "skipped", "reason": "open positions"}
    if default_learner.busy:
        return {"status": "skipped", "reason": "learn busy"}

    closes = default_learner._closes_since_learn  # noqa: SLF001
    want_micro = closes >= max(1, closes_trigger)
    want_daily = daily and (last_learn_day is None or last_learn_day.date() != now.date())

    if not want_micro and not want_daily:
        return {"status": "skipped", "reason": "no trigger"}

    job = LearnJob(
        df=df,
        feature_cols=feature_cols,
        force=want_micro and not want_daily,
        micro=want_micro and not want_daily,
        symbol=symbol or BOT.symbol,
        notify=notify,
    )
    started = default_learner.submit(job)
    kind = "micro" if job.micro else "daily"
    return {
        "status": "started" if started else "queued",
        "kind": kind,
        "closes": closes,
    }
