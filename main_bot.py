"""
Live / paper trading loop, MT5 execution, Telegram alerts, train & backtest CLI.

Usage
-----
    python main_bot.py train [--timesteps 100000] [--synthetic]
    python main_bot.py backtest [--synthetic]
    python main_bot.py run [--synthetic]
    python main_bot.py learn [--synthetic] [--force]
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

from channel_updates import maybe_post_channel_prediction, maybe_post_channel_update
from chart_renderer import append_advisor_note, render_equity_chart, render_trade_chart
from config import (
    BOT,
    BRAND_SHORT,
    CHANNEL,
    DATA,
    DATA_DIR,
    GEMINI,
    MODELS_DIR,
    MULTI,
    ONLINE,
    QUALITY,
    RISK,
    BotConfig,
)
from data_pipeline import (
    MT5ConnectionError,
    build_feature_frame,
    fetch_account_state,
    filter_watchlist_symbols,
    initialize_mt5,
    load_historical_features,
    load_live_features,
    shutdown_mt5,
    synthesize_ohlcv,
)
from desk_coach import default_coach
from environment import TradingEnv, build_live_observation
from gemini_advisor import TradeAdvice, compose_alert, default_advisor
from live_brain import (
    currency_exposure_block,
    default_learner,
    maybe_schedule_learn,
    record_reentry_cooldown,
    reentry_blocked,
    should_fast_skip_gemini,
)
from loss_guard import default_loss_guard
from macro_filter import MacroFilter, default_macro_filter
from online_learner import run_daily_update
from opportunity_scanner import (
    alert_opportunities,
    entry_candidates,
    scan_opportunities,
)
from quality_gate import evaluate_hot_grab, evaluate_quality
from risk_engine import MultiSymbolRiskManager, RiskEngine, TradeOrder
from rocket_exit import RocketExitManager
from telegram_commands import (
    RuntimeControl,
    TelegramCommandListener,
    channel_deep_link_keyboard,
    entry_alert_keyboard,
    exit_alert_keyboard,
    format_positions_card,
    format_scan_card,
    format_status_card,
    main_inline_keyboard,
    main_reply_keyboard,
    news_warning_keyboard,
    opportunity_alert_keyboard,
    positions_close_keyboard,
    positions_manage_keyboard,
)
from telegram_format import (
    fmt_money,
    fmt_price,
    format_backtest,
    format_daily_digest,
    format_desk_pulse,
    format_entry,
    format_error,
    format_exit,
    format_journal_card,
    format_news_warning_card,
    format_pnl_card,
    format_shutdown,
    format_startup,
    format_train_complete,
)
from trade_journal import TradeJournal, default_journal

logger = logging.getLogger(__name__)

try:
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover
    mt5 = None  # type: ignore

try:
    from stable_baselines3 import PPO
except ImportError:  # pragma: no cover
    PPO = None  # type: ignore


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def _telegram_targets(cfg: BotConfig, chat_id: str | None) -> list[str]:
    """Resolve one or more chat IDs (comma-separated TELEGRAM_CHAT_ID supported)."""
    raw = chat_id if chat_id is not None else cfg.telegram_chat_id
    if not raw:
        return []
    return [p.strip() for p in str(raw).split(",") if p.strip()]


def send_telegram(
    message: str,
    cfg: BotConfig | None = None,
    *,
    chat_id: str | None = None,
    reply_markup: dict | None = None,
) -> bool:
    """Send a real-time alert via Telegram Bot API (supports multiple chat IDs)."""
    from telegram_format import sanitize_telegram_html

    cfg = cfg or BOT
    token = cfg.telegram_bot_token
    targets = _telegram_targets(cfg, chat_id)
    if not token or not targets:
        logger.debug("Telegram not configured; skipping alert.")
        return False

    # Sanitize first so coach/Gemini HTML does not 400 the whole send
    safe = sanitize_telegram_html(message or "")
    if not safe:
        safe = "…"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok_any = False
    for target in targets:
        attempts: list[dict[str, Any]] = [
            {
                "chat_id": target,
                "text": safe,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            # Fallback: plain text (no HTML) if parse fails
            {
                "chat_id": target,
                "text": safe[:3900],
                "disable_web_page_preview": True,
            },
        ]
        if reply_markup is not None:
            attempts[0]["reply_markup"] = reply_markup
            attempts[1]["reply_markup"] = reply_markup
        # Last resort: plain text without keyboard
        attempts.append(
            {
                "chat_id": target,
                "text": safe[:3900],
                "disable_web_page_preview": True,
            }
        )

        sent = False
        for payload in attempts:
            for flood_try in range(3):
                try:
                    resp = requests.post(url, json=payload, timeout=15)
                    if resp.ok:
                        ok_any = True
                        sent = True
                        break
                    if resp.status_code == 429:
                        wait = 3.0
                        try:
                            wait = float(
                                (resp.json().get("parameters") or {}).get(
                                    "retry_after", wait
                                )
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("Failed parsing Telegram retry_after: %s", exc)
                        logger.warning(
                            "Telegram 429 flood control — retry in %.1fs (%s)",
                            wait,
                            target,
                        )
                        time.sleep(min(max(wait, 1.0), 30.0))
                        continue
                    detail = ""
                    try:
                        detail = str(resp.json())
                    except Exception:  # noqa: BLE001
                        detail = (resp.text or "")[:300]
                    logger.warning(
                        "Telegram send %s → %s %s",
                        target,
                        resp.status_code,
                        detail,
                    )
                    break
                except requests.RequestException as exc:
                    logger.warning("Telegram send failed (%s): %s", target, exc)
                    break
            if sent:
                break
        if not sent:
            logger.warning("Telegram send exhausted retries for %s", target)
    return ok_any


def send_telegram_photo(
    caption: str,
    image_bytes: bytes,
    cfg: BotConfig | None = None,
    *,
    chat_id: str | None = None,
    reply_markup: dict | None = None,
) -> bool:
    """Send a PNG chart via Telegram sendPhoto (HTML caption, max 1024 chars)."""
    from telegram_format import sanitize_telegram_html

    cfg = cfg or BOT
    token = cfg.telegram_bot_token
    targets = _telegram_targets(cfg, chat_id)
    if not token or not targets:
        logger.debug("Telegram not configured; skipping photo.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    safe_cap = sanitize_telegram_html(caption or "", limit=1024)
    ok_any = False
    for target in targets:
        try:
            import json as _json

            files = {"photo": ("chart.png", image_bytes, "image/png")}
            data: dict[str, Any] = {
                "chat_id": target,
                "caption": safe_cap[:1024],
                "parse_mode": "HTML",
            }
            if reply_markup is not None:
                data["reply_markup"] = _json.dumps(reply_markup)
            resp = requests.post(url, data=data, files=files, timeout=30)
            if resp.ok:
                ok_any = True
                continue
            # Fallback: plain caption (re-open file bytes)
            files2 = {"photo": ("chart.png", image_bytes, "image/png")}
            data_plain: dict[str, Any] = {
                "chat_id": target,
                "caption": safe_cap[:1024],
            }
            if reply_markup is not None:
                data_plain["reply_markup"] = _json.dumps(reply_markup)
            resp2 = requests.post(url, data=data_plain, files=files2, timeout=30)
            if resp2.ok:
                ok_any = True
            else:
                logger.warning(
                    "Telegram photo send failed (%s): %s %s",
                    target,
                    resp.status_code,
                    (resp.text or "")[:200],
                )
        except requests.RequestException as exc:
            logger.warning("Telegram photo send failed (%s): %s", target, exc)
    return ok_any


def send_channel_text(
    message: str,
    cfg: BotConfig | None = None,
    *,
    reply_markup: dict | None = None,
) -> bool:
    cfg = cfg or BOT
    channel = cfg.telegram_channel_id or CHANNEL.channel_id
    return send_telegram(message, cfg=cfg, chat_id=channel, reply_markup=reply_markup)


def send_channel_photo(
    caption: str,
    image_bytes: bytes,
    cfg: BotConfig | None = None,
    *,
    reply_markup: dict | None = None,
) -> bool:
    cfg = cfg or BOT
    channel = cfg.telegram_channel_id or CHANNEL.channel_id
    return send_telegram_photo(
        caption, image_bytes, cfg=cfg, chat_id=channel, reply_markup=reply_markup
    )


def send_trade_alert(
    caption: str,
    *,
    df: pd.DataFrame | None = None,
    symbol: str = "",
    side: str | None = None,
    entry: float | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    exit_price: float | None = None,
    title_suffix: str | None = None,
    cfg: BotConfig | None = None,
    reply_markup: dict | None = None,
) -> bool:
    """Prefer chart+caption; fall back to text-only if render/send fails."""
    image: bytes | None = None
    if df is not None and not df.empty and symbol:
        image = render_trade_chart(
            df,
            symbol=symbol,
            side=side,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            exit_price=exit_price,
            title_suffix=title_suffix,
        )
    if image:
        ok = send_telegram_photo(caption, image, cfg=cfg, reply_markup=reply_markup)
        if ok:
            return True
    return send_telegram(caption, cfg=cfg, reply_markup=reply_markup)


# ---------------------------------------------------------------------------
# Policy helpers
# ---------------------------------------------------------------------------


class HoldPolicy:
    """Fallback policy that always HOLDs when no trained model is available."""

    def predict(self, obs, deterministic: bool = True):
        return 0, None


def load_policy(model_path: Path, expected_obs_dim: int | None = None):
    """Load a PPO policy or return a HOLD fallback; warn on obs-dim mismatch."""
    if PPO is None:
        logger.warning("stable-baselines3 not installed; using HOLD policy.")
        return HoldPolicy()
    if not model_path.exists():
        logger.warning("Model not found at %s; using HOLD policy.", model_path)
        return HoldPolicy()
    logger.info("Loading policy from %s", model_path)
    model = PPO.load(str(model_path))
    if expected_obs_dim is not None:
        try:
            model_dim = int(model.observation_space.shape[0])  # type: ignore[index]
            if model_dim != expected_obs_dim:
                logger.warning(
                    "Observation dim mismatch: model=%s env=%s. "
                    "Retrain with: python main_bot.py train --synthetic (or MT5 history).",
                    model_dim,
                    expected_obs_dim,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not inspect model observation space: %s", exc)
    return model


# ---------------------------------------------------------------------------
# MT5 execution with retries
# ---------------------------------------------------------------------------


def _retry(fn, *, retries: int, base_delay: float, label: str):
    """Exponential backoff retry for network / terminal timeouts."""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            msg = str(exc)
            if "10027" in msg or "AutoTrading disabled" in msg or "10026" in msg:
                raise RuntimeError(
                    "AutoTrading is disabled in MetaTrader 5. "
                    "Click the 'Algo Trading' button in the MT5 toolbar (must be green/ON), "
                    "and enable Tools → Options → Expert Advisors → Allow algorithmic trading. "
                    "Then restart: python main_bot.py run"
                ) from exc
            delay = base_delay * (2**attempt)
            logger.warning("%s failed (attempt %s/%s): %s", label, attempt + 1, retries, exc)
            time.sleep(delay)
    raise RuntimeError(f"{label} failed after {retries} retries") from last_exc


def _ensure_autotrading_enabled() -> None:
    """Fail fast if the MT5 terminal has AutoTrading turned off."""
    if mt5 is None:
        raise MT5ConnectionError("MetaTrader5 not installed.")
    terminal = mt5.terminal_info()
    if terminal is None:
        raise MT5ConnectionError(f"terminal_info failed: {mt5.last_error()}")
    if not terminal.trade_allowed:
        raise RuntimeError(
            "AutoTrading is disabled in MetaTrader 5 (terminal.trade_allowed=False). "
            "Click the 'Algo Trading' toolbar button so it turns green/ON, then re-run the bot."
        )


def _mt5_symbol_info(symbol: str):
    if mt5 is None:
        raise MT5ConnectionError("MetaTrader5 not installed.")
    info = mt5.symbol_info(symbol)
    if info is None:
        raise MT5ConnectionError(f"symbol_info({symbol}) failed: {mt5.last_error()}")
    return info


def _mt5_tick(symbol: str):
    if mt5 is None:
        raise MT5ConnectionError("MetaTrader5 not installed.")
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise MT5ConnectionError(f"symbol_info_tick({symbol}) failed: {mt5.last_error()}")
    return tick


BOT_MAGIC = 20260728
ADOPTED_TICKETS_PATH = DATA_DIR / "adopted_tickets.json"


def _load_adopted_tickets() -> set[int]:
    try:
        if not ADOPTED_TICKETS_PATH.exists():
            return set()
        raw = json.loads(ADOPTED_TICKETS_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return {int(x) for x in raw if str(x).isdigit() or isinstance(x, int)}
        if isinstance(raw, dict):
            return {int(x) for x in (raw.get("tickets") or [])}
    except Exception:
        logger.debug("adopted_tickets load failed", exc_info=True)
    return set()


def _persist_adopted_tickets(tickets: set[int]) -> None:
    try:
        ADOPTED_TICKETS_PATH.parent.mkdir(parents=True, exist_ok=True)
        ADOPTED_TICKETS_PATH.write_text(
            json.dumps({"tickets": sorted(tickets)}, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logger.warning("adopted_tickets save failed", exc_info=True)


def remember_adopted_ticket(ticket: int) -> None:
    tickets = _load_adopted_tickets()
    tickets.add(int(ticket))
    _persist_adopted_tickets(tickets)


def forget_adopted_ticket(ticket: int | None) -> None:
    if not ticket:
        return
    tickets = _load_adopted_tickets()
    if int(ticket) in tickets:
        tickets.discard(int(ticket))
        _persist_adopted_tickets(tickets)


def split_broker_positions(
    *,
    paper: bool = False,
) -> tuple[list[Any], list[Any]]:
    """Return (bot_magic_positions, external_positions) from MT5."""
    if paper or mt5 is None:
        return [], []
    try:
        poses = list(mt5.positions_get() or ())
    except Exception:  # noqa: BLE001
        return [], []
    adopted = _load_adopted_tickets()
    bot: list[Any] = []
    external: list[Any] = []
    for p in poses:
        magic = int(getattr(p, "magic", 0) or 0)
        ticket = int(getattr(p, "ticket", 0) or 0)
        if magic == BOT_MAGIC or ticket in adopted:
            bot.append(p)
        else:
            external.append(p)
    return bot, external


def broker_position_rows(
    positions: list[Any],
    *,
    rows: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Normalize MT5 position objects into Telegram card dicts."""
    out: list[dict[str, Any]] = []
    rows = rows or {}
    for pos in positions:
        sym = str(pos.symbol)
        side = "buy" if int(pos.type) == 0 else "sell"
        entry = float(pos.price_open)
        price = float(getattr(pos, "price_current", 0) or 0)
        if price <= 0 and sym in rows:
            price = float(rows[sym]["close"])
        if price <= 0:
            price = entry
        out.append(
            {
                "symbol": sym,
                "side": side,
                "entry": entry,
                "price": price,
                "unrealized": float(getattr(pos, "profit", 0) or 0)
                + float(getattr(pos, "swap", 0) or 0)
                + float(getattr(pos, "commission", 0) or 0),
                "stop_loss": float(pos.sl or 0),
                "take_profit": float(pos.tp or 0),
                "ticket": int(pos.ticket),
                "magic": int(getattr(pos, "magic", 0) or 0),
                "volume": float(pos.volume),
            }
        )
    return out


def stamp_mt5_position(
    pos: Any,
    *,
    manager: MultiSymbolRiskManager,
    rocket: RocketExitManager,
    journal: TradeJournal,
    journal_ids: dict[str, int],
    watchlist: set[str] | None = None,
) -> dict[str, Any]:
    """Attach one MT5 position into local engine/rocket/journal. Returns status dict."""
    sym = str(pos.symbol)
    if watchlist is not None and sym not in watchlist:
        return {
            "status": "rejected",
            "reason": f"{sym} is not on the watchlist — adopt only watchlist symbols",
        }
    eng = manager.engine(sym)
    if eng.position.side != "flat":
        return {
            "status": "rejected",
            "reason": f"{sym} already managed locally",
        }
    side = "buy" if int(pos.type) == 0 else "sell"
    entry = float(pos.price_open)
    volume = float(pos.volume)
    sl = float(pos.sl or 0.0)
    tp = float(pos.tp or 0.0)
    ticket = int(pos.ticket)
    atr = abs(entry - sl) / max(RISK.stop_loss_atr_mult, 1e-8) if sl else 0.0
    eng.register_fill(
        side,  # type: ignore[arg-type]
        volume,
        entry,
        sl,
        tp,
        atr,
        ticket=ticket,
    )
    rocket.register_open(
        sym,
        entry=entry,
        stop_loss=sl or entry,
        volume=volume,
        contract_size=eng.contract_size,
    )
    open_id = journal.find_open_id(sym)
    if open_id is not None:
        journal_ids[sym] = open_id
        journal.set_ticket(open_id, ticket)
    else:
        journal_ids[sym] = journal.log_open(
            symbol=sym,
            side=side,
            volume=volume,
            entry=entry,
            action=1 if side == "buy" else 2,
            ticket=ticket,
        )
    return {
        "status": "ok",
        "symbol": sym,
        "side": side,
        "ticket": ticket,
        "volume": volume,
        "entry": entry,
    }


def _reconnect_mt5(cfg: BotConfig, *, retries: int = 3, delay: float = 5.0) -> bool:
    """Attempt to re-initialize MT5 after a disconnect. Returns True on success."""
    for attempt in range(max(1, retries)):
        try:
            try:
                shutdown_mt5()
            except Exception as exc:  # noqa: BLE001
                logger.debug("shutdown_mt5 error during cleanup: %s", exc)
            initialize_mt5(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path)
            if mt5 is not None and mt5.terminal_info() is not None:
                logger.info("MT5 reconnected (attempt %s/%s)", attempt + 1, retries)
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "MT5 reconnect failed (attempt %s/%s): %s",
                attempt + 1,
                retries,
                exc,
            )
        time.sleep(delay * (attempt + 1))
    return False


def _mt5_connected() -> bool:
    if mt5 is None:
        return False
    try:
        return mt5.terminal_info() is not None
    except Exception:  # noqa: BLE001
        return False


def spread_too_wide(symbol: str, atr: float, *, cfg: BotConfig | None = None) -> tuple[bool, str]:
    """
    Live-only pre-entry gate: reject when bid-ask spread > fraction of ATR.
    Paper / synthetic callers should skip this check.
    """
    cfg = cfg or BOT
    atr = max(float(atr), 1e-12)
    limit = float(RISK.max_spread_atr_fraction) * atr
    try:
        tick = _mt5_tick(symbol)
        spread = abs(float(tick.ask) - float(tick.bid))
    except Exception as exc:  # noqa: BLE001
        return True, f"spread check failed: {exc}"
    if spread > limit:
        return (
            True,
            (
                f"Spread too wide ({spread:.5f} > {limit:.5f} = "
                f"{RISK.max_spread_atr_fraction:.2f}×ATR)"
            ),
        )
    return False, ""


def modify_mt5_sltp(
    symbol: str,
    *,
    stop_loss: float,
    take_profit: float | None = None,
    ticket: int | None = None,
    paper: bool = False,
) -> dict[str, Any]:
    """Push updated SL/TP to the broker (TRADE_ACTION_SLTP). Paper = no-op success."""
    if paper:
        return {"status": "paper_sltp", "sl": stop_loss, "tp": take_profit}
    if mt5 is None:
        return {"status": "error", "reason": "MetaTrader5 not installed"}
    try:
        positions = list(mt5.positions_get(symbol=symbol) or ())
        if ticket is not None:
            # Ticket wins (adopted / exact match) — magic may be 0 on manual trades
            positions = [p for p in positions if int(p.ticket) == int(ticket)]
        else:
            positions = [
                p for p in positions if int(getattr(p, "magic", 0) or 0) == BOT_MAGIC
            ]
        if not positions:
            return {"status": "no_position", "reason": "No bot position to modify"}
        pos = positions[0]
        tp = float(take_profit) if take_profit is not None else float(pos.tp or 0.0)
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": symbol,
            "position": int(pos.ticket),
            "sl": float(stop_loss),
            "tp": tp,
            "magic": int(getattr(pos, "magic", 0) or BOT_MAGIC),
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = mt5.last_error() if result is None else (result.retcode, result.comment)
            logger.warning("MT5 SLTP modify failed %s: %s", symbol, err)
            return {"status": "error", "reason": str(err)}
        logger.info(
            "MT5 SLTP synced %s ticket=%s SL=%.5f TP=%.5f",
            symbol,
            pos.ticket,
            stop_loss,
            tp,
        )
        return {"status": "ok", "ticket": int(pos.ticket), "sl": stop_loss, "tp": tp}
    except Exception as exc:  # noqa: BLE001
        logger.warning("MT5 SLTP modify exception %s: %s", symbol, exc)
        return {"status": "error", "reason": str(exc)}


def send_mt5_order(
    symbol: str,
    order: TradeOrder,
    *,
    paper: bool,
    cfg: BotConfig | None = None,
    market_price: float | None = None,
    ticket: int | None = None,
) -> dict[str, Any]:
    """Execute a risk-approved order on MT5 (or simulate in paper mode)."""
    cfg = cfg or BOT
    if order.action in {"hold"} or not order.approved:
        return {"status": "skipped", "reason": order.reason}

    if paper:
        price = market_price
        if price is None:
            try:
                if mt5 is not None:
                    tick = _mt5_tick(symbol)
                    if order.action == "buy":
                        price = float(tick.ask)
                    elif order.action in {"sell", "close"}:
                        price = float(tick.bid)
                    else:
                        price = float((tick.bid + tick.ask) / 2)
            except Exception:  # noqa: BLE001
                price = None
        if price is None:
            raise RuntimeError("Paper fill requires market_price when MT5 ticks are unavailable.")
        fill = {
            "status": "paper_fill",
            "action": order.action,
            "volume": order.volume,
            "price": float(price),
            "sl": order.stop_loss,
            "tp": order.take_profit,
            "reason": order.reason,
            "time": datetime.now(timezone.utc).isoformat(),
            "profit": None,
            "ticket": ticket,
        }
        logger.info("PAPER fill: %s", fill)
        return fill

    if mt5 is None:
        raise MT5ConnectionError("MetaTrader5 not installed.")

    _ensure_autotrading_enabled()

    def _execute() -> dict[str, Any]:
        info = _mt5_symbol_info(symbol)
        tick = _mt5_tick(symbol)
        filling = mt5.ORDER_FILLING_IOC
        if info.filling_mode & 2:
            filling = mt5.ORDER_FILLING_IOC
        elif info.filling_mode & 1:
            filling = mt5.ORDER_FILLING_FOK
        else:
            filling = mt5.ORDER_FILLING_RETURN

        if order.action == "close":
            positions = list(mt5.positions_get(symbol=symbol) or ())
            if ticket is not None:
                positions = [p for p in positions if int(p.ticket) == int(ticket)]
            else:
                positions = [
                    p
                    for p in positions
                    if int(getattr(p, "magic", 0) or 0) == BOT_MAGIC
                ]
            if not positions:
                return {
                    "status": "no_position",
                    "reason": "Nothing to close (ticket/magic)",
                }
            results = []
            total_profit = 0.0
            last_price = float(tick.bid)
            for pos in positions:
                close_type = (
                    mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
                )
                price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask
                full_pos_vol = float(pos.volume)
                close_vol = full_pos_vol
                if order.volume is not None and 0.0 < float(order.volume) < full_pos_vol:
                    vmin = float(info.volume_min or 0.01)
                    vstep = float(info.volume_step or 0.01)
                    v_req = float(order.volume)
                    close_vol = max(vmin, (v_req // vstep) * vstep)
                    close_vol = float(round(min(close_vol, full_pos_vol), 2))

                ratio = close_vol / full_pos_vol if full_pos_vol > 0 else 1.0
                broker_pnl = (
                    float(getattr(pos, "profit", 0) or 0)
                    + float(getattr(pos, "swap", 0) or 0)
                    + float(getattr(pos, "commission", 0) or 0)
                ) * ratio
                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": symbol,
                    "volume": close_vol,
                    "type": close_type,
                    "position": pos.ticket,
                    "price": price,
                    "deviation": 20,
                    "magic": int(getattr(pos, "magic", 0) or BOT_MAGIC),
                    "comment": "RL bot partial/close",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": filling,
                }
                result = mt5.order_send(request)
                if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                    err = mt5.last_error() if result is None else result.retcode
                    raise RuntimeError(f"Close failed: {err}")
                fill_px = float(result.price or price)
                last_price = fill_px
                total_profit += broker_pnl
                results.append(
                    {
                        "ticket": result.order,
                        "position": int(pos.ticket),
                        "price": fill_px,
                        "volume": close_vol,
                        "profit": broker_pnl,
                    }
                )
            return {
                "status": "closed",
                "closes": results,
                "price": last_price,
                "profit": total_profit,
                "volume": sum(float(r["volume"]) for r in results),
            }

        order_type = mt5.ORDER_TYPE_BUY if order.action == "buy" else mt5.ORDER_TYPE_SELL
        price = tick.ask if order.action == "buy" else tick.bid
        # Normalize volume to broker rules (prevents 10014 Invalid volume)
        vmin = float(info.volume_min or 0.01)
        vmax = float(info.volume_max or 100.0)
        vstep = float(info.volume_step or 0.01)
        vol = float(order.volume)
        vol = min(vol, vmax, float(RISK.max_lot))
        vol = max(vmin, (vol // vstep) * vstep) if vol > 0 else 0.0
        vol = float(round(vol, 2))
        if vol < vmin:
            return {
                "status": "rejected",
                "reason": f"Invalid volume after clamp ({order.volume} → {vol})",
                "action": order.action,
            }
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": vol,
            "type": order_type,
            "price": price,
            "sl": float(order.stop_loss),
            "tp": float(order.take_profit),
            "deviation": 20,
            "magic": BOT_MAGIC,
            "comment": f"RL bot {order.action}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = mt5.last_error() if result is None else (result.retcode, result.comment)
            # Soft-fail invalid volume / rejects — don't crash the live loop
            logger.warning("MT5 order rejected %s vol=%.2f: %s", symbol, vol, err)
            return {
                "status": "rejected",
                "reason": str(err),
                "volume": vol,
                "action": order.action,
            }
        fill_vol = float(getattr(result, "volume", 0) or vol)
        if fill_vol <= 0:
            fill_vol = vol
        # Prefer position ticket after fill
        pos_ticket = None
        try:
            opened = mt5.positions_get(symbol=symbol) or ()
            for p in opened:
                if int(getattr(p, "magic", 0) or 0) == BOT_MAGIC:
                    pos_ticket = int(p.ticket)
                    fill_vol = float(p.volume or fill_vol)
                    break
        except Exception:  # noqa: BLE001
            pos_ticket = int(result.order) if result.order else None
        return {
            "status": "filled",
            "ticket": pos_ticket or int(result.order or 0) or None,
            "price": float(result.price or price),
            "volume": fill_vol,
            "sl": order.stop_loss,
            "tp": order.take_profit,
            "action": order.action,
        }

    try:
        return _retry(
            _execute,
            retries=cfg.max_retries,
            base_delay=cfg.retry_base_delay,
            label="MT5 order_send",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("MT5 order_send soft-fail: %s", exc)
        return {"status": "error", "reason": str(exc), "action": order.action}


# ---------------------------------------------------------------------------
# Backtest metrics
# ---------------------------------------------------------------------------


@dataclass
class BacktestResult:
    total_net_profit: float = 0.0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    trades: int = 0
    equity_curve: list[float] = field(default_factory=list)

    def as_dict(self) -> dict[str, float | int]:
        return {
            "Total Net Profit": round(self.total_net_profit, 2),
            "Win Rate %": round(self.win_rate_pct, 2),
            "Profit Factor": round(self.profit_factor, 3),
            "Max Drawdown %": round(self.max_drawdown_pct, 2),
            "Sharpe Ratio": round(self.sharpe_ratio, 3),
            "Trades": self.trades,
        }


def compute_metrics(
    equity_curve: list[float],
    trade_pnls: list[float],
    periods_per_year: float = 35040.0,
) -> BacktestResult:
    """Compute standard performance metrics from equity and trade PnL series."""
    eq = np.asarray(equity_curve, dtype=float)
    if len(eq) < 2:
        return BacktestResult()

    rets = np.diff(eq) / np.maximum(eq[:-1], 1e-8)
    total_net = float(eq[-1] - eq[0])
    peak = np.maximum.accumulate(eq)
    dd = (peak - eq) / np.maximum(peak, 1e-8)
    max_dd = float(dd.max() * 100)

    sharpe = 0.0
    if rets.std() > 0:
        sharpe = float((rets.mean() / rets.std()) * np.sqrt(periods_per_year))

    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p <= 0]
    win_rate = (len(wins) / len(trade_pnls) * 100) if trade_pnls else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0

    return BacktestResult(
        total_net_profit=total_net,
        win_rate_pct=win_rate,
        profit_factor=profit_factor if np.isfinite(profit_factor) else 999.0,
        max_drawdown_pct=max_dd,
        sharpe_ratio=sharpe,
        trades=len(trade_pnls),
        equity_curve=list(eq),
    )


def run_backtest(
    df: pd.DataFrame,
    feature_cols: list[str],
    policy,
    *,
    initial_balance: float | None = None,
    symbol: str | None = None,
    apply_macro_filters: bool = True,
) -> BacktestResult:
    """
    Replay policy with quality gate + rocket exits (aligned with live).

    New entries require: quality gate pass AND policy agrees with gate side.
    """
    from quality_gate import best_quality_side
    from rocket_exit import RocketExitManager

    initial_balance = initial_balance or BOT.initial_balance
    symbol = symbol or BOT.symbol
    macro = MacroFilter()
    env = TradingEnv(
        df,
        feature_cols,
        initial_balance=initial_balance,
        symbol=symbol,
        use_macro=True,
        enforce_macro_gate=apply_macro_filters,
        macro_filter=macro,
    )
    # Env uses long-only BUY/SELL; risk engine tracks full side for rocket/stops
    risk = RiskEngine(symbol=symbol, macro_filter=macro, enforce_macro=apply_macro_filters)
    risk.day_start_balance = initial_balance
    rocket = RocketExitManager()

    obs, _ = env.reset()
    equity_curve = [initial_balance]
    trade_pnls: list[float] = []
    prev_realized = 0.0

    terminated = False
    truncated = False
    while not (terminated or truncated):
        action, _ = policy.predict(obs, deterministic=True)
        action = int(action)

        idx = env._step_idx
        row = df.loc[idx]
        price = float(row["close"])
        atr = float(row["atr_14"])
        high = float(row["high"])
        low = float(row["low"])
        bar_time = env._bar_time()

        # Manage open risk/rocket before new entries (mirrors live loop)
        exit_fill: float | None = None
        if risk.position.side != "flat":
            risk.update_stops(price, atr)
            decision = rocket.evaluate(
                symbol,
                risk.position,
                price=price,
                atr=atr,
                contract_size=risk.contract_size,
                now=bar_time,
                bar_time=bar_time,
            )
            if decision.action == "tighten_sl" and decision.new_stop_loss is not None:
                if risk.position.side == "buy":
                    risk.position.stop_loss = max(
                        risk.position.stop_loss, decision.new_stop_loss
                    )
                else:
                    if risk.position.stop_loss == 0:
                        risk.position.stop_loss = decision.new_stop_loss
                    else:
                        risk.position.stop_loss = min(
                            risk.position.stop_loss, decision.new_stop_loss
                        )

            hit = risk.stop_hit(high, low)
            if decision.action == "close" or hit != "none":
                action = TradingEnv.ACTION_SELL
                if hit == "tp" and risk.position.take_profit:
                    exit_fill = float(risk.position.take_profit)
                elif hit == "sl" and risk.position.stop_loss:
                    exit_fill = float(risk.position.stop_loss)
                # rocket close / policy path: fill at bar close
            else:
                # Ignore raw policy SELL while long — exits are rocket/SL/TP only
                action = TradingEnv.ACTION_HOLD
        # Quality gate for new entries — policy must agree with gate side
        if risk.position.side == "flat" and env.position == 0:
            gate = best_quality_side(
                symbol,
                row,
                now=bar_time,
                macro=macro,
                policy_action=action,
            )
            if not apply_macro_filters:
                # still require EMA/RSI/score but skip session if disabled
                pass
            want_buy = gate.allowed and gate.side == "buy"
            # Quality-first: take gated buys unless policy explicitly wants SELL
            if want_buy and action != TradingEnv.ACTION_SELL:
                action = TradingEnv.ACTION_BUY
            else:
                action = TradingEnv.ACTION_HOLD

            if apply_macro_filters and action == TradingEnv.ACTION_BUY:
                allowed, _ = macro.is_trading_allowed(bar_time, symbol)
                if not allowed:
                    action = TradingEnv.ACTION_HOLD

        order = risk.approve_and_size(action, env.equity, price, atr, now=bar_time)

        env_action = TradingEnv.ACTION_HOLD
        if order.action == "buy" and order.approved:
            env_action = TradingEnv.ACTION_BUY
        elif order.action in {"sell", "close"} and order.approved:
            env_action = TradingEnv.ACTION_SELL
        elif action == TradingEnv.ACTION_SELL and env.position == 1:
            # Rocket/SL forced close path
            env_action = TradingEnv.ACTION_SELL

        step_exit = exit_fill if env_action == TradingEnv.ACTION_SELL else None
        obs, _reward, terminated, truncated, info = env.step(
            env_action, exit_price=step_exit
        )

        if env.position == 1 and risk.position.side == "flat":
            sl, tp = risk.initial_stops("buy", env.entry_price, atr)
            risk.register_fill("buy", order.volume or 0.01, env.entry_price, sl, tp, atr)
            rocket.register_open(
                symbol,
                entry=env.entry_price,
                stop_loss=sl,
                volume=order.volume or 0.01,
                contract_size=risk.contract_size,
            )
        elif env.position == 0 and risk.position.side != "flat":
            rocket.register_close(symbol)
            risk.register_close()

        realized_delta = info["realized_pnl"] - prev_realized
        if abs(realized_delta) > 1e-9:
            trade_pnls.append(realized_delta)
            prev_realized = info["realized_pnl"]

        equity_curve.append(info["equity"])
        risk.record_realized_pnl(info["equity"])

    return compute_metrics(equity_curve, trade_pnls)


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------


def train_agent(
    df: pd.DataFrame,
    feature_cols: list[str],
    *,
    timesteps: int = 150_000,
    model_path: Path | None = None,
    symbol: str | None = None,
    train_frac: float = 10 / 12,
) -> Path:
    """
    Train a PPO agent from scratch on a train split; smoke-check on held-out bars.

    Default split: first ~10/12 of history for training, last ~2/12 for validation.
    """
    if PPO is None:
        raise RuntimeError("stable-baselines3 is required for training. pip install stable-baselines3")

    model_path = model_path or BOT.model_path
    symbol = symbol or BOT.symbol
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    n = len(df)
    split = max(int(n * train_frac), min(500, n - 50))
    split = min(split, n - 50) if n > 100 else n
    train_df = df.iloc[:split].reset_index(drop=True)
    val_df = df.iloc[split:].reset_index(drop=True) if split < n else train_df

    env = TradingEnv(
        train_df,
        feature_cols,
        initial_balance=BOT.initial_balance,
        symbol=symbol,
        use_macro=True,
        enforce_macro_gate=True,
    )
    # Fresh policy — do not load drifted checkpoint
    model = PPO(
        "MlpPolicy",
        env,
        verbose=1,
        learning_rate=3e-4,
        n_steps=2048,
        batch_size=64,
        gamma=0.99,
        ent_coef=0.01,
    )
    logger.info(
        "Training PPO from scratch: %s steps on %s/%s bars (obs_dim=%s)...",
        timesteps,
        len(train_df),
        n,
        env.observation_space.shape,
    )
    model.learn(total_timesteps=timesteps)
    model.save(str(model_path))
    logger.info("Saved model to %s", model_path)

    if len(val_df) >= 50:
        val_result = run_backtest(
            val_df,
            feature_cols,
            model,
            symbol=symbol,
            apply_macro_filters=True,
        )
        logger.info(
            "Hold-out validation (%s bars): PF=%.3f WR=%.1f%% MDD=%.1f%% trades=%s net=%.2f",
            len(val_df),
            val_result.profit_factor,
            val_result.win_rate_pct,
            val_result.max_drawdown_pct,
            val_result.trades,
            val_result.total_net_profit,
        )

    send_telegram(format_train_complete(timesteps=timesteps, model_path=str(model_path)))
    return model_path


def run_walkforward(
    df: pd.DataFrame,
    feature_cols: list[str],
    policy,
    *,
    n_folds: int = 4,
    symbol: str | None = None,
    apply_macro_filters: bool = True,
) -> list[dict[str, float | int]]:
    """
    Split history into ``n_folds`` contiguous test windows and backtest each.

    Fold k uses bars [k*fold_size : (k+1)*fold_size] as the out-of-sample window.
    Reports profit factor, Sharpe, drawdown, win rate, and trade count per fold.
    """
    symbol = symbol or BOT.symbol
    n = len(df)
    fold_size = max(n // n_folds, 200)
    folds: list[dict[str, float | int]] = []
    for i in range(n_folds):
        start = i * fold_size
        end = n if i == n_folds - 1 else min((i + 1) * fold_size, n)
        if end - start < 100:
            continue
        slice_df = df.iloc[start:end].reset_index(drop=True)
        result = run_backtest(
            slice_df,
            feature_cols,
            policy,
            symbol=symbol,
            apply_macro_filters=apply_macro_filters,
        )
        folds.append(
            {
                "fold": i + 1,
                "bars": end - start,
                "start_idx": start,
                "end_idx": end,
                "profit_factor": round(result.profit_factor, 3),
                "sharpe": round(result.sharpe_ratio, 3),
                "max_dd_pct": round(result.max_drawdown_pct, 2),
                "win_rate_pct": round(result.win_rate_pct, 2),
                "trades": result.trades,
                "net_profit": round(result.total_net_profit, 2),
            }
        )
        logger.info(
            "Walk-forward fold %s/%s bars=%s PF=%.3f WR=%.1f%% MDD=%.1f%% Sharpe=%.2f trades=%s net=%.2f",
            i + 1,
            n_folds,
            end - start,
            result.profit_factor,
            result.win_rate_pct,
            result.max_drawdown_pct,
            result.sharpe_ratio,
            result.trades,
            result.total_net_profit,
        )
    return folds


# ---------------------------------------------------------------------------
# Live / paper loop
# ---------------------------------------------------------------------------


@dataclass
class PaperAccount:
    """Account state — synced from MT5 in live mode, simulated in paper."""

    balance: float = 10_000.0
    equity: float = 10_000.0
    currency: str = "USD"
    login: str = ""
    live_sync: bool = False

    def sync_from_mt5(self) -> bool:
        """Pull real balance/equity from MT5. Returns True on success."""
        try:
            state = fetch_account_state()
            self.balance = float(state["balance"])
            self.equity = float(state["equity"])
            self.currency = str(state.get("currency") or self.currency)
            self.login = str(state.get("login") or self.login)
            self.live_sync = True
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("MT5 account sync failed: %s", exc)
            return False


def _load_feature_data(synthetic: bool) -> tuple[pd.DataFrame, list[str]]:
    if synthetic:
        primary, context = synthesize_ohlcv(bars=35040)
        return build_feature_frame(primary, context)
    return load_historical_features()


def _apply_mt5_symbol_hints(manager: MultiSymbolRiskManager, symbol: str) -> None:
    """Pull contract size / lot step from MT5 when connected."""
    if mt5 is None:
        return
    try:
        info = mt5.symbol_info(symbol)
        if info is None:
            return
        manager.engine(symbol).apply_symbol_info(
            trade_contract_size=float(getattr(info, "trade_contract_size", 0) or 0),
            volume_min=float(getattr(info, "volume_min", 0) or 0),
            volume_max=float(getattr(info, "volume_max", 0) or 0),
            volume_step=float(getattr(info, "volume_step", 0) or 0),
            point=float(getattr(info, "point", 0) or 0),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed applying MT5 symbol info for %s: %s", symbol, exc)


def _synthetic_start_price(symbol: str) -> float:
    """Sensible seed prices for paper/synthetic paths by asset class."""
    u = symbol.upper()
    if "JPY" in u:
        return 150.0
    if "XAU" in u or "GOLD" in u:
        return 2300.0
    if "XAG" in u:
        return 28.0
    if "BTC" in u:
        return 65_000.0
    if "ETH" in u:
        return 3_200.0
    if "SOL" in u:
        return 150.0
    if "XRP" in u:
        return 0.55
    if "LTC" in u:
        return 85.0
    if "BNB" in u:
        return 580.0
    if "ADA" in u:
        return 0.45
    return 1.1


def _load_symbol_frame(
    symbol: str,
    *,
    synthetic: bool,
    seed: int,
) -> tuple[pd.DataFrame, list[str]]:
    if synthetic:
        # Distinct seeds per symbol so synthetic paths diverge
        primary, context = synthesize_ohlcv(
            bars=DATA.lookback_bars,
            seed=seed + abs(hash(symbol)) % 10_000,
            start_price=_synthetic_start_price(symbol),
        )
        return build_feature_frame(primary, context)
    return load_live_features(symbol)


def _side_to_action(side: str) -> int:
    s = (side or "").lower()
    if s == "buy":
        return TradingEnv.ACTION_BUY
    if s == "sell":
        return TradingEnv.ACTION_SELL
    return TradingEnv.ACTION_HOLD


def close_position_now(
    symbol: str,
    reason: str,
    *,
    manager: MultiSymbolRiskManager,
    rocket: RocketExitManager,
    account: PaperAccount,
    journal: TradeJournal,
    journal_ids: dict[str, int],
    loss_guard: Any,
    frames: dict[str, tuple[Any, Any]] | None = None,
    rows: dict[str, Any] | None = None,
    paper: bool = True,
    cfg: BotConfig | None = None,
    total_unrealized: float = 0.0,
    notify: bool = True,
) -> dict[str, Any]:
    """
    Close one open position (shared by exit loop and Telegram /close commands).

    Returns a result dict with status, pnl, r_multiple, etc.
    """
    cfg = cfg or BOT
    eng = manager.engine(symbol)
    if eng.position.side == "flat":
        return {"status": "no_position", "symbol": symbol}

    price = None
    if rows and symbol in rows:
        price = float(rows[symbol]["close"])
    if price is None:
        try:
            if mt5 is not None:
                tick = _mt5_tick(symbol)
                price = float(tick.bid if eng.position.side == "buy" else tick.ask)
        except Exception:  # noqa: BLE001
            price = eng.position.entry_price

    price = float(price or eng.position.entry_price)
    if eng.position.side == "buy":
        unrealized = (
            (price - eng.position.entry_price)
            * eng.position.volume
            * eng.contract_size
        )
    else:
        unrealized = (
            (eng.position.entry_price - price)
            * eng.position.volume
            * eng.contract_size
        )

    # R-multiple from rocket initial risk before we pop state
    rocket_st = rocket.states.get(symbol)
    initial_risk = float(rocket_st.initial_risk_money) if rocket_st else 0.0

    close_order = TradeOrder(
        action="close",
        volume=eng.position.volume,
        reason=reason,
    )
    fill = send_mt5_order(
        symbol,
        close_order,
        paper=paper,
        cfg=cfg,
        market_price=price,
        ticket=eng.position.ticket,
    )
    fill_status = str(fill.get("status") or "")
    if fill_status in {"rejected", "error", "no_position"}:
        return {
            "status": fill_status,
            "symbol": symbol,
            "reason": fill.get("reason", reason),
            "pnl": unrealized,
        }

    # Prefer broker fill price / reported profit when available
    if fill.get("price") is not None:
        try:
            price = float(fill["price"])
        except (TypeError, ValueError):
            pass
    if fill.get("profit") is not None and not paper:
        try:
            unrealized = float(fill["profit"])
        except (TypeError, ValueError):
            pass
    elif not paper and fill.get("closes"):
        # Recompute from fill prices if profit missing
        try:
            fill_px = float(fill["closes"][0]["price"])
            price = fill_px
            if eng.position.side == "buy":
                unrealized = (
                    (fill_px - eng.position.entry_price)
                    * eng.position.volume
                    * eng.contract_size
                )
            else:
                unrealized = (
                    (eng.position.entry_price - fill_px)
                    * eng.position.volume
                    * eng.contract_size
                )
        except (TypeError, ValueError, KeyError, IndexError):
            pass

    r_multiple = (unrealized / initial_risk) if initial_risk > 1e-8 else None

    if account.live_sync and not paper:
        account.sync_from_mt5()
    else:
        account.balance += unrealized
        account.equity = account.balance + (total_unrealized - unrealized)

    journal.log_close(
        trade_id=journal_ids.get(symbol),
        symbol=symbol,
        exit_price=price,
        pnl=unrealized,
    )
    lg = loss_guard.record_close(unrealized, now=datetime.now(timezone.utc))
    logger.info("Close streak=%s", lg.get("streak"))
    record_reentry_cooldown(symbol, unrealized)
    default_learner.note_closed_trade()
    journal_ids.pop(symbol, None)
    rocket.register_close(symbol)

    entry_px = eng.position.entry_price
    side_px = eng.position.side
    sl_px = eng.position.stop_loss
    tp_px = eng.position.take_profit
    closed_ticket = eng.position.ticket
    eng.register_close()
    forget_adopted_ticket(closed_ticket)
    eng.record_realized_pnl(account.equity)

    snap = journal.performance_snapshot(
        equity=account.equity,
        balance=account.balance,
        initial_balance=cfg.initial_balance,
    )
    win_rate = float(snap.get("win_rate") or 0)

    msg = format_exit(
        symbol=symbol,
        reason=reason,
        exit_price=price,
        pnl=unrealized,
        equity=account.equity,
        fill_status=fill_status,
        side=side_px if side_px in {"buy", "sell"} else None,
        entry=entry_px if entry_px else None,
        compact=True,
        r_multiple=r_multiple,
        win_rate=win_rate,
        mode="PAPER" if paper else "LIVE",
    )
    logger.info(
        "%s close PnL=%.2f equity=%.2f | %s",
        symbol,
        unrealized,
        account.equity,
        reason,
    )
    if notify:
        df_chart = None
        if frames and symbol in frames:
            df_chart = frames[symbol][0]
        send_trade_alert(
            msg,
            df=df_chart,
            symbol=symbol,
            side=side_px if side_px in {"buy", "sell"} else None,
            entry=entry_px if entry_px else None,
            stop_loss=sl_px if sl_px else None,
            take_profit=tp_px if tp_px else None,
            exit_price=price,
            title_suffix="EXIT",
            cfg=cfg,
            reply_markup=exit_alert_keyboard(),
        )
    return {
        "status": "closed",
        "symbol": symbol,
        "pnl": unrealized,
        "r_multiple": r_multiple,
        "win_rate": win_rate,
        "equity": account.equity,
        "reason": reason,
    }


def build_coach_context(
    *,
    manager: MultiSymbolRiskManager,
    account: PaperAccount,
    control: RuntimeControl,
    mode: str,
    get_live_context: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Snapshot for desk coach replies (positions, scans, risk params)."""
    ctx = get_live_context() or {}
    rows = ctx.get("rows") or {}
    opps = ctx.get("opps") or []
    positions: list[dict[str, Any]] = []
    for sym in manager.open_symbols():
        eng = manager.engine(sym)
        price = float(rows[sym]["close"]) if sym in rows else eng.position.entry_price
        if eng.position.side == "buy":
            unreal = (
                (price - eng.position.entry_price)
                * eng.position.volume
                * eng.contract_size
            )
        else:
            unreal = (
                (eng.position.entry_price - price)
                * eng.position.volume
                * eng.contract_size
            )
        positions.append(
            {
                "symbol": sym,
                "side": eng.position.side,
                "entry": eng.position.entry_price,
                "price": price,
                "volume": eng.position.volume,
                "unrealized": unreal,
                "stop_loss": eng.position.stop_loss,
                "take_profit": eng.position.take_profit,
            }
        )
    scans: list[dict[str, Any]] = []
    for o in opps[:5]:
        scans.append(
            {
                "symbol": getattr(o, "symbol", ""),
                "side": getattr(o, "side", ""),
                "score": float(getattr(o, "score", 0) or 0),
                "price": float(getattr(o, "price", 0) or 0),
                "atr": float(getattr(o, "atr", 0) or 0),
                "allowed": bool(getattr(o, "allowed", False)),
            }
        )
    return {
        "mode": mode,
        "paused": control.paused,
        "equity": account.equity,
        "balance": account.balance,
        "open_count": manager.open_count(),
        "max_open": MULTI.max_open_positions,
        "risk_money_override": control.risk_money_override,
        "sl_atr": RISK.stop_loss_atr_mult,
        "tp_atr": RISK.take_profit_atr_mult,
        "hot_grab": QUALITY.hot_grab_score,
        "min_score": QUALITY.min_entry_score,
        "positions": positions,
        "scans": scans,
    }


def _build_command_listener(
    *,
    control: RuntimeControl,
    manager: MultiSymbolRiskManager,
    rocket: RocketExitManager,
    account: PaperAccount,
    journal: TradeJournal,
    journal_ids: dict[str, int],
    loss_guard: Any,
    cfg: BotConfig,
    mode: str,
    paper: bool,
    equity_history: list[float],
    session_start_equity: float,
    get_live_context: Callable[[], dict[str, Any]],
) -> TelegramCommandListener:
    """Wire Telegram /commands to live-loop state."""

    def reply(text: str, chat_id: str | None = None, reply_markup: dict | None = None) -> bool:
        return send_telegram(text, cfg=cfg, chat_id=chat_id, reply_markup=reply_markup)

    def reply_with_menu(text: str, chat_id: str, *, inline: dict | None = None) -> bool:
        markup = inline if inline is not None else main_inline_keyboard(paused=control.paused)
        return reply(text, chat_id, reply_markup=markup)

    listener = TelegramCommandListener(
        control=control,
        cfg=cfg,
        reply=reply,
        get_equity=lambda: float(account.equity),
    )

    def cmd_chat(text: str, chat_id: str) -> None:
        """Free-text / coach-button path — never executes trades."""
        intent = None
        body = text
        if text.startswith("__intent:") and "\n" in text:
            header, body = text.split("\n", 1)
            intent = header.replace("__intent:", "").replace("__", "").strip() or None
        # Typing indicator instead of spammy "thinking…" text
        listener.send_typing(chat_id)
        try:
            context = build_coach_context(
                manager=manager,
                account=account,
                control=control,
                mode=mode,
                get_live_context=get_live_context,
            )
            answer = default_coach.chat(body, chat_id, context=context, intent=intent)
            ok = reply_with_menu(answer, chat_id)
            if not ok:
                # Absolute last resort — no HTML, no keyboard
                send_telegram(
                    "Coach reply failed to send. Try typing your question again, "
                    "or tap Status / Menu.",
                    cfg=cfg,
                    chat_id=chat_id,
                )
        except Exception as exc:
            logger.exception("cmd_chat failed")
            reply_with_menu(
                f"⚠️ Coach hit an error: {exc}\nTap <b>Ask coach</b> to retry.",
                chat_id,
            )

    listener.chat_handler = cmd_chat

    def cmd_status(_args: list[str], chat_id: str) -> None:
        snap = journal.performance_snapshot(
            equity=account.equity,
            balance=account.balance,
            initial_balance=session_start_equity or cfg.initial_balance,
        )
        msg = format_status_card(
            mode=mode,
            equity=account.equity,
            balance=account.balance,
            snap=snap,
            control=control,
            open_count=manager.open_count(),
            max_open=MULTI.max_open_positions,
        )
        reply_with_menu(msg, chat_id)

    def cmd_positions(_args: list[str], chat_id: str) -> None:
        ctx = get_live_context()
        rows = ctx.get("rows") or {}
        positions: list[dict[str, Any]] = []
        for sym in manager.open_symbols():
            eng = manager.engine(sym)
            price = float(rows[sym]["close"]) if sym in rows else eng.position.entry_price
            if eng.position.side == "buy":
                unreal = (
                    (price - eng.position.entry_price)
                    * eng.position.volume
                    * eng.contract_size
                )
            else:
                unreal = (
                    (eng.position.entry_price - price)
                    * eng.position.volume
                    * eng.contract_size
                )
            positions.append(
                {
                    "symbol": sym,
                    "side": eng.position.side,
                    "entry": eng.position.entry_price,
                    "price": price,
                    "unrealized": unreal,
                    "stop_loss": eng.position.stop_loss,
                    "take_profit": eng.position.take_profit,
                    "ticket": eng.position.ticket,
                }
            )

        external_rows: list[dict[str, Any]] = []
        if not paper:
            _bot_mt5, external_mt5 = split_broker_positions(paper=paper)
            managed_tickets = {
                int(p.get("ticket") or 0)
                for p in positions
                if p.get("ticket")
            }
            managed_syms = {str(p["symbol"]) for p in positions}
            for row in broker_position_rows(external_mt5, rows=rows):
                # Hide tickets already shown as bot-managed (adopted in-memory)
                if int(row.get("ticket") or 0) in managed_tickets:
                    continue
                if str(row.get("symbol")) in managed_syms:
                    continue
                external_rows.append(row)

        manage_kb = positions_manage_keyboard(
            bot_symbols=[str(p["symbol"]) for p in positions],
            external=external_rows,
        )
        reply_with_menu(
            format_positions_card(positions, external=external_rows),
            chat_id,
            inline=manage_kb
            or positions_close_keyboard([str(p["symbol"]) for p in positions])
            or main_inline_keyboard(paused=control.paused),
        )

    def cmd_adopt(args: list[str], chat_id: str) -> None:
        if paper:
            reply_with_menu("Adopt is only available in LIVE mode.", chat_id)
            return
        if not args:
            reply_with_menu(
                "Usage: tap <b>Adopt</b> on /positions, or /adopt TICKET",
                chat_id,
            )
            return
        try:
            ticket = int(str(args[0]).strip())
        except ValueError:
            reply_with_menu(f"Invalid ticket <code>{args[0]}</code>.", chat_id)
            return
        if mt5 is None:
            reply_with_menu("MT5 not available.", chat_id)
            return
        try:
            poses = list(mt5.positions_get() or ())
        except Exception as exc:  # noqa: BLE001
            reply_with_menu(f"MT5 error: {exc}", chat_id)
            return
        pos = next((p for p in poses if int(p.ticket) == ticket), None)
        if pos is None:
            reply_with_menu(f"No broker position with ticket <code>{ticket}</code>.", chat_id)
            return
        watch = set(MULTI.symbols)
        result = stamp_mt5_position(
            pos,
            manager=manager,
            rocket=rocket,
            journal=journal,
            journal_ids=journal_ids,
            watchlist=watch,
        )
        if result.get("status") != "ok":
            reply_with_menu(
                f"❌ Adopt failed: {result.get('reason')}",
                chat_id,
            )
            return
        remember_adopted_ticket(ticket)
        sym = str(result.get("symbol"))
        reply_with_menu(
            f"✅ Adopted <b>{sym}</b> ticket <code>{ticket}</code>\n"
            f"{str(result.get('side')).upper()} · "
            f"vol {float(result.get('volume') or 0):.2f}\n"
            "Bot will manage SL / trail / close.",
            chat_id,
        )
        logger.info(
            "Adopted external ticket=%s %s %s",
            ticket,
            result.get("side"),
            sym,
        )

    def cmd_scan(_args: list[str], chat_id: str) -> None:
        ctx = get_live_context()
        opps = ctx.get("opps") or []
        reply_with_menu(format_scan_card(opps), chat_id)

    def cmd_pause(_args: list[str], chat_id: str) -> None:
        control.pause("telegram /pause")
        reply_with_menu(
            "⏸ New entries paused. Exits still managed.\nTap <b>▶ Resume</b> to continue.",
            chat_id,
        )

    def cmd_resume(_args: list[str], chat_id: str) -> None:
        control.resume()
        reply_with_menu("▶ Entries resumed. Analyzing → entering normally.", chat_id)

    def _match_symbol(query: str | None, *, prefer_open: bool = True) -> str | None:
        avail = list(manager.symbols) if hasattr(manager, "symbols") else list(MULTI.symbols)
        open_syms = list(manager.open_symbols())
        q = (query or "").strip().upper()
        if not q:
            if prefer_open and open_syms:
                return open_syms[0]
            return avail[0] if avail else "EURUSDm"
        # 1. Exact match
        for s in (open_syms if prefer_open else avail) + avail:
            if s.upper() == q:
                return s
        # 2. Suffix match (e.g. EURUSD -> EURUSDm)
        for s in (open_syms if prefer_open else avail) + avail:
            if s.upper().startswith(q) or q.startswith(s.upper()):
                return s
        # 3. Nickname alias match
        aliases = {
            "GOLD": "XAUUSD",
            "SILVER": "XAGUSD",
            "BITCOIN": "BTCUSD",
            "BTC": "BTCUSD",
            "ETHEREUM": "ETHUSD",
            "ETH": "ETHUSD",
            "SOLANA": "SOLUSD",
            "SOL": "SOLUSD",
            "FIB": "EURUSD",
            "GU": "GBPUSD",
            "EU": "EURUSD",
            "UJ": "USDJPY",
        }
        for alias_k, alias_v in aliases.items():
            if alias_k in q:
                for s in (open_syms if prefer_open else avail) + avail:
                    if alias_v in s.upper():
                        return s
        # 4. Substring match
        for s in (open_syms if prefer_open else avail) + avail:
            if q in s.upper() or s.upper() in q:
                return s
        return None

    def cmd_close(args: list[str], chat_id: str) -> None:
        if not args:
            reply_with_menu("Usage: /close SYMBOL (e.g. /close EURUSDm)", chat_id)
            return
        match = _match_symbol(args[0], prefer_open=True)
        if match is None or match not in manager.open_symbols():
            reply_with_menu(f"No open position matching <code>{args[0]}</code>.", chat_id)
            return
        ctx = get_live_context()
        result = close_position_now(
            match,
            "Telegram /close",
            manager=manager,
            rocket=rocket,
            account=account,
            journal=journal,
            journal_ids=journal_ids,
            loss_guard=loss_guard,
            frames=ctx.get("frames"),
            rows=ctx.get("rows"),
            paper=paper,
            cfg=cfg,
            notify=True,
        )
        if result.get("status") == "closed":
            reply_with_menu(
                f"✅ Closed <b>{match}</b> · PnL {fmt_money(float(result.get('pnl') or 0), signed=True)}",
                chat_id,
            )
        else:
            reply_with_menu(
                f"Could not close {match}: {result.get('status')} {result.get('reason')}",
                chat_id,
            )

    def cmd_closeall(_args: list[str], chat_id: str) -> None:
        open_syms = list(manager.open_symbols())
        if not open_syms:
            reply_with_menu("📭 No open positions to close.", chat_id)
            return
        ctx = get_live_context()
        closed = 0
        for sym in open_syms:
            result = close_position_now(
                sym,
                "Telegram /closeall",
                manager=manager,
                rocket=rocket,
                account=account,
                journal=journal,
                journal_ids=journal_ids,
                loss_guard=loss_guard,
                frames=ctx.get("frames"),
                rows=ctx.get("rows"),
                paper=paper,
                cfg=cfg,
                notify=True,
            )
            if result.get("status") == "closed":
                closed += 1
        reply_with_menu(f"✅ Closed {closed}/{len(open_syms)} positions.", chat_id)

    def cmd_setrisk(args: list[str], chat_id: str) -> None:
        if not args:
            cur = control.risk_money_override
            reply_with_menu(
                f"Current risk $ override: "
                f"{'$' + format(cur, ',.0f') if cur else 'config default'}\n"
                "Tap a preset button or type <code>/setrisk 250</code>",
                chat_id,
            )
            return
        raw = args[0].strip().lower()
        if raw in {"clear", "reset", "default", "0"}:
            control.set_risk_money(None)
            manager.set_risk_money_override(None)
            reply_with_menu("Risk $ override cleared — using config MAX_RISK_MONEY.", chat_id)
            return
        try:
            amount = float(raw.replace(",", "").replace("$", ""))
        except ValueError:
            reply_with_menu("Usage: /setrisk 250", chat_id)
            return
        if amount <= 0:
            reply_with_menu("Risk must be positive (or use /setrisk clear).", chat_id)
            return
        control.set_risk_money(amount)
        manager.set_risk_money_override(amount)
        reply_with_menu(f"💸 Max risk per trade set to <b>${amount:,.0f}</b>", chat_id)

    def cmd_report(_args: list[str], chat_id: str) -> None:
        snap = journal.performance_snapshot(
            equity=account.equity,
            balance=account.balance,
            initial_balance=session_start_equity or cfg.initial_balance,
        )
        analytics = journal.detailed_analytics()
        pf_s = f"{analytics.get('profit_factor', 0.0):.2f}"
        exp_s = f"${analytics.get('expectancy', 0.0):+,.2f}"
        sharpe_s = f"{analytics.get('sharpe', 0.0):.2f}"
        text = (
            "📈 <b>PERFORMANCE REPORT</b>\n\n"
            f"Equity · <b>${fmt_money(float(snap.get('equity') or 0))}</b>\n"
            f"Invested · ${fmt_money(float(snap.get('invested') or 0))}\n"
            f"Total PnL · {fmt_money(float(snap.get('total_pnl') or 0), signed=True)} "
            f"({float(snap.get('total_return_pct') or 0):+.2f}%)\n"
            f"Today · {fmt_money(float(snap.get('today_pnl') or 0), signed=True)}\n"
            f"Win rate · <b>{float(snap.get('win_rate') or 0):.0f}%</b> "
            f"({int(snap.get('wins') or 0)}W / {int(snap.get('losses') or 0)}L)\n"
            f"Profit Factor · <b>{pf_s}</b> · Exp · <b>{exp_s}</b>\n"
            f"Sharpe Ratio · <b>{sharpe_s}</b>\n"
            f"Closed · {int(snap.get('closed_trades') or 0)} · "
            f"Open · {int(snap.get('open_positions') or 0)}"
        )
        reply_with_menu(text, chat_id)
        if len(equity_history) >= 2:
            png = render_equity_chart(
                equity_history,
                title=f"{BRAND_SHORT} · Equity",
                invested=session_start_equity or cfg.initial_balance,
            )
            if png:
                send_telegram_photo(
                    f"📈 {BRAND_SHORT} equity curve",
                    png,
                    cfg=cfg,
                    chat_id=chat_id,
                )

    def cmd_journal(_args: list[str], chat_id: str) -> None:
        trades = journal.load_recent(days=30)
        reply_with_menu(format_journal_card(trades, limit=10), chat_id)

    def cmd_pnl(_args: list[str], chat_id: str) -> None:
        snap = journal.performance_snapshot(
            equity=account.equity,
            balance=account.balance,
            initial_balance=session_start_equity or cfg.initial_balance,
        )
        reply_with_menu(
            format_pnl_card(
                snap,
                mode=mode,
                open_count=manager.open_count(),
            ),
            chat_id,
        )

    def cmd_partial(args: list[str], chat_id: str) -> None:
        if not args:
            reply_with_menu("Usage: /partial SYMBOL [PCT] (e.g. /partial EURUSDm 50)", chat_id)
            return
        match = _match_symbol(args[0], prefer_open=True)
        if match is None or match not in manager.open_symbols():
            reply_with_menu(f"No open position matching <code>{args[0]}</code>.", chat_id)
            return

        pct_val = 50.0
        if len(args) > 1:
            try:
                pct_val = float(args[1].replace("%", ""))
            except ValueError:
                pct_val = 50.0
        pct_val = max(10.0, min(90.0, pct_val))
        pct_fraction = pct_val / 100.0

        eng = manager.engine(match)
        closed_vol = eng.partial_close(pct_fraction)
        if closed_vol <= 0:
            reply_with_menu(f"Could not partially close <b>{match}</b> (volume too small).", chat_id)
            return

        if not paper:
            try:
                order_obj = TradeOrder(
                    action="close",
                    volume=closed_vol,
                    stop_loss=eng.position.stop_loss,
                    take_profit=eng.position.take_profit,
                    approved=True,
                    reason=f"Telegram partial {int(pct_val)}%",
                )
                fill_res = send_mt5_order(
                    match,
                    order_obj,
                    paper=False,
                    cfg=cfg,
                    ticket=eng.position.ticket,
                )
                logger.info("MT5 partial close %s: %s", match, fill_res)
            except Exception as exc:  # noqa: BLE001
                logger.warning("MT5 partial close error %s: %s", match, exc)

        reply_with_menu(
            f"🎯 <b>Partial Close {int(pct_val)}% Executed</b>\n"
            f"Symbol · <b>{match}</b>\n"
            f"Closed Vol · <b>{closed_vol:.2f} lots</b>\n"
            f"Remaining · <b>{eng.position.volume:.2f} lots</b>",
            chat_id,
        )

    def cmd_be(args: list[str], chat_id: str) -> None:
        if not args:
            reply_with_menu("Usage: /be SYMBOL (e.g. /be EURUSDm)", chat_id)
            return
        match = _match_symbol(args[0], prefer_open=True)
        if match is None or match not in manager.open_symbols():
            reply_with_menu(f"No open position matching <code>{args[0]}</code>.", chat_id)
            return

        eng = manager.engine(match)
        prev_sl = float(eng.position.stop_loss or 0.0)
        be_ok = eng.move_sl_to_be(spread_pips_buffer=1.0)
        new_sl = float(eng.position.stop_loss or 0.0)
        if not be_ok or abs(new_sl - prev_sl) < 1e-12:
            reply_with_menu(f"SL is already at or past Break-Even on <b>{match}</b>.", chat_id)
            return

        if not paper:
            modify_mt5_sltp(
                match,
                stop_loss=new_sl,
                take_profit=eng.position.take_profit,
                ticket=eng.position.ticket,
                paper=False,
            )

        reply_with_menu(
            f"🛡️ <b>Stop Loss Locked to Break-Even</b>\n"
            f"Symbol · <b>{match}</b>\n"
            f"Entry · <code>{fmt_price(match, eng.position.entry_price)}</code>\n"
            f"New SL · <code>{fmt_price(match, new_sl)}</code> (+1 pip buffer)",
            chat_id,
        )

    def cmd_be_all(_args: list[str], chat_id: str) -> None:
        open_syms = list(manager.open_symbols())
        if not open_syms:
            reply_with_menu("📭 No open positions to protect.", chat_id)
            return
        locked = 0
        for sym in open_syms:
            eng = manager.engine(sym)
            prev_sl = float(eng.position.stop_loss or 0.0)
            if eng.move_sl_to_be(spread_pips_buffer=1.0):
                new_sl = float(eng.position.stop_loss or 0.0)
                if abs(new_sl - prev_sl) > 1e-12:
                    if not paper:
                        modify_mt5_sltp(
                            sym,
                            stop_loss=new_sl,
                            take_profit=eng.position.take_profit,
                            ticket=eng.position.ticket,
                            paper=False,
                        )
                    locked += 1
        reply_with_menu(f"🛡️ Protected {locked}/{len(open_syms)} positions with Break-Even SL.", chat_id)

    def cmd_chart(args: list[str], chat_id: str) -> None:
        raw_q = args[0].strip() if args else ""
        sym = _match_symbol(raw_q, prefer_open=True) or "EURUSDm"
        ctx = get_live_context()
        frames_dict = ctx.get("frames") or {}
        df_chart = frames_dict.get(sym, (None, None))[0]
        if df_chart is None or df_chart.empty:
            df_chart, _ = _load_symbol_frame(sym, synthetic=paper, seed=1)
        if df_chart is None or df_chart.empty:
            reply_with_menu(f"⚠️ Could not load chart data for <code>{sym}</code>.", chat_id)
            return

        eng = manager.engine(sym)
        entry_px = float(eng.position.entry_price) if eng.position.side != "flat" else None
        sl_px = float(eng.position.stop_loss) if eng.position.side != "flat" else None
        tp_px = float(eng.position.take_profit) if eng.position.side != "flat" else None
        side_txt = eng.position.side if eng.position.side != "flat" else None

        png = render_trade_chart(
            df_chart,
            symbol=sym,
            side=side_txt,
            entry=entry_px,
            stop_loss=sl_px,
            take_profit=tp_px,
            title_suffix="LIVE",
        )
        if png:
            send_telegram_photo(f"📊 <b>{sym}</b> · Live M15 Chart", png, cfg=cfg, chat_id=chat_id)
        else:
            reply_with_menu(f"⚠️ Chart render failed for <code>{sym}</code>.", chat_id)

    def cmd_digest(_args: list[str], chat_id: str) -> None:
        snap = journal.performance_snapshot(
            equity=account.equity,
            balance=account.balance,
            initial_balance=session_start_equity or cfg.initial_balance,
        )
        card = format_daily_digest(
            equity=account.equity,
            today_pnl=float(snap.get("today_pnl") or 0.0),
            win_rate=float(snap.get("win_rate") or 0.0),
            wins=int(snap.get("wins") or 0),
            losses=int(snap.get("losses") or 0),
            total_trades=int(snap.get("closed_trades") or 0),
            mode=mode,
        )
        reply_with_menu(card, chat_id)

    listener.register("status", cmd_status)
    listener.register("positions", cmd_positions)
    listener.register("scan", cmd_scan)
    listener.register("pause", cmd_pause)
    listener.register("resume", cmd_resume)
    listener.register("close", cmd_close)
    listener.register("closeall", cmd_closeall)
    listener.register("setrisk", cmd_setrisk)
    listener.register("report", cmd_report)
    listener.register("journal", cmd_journal)
    listener.register("pnl", cmd_pnl)
    listener.register("adopt", cmd_adopt)
    listener.register("partial", cmd_partial)
    listener.register("be", cmd_be)
    listener.register("be_all", cmd_be_all)
    listener.register("chart", cmd_chart)
    listener.register("digest", cmd_digest)
    return listener


# ---------------------------------------------------------------------------
# Live multi-pair loop
# ---------------------------------------------------------------------------


def hydrate_open_positions(
    *,
    manager: MultiSymbolRiskManager,
    rocket: RocketExitManager,
    journal: TradeJournal,
    journal_ids: dict[str, int],
    paper: bool,
) -> int:
    """
    Rebuild in-memory engines from MT5 bot positions after restart.

    Restores magic==BOT_MAGIC and explicitly adopted tickets.
    """
    if paper or mt5 is None:
        return 0
    try:
        positions = mt5.positions_get() or ()
    except Exception as exc:  # noqa: BLE001
        logger.warning("hydrate positions_get failed: %s", exc)
        return 0

    adopted = _load_adopted_tickets()
    restored = 0
    still_open_adopted: set[int] = set()
    for pos in positions:
        ticket = int(pos.ticket)
        magic = int(getattr(pos, "magic", 0) or 0)
        if magic == BOT_MAGIC or ticket in adopted:
            if ticket in adopted:
                still_open_adopted.add(ticket)
        else:
            continue
        result = stamp_mt5_position(
            pos,
            manager=manager,
            rocket=rocket,
            journal=journal,
            journal_ids=journal_ids,
            watchlist=None,
        )
        if result.get("status") == "ok":
            restored += 1
            logger.info(
                "Hydrated %s %s ticket=%s vol=%.2f entry=%.5f SL=%.5f",
                result.get("side"),
                result.get("symbol"),
                result.get("ticket"),
                float(result.get("volume") or 0),
                float(result.get("entry") or 0),
                float(getattr(pos, "sl", 0) or 0),
            )
        elif result.get("status") == "rejected" and "already managed" not in str(
            result.get("reason") or ""
        ):
            logger.warning("hydrate skip: %s", result.get("reason"))

    # Drop adopted tickets that are no longer open at the broker
    if adopted != still_open_adopted:
        _persist_adopted_tickets(still_open_adopted)

    if restored:
        logger.info("Hydrated %s open bot position(s) from MT5", restored)
    return restored


def run_live_loop(*, synthetic: bool = False, max_iterations: int | None = None) -> None:
    """
    Multi-pair loop: manage exits → scan opportunities → enter best slot → learn.
    """
    cfg = BOT
    paper = cfg.paper_trading or synthetic
    if synthetic and not cfg.paper_trading:
        logger.warning("Synthetic mode forces PAPER fills (PAPER_TRADING was false).")

    configured_symbols = MULTI.symbols
    symbols: tuple[str, ...] = configured_symbols
    macro = default_macro_filter
    journal = default_journal
    rocket = RocketExitManager()
    loss_guard = default_loss_guard
    account = PaperAccount(balance=cfg.initial_balance, equity=cfg.initial_balance)
    session_start_equity = float(cfg.initial_balance)
    journal_ids: dict[str, int] = {}
    equity_history: list[float] = []

    expected_dim = None
    policy = load_policy(cfg.model_path, expected_obs_dim=None)

    if not synthetic:
        try:
            if not _reconnect_mt5(cfg, retries=3, delay=3.0):
                raise MT5ConnectionError("MT5 initialize failed after retries")
            symbols = filter_watchlist_symbols(configured_symbols)
            if not symbols:
                raise MT5ConnectionError(
                    "No watchlist symbols resolved on this broker. "
                    "Check SYMBOLS in .env against Market Watch."
                )
            if len(symbols) < len(configured_symbols):
                logger.warning(
                    "Active watchlist %s/%s symbols (others skipped)",
                    len(symbols),
                    len(configured_symbols),
                )
            if not paper and account.sync_from_mt5():
                session_start_equity = float(account.equity)
                logger.info(
                    "MT5 account synced login=%s balance=%.2f equity=%.2f %s",
                    account.login,
                    account.balance,
                    account.equity,
                    account.currency,
                )
            else:
                logger.warning(
                    "Using local balance %.2f (paper=%s sync=%s)",
                    account.balance,
                    paper,
                    account.live_sync,
                )
        except MT5ConnectionError as exc:
            if not paper:
                # Live mode must not silently degrade to synthetic/paper
                logger.error("MT5 connection failed in LIVE mode: %s — aborting.", exc)
                try:
                    send_telegram(
                        f"⚠️ <b>LIVE bot aborted</b>\nMT5 connect failed: {exc}\n"
                        "Watchdog should restart. Check MT5 AutoTrading + login."
                    )
                except Exception as tg_exc:  # noqa: BLE001
                    logger.debug("Failed sending abort telegram: %s", tg_exc)
                raise SystemExit(2) from exc
            logger.error("MT5 connection failed: %s — falling back to synthetic.", exc)
            synthetic = True
            paper = True
            symbols = configured_symbols

    manager = MultiSymbolRiskManager(
        symbols=symbols,
        max_open_positions=MULTI.max_open_positions,
        macro_filter=macro,
    )
    if not synthetic and mt5 is not None:
        for sym in symbols:
            _apply_mt5_symbol_hints(manager, sym)
        if not paper:
            hydrate_open_positions(
                manager=manager,
                rocket=rocket,
                journal=journal,
                journal_ids=journal_ids,
                paper=paper,
            )
        live_tickets: set[int] = set()
        for eng in manager.engines.values():
            if eng.position.side != "flat" and eng.position.ticket:
                live_tickets.add(int(eng.position.ticket))
        live_syms = {s.upper() for s in manager.open_symbols()}
        n_orph = journal.orphan_stale_opens(
            live_tickets=live_tickets,
            live_symbols=live_syms,
        )
        if n_orph:
            logger.info("Reconciled %s orphaned journal open row(s)", n_orph)
    else:
        # Synthetic: drop ghost opens that aren't managed in-memory
        n_orph = journal.orphan_stale_opens(
            live_tickets=set(),
            live_symbols={s.upper() for s in manager.open_symbols()},
        )
        if n_orph:
            logger.info("Reconciled %s orphaned journal open row(s) (synthetic)", n_orph)

    loss_guard.on_session_start(account.equity)
    equity_history = [float(account.equity)]

    mode = "PAPER" if paper else "LIVE"
    sym_list = ", ".join(symbols)

    # Shared live context for Telegram commands (updated each cycle)
    live_ctx: dict[str, Any] = {"frames": {}, "rows": {}, "opps": []}
    warned_news_events: set[str] = set()

    def get_live_context() -> dict[str, Any]:
        return live_ctx

    runtime_control = RuntimeControl()
    cmd_listener = _build_command_listener(
        control=runtime_control,
        manager=manager,
        rocket=rocket,
        account=account,
        journal=journal,
        journal_ids=journal_ids,
        loss_guard=loss_guard,
        cfg=cfg,
        mode=mode,
        paper=paper,
        equity_history=equity_history,
        session_start_equity=session_start_equity,
        get_live_context=get_live_context,
    )
    cmd_listener.start()
    if runtime_control.risk_money_override is not None:
        manager.set_risk_money_override(runtime_control.risk_money_override)
        logger.info(
            "Restored risk $ override=%.0f from desk settings",
            runtime_control.risk_money_override,
        )
    if runtime_control.paused:
        logger.info(
            "Restored pause from desk settings (%s)",
            runtime_control.pause_reason or "manual",
        )

    start_msg = format_startup(mode=mode, symbols=symbols, equity=account.equity)
    if account.live_sync:
        start_msg += (
            f"\nMT5 · login <code>{account.login}</code> · "
            f"bal <b>${account.balance:,.2f}</b> · {account.currency}"
        )
    if len(symbols) < len(configured_symbols):
        skipped_n = len(configured_symbols) - len(symbols)
        start_msg += f"\nWatchlist · {len(symbols)} active · {skipped_n} skipped (broker)"
    if runtime_control.paused:
        start_msg += f"\n⏸ <b>PAUSED</b> · {runtime_control.pause_reason or 'manual'}"
    # One startup composition: pin keyboard + status, then compact inline card
    send_telegram(start_msg, reply_markup=main_reply_keyboard())
    send_telegram(
        "📱 <b>Command center</b> — tap a button or type /help",
        reply_markup=main_inline_keyboard(paused=runtime_control.paused),
    )
    logger.info("Starting %s multi-pair loop on [%s] (synthetic=%s)", mode, sym_list, synthetic)
    logger.info(
        "Gemini advisor: enabled=%s available=%s model=%s fast_skip=%.2f",
        GEMINI.enabled,
        default_advisor.available,
        GEMINI.model,
        QUALITY.fast_entry_score,
    )

    iterations = 0
    last_learn_day: datetime | None = None
    # Start cooldown so first pulse is after DESK_PULSE_HOURS (startup already covers now)
    last_desk_pulse: datetime | None = datetime.now(timezone.utc)
    last_heartbeat: datetime | None = None
    last_entry_skip_reason: str = ""
    mt5_down_alerted = False
    try:
        while max_iterations is None or iterations < max_iterations:
            iterations += 1
            now = datetime.now(timezone.utc)
            try:
                # Mid-loop MT5 health / reconnect (live only)
                if not paper and not synthetic:
                    if not _mt5_connected():
                        if not mt5_down_alerted:
                            send_telegram(
                                "⚠️ <b>MT5 disconnected</b> — pausing entries, "
                                "attempting reconnect…"
                            )
                            mt5_down_alerted = True
                        if _reconnect_mt5(cfg, retries=2, delay=5.0):
                            send_telegram("✅ <b>MT5 reconnected</b> — trading resumes.")
                            mt5_down_alerted = False
                            for sym in symbols:
                                _apply_mt5_symbol_hints(manager, sym)
                        else:
                            logger.warning("MT5 still down — skipping this cycle")
                            time.sleep(cfg.poll_seconds)
                            continue
                    elif mt5_down_alerted:
                        mt5_down_alerted = False
                        send_telegram("✅ <b>MT5 connection restored</b>")

                if last_heartbeat is None or (now - last_heartbeat).total_seconds() >= 300:
                    mt5_ok = False
                    open_bot = -1
                    open_external = -1
                    try:
                        if mt5 is not None and not paper:
                            mt5_ok = bool(mt5.terminal_info())
                            bot_poses, ext_poses = split_broker_positions(paper=False)
                            open_bot = len(bot_poses)
                            open_external = len(ext_poses)
                    except Exception:  # noqa: BLE001
                        mt5_ok = False
                    logger.info(
                        "Heartbeat | mode=%s open_local=%s open_bot=%s open_external=%s mt5=%s equity=%.2f iter=%s",
                        mode,
                        manager.open_count(),
                        open_bot,
                        open_external,
                        mt5_ok,
                        account.equity,
                        iterations,
                    )
                    last_heartbeat = now

                frames: dict[str, tuple[pd.DataFrame, list[str]]] = {}
                rows: dict[str, Any] = {}
                for sym in symbols:
                    df, feature_cols = _load_symbol_frame(sym, synthetic=synthetic, seed=iterations)
                    frames[sym] = (df, feature_cols)
                    rows[sym] = df.iloc[-1]
                live_ctx["frames"] = frames
                live_ctx["rows"] = rows

                # Equity: MT5 is source of truth in live mode
                total_unrealized = 0.0
                if account.live_sync and not paper:
                    account.sync_from_mt5()
                else:
                    for sym in manager.open_symbols():
                        eng = manager.engine(sym)
                        price = float(rows[sym]["close"])
                        if eng.position.side == "buy":
                            total_unrealized += (
                                (price - eng.position.entry_price)
                                * eng.position.volume
                                * eng.contract_size
                            )
                        elif eng.position.side == "sell":
                            total_unrealized += (
                                (eng.position.entry_price - price)
                                * eng.position.volume
                                * eng.contract_size
                            )
                    account.equity = account.balance + total_unrealized
                _halted, newly_halted = manager.sync_circuit_breaker(account.equity, now)
                if newly_halted and RISK.circuit_breaker_flatten:
                    open_now = list(manager.open_symbols())
                    send_telegram(
                        "🛑 <b>Circuit breaker</b> — daily loss limit hit.\n"
                        f"Flattening {len(open_now)} open position(s). "
                        f"No new entries until {manager.halt_until}."
                    )
                    for sym in open_now:
                        close_position_now(
                            sym,
                            "Circuit breaker daily loss halt",
                            manager=manager,
                            rocket=rocket,
                            account=account,
                            journal=journal,
                            journal_ids=journal_ids,
                            loss_guard=loss_guard,
                            frames=frames,
                            rows=rows,
                            paper=paper,
                            cfg=cfg,
                            total_unrealized=total_unrealized,
                            notify=True,
                        )
                elif newly_halted:
                    send_telegram(
                        "🛑 <b>Circuit breaker</b> — daily loss limit hit.\n"
                        f"New entries paused until {manager.halt_until}."
                    )
                loss_guard.update_equity(account.equity)
                equity_history.append(float(account.equity))
                if len(equity_history) > 500:
                    del equity_history[:-400]
                chan = maybe_post_channel_update(
                    equity=account.equity,
                    balance=account.balance,
                    mode=mode,
                    equity_history=equity_history,
                    journal=journal,
                    notify_text=send_channel_text,
                    notify_photo=send_channel_photo,
                    invested_baseline=session_start_equity,
                )
                if chan.get("posted"):
                    logger.info("Channel progress posted (%s)", chan.get("event"))
                elif chan.get("reason") not in {"cooldown", "no_channel_id", "disabled"}:
                    logger.debug("Channel update skipped: %s", chan.get("reason"))

                # ---- 1) Manage open positions: SL/TP, rocket, loss cut ----
                for sym in list(manager.open_symbols()):
                    eng = manager.engine(sym)
                    row = rows[sym]
                    price = float(row["close"])
                    atr = float(row["atr_14"])
                    high = float(row["high"])
                    low = float(row["low"])
                    bar_time = now
                    try:
                        df_sym = frames[sym][0]
                        ts = df_sym.index[-1]
                        bar_time = pd.Timestamp(ts).to_pydatetime()
                        if bar_time.tzinfo is None:
                            bar_time = bar_time.replace(tzinfo=timezone.utc)
                    except Exception:  # noqa: BLE001
                        bar_time = now

                    prev_sl = float(eng.position.stop_loss or 0.0)
                    prev_tp = float(eng.position.take_profit or 0.0)
                    eng.update_stops(price, atr)
                    decision = rocket.evaluate(
                        sym,
                        eng.position,
                        price=price,
                        atr=atr,
                        contract_size=eng.contract_size,
                        now=now,
                        bar_time=bar_time,
                    )
                    if decision.action == "tighten_sl" and decision.new_stop_loss is not None:
                        if eng.position.side == "buy":
                            eng.position.stop_loss = max(eng.position.stop_loss, decision.new_stop_loss)
                        else:
                            if eng.position.stop_loss == 0:
                                eng.position.stop_loss = decision.new_stop_loss
                            else:
                                eng.position.stop_loss = min(eng.position.stop_loss, decision.new_stop_loss)
                        logger.info("%s %s SL→%.5f", sym, decision.reason, eng.position.stop_loss)

                    new_sl = float(eng.position.stop_loss or 0.0)
                    if (
                        not paper
                        and new_sl > 0
                        and abs(new_sl - prev_sl) > 1e-12
                    ):
                        modify_mt5_sltp(
                            sym,
                            stop_loss=new_sl,
                            take_profit=prev_tp or eng.position.take_profit,
                            ticket=eng.position.ticket,
                            paper=False,
                        )

                    hit = eng.stop_hit(high, low)
                    should_close = decision.action == "close" or hit != "none"
                    close_reason = decision.reason if decision.action == "close" else f"Protective {hit.upper()}"

                    if should_close:
                        close_position_now(
                            sym,
                            close_reason,
                            manager=manager,
                            rocket=rocket,
                            account=account,
                            journal=journal,
                            journal_ids=journal_ids,
                            loss_guard=loss_guard,
                            frames=frames,
                            rows=rows,
                            paper=paper,
                            cfg=cfg,
                            total_unrealized=total_unrealized,
                            notify=True,
                        )

                # Refresh open set after exits
                open_syms = set(manager.open_symbols())

                # ---- 2) Build obs + policy hints, scan opportunities ----
                policy_actions: dict[str, int] = {}
                feature_cols_ref: list[str] | None = None
                df_ref: pd.DataFrame | None = None
                for sym, (df, feature_cols) in frames.items():
                    feature_cols_ref = feature_cols
                    df_ref = df
                    row = rows[sym]
                    eng = manager.engine(sym)
                    macro_feats = macro.macro_features(now, sym)
                    macro_vec = [
                        macro_feats[k]
                        for k in (
                            "in_blackout",
                            "mins_to_event_scaled",
                            "in_london",
                            "in_ny",
                            "in_overlap",
                        )
                    ]
                    features = row[feature_cols].to_numpy(dtype=np.float32)
                    position_flag = (
                        1.0 if eng.position.side == "buy"
                        else (-1.0 if eng.position.side == "sell" else 0.0)
                    )
                    price = float(row["close"])
                    unrealized = 0.0
                    if eng.position.side == "buy":
                        unrealized = (
                            (price - eng.position.entry_price)
                            * eng.position.volume
                            * eng.contract_size
                        )
                    elif eng.position.side == "sell":
                        unrealized = (
                            (eng.position.entry_price - price)
                            * eng.position.volume
                            * eng.contract_size
                        )
                    unrealized_norm = float(
                        np.clip(unrealized / max(cfg.initial_balance, 1.0), -5, 5)
                    )
                    obs = build_live_observation(
                        features, position_flag, unrealized_norm, macro_vec
                    )
                    if expected_dim is None:
                        expected_dim = int(obs.shape[0])
                        policy = load_policy(cfg.model_path, expected_obs_dim=expected_dim)
                    raw_action, _ = policy.predict(obs, deterministic=True)
                    policy_actions[sym] = int(raw_action)

                opps = scan_opportunities(
                    rows, policy_actions=policy_actions, macro=macro, now=now
                )
                if opps:
                    top = opps[0]
                    logger.info(
                        "Monitor cycle#%s | top=%s %s score=%.2f | open=%s/%s | equity=%.2f | poll=%ss",
                        iterations,
                        top.side.upper(),
                        top.symbol,
                        top.score,
                        manager.open_count(),
                        MULTI.max_open_positions,
                        account.equity,
                        cfg.poll_seconds,
                    )
                if not runtime_control.mute_opportunity_alerts:
                    opp_kb = opportunity_alert_keyboard()

                    def _notify_opp(
                        msg: str, _kb: dict[str, Any] = opp_kb, **_kw: Any
                    ) -> bool:
                        return send_telegram(msg, reply_markup=_kb)

                    def _notify_opp_photo(
                        cap: str, img: bytes, _kb: dict[str, Any] = opp_kb, **_kw: Any
                    ) -> bool:
                        return send_telegram_photo(cap, img, reply_markup=_kb)

                    alert_opportunities(
                        opps,
                        notify=_notify_opp,
                        notify_photo=_notify_opp_photo,
                        frames=frames,
                    )
                else:
                    alert_opportunities(opps, notify=None, frames=frames)

                if not runtime_control.mute_channel_preds:
                    chan_kb = channel_deep_link_keyboard(cfg.telegram_bot_username)

                    def _chan_text(msg: str, _kb: dict[str, Any] = chan_kb) -> bool:
                        return send_channel_text(msg, reply_markup=_kb)

                    def _chan_photo(cap: str, img: bytes, _kb: dict[str, Any] = chan_kb) -> bool:
                        return send_channel_photo(cap, img, reply_markup=_kb)

                    pred = maybe_post_channel_prediction(
                        opps,
                        frames=frames,
                        notify_text=_chan_text,
                        notify_photo=_chan_photo,
                    )
                    if pred.get("posted"):
                        logger.info(
                            "Channel free prediction %s %s score=%.2f",
                            str(pred.get("side", "")).upper(),
                            pred.get("symbol"),
                            float(pred.get("score") or 0),
                        )
                live_ctx["opps"] = opps

                # Pre-news ambient warning check for open positions
                active_open = list(manager.open_symbols())
                if active_open:
                    for sym_c in active_open:
                        _, _, near_mins = macro.blackout_status(now, sym_c)
                        if 0 < near_mins <= 20:
                            for ev in macro.high_impact_events(now, sym_c):
                                d_mins = (ev.when - now).total_seconds() / 60.0
                                if 0 < d_mins <= 20 and ev.title not in warned_news_events:
                                    warned_news_events.add(ev.title)
                                    warn_card = format_news_warning_card(
                                        event_title=ev.title,
                                        currency=ev.country,
                                        minutes_until=round(d_mins),
                                        open_positions_count=len(active_open),
                                    )
                                    send_telegram(warn_card, reply_markup=news_warning_keyboard())

                # Why flat? (score vs min) — used by console + desk pulse
                entry_skip_reason = ""
                if not runtime_control.paused and manager.has_capacity():
                    ranked = [
                        o
                        for o in opps
                        if getattr(o, "side", None) in {"buy", "sell"}
                        and o.symbol not in open_syms
                    ]
                    eligible = [
                        o
                        for o in ranked
                        if getattr(o, "allowed", False)
                        and float(o.score or 0) >= float(QUALITY.min_entry_score)
                    ]
                    if eligible:
                        entry_skip_reason = ""
                    elif not ranked:
                        entry_skip_reason = "No directional setups scored"
                    else:
                        top = max(ranked, key=lambda o: float(o.score or 0))
                        top_sc = float(top.score or 0)
                        if top_sc < float(QUALITY.min_entry_score):
                            entry_skip_reason = (
                                f"top={top.symbol} {str(top.side).upper()} "
                                f"{top_sc:.2f} < min {QUALITY.min_entry_score:.2f} "
                                f"(hot {QUALITY.hot_grab_score:.2f})"
                            )
                        else:
                            gate = getattr(top, "gate_reason", None) or "quality gate"
                            entry_skip_reason = (
                                f"top={top.symbol} {str(top.side).upper()} "
                                f"{top_sc:.2f} blocked ({gate})"
                            )
                    if entry_skip_reason:
                        last_entry_skip_reason = entry_skip_reason

                # Hourly private Desk Pulse (quiet monitoring)
                pulse_hours = float(getattr(MULTI, "desk_pulse_hours", 1.0))
                need_pulse = last_desk_pulse is None or (
                    (now - last_desk_pulse).total_seconds() >= pulse_hours * 3600.0
                )
                if need_pulse and not runtime_control.mute_pulse:
                    snap = journal.performance_snapshot(
                        equity=account.equity,
                        balance=account.balance,
                        initial_balance=session_start_equity or cfg.initial_balance,
                    )
                    top_sym = ""
                    top_sc: float | None = None
                    if opps:
                        top_sym = opps[0].symbol
                        top_sc = float(opps[0].score)
                    streak = ""
                    if loss_guard.consecutive_wins > 0:
                        streak = f"W{loss_guard.consecutive_wins}"
                    elif loss_guard.consecutive_losses > 0:
                        streak = f"L{loss_guard.consecutive_losses}"
                    idle_note = ""
                    if manager.open_count() == 0 and last_entry_skip_reason:
                        idle_note = f"No entry · {last_entry_skip_reason}"
                    pulse = format_desk_pulse(
                        mode=mode,
                        equity=account.equity,
                        snap=snap,
                        open_count=manager.open_count(),
                        max_open=MULTI.max_open_positions,
                        streak=streak,
                        top_symbol=top_sym,
                        top_score=top_sc,
                        paused=runtime_control.paused,
                        idle_note=idle_note,
                    )
                    send_telegram(
                        pulse,
                        reply_markup=main_inline_keyboard(paused=runtime_control.paused),
                    )
                    last_desk_pulse = now
                    logger.info("Desk pulse posted")
                elif need_pulse and runtime_control.mute_pulse:
                    last_desk_pulse = now
                    logger.debug("Desk pulse muted — skipped")

                # ---- 3) Grab hot setups first, then fill remaining slots ----
                if runtime_control.paused:
                    logger.info(
                        "Entries paused (%s) — managing exits only | equity=%.2f",
                        runtime_control.pause_reason or "manual",
                        account.equity,
                    )
                elif manager.has_capacity():
                    slots = min(
                        MULTI.entries_per_cycle,
                        MULTI.max_open_positions - manager.open_count(),
                    )
                    # Prefer hot scanner setups (act, don't just alert)
                    hot = [
                        o
                        for o in opps
                        if o.allowed
                        and o.side in {"buy", "sell"}
                        and o.symbol not in open_syms
                        and o.score >= QUALITY.hot_grab_score
                    ]
                    rest = entry_candidates(opps, open_syms, limit=max(1, slots))
                    # Dedupe: hot first, then other candidates
                    seen_c: set[str] = set()
                    candidates: list = []
                    for o in hot + rest:
                        if o.symbol in seen_c:
                            continue
                        seen_c.add(o.symbol)
                        candidates.append(o)
                        if len(candidates) >= max(1, slots):
                            break
                    if not candidates:
                        reason = entry_skip_reason or "no setups cleared min score / gates"
                        logger.info(
                            "No entry | %s | open=%s/%s | equity=%.2f",
                            reason,
                            manager.open_count(),
                            MULTI.max_open_positions,
                            account.equity,
                        )
                    else:
                        last_entry_skip_reason = ""
                    for candidate in candidates:
                        if not manager.has_capacity():
                            break
                        sym = candidate.symbol
                        if sym in open_syms:
                            continue
                        blocked, cool_reason = reentry_blocked(sym, now=now)
                        if blocked:
                            logger.info("Skip %s — %s", sym, cool_reason)
                            continue
                        open_positions = [
                            {
                                "symbol": s,
                                "side": manager.engine(s).position.side,
                            }
                            for s in open_syms
                        ]
                        cluster_block, cluster_reason = currency_exposure_block(
                            sym,
                            candidate.side,
                            open_positions=open_positions,
                            score=float(candidate.score),
                            max_exposure=int(RISK.max_currency_exposure),
                        )
                        if cluster_block:
                            logger.info("Skip %s — %s", sym, cluster_reason)
                            continue
                        eng = manager.engine(sym)
                        row = rows[sym]
                        price = float(row["close"])
                        atr = float(row["atr_14"])
                        if not paper:
                            wide, spread_reason = spread_too_wide(sym, atr, cfg=cfg)
                            if wide:
                                logger.info("Skip %s — %s", sym, spread_reason)
                                continue
                        action = _side_to_action(candidate.side)
                        if action == TradingEnv.ACTION_HOLD:
                            action = policy_actions.get(sym, 0)

                        is_hot = candidate.score >= QUALITY.hot_grab_score
                        # LossGuard may size-down after loss streaks (never auto-pauses)
                        _, _, risk_scale = loss_guard.allow_new_entry(
                            equity=account.equity,
                            score=float(candidate.score),
                            open_count=manager.open_count(),
                            now=now,
                        )

                        if is_hot:
                            gate = evaluate_hot_grab(
                                sym,
                                row,
                                candidate.side if candidate.side in {"buy", "sell"} else "flat",
                                scanner_score=float(candidate.score),
                                now=now,
                                macro=macro,
                                policy_action=policy_actions.get(sym),
                            )
                            if gate.allowed:
                                logger.info(
                                    "HOT GRAB %s %s score=%.2f — acting now",
                                    candidate.side.upper(),
                                    sym,
                                    candidate.score,
                                )
                        else:
                            gate = evaluate_quality(
                                sym,
                                row,
                                candidate.side if candidate.side in {"buy", "sell"} else "flat",
                                now=now,
                                macro=macro,
                                policy_action=policy_actions.get(sym),
                            )
                        if not gate.allowed:
                            logger.info(
                                "Gate reject %s %s | %s (score=%.2f hot=%s)",
                                sym,
                                candidate.side,
                                gate.reject_reason,
                                gate.score,
                                is_hot,
                            )
                            continue
                        # Policy must not oppose gate side (HOLD ok — quality-first)
                        # Hot grabs ignore soft policy opposition — scanner wins
                        if not is_hot:
                            if gate.side == "buy" and action == TradingEnv.ACTION_SELL:
                                logger.info("Policy opposes BUY gate on %s — skip", sym)
                                continue
                            if gate.side == "sell" and action == TradingEnv.ACTION_BUY:
                                logger.info("Policy opposes SELL gate on %s — skip", sym)
                                continue
                        action = _side_to_action(gate.side)

                        macro_feats = macro.macro_features(now, sym)
                        # Elite scores skip Gemini for speed; others get real veto
                        if should_fast_skip_gemini(float(candidate.score)):
                            advice = TradeAdvice(
                                decision="approve",
                                size_hint="full",
                                confidence=float(candidate.score),
                                headline=(
                                    f"HOT GRAB score={candidate.score:.2f}"
                                    if is_hot
                                    else f"Fast-skip score={candidate.score:.2f}"
                                ),
                                risk_notes=["Elite score — Gemini skipped"],
                                sources_summary="",
                                fallback=True,
                            )
                        else:
                            advice = default_advisor.advise_trade(
                                {
                                    "symbol": sym,
                                    "side": candidate.side,
                                    "score": float(candidate.score),
                                    "reasons": list(candidate.reasons or [])[:5],
                                    "price": price,
                                    "atr": atr,
                                    "rsi": float(row["rsi_14"]) if "rsi_14" in row.index else None,
                                    "ema20": float(row["ema_20"]) if "ema_20" in row.index else None,
                                    "ema50": float(row["ema_50"]) if "ema_50" in row.index else None,
                                    "ema200": float(row["ema_200"]) if "ema_200" in row.index else None,
                                    "macro": macro_feats,
                                    "open_positions": manager.open_count(),
                                    "equity": account.equity,
                                }
                            )
                        logger.info(
                            "ENTER CHECK %s %s score=%.2f gemini=%s/%s fallback=%s",
                            candidate.side.upper(),
                            sym,
                            candidate.score,
                            advice.decision,
                            advice.size_hint,
                            advice.fallback,
                        )
                        if not advice.allow_entry:
                            logger.info(
                                "Gemini VETO %s %s | %s | %s",
                                sym,
                                candidate.side,
                                advice.headline,
                                advice.risk_notes,
                            )
                            send_telegram(
                                compose_alert(
                                    "veto",
                                    {
                                        "symbol": sym,
                                        "side": candidate.side,
                                        "headline": advice.headline,
                                        "risk_notes": advice.risk_notes,
                                        "sources_summary": advice.sources_summary,
                                        "confidence": advice.confidence,
                                    },
                                )
                            )
                            continue

                        order = manager.approve_entry(
                            sym,
                            action,
                            account.equity,
                            price,
                            atr,
                            now=now,
                            lot_mult=advice.lot_mult * risk_scale,
                        )
                        if order.action in {"buy", "sell"} and order.approved:
                            fill = send_mt5_order(
                                sym, order, paper=paper, cfg=cfg, market_price=price
                            )
                            status = fill.get("status", "")
                            if status in {"filled", "paper_fill"}:
                                fill_price = float(fill.get("price") or price)
                                fill_vol = float(fill.get("volume") or order.volume)
                                fill_ticket = fill.get("ticket")
                                try:
                                    fill_ticket = int(fill_ticket) if fill_ticket else None
                                except (TypeError, ValueError):
                                    fill_ticket = None
                                side = "buy" if order.action == "buy" else "sell"
                                eng.register_fill(
                                    side,  # type: ignore[arg-type]
                                    fill_vol,
                                    fill_price,
                                    order.stop_loss,
                                    order.take_profit,
                                    atr,
                                    ticket=fill_ticket,
                                )
                                rocket.register_open(
                                    sym,
                                    entry=fill_price,
                                    stop_loss=order.stop_loss,
                                    volume=fill_vol,
                                    contract_size=eng.contract_size,
                                    bar_time=now,
                                )
                                fee = (
                                    fill_price
                                    * fill_vol
                                    * eng.contract_size
                                    * RISK.transaction_fee
                                )
                                if account.live_sync and not paper:
                                    account.sync_from_mt5()
                                else:
                                    account.balance -= fee
                                    account.equity = account.balance
                                cols_s = frames[sym][1]
                                feats = row[cols_s].to_numpy(dtype=np.float32)
                                macro_vec = [
                                    macro_feats[k]
                                    for k in (
                                        "in_blackout",
                                        "mins_to_event_scaled",
                                        "in_london",
                                        "in_ny",
                                        "in_overlap",
                                    )
                                ]
                                obs = build_live_observation(feats, 0.0, 0.0, macro_vec)
                                journal_ids[sym] = journal.log_open(
                                    symbol=sym,
                                    side=side,
                                    volume=fill_vol,
                                    entry=fill_price,
                                    action=action,
                                    obs=obs.tolist(),
                                    macro=macro_feats,
                                    ticket=fill_ticket,
                                )
                                smc_tags = [
                                    r.upper() for r in candidate.reasons
                                    if any(k in r.lower() for k in ("order block", "fvg", "bos", "structure", "zone"))
                                ]
                                h1_align = any("h1" in r.lower() or "trend" in r.lower() for r in candidate.reasons)
                                mtf_str = "M15 🟢 | H1 🟢" if h1_align else "M15 🟢 | H1 🟡"
                                msg = format_entry(
                                    side=order.action,
                                    symbol=sym,
                                    entry=fill_price,
                                    stop_loss=order.stop_loss,
                                    take_profit=order.take_profit,
                                    volume=fill_vol,
                                    score=candidate.score,
                                    reasons=candidate.reasons[:3],
                                    equity=account.equity,
                                    mode=mode,
                                    compact=True,
                                    confluence_mtf=mtf_str,
                                    smc_tags=smc_tags,
                                )
                                if GEMINI.enabled:
                                    note = advice.headline or (
                                        "; ".join(advice.risk_notes[:1])
                                        if advice.risk_notes
                                        else ""
                                    )
                                    msg = append_advisor_note(msg, note)
                                logger.info(
                                    "%s %s @ %.5f vol=%.2f score=%.2f gemini=%s/%s",
                                    order.action.upper(),
                                    sym,
                                    fill_price,
                                    fill_vol,
                                    candidate.score,
                                    advice.decision,
                                    advice.size_hint,
                                )
                                df_chart = frames.get(sym, (None, None))[0]
                                send_trade_alert(
                                    msg,
                                    df=df_chart,
                                    symbol=sym,
                                    side=order.action,
                                    entry=fill_price,
                                    stop_loss=order.stop_loss,
                                    take_profit=order.take_profit,
                                    title_suffix="ENTRY",
                                    reply_markup=entry_alert_keyboard(sym),
                                )
                                open_syms.add(sym)
                            else:
                                logger.info(
                                    "Entry not filled %s: %s (%s)",
                                    sym,
                                    status,
                                    order.reason,
                                )
                        else:
                            logger.info(
                                "No entry %s | reason=%s | top=%s/%.2f | gemini=%s",
                                sym,
                                order.reason,
                                candidate.side,
                                candidate.score,
                                advice.decision,
                            )
                else:
                    logger.info(
                        "At max positions (%s) | equity=%.2f",
                        MULTI.max_open_positions,
                        account.equity,
                    )

                # ---- 4) Background online learn (never blocks the loop) ----
                if ONLINE.enabled:
                    learn_result = maybe_schedule_learn(
                        df=df_ref,
                        feature_cols=feature_cols_ref,
                        open_count=manager.open_count(),
                        closes_trigger=ONLINE.closes_trigger,
                        daily=True,
                        last_learn_day=last_learn_day,
                        now=now,
                        notify=send_telegram,
                        symbol=cfg.symbol,
                    )
                    if learn_result.get("status") in {"started", "queued"}:
                        last_learn_day = now
                        logger.info(
                            "Background learn %s (%s) closes=%s",
                            learn_result.get("status"),
                            learn_result.get("kind"),
                            learn_result.get("closes"),
                        )
                    # Hot-reload policy if a background job finished updated
                    lr = default_learner.last_result
                    if lr.get("status") == "updated" and lr.get("_reloaded") is not True:
                        policy = load_policy(
                            cfg.model_path, expected_obs_dim=expected_dim
                        )
                        lr["_reloaded"] = True
                        logger.info(
                            "Policy hot-reloaded after learn sharpe %.3f → %.3f",
                            float(lr.get("baseline_sharpe") or 0),
                            float(lr.get("new_sharpe") or 0),
                        )

            except Exception as exc:
                logger.exception("Loop iteration error")
                send_telegram(format_error(exc))

            time.sleep(cfg.poll_seconds)
    finally:
        cmd_listener.stop()
        if not synthetic:
            shutdown_mt5()
        send_telegram(format_shutdown(mode=mode, equity=account.equity))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RL Forex Trading Bot (MT5)")
    sub = parser.add_subparsers(dest="command", required=True)

    train_p = sub.add_parser("train", help="Train PPO on historical / synthetic data")
    train_p.add_argument("--timesteps", type=int, default=150_000)
    train_p.add_argument("--synthetic", action="store_true", help="Use synthetic OHLCV")

    bt_p = sub.add_parser("backtest", help="Backtest trained policy (~12 months)")
    bt_p.add_argument("--synthetic", action="store_true", help="Use synthetic OHLCV")
    bt_p.add_argument(
        "--no-macro",
        action="store_true",
        help="Disable session/blackout filters in backtest",
    )

    wf_p = sub.add_parser(
        "walkforward",
        help="Out-of-sample walk-forward folds on historical data",
    )
    wf_p.add_argument("--synthetic", action="store_true", help="Use synthetic OHLCV")
    wf_p.add_argument("--folds", type=int, default=4, help="Number of contiguous folds")
    wf_p.add_argument(
        "--no-macro",
        action="store_true",
        help="Disable session/blackout filters",
    )

    run_p = sub.add_parser("run", help="Start live / paper trading loop")
    run_p.add_argument("--synthetic", action="store_true", help="Simulate without MT5")
    run_p.add_argument("--max-iterations", type=int, default=None)

    learn_p = sub.add_parser("learn", help="Force one online fine-tune now")
    learn_p.add_argument("--synthetic", action="store_true", help="Use synthetic OHLCV")
    learn_p.add_argument("--force", action="store_true", default=True, help="Ignore daily/min-trade guards")

    return parser


def main(argv: list[str] | None = None) -> None:
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    if not root.handlers:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    from logging.handlers import RotatingFileHandler

    fh = RotatingFileHandler(
        log_dir / "bot.log",
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "train":
        if args.synthetic:
            primary, context = synthesize_ohlcv(bars=20000)
            df, cols = build_feature_frame(primary, context)
        else:
            initialize_mt5(BOT.mt5_login, BOT.mt5_password, BOT.mt5_server, BOT.mt5_path)
            try:
                df, cols = load_historical_features()
            finally:
                shutdown_mt5()
        train_agent(df, cols, timesteps=args.timesteps)
        logger.info(
            "NOTE: Observation space now includes macro features. "
            "Old checkpoints are incompatible — this train writes a fresh model."
        )

    elif args.command == "backtest":
        if args.synthetic:
            df, cols = _load_feature_data(True)
        else:
            initialize_mt5(BOT.mt5_login, BOT.mt5_password, BOT.mt5_server, BOT.mt5_path)
            try:
                df, cols = load_historical_features()
            finally:
                shutdown_mt5()
        # Probe obs dim
        probe = TradingEnv(df.iloc[:50], cols, symbol=BOT.symbol, use_macro=True)
        policy = load_policy(BOT.model_path, expected_obs_dim=int(probe.observation_space.shape[0]))
        result = run_backtest(
            df,
            cols,
            policy,
            apply_macro_filters=not args.no_macro,
        )
        print("\n=== Backtest Results (12m) ===")
        for k, v in result.as_dict().items():
            print(f"{k}: {v}")
        send_telegram(format_backtest(result.as_dict()))

    elif args.command == "walkforward":
        if args.synthetic:
            df, cols = _load_feature_data(True)
        else:
            initialize_mt5(BOT.mt5_login, BOT.mt5_password, BOT.mt5_server, BOT.mt5_path)
            try:
                df, cols = load_historical_features()
            finally:
                shutdown_mt5()
        probe = TradingEnv(df.iloc[:50], cols, symbol=BOT.symbol, use_macro=True)
        policy = load_policy(
            BOT.model_path, expected_obs_dim=int(probe.observation_space.shape[0])
        )
        folds = run_walkforward(
            df,
            cols,
            policy,
            n_folds=max(2, int(args.folds)),
            apply_macro_filters=not args.no_macro,
        )
        print("\n=== Walk-Forward Results ===")
        for f in folds:
            print(
                f"Fold {f['fold']}: bars={f['bars']} PF={f['profit_factor']} "
                f"WR={f['win_rate_pct']}% MDD={f['max_dd_pct']}% "
                f"Sharpe={f['sharpe']} trades={f['trades']} net={f['net_profit']}"
            )
        if folds:
            avg_pf = sum(float(f["profit_factor"]) for f in folds) / len(folds)
            avg_wr = sum(float(f["win_rate_pct"]) for f in folds) / len(folds)
            print(f"\nAvg PF={avg_pf:.3f}  Avg WR={avg_wr:.1f}%  folds={len(folds)}")

    elif args.command == "run":
        run_live_loop(synthetic=args.synthetic, max_iterations=args.max_iterations)

    elif args.command == "learn":
        if args.synthetic:
            primary, context = synthesize_ohlcv(bars=3000)
            df, cols = build_feature_frame(primary, context)
        else:
            initialize_mt5(BOT.mt5_login, BOT.mt5_password, BOT.mt5_server, BOT.mt5_path)
            try:
                df, cols = load_historical_features(months=1)
            finally:
                shutdown_mt5()
        result = run_daily_update(
            df,
            cols,
            force=True,
            has_open_position=False,
            notify=send_telegram,
        )
        print(result)


if __name__ == "__main__":
    main()
