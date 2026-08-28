"""Non-spammy Telegram channel progress updates (digests, milestones, daily wrap)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from config import BOT, BRAND_SHORT, CHANNEL, RISK, ChannelConfig
from chart_renderer import levels_from_atr, render_equity_chart, render_trade_chart
from telegram_format import format_channel_prediction, format_channel_progress
from trade_journal import TradeJournal, default_journal

logger = logging.getLogger(__name__)

NotifyText = Callable[..., bool]
NotifyPhoto = Callable[..., bool]


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _channel_ready(cfg: ChannelConfig) -> str | None:
    """Return skip reason or None if ready to post."""
    if not cfg.enabled:
        return "disabled"
    channel_id = cfg.channel_id or BOT.telegram_channel_id
    if not channel_id or not str(channel_id).strip():
        return "no_channel_id"
    return None


def decide_channel_event(
    *,
    equity: float,
    now: datetime,
    state: dict[str, Any],
    cfg: ChannelConfig,
) -> str | None:
    """
    Return event type to post, or None if we should stay quiet.

    Priority: daily_wrap > milestone > digest
    """
    last_digest = _parse_ts(state.get("last_digest_at"))
    last_wrap_day = state.get("last_daily_wrap_day")
    last_high = float(state.get("last_posted_equity_high") or 0.0)

    today = now.date().isoformat()
    if now.hour >= cfg.daily_wrap_hour_utc and last_wrap_day != today:
        return "daily_wrap"

    if last_high <= 0:
        pass
    elif equity >= last_high * (1.0 + cfg.milestone_pct):
        return "milestone"

    digest_seconds = max(0.5, float(cfg.digest_hours)) * 3600.0
    if last_digest is None:
        return "digest"
    elapsed = (now - last_digest.astimezone(timezone.utc)).total_seconds()
    if elapsed >= digest_seconds:
        return "digest"
    return None


def maybe_post_channel_update(
    *,
    equity: float,
    balance: float,
    mode: str = "LIVE",
    equity_history: Sequence[float] | None = None,
    journal: TradeJournal | None = None,
    notify_text: NotifyText | None = None,
    notify_photo: NotifyPhoto | None = None,
    force_event: str | None = None,
    cfg: ChannelConfig | None = None,
    now: datetime | None = None,
    invested_baseline: float | None = None,
) -> dict[str, Any]:
    """
    Post a progress update to the public channel when anti-spam gates allow.

    ``invested_baseline`` should be the real starting equity from MT5.
    """
    cfg = cfg or CHANNEL
    result: dict[str, Any] = {"posted": False, "event": None, "reason": "ok"}

    skip = _channel_ready(cfg)
    if skip:
        result["reason"] = skip
        return result
    if notify_text is None and notify_photo is None:
        result["reason"] = "no_notify"
        return result

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    state = _load_state(cfg.state_path)
    event = force_event or decide_channel_event(
        equity=equity, now=now, state=state, cfg=cfg
    )
    if event is None:
        result["reason"] = "cooldown"
        return result

    journal = journal or default_journal
    baseline = (
        float(invested_baseline)
        if invested_baseline is not None
        else float(equity)
    )
    if abs(baseline - 10_000.0) < 1e-6 and equity > 0:
        baseline = float(equity)
    stats = journal.performance_snapshot(
        equity=equity,
        balance=balance,
        initial_balance=baseline,
    )
    caption = format_channel_progress(stats, event=event, mode=mode)

    history = list(equity_history or [])
    if not history or history[-1] != equity:
        history = history + [equity]
    if len(history) > 240:
        history = history[-240:]

    image = render_equity_chart(
        history,
        title=f"{BRAND_SHORT} · {event.replace('_', ' ').title()}",
        invested=float(stats["invested"]),
    )

    sent = False
    if image and notify_photo is not None:
        try:
            sent = bool(notify_photo(caption, image))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Channel photo notify failed: %s", exc)
            sent = False
    if not sent and notify_text is not None:
        try:
            sent = bool(notify_text(caption))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Channel text notify failed: %s", exc)
            sent = False

    if not sent:
        result["reason"] = "send_failed"
        return result

    state["last_digest_at"] = now.isoformat()
    if event == "daily_wrap":
        state["last_daily_wrap_day"] = now.date().isoformat()
    prev_high = float(state.get("last_posted_equity_high") or 0.0)
    state["last_posted_equity_high"] = max(prev_high, float(equity))
    state["last_event"] = event
    state["last_equity"] = float(equity)
    _save_state(cfg.state_path, state)

    logger.info("Channel update posted event=%s equity=%.2f", event, equity)
    result.update({"posted": True, "event": event, "stats": stats})
    return result


def maybe_post_channel_prediction(
    opps: Sequence[Any],
    *,
    frames: dict[str, Any] | None = None,
    notify_text: NotifyText | None = None,
    notify_photo: NotifyPhoto | None = None,
    force: bool = False,
    cfg: ChannelConfig | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    Post one free high-confidence prediction to the channel (rate-limited).

    Only allowed + score >= pred_min_score setups. Max pred_max_per_day,
    cooldown between posts, and no duplicate symbol/side within cooldown.
    """
    cfg = cfg or CHANNEL
    result: dict[str, Any] = {"posted": False, "reason": "ok"}

    skip = _channel_ready(cfg)
    if skip:
        result["reason"] = skip
        return result
    if not cfg.pred_enabled and not force:
        result["reason"] = "pred_disabled"
        return result
    if notify_text is None and notify_photo is None:
        result["reason"] = "no_notify"
        return result

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    candidates = [
        o
        for o in opps
        if getattr(o, "allowed", False)
        and getattr(o, "side", "flat") in {"buy", "sell"}
        and float(getattr(o, "score", 0.0)) >= float(cfg.pred_min_score)
    ]
    if not candidates:
        result["reason"] = "no_high_score"
        return result
    top = max(candidates, key=lambda o: float(getattr(o, "score", 0.0)))

    state = _load_state(cfg.state_path)
    today = now.date().isoformat()
    pred_day = state.get("pred_day")
    pred_count = int(state.get("pred_count_today") or 0)
    if pred_day != today:
        pred_count = 0
    if not force and pred_count >= int(cfg.pred_max_per_day):
        result["reason"] = "daily_cap"
        return result

    last_pred = _parse_ts(state.get("last_pred_at"))
    cooldown = max(0.25, float(cfg.pred_cooldown_hours)) * 3600.0
    if not force and last_pred is not None:
        elapsed = (now - last_pred.astimezone(timezone.utc)).total_seconds()
        if elapsed < cooldown:
            result["reason"] = "pred_cooldown"
            return result

    last_key = state.get("last_pred_key")
    key = f"{getattr(top, 'symbol')}:{getattr(top, 'side')}"
    if not force and last_key == key and last_pred is not None:
        elapsed = (now - last_pred.astimezone(timezone.utc)).total_seconds()
        if elapsed < cooldown * 2:
            result["reason"] = "same_setup"
            return result

    symbol = str(top.symbol)
    side = str(top.side)
    score = float(top.score)
    price = float(getattr(top, "price", 0.0) or 0.0)
    atr = float(getattr(top, "atr", 0.0) or 0.0)
    reasons = list(getattr(top, "reasons", []) or [])

    caption = format_channel_prediction(
        symbol=symbol,
        side=side,
        score=score,
        price=price,
        atr=atr,
        reasons=reasons,
        sl_atr_mult=RISK.stop_loss_atr_mult,
        tp_atr_mult=RISK.take_profit_atr_mult,
    )

    levels = levels_from_atr(
        side=side,
        price=price,
        atr=atr,
        sl_atr_mult=RISK.stop_loss_atr_mult,
        tp_atr_mult=RISK.take_profit_atr_mult,
    )
    sl = levels[0] if levels else None
    tp = levels[1] if levels else None

    image = None
    if frames:
        raw = frames.get(symbol)
        df = raw[0] if isinstance(raw, tuple) else raw
        if df is not None:
            image = render_trade_chart(
                df,
                symbol=symbol,
                side=side,
                entry=price if price else None,
                stop_loss=sl,
                take_profit=tp,
                title_suffix="FREE PRED",
            )

    sent = False
    if image and notify_photo is not None:
        try:
            sent = bool(notify_photo(caption, image))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Channel prediction photo failed: %s", exc)
    if not sent and notify_text is not None:
        try:
            sent = bool(notify_text(caption))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Channel prediction text failed: %s", exc)

    if not sent:
        result["reason"] = "send_failed"
        return result

    state["last_pred_at"] = now.isoformat()
    state["last_pred_key"] = key
    state["pred_day"] = today
    state["pred_count_today"] = pred_count + 1
    _save_state(cfg.state_path, state)

    logger.info(
        "Channel prediction posted %s %s score=%.2f",
        side.upper(),
        symbol,
        score,
    )
    result.update(
        {
            "posted": True,
            "symbol": symbol,
            "side": side,
            "score": score,
        }
    )
    return result
