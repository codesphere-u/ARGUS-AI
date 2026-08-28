import json
import logging
from typing import Dict, Any, Optional
from argus_ai.core.models import TradePassport

logger = logging.getLogger("argus.alerts")


class InteractiveAlertDispatcher:
    """
    Interactive Alert Dispatcher:
    Formats high-impact Trade Passport summaries for Telegram, Discord, or custom webhooks
    with actionable inline button payloads (Approve, Downsize, Veto, Kill Switch).
    """

    @staticmethod
    def format_telegram_alert(passport: TradePassport) -> Dict[str, Any]:
        """Format an interactive Telegram message payload with inline keyboard."""
        status_emojis = {
            "APPROVED": "✅ [APPROVED FOR ALPACA DISPATCH]",
            "DOWNSIZED": "⚠️ [APPROVED WITH 50% DOWNSIZE]",
            "VETOED": "❌ [VETOED BY DEVIL'S ADVOCATE]",
            "REJECTED": "⛔ [REJECTED - INSUFFICIENT EDGE]"
        }
        status_hdr = status_emojis.get(passport.consensus.status, passport.consensus.status)

        text = (
            f"🛡️ *ARGUS AI INVESTMENT COMMITTEE ALERT*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"*{status_hdr}*\n\n"
            f"📊 *Asset:* `{passport.ticker}` ({passport.direction})\n"
            f"💵 *Current Price:* `${passport.current_price:,.2f}`\n"
            f"🎯 *Target Entry:* `${passport.target_entry:,.2f}`\n"
            f"🛑 *Stop Loss (1.5x ATR):* `${passport.stop_loss:,.2f}`\n"
            f"🏆 *Take Profit (2:1+):* `${passport.take_profit:,.2f}`\n"
            f"⚖️ *Risk/Reward:* `{passport.risk_reward_ratio:.2f}:1`\n"
            f"💰 *Allocated Risk:* `${passport.account_risk_usd:,.2f} USD` ({passport.account_risk_pct:.2f}%)\n\n"
            f"😈 *Devil's Advocate Dissent:*\n"
            f"_{passport.devils_advocate.adversarial_thesis[:220]}..._\n\n"
            f"⚠️ *Identified Traps:* `{', '.join(passport.devils_advocate.identified_traps[:2]) or 'None'}`\n"
            f"🆔 *Passport ID:* `{passport.passport_id}`"
        )

        inline_keyboard = [
            [
                {"text": "⚡ Approve (Alpaca)", "callback_data": f"approve_{passport.passport_id}"},
                {"text": "⚠️ Cut Sizing 50%", "callback_data": f"downsize_{passport.passport_id}"}
            ],
            [
                {"text": "⛔ Manual Veto", "callback_data": f"veto_{passport.passport_id}"},
                {"text": "🚨 TRIP KILL SWITCH", "callback_data": "kill_switch_trip"}
            ]
        ]

        return {
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": {"inline_keyboard": inline_keyboard}
        }

    @staticmethod
    def format_discord_webhook(passport: TradePassport) -> Dict[str, Any]:
        """Format an embed payload for Discord webhook notifications."""
        color = 0x10B981 if passport.consensus.status == "APPROVED" else (
            0xF59E0B if passport.consensus.status == "DOWNSIZED" else 0xEF4444
        )

        return {
            "embeds": [
                {
                    "title": f"🛡️ Trade Passport: {passport.ticker} ({passport.direction})",
                    "description": passport.consensus.executive_summary,
                    "color": color,
                    "fields": [
                        {"name": "Status", "value": passport.consensus.status, "inline": True},
                        {"name": "Risk/Reward", "value": f"{passport.risk_reward_ratio:.2f}:1", "inline": True},
                        {"name": "Allocated USD", "value": f"${passport.position_size_usd:,.2f}", "inline": True},
                        {"name": "Stop Loss", "value": f"${passport.stop_loss:,.2f}", "inline": True},
                        {"name": "Take Profit", "value": f"${passport.take_profit:,.2f}", "inline": True},
                        {"name": "Devil's Advocate", "value": passport.devils_advocate.adversarial_thesis[:180] + "...", "inline": False}
                    ],
                    "footer": {"text": f"Passport ID: {passport.passport_id}"}
                }
            ]
        }
