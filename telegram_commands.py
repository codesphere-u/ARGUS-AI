"""Interactive Telegram command center with tap-buttons."""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from config import BOT, BRAND_NAME, DATA_DIR, DESK_SETTINGS_PATH, BotConfig
from telegram_format import fmt_money, fmt_price

logger = logging.getLogger(__name__)

# Handler signature: (args: list[str], chat_id: str) -> None
CommandHandler = Callable[[list[str], str], None]

POLL_LOCK_PATH = DATA_DIR / "telegram_poll.lock"

BOT_COMMANDS = [
    {"command": "status", "description": "Equity, PnL, win rate"},
    {"command": "positions", "description": "Open trades"},
    {"command": "scan", "description": "Opportunity scores"},
    {"command": "chart", "description": "Render chart for symbol"},
    {"command": "calc", "description": "Lot size & risk calculator"},
    {"command": "digest", "description": "Daily recap summary"},
    {"command": "pause", "description": "Pause new entries"},
    {"command": "resume", "description": "Resume entries"},
    {"command": "close", "description": "Close one symbol"},
    {"command": "closeall", "description": "Close all positions"},
    {"command": "partial", "description": "Partial volume close (e.g. /partial EURUSDm 50)"},
    {"command": "be", "description": "Lock Stop-Loss to Break-Even"},
    {"command": "setrisk", "description": "Max $ risk per trade"},
    {"command": "report", "description": "Performance report & equity chart"},
    {"command": "journal", "description": "Recent closed trades"},
    {"command": "pnl", "description": "Today + total PnL card"},
    {"command": "settings", "description": "Mute pulse / alerts"},
    {"command": "coach", "description": "Ask the desk coach"},
    {"command": "menu", "description": "Re-pin control keyboard"},
    {"command": "help", "description": "Command center help"},
]

# Reply-keyboard label → command name
BUTTON_TO_CMD: dict[str, str] = {
    "📊 Status": "status",
    "📂 Positions": "positions",
    "🔎 Scan": "scan",
    "📊 Chart": "chart",
    "🧮 Calc": "calc",
    "📜 Digest": "digest",
    "⏸ Pause": "pause",
    "▶ Resume": "resume",
    "📈 Report": "report",
    "🛑 Close All": "closeall",
    "💸 Set Risk": "setrisk",
    "📱 Menu": "menu",
    "📓 Journal": "journal",
    "💰 PnL": "pnl",
    "⚙️ Settings": "settings",
    "💬 Ask coach": "coach",
    "📚 Teach me": "teach",
    "📋 How to copy": "copyhelp",
    # Plain-text fallbacks
    "Status": "status",
    "Positions": "positions",
    "Scan": "scan",
    "Chart": "chart",
    "Calc": "calc",
    "Digest": "digest",
    "Pause": "pause",
    "Resume": "resume",
    "Report": "report",
    "Close All": "closeall",
    "Set Risk": "setrisk",
    "Menu": "menu",
    "Help": "help",
    "Journal": "journal",
    "PnL": "pnl",
    "Settings": "settings",
    "Ask coach": "coach",
    "Teach me": "teach",
    "How to copy": "copyhelp",
}


HELP_TEXT = f"""\
📱 <b>{BRAND_NAME} Command Center</b>

Tap any menu button below or type a command:

<b>Navigation & Market Data:</b>
<code>/status</code> — Live equity, PnL & win rate
<code>/positions</code> — Bot & manual open trades
<code>/scan</code> — Ranked multi-pair opportunity deck
<code>/chart [SYMBOL]</code> — On-demand M15 dark candlestick chart
<code>/calc</code> — Dynamic lot size & risk calculator
<code>/digest</code> — Daily performance summary

<b>Trade & Risk Control:</b>
<code>/partial SYMBOL [PCT]</code> — Close 25%, 50%, or 75% lot volume
<code>/be SYMBOL</code> — Lock Stop-Loss to Break-Even (+1 pip buffer)
<code>/close SYMBOL</code> — Close one open trade
<code>/closeall</code> — 1-tap close all open positions
<code>/setrisk [AMOUNT]</code> — Set max $ risk per trade
<code>/pause</code> / <code>/resume</code> — Toggle entry execution

<b>Analytics & Coaching:</b>
<code>/report</code> — Detailed report & dark equity curve
<code>/journal</code> — Recent closed trade log
<code>/pnl</code> — Today + total PnL metrics
<code>/settings</code> — Desk notifications & alert toggles
<code>/coach</code> — Ask the AI desk coach anything
<code>/menu</code> — Re-pin bottom control keyboard
"""

WELCOME_TEXT = f"""\
👋 <b>Welcome to {BRAND_NAME} Bot!</b>

Your intelligent Forex & Crypto trading assistant is now ready.

<b>Features & Tools:</b>
📊 Live Trading Signals & Confluence Analysis
🥇 Gold (XAUUSD) & Crypto (BTC/ETH/SOL) Coverage
📐 Multi-Timeframe Alignment & Smart Money Concepts (SMC)
🔔 Ambient Pre-News Blackout Safeguards
🧮 Dynamic Lot Size & Risk Guidance
📈 Visual Equity Curve & Chart Rendering
💬 AI Desk Coach — ask questions, get setups, or learn strategies

Use the buttons below, tap <b>💬 Ask coach</b>, or type a question anytime.

<i>Trade smart. Protect your capital. Stay disciplined.</i>

⚠️ <b>Disclaimer:</b> Educational and informational assistant. Not financial advice. Always manage your own risk.
"""

UNAUTHORIZED_TEXT = f"""\
🔒 <b>{BRAND_NAME}</b> is a private trading desk.

This bot does not accept commands from this chat.

📢 Follow free setups on the public channel:
https://t.me/AlvinHacksFX
"""

PUBLIC_WELCOME_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "📢 Join Alvin Hacks FX Channel", "url": "https://t.me/AlvinHacksFX"}],
    ]
}


def main_reply_keyboard() -> dict[str, Any]:
    """Persistent bottom keyboard — always visible in the chat."""
    return {
        "keyboard": [
            [{"text": "📊 Status"}, {"text": "📂 Positions"}, {"text": "🔎 Scan"}],
            [{"text": "📊 Chart"}, {"text": "🧮 Calc"}, {"text": "📜 Digest"}],
            [{"text": "⏸ Pause"}, {"text": "▶ Resume"}, {"text": "📈 Report"}],
            [{"text": "📓 Journal"}, {"text": "💰 PnL"}, {"text": "⚙️ Settings"}],
            [{"text": "🛑 Close All"}, {"text": "💸 Set Risk"}, {"text": "📱 Menu"}],
            [{"text": "💬 Ask coach"}, {"text": "📚 Teach me"}, {"text": "📋 How to copy"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def tab_inline_keyboard(active_tab: str = "dash", paused: bool = False) -> dict[str, Any]:
    """Dynamic active-tab highlighted inline keyboard for smooth in-place UI navigation."""
    def _lbl(name: str, key: str, emoji: str) -> str:
        return f"🔘 {name}" if active_tab == key else f"{emoji} {name}"

    pause_btn = (
        {"text": "▶ Resume", "callback_data": "cmd:resume"}
        if paused
        else {"text": "⏸ Pause", "callback_data": "cmd:pause"}
    )
    return {
        "inline_keyboard": [
            [
                {"text": _lbl("Status", "dash", "📊"), "callback_data": "cmd:tab:dash"},
                {"text": _lbl("Trades", "trades", "📂"), "callback_data": "cmd:tab:trades"},
                {"text": _lbl("Scans", "scans", "⚡"), "callback_data": "cmd:tab:scans"},
            ],
            [
                {"text": _lbl("Risk", "risk", "🛡️"), "callback_data": "cmd:tab:risk"},
                {"text": _lbl("Stats", "perf", "📈"), "callback_data": "cmd:tab:perf"},
                {"text": _lbl("Settings", "settings", "⚙️"), "callback_data": "cmd:tab:settings"},
            ],
            [
                {"text": "📊 Chart", "callback_data": "cmd:chart"},
                {"text": "🧮 Calc", "callback_data": "cmd:calc"},
                {"text": "📜 Digest", "callback_data": "cmd:digest"},
            ],
            [
                pause_btn,
                {"text": "🛡️ BE All", "callback_data": "cmd:be_all"},
                {"text": "🛑 Close All", "callback_data": "cmd:closeall"},
            ],
            [
                {"text": "💬 Ask coach", "callback_data": "cmd:coach"},
                {"text": "📚 Teach me", "callback_data": "cmd:teach"},
                {"text": "📋 Copy help", "callback_data": "cmd:copyhelp"},
            ],
        ]
    }


def main_inline_keyboard(*, paused: bool = False, active_tab: str = "dash") -> dict[str, Any]:
    """Inline buttons under a menu / status message."""
    return tab_inline_keyboard(active_tab=active_tab, paused=paused)


def confirm_closeall_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Yes, close all", "callback_data": "cmd:closeall_yes"},
                {"text": "❌ Cancel", "callback_data": "cmd:closeall_no"},
            ]
        ]
    }


def confirm_close_keyboard(symbol: str) -> dict[str, Any]:
    sym = (symbol or "")[:40]
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"✅ Close {sym}",
                    "callback_data": f"cmd:close_yes:{sym}"[:64],
                },
                {"text": "❌ Cancel", "callback_data": "cmd:close_no"},
            ]
        ]
    }


def setrisk_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "$100", "callback_data": "cmd:setrisk:100"},
                {"text": "$250", "callback_data": "cmd:setrisk:250"},
                {"text": "$500", "callback_data": "cmd:setrisk:500"},
            ],
            [
                {"text": "$1000", "callback_data": "cmd:setrisk:1000"},
                {"text": "Clear override", "callback_data": "cmd:setrisk:clear"},
            ],
            [{"text": "📱 Menu", "callback_data": "cmd:help"}],
        ]
    }


def positions_close_keyboard(symbols: list[str]) -> dict[str, Any] | None:
    if not symbols:
        return None
    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for sym in symbols[:12]:
        row.append(
            {"text": f"✕ {sym}", "callback_data": f"cmd:closeask:{sym}"[:64]}
        )
        if len(row) >= 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            {"text": "🛑 Close All", "callback_data": "cmd:closeall"},
            {"text": "📱 Menu", "callback_data": "cmd:help"},
        ]
    )
    return {"inline_keyboard": rows}


def positions_manage_keyboard(
    *,
    bot_symbols: list[str],
    external: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Close buttons for bot-managed + Adopt for external tickets."""
    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for sym in bot_symbols[:8]:
        row.append({"text": f"✕ {sym}", "callback_data": f"cmd:closeask:{sym}"[:64]})
        if len(row) >= 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    for ext in external[:6]:
        ticket = int(ext.get("ticket") or 0)
        sym = str(ext.get("symbol") or "?")
        if ticket <= 0:
            continue
        rows.append(
            [
                {
                    "text": f"📥 Adopt {sym}",
                    "callback_data": f"cmd:adoptask:{ticket}"[:64],
                }
            ]
        )
    if not rows:
        return None
    rows.append(
        [
            {"text": "🛑 Close All", "callback_data": "cmd:closeall"},
            {"text": "📱 Menu", "callback_data": "cmd:help"},
        ]
    )
    return {"inline_keyboard": rows}


def confirm_adopt_keyboard(ticket: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Yes, adopt",
                    "callback_data": f"cmd:adopt_yes:{ticket}"[:64],
                },
                {"text": "❌ Cancel", "callback_data": "cmd:adopt_no"},
            ]
        ]
    }


def confirm_partial_keyboard(symbol: str) -> dict[str, Any]:
    sym = (symbol or "")[:40]
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"✅ Yes, Partial Close 50% {sym}",
                    "callback_data": f"cmd:partial_yes:{sym}"[:64],
                },
                {"text": "❌ Cancel", "callback_data": "cmd:close_no"},
            ]
        ]
    }


def confirm_be_keyboard(symbol: str) -> dict[str, Any]:
    sym = (symbol or "")[:40]
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"✅ Lock BE {sym}",
                    "callback_data": f"cmd:be_yes:{sym}"[:64],
                },
                {"text": "❌ Cancel", "callback_data": "cmd:close_no"},
            ]
        ]
    }


def contextual_position_keyboard(symbol: str) -> dict[str, Any]:
    """Adaptive inline keyboard for open position management (25%, 50%, 75%, BE, Close)."""
    sym = (symbol or "")[:40]
    return {
        "inline_keyboard": [
            [
                {"text": "🎯 25%", "callback_data": f"cmd:partial_pct:{sym}:25"[:64]},
                {"text": "🎯 50%", "callback_data": f"cmd:partial_pct:{sym}:50"[:64]},
                {"text": "🎯 75%", "callback_data": f"cmd:partial_pct:{sym}:75"[:64]},
            ],
            [
                {"text": "🛡️ Lock BE", "callback_data": f"cmd:beask:{sym}"[:64]},
                {"text": f"✕ Close {sym}", "callback_data": f"cmd:closeask:{sym}"[:64]},
            ],
            [
                {"text": "📂 Positions", "callback_data": "cmd:positions"},
                {"text": f"💬 Coach {sym}", "callback_data": f"cmd:coachsym:{sym}"[:64]},
            ],
        ]
    }


def entry_alert_keyboard(symbol: str) -> dict[str, Any]:
    """Inline actions under an ENTRY alert — Adaptive 25%/50%/75% Partial, BE, Close, Coach."""
    return contextual_position_keyboard(symbol)


def exit_alert_keyboard() -> dict[str, Any]:
    """Inline actions under an EXIT alert."""
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Status", "callback_data": "cmd:status"},
                {"text": "🔎 Scan", "callback_data": "cmd:scan"},
                {"text": "📱 Menu", "callback_data": "cmd:help"},
            ]
        ]
    }


def contextual_flat_keyboard() -> dict[str, Any]:
    """Adaptive inline keyboard when NO positions are open — scan & explore mode."""
    return {
        "inline_keyboard": [
            [
                {"text": "⚡ Scan Deck", "callback_data": "cmd:scan"},
                {"text": "🧮 Lot Calc", "callback_data": "cmd:calc"},
            ],
            [
                {"text": "📊 Market Matrix", "callback_data": "cmd:tab:dash"},
                {"text": "🤖 AI Briefing", "callback_data": "cmd:coach"},
            ],
            [
                {"text": "📈 Report", "callback_data": "cmd:report"},
                {"text": "📱 Menu", "callback_data": "cmd:help"},
            ],
        ]
    }


def news_warning_keyboard() -> dict[str, Any]:
    """CTA under pre-news warning — 1-tap protect positions."""
    return {
        "inline_keyboard": [
            [
                {"text": "🛡️ Protect Positions (Lock BE All)", "callback_data": "cmd:be_all"},
                {"text": "⏸ Pause Entries", "callback_data": "cmd:pause"},
            ],
            [
                {"text": "📂 Positions", "callback_data": "cmd:positions"},
                {"text": "📱 Dismiss", "callback_data": "cmd:help"},
            ],
        ]
    }


def opportunity_alert_keyboard() -> dict[str, Any]:
    """Compact CTAs under private opportunity alerts."""
    return {
        "inline_keyboard": [
            [
                {"text": "🔎 Scan", "callback_data": "cmd:scan"},
                {"text": "📋 Copy help", "callback_data": "cmd:copyhelp"},
                {"text": "📱 Menu", "callback_data": "cmd:help"},
            ]
        ]
    }


def channel_deep_link_keyboard(bot_username: str | None) -> dict[str, Any] | None:
    """CTA from public channel back to private bot."""
    user = (bot_username or "").lstrip("@").strip()
    if not user:
        return None
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🤖 Open private bot",
                    "url": f"https://t.me/{user}?start=from_channel",
                }
            ]
        ]
    }


def settings_keyboard(control: RuntimeControl) -> dict[str, Any]:
    def _tog(on: bool) -> str:
        return "ON" if on else "OFF"

    return {
        "inline_keyboard": [
            [
                {
                    "text": f"Desk pulse · {_tog(not control.mute_pulse)}",
                    "callback_data": "cmd:mute:pulse",
                }
            ],
            [
                {
                    "text": f"Setup alerts · {_tog(not control.mute_opportunity_alerts)}",
                    "callback_data": "cmd:mute:opp",
                }
            ],
            [
                {
                    "text": f"Channel preds · {_tog(not control.mute_channel_preds)}",
                    "callback_data": "cmd:mute:chan",
                }
            ],
            [{"text": "📱 Menu", "callback_data": "cmd:help"}],
        ]
    }


def format_settings_card(control: RuntimeControl) -> str:
    def _line(label: str, muted: bool) -> str:
        state = "🔇 muted" if muted else "🔔 on"
        return f"{label} · <b>{state}</b>"

    return "\n".join(
        [
            "⚙️ <b>DESK SETTINGS</b>",
            "",
            _line("Desk pulse", control.mute_pulse),
            _line("Setup alerts", control.mute_opportunity_alerts),
            _line("Channel preds", control.mute_channel_preds),
            "",
            "<i>Trading continues — mute only quietens notifications.</i>",
            "Tap a row to toggle.",
        ]
    )


@dataclass
class RuntimeControl:
    """
    Mutable runtime flags shared between the live loop and Telegram commands.

    Manual pause only — distinct from auto loss-guard pausing (which stays off).
    """

    paused: bool = False
    pause_reason: str = ""
    risk_money_override: float | None = None
    mute_pulse: bool = False
    mute_opportunity_alerts: bool = False
    mute_channel_preds: bool = False
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    settings_path: Path = field(default_factory=lambda: DESK_SETTINGS_PATH)

    def pause(self, reason: str = "manual") -> None:
        self.paused = True
        self.pause_reason = reason
        self.save_settings()

    def resume(self) -> None:
        self.paused = False
        self.pause_reason = ""
        self.save_settings()

    def set_risk_money(self, amount: float | None) -> None:
        self.risk_money_override = amount
        self.save_settings()

    def load_settings(self) -> None:
        path = self.settings_path
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.mute_pulse = bool(data.get("mute_pulse", False))
            self.mute_opportunity_alerts = bool(
                data.get("mute_opportunity_alerts", False)
            )
            self.mute_channel_preds = bool(data.get("mute_channel_preds", False))
            self.paused = bool(data.get("paused", False))
            self.pause_reason = str(data.get("pause_reason") or "")
            risk = data.get("risk_money_override")
            if risk is None or risk == "" or risk == "null":
                self.risk_money_override = None
            else:
                try:
                    self.risk_money_override = float(risk)
                except (TypeError, ValueError):
                    self.risk_money_override = None
            logger.info(
                "Desk settings loaded (paused=%s risk=%s pulse=%s opp=%s chan=%s)",
                self.paused,
                self.risk_money_override,
                "mute" if self.mute_pulse else "on",
                "mute" if self.mute_opportunity_alerts else "on",
                "mute" if self.mute_channel_preds else "on",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load desk settings: %s", exc)

    def save_settings(self) -> None:
        path = self.settings_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "mute_pulse": self.mute_pulse,
                        "mute_opportunity_alerts": self.mute_opportunity_alerts,
                        "mute_channel_preds": self.mute_channel_preds,
                        "paused": self.paused,
                        "pause_reason": self.pause_reason,
                        "risk_money_override": self.risk_money_override,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to save desk settings: %s", exc)

    def toggle_mute(self, key: str) -> bool:
        """Toggle a mute flag; returns new muted state."""
        if key == "pulse":
            self.mute_pulse = not self.mute_pulse
            self.save_settings()
            return self.mute_pulse
        if key in {"opp", "opportunity", "opportunities"}:
            self.mute_opportunity_alerts = not self.mute_opportunity_alerts
            self.save_settings()
            return self.mute_opportunity_alerts
        if key in {"chan", "channel", "preds"}:
            self.mute_channel_preds = not self.mute_channel_preds
            self.save_settings()
            return self.mute_channel_preds
        raise ValueError(f"Unknown mute key: {key}")


@dataclass
class TelegramCommandListener:
    """
    Background long-poller for Telegram bot commands + button taps + free-text coach.

    Supports:
    - Slash commands (/status)
    - Reply keyboard taps (📊 Status)
    - Inline keyboard callbacks (callback_data=cmd:…)
    - Free-text chat via ``chat_handler`` (desk coach)
    """

    control: RuntimeControl
    handlers: dict[str, CommandHandler] = field(default_factory=dict)
    cfg: BotConfig = field(default_factory=lambda: BOT)
    reply: Callable[..., bool] | None = None
    chat_handler: Callable[[str, str], None] | None = None
    get_equity: Callable[[], float] | None = None
    allowed_chats: set[str] = field(default_factory=set)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _offset: int = 0
    _lock_held: bool = field(default=False, repr=False)
    _coach_queue: queue.Queue | None = field(default=None, repr=False)
    _coach_thread: threading.Thread | None = field(default=None, repr=False)
    _real_chat_handler: Callable[[str, str], None] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.cfg is None:
            self.cfg = BOT
        if not self.allowed_chats:
            raw = self.cfg.telegram_chat_id or ""
            self.allowed_chats = {p.strip() for p in str(raw).split(",") if p.strip()}
        self.handlers.setdefault("help", self._cmd_help)
        self.handlers.setdefault("start", self._cmd_start)
        self.handlers.setdefault("menu", self._cmd_menu)
        self.handlers.setdefault("settings", self._cmd_settings)
        self.handlers.setdefault("calc", self._cmd_calc)

    def _cmd_help(self, _args: list[str], chat_id: str) -> None:
        self.send_menu(chat_id)

    def _cmd_start(self, _args: list[str], chat_id: str) -> None:
        self.send_welcome(chat_id)

    def _cmd_calc(self, args: list[str], chat_id: str) -> None:
        """
        Calculate lot size.
        Usage: /calc SYMBOL ENTRY SL [RISK%]
        """
        from telegram_format import format_lot_calculator
        if not args or len(args) < 3:
            self._send(
                "🧮 <b>Lot Calculator Usage:</b>\n"
                "<code>/calc SYMBOL ENTRY SL [RISK%]</code>\n\n"
                "<i>Example:</i> <code>/calc EURUSDm 1.0850 1.0820 1%</code>",
                chat_id,
            )
            return
        sym = args[0].upper()
        try:
            entry = float(args[1])
            sl = float(args[2])
            risk_pct = 1.0
            if len(args) >= 4:
                raw_r = args[3].replace("%", "").replace("$", "")
                risk_pct = float(raw_r)
            equity = float(self.get_equity()) if self.get_equity is not None else 10000.0
            risk_money = equity * (risk_pct / 100.0)
            sl_dist = abs(entry - sl)
            contract_size = 100.0 if any(t in sym for t in ("XAU", "XAG", "GOLD")) else 100000.0
            loss_per_lot = contract_size * sl_dist
            computed_lot = risk_money / loss_per_lot if loss_per_lot > 0 else 0.01
            computed_lot = max(0.01, min(2.0, round(computed_lot, 2)))
            card = format_lot_calculator(
                symbol=sym,
                equity=equity,
                risk_pct=risk_pct,
                entry=entry,
                stop_loss=sl,
                computed_lot=computed_lot,
                risk_money=risk_money,
            )
            self._send(card, chat_id, inline_markup=main_inline_keyboard(paused=self.control.paused))
        except (ValueError, ZeroDivisionError) as exc:
            self._send(f"⚠️ Invalid parameters: {exc}", chat_id)

    def _cmd_menu(self, _args: list[str], chat_id: str) -> None:
        """Re-pin the persistent bottom reply keyboard."""
        self._send(
            "⌨️ <b>Control keyboard restored</b>\nUse the buttons below anytime.",
            chat_id,
            reply_markup=main_reply_keyboard(),
        )

    def _cmd_settings(self, _args: list[str], chat_id: str) -> None:
        self._send(
            format_settings_card(self.control),
            chat_id,
            inline_markup=settings_keyboard(self.control),
        )

    def send_welcome(self, chat_id: str | None = None, *, authorized: bool | None = None) -> bool:
        """Send branded /start welcome. Full controls only for allowed chats."""
        cid = str(chat_id or "")
        is_auth = (
            authorized
            if authorized is not None
            else (not self.allowed_chats or cid in self.allowed_chats)
        )
        if is_auth:
            # Reply keyboard first so it never disappears when inline cards follow
            self._send(
                "⌨️ Controls pinned below.",
                chat_id,
                reply_markup=main_reply_keyboard(),
            )
            return self._send(
                WELCOME_TEXT,
                chat_id,
                inline_markup=main_inline_keyboard(paused=self.control.paused),
            )
        return self._send(
            WELCOME_TEXT,
            chat_id,
            inline_markup=PUBLIC_WELCOME_KEYBOARD,
        )

    def send_menu(self, chat_id: str | None = None) -> bool:
        """Send command center with reply keyboard pin + inline action card."""
        self._send(
            "⌨️ Controls pinned below.",
            chat_id,
            reply_markup=main_reply_keyboard(),
        )
        return self._send(
            HELP_TEXT,
            chat_id,
            inline_markup=main_inline_keyboard(paused=self.control.paused),
        )

    def send_typing(self, chat_id: str | None = None) -> bool:
        """Show Telegram typing indicator (coach / long replies)."""
        token = self.cfg.telegram_bot_token
        targets = [chat_id] if chat_id else list(self.allowed_chats)
        if not token or not targets:
            return False
        url = f"https://api.telegram.org/bot{token}/sendChatAction"
        ok = False
        for target in targets:
            if not target:
                continue
            try:
                resp = requests.post(
                    url,
                    json={"chat_id": target, "action": "typing"},
                    timeout=10,
                )
                ok = ok or resp.ok
            except requests.RequestException as exc:
                logger.debug("sendChatAction failed (%s): %s", target, exc)
        return ok

    def _send(
        self,
        text: str,
        chat_id: str | None = None,
        *,
        reply_markup: dict[str, Any] | None = None,
        inline_markup: dict[str, Any] | None = None,
    ) -> bool:
        # Prefer inline for action cards; never OR them (that dropped the bottom keyboard).
        # Callers that need both should send reply keyboard in a separate message first.
        markup = inline_markup if inline_markup is not None else reply_markup
        if self.reply is not None:
            try:
                return bool(self.reply(text, chat_id, reply_markup=markup))
            except TypeError:
                return bool(self.reply(text, chat_id))
        return self._api_send(text, chat_id, reply_markup=markup)

    def _api_send(
        self,
        text: str,
        chat_id: str | None,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        token = self.cfg.telegram_bot_token
        if not token:
            return False
        targets = [chat_id] if chat_id else list(self.allowed_chats)
        ok = False
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        for target in targets:
            if not target:
                continue
            payload: dict[str, Any] = {
                "chat_id": target,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            try:
                resp = requests.post(url, json=payload, timeout=15)
                resp.raise_for_status()
                ok = True
            except requests.RequestException as exc:
                logger.warning("Telegram button send failed (%s): %s", target, exc)
        return ok

    def _answer_callback(self, callback_id: str, text: str = "") -> None:
        token = self.cfg.telegram_bot_token
        if not token or not callback_id:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text[:200]},
                timeout=10,
            )
        except requests.RequestException as exc:
            logger.debug("answerCallbackQuery failed: %s", exc)

    def register(self, name: str, handler: CommandHandler) -> None:
        self.handlers[name.lstrip("/").lower()] = handler

    def _delete_webhook(self) -> None:
        token = self.cfg.telegram_bot_token
        if not token:
            return
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/deleteWebhook",
                json={"drop_pending_updates": False},
                timeout=15,
            )
            if resp.ok:
                logger.info("Telegram deleteWebhook ok")
            else:
                logger.warning("deleteWebhook failed: %s %s", resp.status_code, resp.text[:200])
        except requests.RequestException as exc:
            logger.warning("deleteWebhook error: %s", exc)

    def _set_my_commands(self) -> None:
        token = self.cfg.telegram_bot_token
        if not token:
            return
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/setMyCommands",
                json={"commands": BOT_COMMANDS},
                timeout=15,
            )
            if resp.ok:
                logger.info("Telegram setMyCommands ok (%s cmds)", len(BOT_COMMANDS))
            else:
                logger.warning("setMyCommands failed: %s %s", resp.status_code, resp.text[:200])
        except requests.RequestException as exc:
            logger.warning("setMyCommands error: %s", exc)

    def _acquire_poll_lock(self) -> bool:
        """Best-effort single-instance lock so duplicate pollers are obvious."""
        path = POLL_LOCK_PATH
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                try:
                    old_pid = int(path.read_text(encoding="utf-8").strip().split()[0])
                except (ValueError, IndexError, OSError):
                    old_pid = -1
                # Stale lock from dead process — steal it
                if old_pid > 0:
                    try:
                        os.kill(old_pid, 0)
                        logger.error(
                            "Telegram poll lock held by pid=%s (%s). "
                            "Stop the other bot process first.",
                            old_pid,
                            path,
                        )
                        return False
                    except OSError:
                        pass
            path.write_text(f"{os.getpid()}\n", encoding="utf-8")
            self._lock_held = True
            return True
        except OSError as exc:
            logger.warning("Could not write telegram poll lock: %s", exc)
            return True  # don't block startup on lock FS issues

    def _release_poll_lock(self) -> None:
        if not self._lock_held:
            return
        try:
            if POLL_LOCK_PATH.exists():
                raw = POLL_LOCK_PATH.read_text(encoding="utf-8").strip()
                if raw.startswith(str(os.getpid())):
                    POLL_LOCK_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        self._lock_held = False

    def start(self) -> None:
        if not self.cfg.telegram_bot_token:
            logger.warning("Telegram commands disabled (no TELEGRAM_BOT_TOKEN).")
            return
        if not self.allowed_chats:
            logger.warning("Telegram commands disabled (no TELEGRAM_CHAT_ID).")
            return
        if self._thread and self._thread.is_alive():
            return
        if not self._acquire_poll_lock():
            logger.error(
                "Refusing to start Telegram poller — another bot process holds "
                "the poll lock. Stop duplicate processes first."
            )
            return
        self._delete_webhook()
        self._set_my_commands()
        self.control.load_settings()
        self._start_coach_worker()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, name="telegram-commands", daemon=True
        )
        self._thread.start()
        logger.info(
            "Telegram command listener started (chats=%s)",
            ",".join(sorted(self.allowed_chats)),
        )

    def _start_coach_worker(self) -> None:
        """Offload Gemini coach replies so getUpdates stays responsive."""
        if self.chat_handler is None:
            return
        if self._coach_thread and self._coach_thread.is_alive():
            return
        self._real_chat_handler = self.chat_handler
        self._coach_queue = queue.Queue()
        self.chat_handler = self._enqueue_coach

        def _worker() -> None:
            assert self._coach_queue is not None
            while not self._stop.is_set():
                try:
                    item = self._coach_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                text, chat_id = item
                try:
                    self.send_typing(chat_id)
                    if self._real_chat_handler is not None:
                        self._real_chat_handler(text, chat_id)
                except Exception as exc:
                    logger.exception("Async coach failed")
                    self._send(
                        f"⚠️ Coach error: {exc}",
                        chat_id,
                        inline_markup=main_inline_keyboard(paused=self.control.paused),
                    )
                finally:
                    self._coach_queue.task_done()

        self._coach_thread = threading.Thread(
            target=_worker, name="telegram-coach", daemon=True
        )
        self._coach_thread.start()
        logger.info("Telegram coach worker started")

    def _enqueue_coach(self, text: str, chat_id: str) -> None:
        if self._coach_queue is None:
            if self._real_chat_handler is not None:
                self._real_chat_handler(text, chat_id)
            return
        self.send_typing(chat_id)
        self._coach_queue.put((text, chat_id))

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        if self._coach_thread and self._coach_thread.is_alive():
            self._coach_thread.join(timeout=5.0)
        self._release_poll_lock()

    def _poll_loop(self) -> None:
        token = self.cfg.telegram_bot_token
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        try:
            resp = requests.get(
                url, params={"offset": -1, "timeout": 0}, timeout=10
            )
            if resp.status_code == 409:
                logger.error(
                    "Another bot poller is running — stop duplicate processes "
                    "(getUpdates HTTP 409 Conflict)."
                )
                time.sleep(10.0)
            else:
                data = resp.json() if resp.ok else {}
                results = data.get("result") or []
                if results:
                    self._offset = int(results[-1]["update_id"]) + 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram getUpdates bootstrap failed: %s", exc)

        while not self._stop.is_set():
            try:
                resp = requests.get(
                    url,
                    params={
                        "offset": self._offset,
                        "timeout": 25,
                        "allowed_updates": json.dumps(["message", "callback_query"]),
                    },
                    timeout=35,
                )
                if resp.status_code == 409:
                    logger.error(
                        "Another bot poller is running — stop duplicate processes "
                        "(getUpdates HTTP 409 Conflict). Backing off 10s."
                    )
                    time.sleep(10.0)
                    continue
                if not resp.ok:
                    logger.warning(
                        "Telegram getUpdates %s: %s",
                        resp.status_code,
                        (resp.text or "")[:200],
                    )
                    time.sleep(3.0)
                    continue
                data = resp.json()
                for update in data.get("result") or []:
                    self._offset = max(self._offset, int(update["update_id"]) + 1)
                    self._dispatch(update)
            except requests.Timeout:
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning("Telegram poll error: %s", exc)
                time.sleep(3.0)

    def _soft_unauthorized(self, chat_id: str) -> None:
        self._send(
            UNAUTHORIZED_TEXT,
            chat_id,
            inline_markup=PUBLIC_WELCOME_KEYBOARD,
        )

    def _dispatch(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self._dispatch_callback(update["callback_query"])
            return
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        if not chat_id:
            return

        # Check for photo uploads (chart vision analysis)
        if "photo" in msg:
            if chat_id not in self.allowed_chats:
                self._soft_unauthorized(chat_id)
                return
            photos = msg.get("photo") or []
            if isinstance(photos, list) and photos:
                best_photo = photos[-1]
                file_id = str(best_photo.get("file_id") or "")
                caption = str(msg.get("caption") or "").strip()
                if file_id:
                    self._handle_photo_update(file_id, caption, chat_id)
                    return

        text = (msg.get("text") or "").strip()
        if not text:
            return

        # Anyone can /start — show welcome (trade commands stay locked)
        first = text.split()[0].split("@", 1)[0].lower()
        if first in {"/start", "start"}:
            self.send_welcome(chat_id)
            return

        if chat_id not in self.allowed_chats:
            self._soft_unauthorized(chat_id)
            return
        self._run_command_text(text, chat_id)

    def _handle_photo_update(self, file_id: str, caption: str, chat_id: str) -> None:
        """Download chart photo from Telegram and pass to Gemini Vision."""
        token = self.cfg.telegram_bot_token
        if not token:
            return
        self.send_typing(chat_id)
        try:
            res = requests.get(
                f"https://api.telegram.org/bot{token}/getFile",
                params={"file_id": file_id},
                timeout=10,
            )
            if not res.ok:
                self._send("⚠️ Could not download chart image from Telegram.", chat_id)
                return
            file_path = res.json().get("result", {}).get("file_path")
            if not file_path:
                self._send("⚠️ File path not returned by Telegram API.", chat_id)
                return
            img_res = requests.get(
                f"https://api.telegram.org/file/bot{token}/{file_path}",
                timeout=15,
            )
            if not img_res.ok:
                self._send("⚠️ Failed downloading photo content.", chat_id)
                return
            from desk_coach import default_coach
            analysis = default_coach.analyze_photo(img_res.content, chat_id, caption=caption)
            self._send(analysis, chat_id, inline_markup=main_inline_keyboard(paused=self.control.paused))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Photo handling failed: %s", exc)
            self._send(f"⚠️ Photo analysis error: {exc}", chat_id)

    def _edit_message(
        self,
        text: str,
        chat_id: str,
        message_id: int,
        *,
        inline_markup: dict | None = None,
    ) -> bool:
        """In-place message editor for smooth Telegram tab switching animations."""
        token = self.cfg.telegram_bot_token
        if not token or not chat_id or not message_id:
            return False
        from telegram_format import sanitize_telegram_html
        safe = sanitize_telegram_html(text or "…")
        url = f"https://api.telegram.org/bot{token}/editMessageText"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": safe,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if inline_markup is not None:
            payload["reply_markup"] = json.dumps(inline_markup)
        try:
            r = requests.post(url, json=payload, timeout=8)
            return r.ok
        except Exception:  # noqa: BLE001
            return False

    def _dispatch_callback(self, cb: dict[str, Any]) -> None:
        msg_obj = cb.get("message") or {}
        chat = msg_obj.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        data = str(cb.get("data") or "")
        callback_id = str(cb.get("id") or "")
        if not chat_id or chat_id not in self.allowed_chats:
            self._answer_callback(callback_id, "Not authorized")
            if chat_id:
                self._soft_unauthorized(chat_id)
            return
        if not data.startswith("cmd:"):
            self._answer_callback(callback_id)
            return

        payload = data[4:]  # after "cmd:"
        parts = payload.split(":", 1)
        cmd = parts[0].lower()
        args: list[str] = []
        if len(parts) > 1 and parts[1]:
            args = [parts[1]]

        # Tabbed navigation handling
        if cmd == "tab":
            tab_key = args[0].lower() if args else "dash"
            tab_labels = {
                "dash": "📊 Status",
                "trades": "📂 Positions",
                "scans": "⚡ Scan Deck",
                "risk": "🛡️ Risk",
                "perf": "📈 Performance",
                "settings": "⚙️ Settings",
            }
            self._answer_callback(callback_id, tab_labels.get(tab_key, tab_key.upper()))
            tab_map = {
                "dash": "status",
                "trades": "positions",
                "scans": "scan",
                "risk": "setrisk",
                "perf": "pnl",
                "settings": "settings",
            }
            target_cmd = tab_map.get(tab_key, "status")
            self._invoke(target_cmd, [], chat_id)
            return

        # Adaptive partial close (25%/50%/75%) callbacks
        if cmd == "partial_pct" and args:
            # args[0] = "SYMBOL:PERCENTAGE" e.g. "EURUSDm:50"
            raw = args[0]
            parts_inner = raw.split(":", 1)
            sym = parts_inner[0]
            pct = int(parts_inner[1]) if len(parts_inner) > 1 else 50
            self._answer_callback(callback_id, f"🎯 Closing {pct}% of {sym}…")
            self._invoke("partial", [sym, str(pct)], chat_id)
            return
        if cmd == "partialask" and args:
            sym = args[0]
            self._answer_callback(callback_id, f"🎯 Confirm partial close {sym}?")
            self._send(
                f"🎯 <b>Partial Close 50% on {sym}?</b>",
                chat_id,
                inline_markup=confirm_partial_keyboard(sym),
            )
            return
        if cmd == "partial_yes" and args:
            self._answer_callback(callback_id, f"🎯 Closing 50% of {args[0]}…")
            self._invoke("partial", args, chat_id)
            return
        if cmd == "beask" and args:
            sym = args[0]
            self._answer_callback(callback_id, f"🛡️ Confirm Lock BE for {sym}?")
            self._send(
                f"🛡️ <b>Move Stop Loss to Break-Even for {sym}?</b>",
                chat_id,
                inline_markup=confirm_be_keyboard(sym),
            )
            return
        if cmd == "be_yes" and args:
            self._answer_callback(callback_id, f"🛡️ SL → Break-Even on {args[0]}")
            self._invoke("be", args, chat_id)
            return
        if cmd == "be_all":
            self._answer_callback(callback_id, "🛡️ Locking BE on all positions…")
            self._invoke("be_all", [], chat_id)
            return
        if cmd == "chart":
            sym_q = args[0] if args else ""
            self._answer_callback(callback_id, f"📊 Rendering {sym_q or 'M15'} Chart…")
            self._invoke("chart", args, chat_id)
            return
        if cmd == "calc":
            self._answer_callback(callback_id, "🧮 Opening Lot Calculator…")
            self._invoke("calc", args, chat_id)
            return
        if cmd == "digest":
            self._answer_callback(callback_id, "📜 Loading Daily Digest…")
            self._invoke("digest", [], chat_id)
            return

        # Built-in confirmation / cancel UX
        if cmd == "closeall":
            self._answer_callback(callback_id, "⚠️ Confirm close ALL positions?")
            self._send(
                "⚠️ <b>Close ALL open positions?</b>",
                chat_id,
                inline_markup=confirm_closeall_keyboard(),
            )
            return
        if cmd == "closeall_no":
            self._answer_callback(callback_id, "❌ Close-all cancelled")
            self._send(
                "❌ Close-all cancelled.",
                chat_id,
                inline_markup=main_inline_keyboard(paused=self.control.paused),
            )
            return
        if cmd == "closeall_yes":
            self._answer_callback(callback_id, "🔄 Closing all positions…")
            self._invoke("closeall", [], chat_id)
            return
        if cmd == "closeask" and args:
            sym = args[0]
            self._answer_callback(callback_id, f"⚠️ Confirm close {sym}?")
            self._send(
                f"⚠️ <b>Close {sym}?</b>",
                chat_id,
                inline_markup=confirm_close_keyboard(sym),
            )
            return
        if cmd == "close_no":
            self._answer_callback(callback_id, "❌ Cancelled")
            self._send(
                "❌ Close cancelled.",
                chat_id,
                inline_markup=main_inline_keyboard(paused=self.control.paused),
            )
            return
        if cmd == "close_yes" and args:
            self._answer_callback(callback_id, f"Closing {args[0]}…")
            self._invoke("close", args, chat_id)
            return
        if cmd == "adoptask" and args:
            ticket = args[0]
            self._answer_callback(callback_id, "Confirm adopt?")
            self._send(
                f"⚠️ <b>Adopt ticket {ticket}?</b>\n"
                "Bot will manage SL / trail / close for this position.",
                chat_id,
                inline_markup=confirm_adopt_keyboard(int(ticket)),
            )
            return
        if cmd == "adopt_no":
            self._answer_callback(callback_id, "Cancelled")
            self._send(
                "❌ Adopt cancelled.",
                chat_id,
                inline_markup=main_inline_keyboard(paused=self.control.paused),
            )
            return
        if cmd == "adopt_yes" and args:
            self._answer_callback(callback_id, "Adopting…")
            self._invoke("adopt", args, chat_id)
            return
        if cmd == "setrisk" and not args:
            self._answer_callback(callback_id, "Pick a risk amount")
            self._send(
                "💸 <b>Set max $ risk per trade</b>\nTap a preset or type <code>/setrisk 300</code>",
                chat_id,
                inline_markup=setrisk_keyboard(),
            )
            return
        if cmd == "setrisk" and args:
            self._answer_callback(callback_id, f"Risk → {args[0]}")
            self._invoke("setrisk", args, chat_id)
            return
        if cmd == "close" and args:
            # Legacy direct close → confirm first
            sym = args[0]
            self._answer_callback(callback_id, f"Confirm close {sym}?")
            self._send(
                f"⚠️ <b>Close {sym}?</b>",
                chat_id,
                inline_markup=confirm_close_keyboard(sym),
            )
            return
        if cmd == "mute" and args:
            key = args[0].lower()
            try:
                muted = self.control.toggle_mute(key)
                self._answer_callback(
                    callback_id, "Muted" if muted else "Unmuted"
                )
            except ValueError:
                self._answer_callback(callback_id, "Unknown setting")
                return
            self._send(
                format_settings_card(self.control),
                chat_id,
                inline_markup=settings_keyboard(self.control),
            )
            return
        if cmd == "settings":
            self._answer_callback(callback_id)
            self._cmd_settings([], chat_id)
            return

        # Desk coach quick-starts
        if cmd in {"coach", "teach", "copyhelp"}:
            self._answer_callback(callback_id)
            if self.chat_handler is None:
                self._send(
                    "💬 Coach is not wired yet. Restart the bot after enabling DESK_COACH.",
                    chat_id,
                    inline_markup=main_inline_keyboard(paused=self.control.paused),
                )
                return
            from desk_coach import COACH_STARTER, COPY_STARTER, TEACH_STARTER

            if cmd == "coach":
                self.chat_handler(f"__intent:coach__\n{COACH_STARTER}", chat_id)
            elif cmd == "teach":
                self.chat_handler(f"__intent:teach__\n{TEACH_STARTER}", chat_id)
            else:
                self.chat_handler(f"__intent:copy__\n{COPY_STARTER}", chat_id)
            return

        if cmd == "coachsym" and args:
            self._answer_callback(callback_id)
            if self.chat_handler is None:
                self._send(
                    "💬 Coach is not ready — restart the bot.",
                    chat_id,
                    inline_markup=main_inline_keyboard(paused=self.control.paused),
                )
                return
            sym = args[0]
            self.chat_handler(
                f"__intent:coach__\nExplain the current setup and risk on {sym}. "
                f"Use CONTEXT for open position / scan score if available.",
                chat_id,
            )
            return

        label = BUTTON_TO_CMD.get(cmd) or cmd
        if label not in self.handlers and cmd in self.handlers:
            label = cmd
        self._answer_callback(callback_id)
        self._invoke(label, args, chat_id)

    def _run_command_text(self, text: str, chat_id: str) -> None:
        # Reply-keyboard tap (exact label match)
        if text in BUTTON_TO_CMD:
            cmd = BUTTON_TO_CMD[text]
            if cmd == "closeall":
                self._send(
                    "⚠️ <b>Close ALL open positions?</b>",
                    chat_id,
                    inline_markup=confirm_closeall_keyboard(),
                )
                return
            if cmd == "setrisk":
                self._send(
                    "💸 <b>Set max $ risk per trade</b>\nTap a preset or type <code>/setrisk 300</code>",
                    chat_id,
                    inline_markup=setrisk_keyboard(),
                )
                return
            if cmd in {"coach", "teach", "copyhelp"}:
                if self.chat_handler is None:
                    self._send(
                        "💬 Coach is not ready — restart the bot.",
                        chat_id,
                        inline_markup=main_inline_keyboard(paused=self.control.paused),
                    )
                    return
                from desk_coach import COACH_STARTER, COPY_STARTER, TEACH_STARTER

                if cmd == "coach":
                    self.chat_handler(f"__intent:coach__\n{COACH_STARTER}", chat_id)
                elif cmd == "teach":
                    self.chat_handler(f"__intent:teach__\n{TEACH_STARTER}", chat_id)
                else:
                    self.chat_handler(f"__intent:copy__\n{COPY_STARTER}", chat_id)
                return
            self._invoke(cmd, [], chat_id)
            return

        if not text.startswith("/"):
            # Free-text → desk coach (does not execute trades)
            if self.chat_handler is not None:
                try:
                    self.chat_handler(text, chat_id)
                except Exception as exc:
                    logger.exception("Coach chat failed")
                    self._send(
                        f"⚠️ Coach error: {exc}",
                        chat_id,
                        inline_markup=main_inline_keyboard(paused=self.control.paused),
                    )
            else:
                self._send(
                    "💬 Type a trading question anytime — or tap <b>Ask coach</b> on the menu.",
                    chat_id,
                    inline_markup=main_inline_keyboard(paused=self.control.paused),
                )
            return
        parts = text.split()
        cmd = parts[0][1:].split("@", 1)[0].lower()
        args = parts[1:]
        if cmd == "closeall" and not args:
            self._send(
                "⚠️ <b>Close ALL open positions?</b>",
                chat_id,
                inline_markup=confirm_closeall_keyboard(),
            )
            return
        if cmd == "setrisk" and not args:
            self._send(
                "💸 <b>Set max $ risk per trade</b>\nTap a preset or type <code>/setrisk 300</code>",
                chat_id,
                inline_markup=setrisk_keyboard(),
            )
            return
        known = set(self.handlers) | {
            "closeall",
            "setrisk",
            "close",
            "help",
            "start",
            "menu",
            "status",
            "positions",
            "scan",
            "pause",
            "resume",
            "report",
            "journal",
            "pnl",
            "settings",
            "adopt",
        }
        if cmd not in known and self.chat_handler is not None:
            self.chat_handler(text, chat_id)
            return
        self._invoke(cmd, args, chat_id)

    def _invoke(self, cmd: str, args: list[str], chat_id: str) -> None:
        handler = self.handlers.get(cmd)
        if handler is None:
            self._send(
                f"Unknown command <code>/{cmd}</code>\nTap <b>📱 Menu</b> for buttons.",
                chat_id,
                inline_markup=main_inline_keyboard(paused=self.control.paused),
            )
            return
        try:
            handler(args, chat_id)
        except Exception as exc:
            logger.exception("Command /%s failed", cmd)
            self._send(f"⚠️ Command error: {exc}", chat_id)


def format_status_card(
    *,
    mode: str,
    equity: float,
    balance: float,
    snap: dict[str, Any],
    control: RuntimeControl,
    open_count: int,
    max_open: int,
) -> str:
    uptime = datetime.now(timezone.utc) - control.started_at
    hours = int(uptime.total_seconds() // 3600)
    mins = int((uptime.total_seconds() % 3600) // 60)
    pause_line = (
        f"⏸ <b>PAUSED</b> · {control.pause_reason or 'manual'}"
        if control.paused
        else "▶ Running"
    )
    risk_line = (
        f"${control.risk_money_override:,.0f} (override)"
        if control.risk_money_override
        else "config default"
    )
    return "\n".join(
        [
            "📊 <b>STATUS</b>",
            "",
            pause_line,
            f"Mode · <b>{mode}</b>",
            f"Equity · <b>${fmt_money(equity)}</b>",
            f"Balance · ${fmt_money(balance)}",
            f"Today · {fmt_money(float(snap.get('today_pnl') or 0), signed=True)}",
            (
                f"Win rate · <b>{float(snap.get('win_rate') or 0):.0f}%</b> "
                f"({int(snap.get('wins') or 0)}W / {int(snap.get('losses') or 0)}L)"
            ),
            f"Open · {open_count}/{max_open}",
            f"Risk $ · {risk_line}",
            f"Uptime · {hours}h {mins}m",
        ]
    )


def format_positions_card(
    positions: list[dict[str, Any]],
    *,
    external: list[dict[str, Any]] | None = None,
) -> str:
    external = external or []
    if not positions and not external:
        return (
            "📭 <b>No open positions</b>\n\n"
            "<i>Bot only manages trades it opened (or you Adopt). "
            "Manual MT5 trades show under External when present.</i>"
        )
    lines: list[str] = []
    if positions:
        lines.extend(["📂 <b>BOT-MANAGED</b>", ""])
        for p in positions:
            sym = str(p.get("symbol", ""))
            side = str(p.get("side", "")).upper()
            entry = float(p.get("entry") or 0)
            price = float(p.get("price") or 0)
            unreal = float(p.get("unrealized") or 0)
            sl = float(p.get("stop_loss") or 0)
            tp = float(p.get("take_profit") or 0)
            sign = "+" if unreal >= 0 else ""
            lines.append(
                f"<b>{sym}</b> {side}\n"
                f"<pre>"
                f"Entry  {fmt_price(sym, entry)}\n"
                f"Now    {fmt_price(sym, price)}\n"
                f"SL     {fmt_price(sym, sl)}\n"
                f"TP     {fmt_price(sym, tp)}\n"
                f"uPnL   {sign}{unreal:,.2f}"
                f"</pre>"
            )
    else:
        lines.extend(["📂 <b>BOT-MANAGED</b>", "None", ""])

    if external:
        lines.extend(["", "👤 <b>EXTERNAL (manual / other EA)</b>", ""])
        for p in external:
            sym = str(p.get("symbol", ""))
            side = str(p.get("side", "")).upper()
            entry = float(p.get("entry") or 0)
            price = float(p.get("price") or 0)
            unreal = float(p.get("unrealized") or 0)
            ticket = int(p.get("ticket") or 0)
            magic = int(p.get("magic") or 0)
            sign = "+" if unreal >= 0 else ""
            lines.append(
                f"<b>{sym}</b> {side} · ticket <code>{ticket}</code>\n"
                f"<pre>"
                f"Entry  {fmt_price(sym, entry)}\n"
                f"Now    {fmt_price(sym, price)}\n"
                f"Magic  {magic}\n"
                f"uPnL   {sign}{unreal:,.2f}"
                f"</pre>"
            )
        lines.append(
            "<i>Tap Adopt to let the bot manage SL/trail/close for that ticket.</i>"
        )
    return "\n".join(lines)


def format_scan_card(opps: list[Any], *, limit: int = 8) -> str:
    if not opps:
        return "🔎 <b>SCAN</b>\nNo opportunities scored."
    lines = ["🔎 <b>SCAN</b>", ""]
    for o in opps[:limit]:
        side = getattr(o, "side", "?")
        score = float(getattr(o, "score", 0) or 0)
        sym = getattr(o, "symbol", "?")
        allowed = "✅" if getattr(o, "allowed", False) else "⛔"
        bar = "▓" * round(score * 8) + "░" * (8 - round(score * 8))
        lines.append(
            f"{allowed} <b>{sym}</b> {str(side).upper()} · "
            f"{score:.2f} <code>{bar}</code>"
        )
    return "\n".join(lines)
