"""RIA Desk coach — conversational trading help over Telegram (Gemini-backed)."""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from config import BRAND_NAME, GEMINI, RISK
from gemini_advisor import GeminiAdvisor, default_advisor
from telegram_format import copy_line, fmt_money, fmt_price, sanitize_telegram_html

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = f"""You are the {BRAND_NAME} Desk Coach — a practical FX/crypto trading assistant
paired with an automated MT5 trading bot.

Voice: clear, calm, professional. No hype. No emojis spam (at most one).
Format: Telegram HTML only (<b>, <i>, <code>, <pre>). No markdown fences.
Length: usually ≤15 short lines unless the user asks for a lesson.

You can:
- Answer trading questions (risk, R:R, ATR stops, sessions, indicators)
- Help set up MT5 (AutoTrading ON), Exness symbols (*m suffix), Telegram buttons
- Teach short lessons (risk %, stop placement, copy-trading workflow)
- Explain THIS bot's live state from CONTEXT (positions, scores, mode, equity)

Hard rules:
- Never claim you placed/closed a trade from chat. Control is via buttons: Pause, Resume, Close, Status.
- Never invent fill prices or PnL — use CONTEXT only.
- If asked to buy/sell/close now, explain how to use the buttons or copy line — do not pretend execution.
- Educational only; remind briefly that trading involves risk when giving strategy advice.
- Prefer actionable next steps.
"""

FALLBACK_HELP = f"""\
💬 <b>{BRAND_NAME} Coach</b> (offline)

I can still help with the basics:

• Tap <b>📊 Status</b> / <b>📂 Positions</b> / <b>🔎 Scan</b>
• Tap <b>How to copy</b> for MT5 paste lines
• Enable Gemini (<code>GEMINI_API_KEY</code>) for full chat coaching
• MT5: turn <b>Algo Trading</b> green, allow algorithmic trading

Ask again after the advisor is online — or use the control buttons below.
"""

TEACH_STARTER = (
    "Teach me a short lesson: risk per trade, stop-loss with ATR, and why R:R matters. "
    "Keep it practical for this bot's settings."
)

COACH_STARTER = (
    "Say hello as the desk coach. Briefly tell me what you can help with "
    "(questions, setup, copy trades, teaching), and invite one question."
)

COPY_STARTER = (
    "Explain how to copy-trade from this bot into MT5 step by step. "
    "Use any copy lines provided in CONTEXT."
)


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def format_copy_desk(
    *,
    positions: list[dict[str, Any]] | None = None,
    scans: list[dict[str, Any]] | None = None,
    sl_atr_mult: float | None = None,
    tp_atr_mult: float | None = None,
) -> str:
    """Deterministic copy-trade card from open positions or top scans."""
    sl_m = float(sl_atr_mult if sl_atr_mult is not None else RISK.stop_loss_atr_mult)
    tp_m = float(tp_atr_mult if tp_atr_mult is not None else RISK.take_profit_atr_mult)
    lines = ["📋 <b>COPY DESK</b>", ""]

    if positions:
        lines.append("<b>Open trades</b> — long-press a line to copy:")
        for p in positions[:6]:
            sym = str(p.get("symbol", ""))
            side = str(p.get("side") or "buy")
            entry = float(p.get("entry") or 0)
            sl = float(p.get("stop_loss") or 0)
            tp = float(p.get("take_profit") or 0)
            vol = p.get("volume")
            if entry <= 0:
                continue
            clip = copy_line(
                side=side,
                symbol=sym,
                entry=entry,
                stop_loss=sl,
                take_profit=tp,
                volume=float(vol) if vol is not None else None,
            )
            lines.append(f"<code>{clip}</code>")
            if p.get("unrealized") is not None:
                lines.append(f"<i>uPnL {fmt_money(float(p['unrealized']), signed=True)}</i>")
        lines.append("")
        lines.append(
            "<i>MT5: New Order → paste levels → confirm. Match lot size to your risk.</i>"
        )
        return "\n".join(lines)

    if scans:
        lines.append("<b>Top setups</b> — provisional SL/TP from ATR:")
        for s in scans[:3]:
            sym = str(s.get("symbol", ""))
            side = str(s.get("side") or "buy")
            price = float(s.get("price") or 0)
            atr = float(s.get("atr") or 0)
            score = float(s.get("score") or 0)
            if price <= 0 or atr <= 0:
                continue
            if side == "sell":
                sl, tp = price + sl_m * atr, price - tp_m * atr
            else:
                sl, tp = price - sl_m * atr, price + tp_m * atr
            clip = copy_line(
                side=side, symbol=sym, entry=price, stop_loss=sl, take_profit=tp
            )
            lines.append(f"Score {score:.2f} · <code>{clip}</code>")
        lines.append("")
        lines.append(
            "<i>These are scanner levels, not filled orders. Confirm on your chart.</i>"
        )
        return "\n".join(lines)

    return (
        "📋 <b>COPY DESK</b>\n\n"
        "No open positions and no scan levels yet.\n"
        "Tap <b>🔎 Scan</b> or wait for an entry alert, then ask again."
    )


@dataclass
class DeskCoach:
    """Gemini-backed coach with short per-chat memory."""

    advisor: GeminiAdvisor = field(default_factory=lambda: default_advisor)
    max_turns: int = 8
    max_per_minute: int = 8
    _memory: dict[str, deque[dict[str, str]]] = field(
        default_factory=lambda: defaultdict(lambda: deque(maxlen=16))
    )
    _rate: dict[str, deque[float]] = field(
        default_factory=lambda: defaultdict(deque)
    )

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.advisor.cfg, "desk_coach_enabled", True))

    def clear(self, chat_id: str) -> None:
        self._memory.pop(str(chat_id), None)

    def _history(self, chat_id: str) -> list[dict[str, str]]:
        return list(self._memory[str(chat_id)])

    def _remember(self, chat_id: str, role: str, content: str) -> None:
        mem = self._memory[str(chat_id)]
        mem.append({"role": role, "content": content[:2000]})
        # Keep last max_turns*2 messages
        while len(mem) > self.max_turns * 2:
            mem.popleft()

    def chat(
        self,
        user_text: str,
        chat_id: str,
        *,
        context: dict[str, Any] | None = None,
        intent: str | None = None,
    ) -> str:
        """
        Answer a user message. ``intent`` may be coach|teach|copy|None.
        Rate-limited to ~8 messages / minute / chat.
        """
        text = (user_text or "").strip()
        if not text:
            return "Send a question — trading, setup, copy trades, or ask me to teach."

        cid = str(chat_id)
        now = time.monotonic()
        bucket = self._rate[cid]
        while bucket and now - bucket[0] > 60.0:
            bucket.popleft()
        if len(bucket) >= self.max_per_minute:
            return (
                "⏳ <b>Slow down</b> — too many coach messages.\n"
                "Wait a minute, then ask again."
            )
        bucket.append(now)

        ctx = context or {}
        copy_block = ""
        intent_l = (intent or "").lower()
        wants_copy = intent_l == "copy" or any(
            k in text.lower()
            for k in ("copy", "paste", "mt5 signal", "how to copy", "copy trade")
        )
        if wants_copy:
            copy_block = format_copy_desk(
                positions=list(ctx.get("positions") or []),
                scans=list(ctx.get("scans") or []),
            )

        if not self.enabled:
            return copy_block or FALLBACK_HELP
        if not self.advisor.available:
            return (copy_block + "\n\n" if copy_block else "") + FALLBACK_HELP

        ctx_blob = self._format_context(ctx)
        if copy_block:
            ctx_blob += "\n\nCOPY_CARD:\n" + copy_block

        intent_note = ""
        if intent_l == "teach":
            intent_note = "Mode: TEACH — deliver a short lesson."
        elif intent_l == "copy":
            intent_note = "Mode: COPY — focus on copy-trading / MT5 paste workflow."
        elif intent_l == "coach":
            intent_note = "Mode: INTRO — greet and offer help."
        elif intent_l == "setup":
            intent_note = "Mode: SETUP — MT5 / Telegram / bot configuration help."

        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "system",
                "content": f"{intent_note}\n\nLIVE CONTEXT:\n{ctx_blob}".strip(),
            },
        ]
        messages.extend(self._history(chat_id))
        messages.append({"role": "user", "content": text})

        try:
            raw = self.advisor.chat(messages, use_search=False)
            reply = _strip_fences(raw)
            if not reply:
                reply = FALLBACK_HELP
            # Prefer appending deterministic copy card when user asked for copy
            if wants_copy and copy_block and "COPY" not in reply.upper():
                reply = reply.rstrip() + "\n\n" + copy_block
            reply = sanitize_telegram_html(reply)
            self._remember(chat_id, "user", text)
            self._remember(chat_id, "model", reply)
            return reply
        except Exception as exc:  # noqa: BLE001
            logger.warning("Desk coach chat failed: %s", exc)
            if copy_block:
                return sanitize_telegram_html(copy_block)
            return sanitize_telegram_html(FALLBACK_HELP)

    def analyze_photo(
        self,
        image_bytes: bytes,
        chat_id: str,
        caption: str = "",
        mime_type: str = "image/jpeg",
    ) -> str:
        """Analyze a chart photo sent by a user in Telegram."""
        if not self.enabled or not self.advisor.available:
            return "📸 <i>Gemini Vision is currently offline or unconfigured.</i>"
        prompt = (
            caption
            or "Analyze this trading chart. Identify key levels, trend bias, and potential trade setups."
        )
        prompt += "\n\nFormat reply in clear, professional Telegram HTML (<b>, <i>, <code>). Max 15 lines."
        try:
            raw = self.advisor.analyze_vision(image_bytes, mime_type=mime_type, prompt=prompt)
            clean = sanitize_telegram_html(raw)
            self._remember(chat_id, "user", f"[User sent a chart photo] {caption}")
            self._remember(chat_id, "model", clean)
            return f"🖼️ <b>CHART VISION ANALYSIS</b>\n\n{clean}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Vision analysis failed: %s", exc)
            return f"⚠️ <i>Chart analysis error: {exc}</i>"

    def _format_context(self, ctx: dict[str, Any]) -> str:
        parts = [
            f"brand={BRAND_NAME}",
            f"mode={ctx.get('mode', '?')}",
            f"paused={ctx.get('paused', False)}",
            f"equity={ctx.get('equity')}",
            f"balance={ctx.get('balance')}",
            f"open_count={ctx.get('open_count')}/{ctx.get('max_open')}",
            f"risk_money_override={ctx.get('risk_money_override')}",
            f"sl_atr={ctx.get('sl_atr')} tp_atr={ctx.get('tp_atr')}",
            f"hot_grab={ctx.get('hot_grab')} min_score={ctx.get('min_score')}",
        ]
        positions = ctx.get("positions") or []
        if positions:
            parts.append("positions:")
            for p in positions[:8]:
                parts.append(
                    f"  {p.get('symbol')} {p.get('side')} entry={p.get('entry')} "
                    f"sl={p.get('stop_loss')} tp={p.get('take_profit')} "
                    f"uPnL={p.get('unrealized')}"
                )
        else:
            parts.append("positions: none")
        scans = ctx.get("scans") or []
        if scans:
            parts.append("top_scans:")
            for s in scans[:5]:
                parts.append(
                    f"  {s.get('symbol')} {s.get('side')} score={s.get('score')} "
                    f"price={s.get('price')} atr={s.get('atr')}"
                )
        return "\n".join(str(p) for p in parts)


default_coach = DeskCoach()
