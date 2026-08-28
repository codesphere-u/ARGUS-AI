"""Gemini pre-trade advisor with optional Google Search grounding + alert composer."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from config import GEMINI, GeminiConfig

logger = logging.getLogger(__name__)

Decision = Literal["approve", "veto", "caution"]
SizeHint = Literal["full", "half", "skip"]

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:  # pragma: no cover
    genai = None  # type: ignore
    genai_types = None  # type: ignore


@dataclass
class TradeAdvice:
    """Structured Gemini pre-trade advice."""

    decision: Decision = "approve"
    confidence: float = 0.5
    risk_notes: list[str] = field(default_factory=list)
    size_hint: SizeHint = "full"
    headline: str = ""
    sources_summary: str = ""
    raw_text: str = ""
    from_cache: bool = False
    fallback: bool = False

    @property
    def lot_mult(self) -> float:
        if self.decision == "veto" or self.size_hint == "skip":
            return 0.0
        if self.size_hint == "half" or self.decision == "caution":
            return 0.5
        return 1.0

    @property
    def allow_entry(self) -> bool:
        return self.decision != "veto" and self.size_hint != "skip" and self.lot_mult > 0


def _extract_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {}
    # Strip markdown fences
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                return {}
    return {}


def _parse_advice(payload: dict[str, Any], raw: str, *, fallback: bool = False) -> TradeAdvice:
    decision = str(payload.get("decision", "approve")).lower().strip()
    if decision not in {"approve", "veto", "caution"}:
        decision = "approve"
    size_hint = str(payload.get("size_hint", "full")).lower().strip()
    if size_hint not in {"full", "half", "skip"}:
        size_hint = "full"
    notes = payload.get("risk_notes") or []
    if isinstance(notes, str):
        notes = [notes]
    conf = payload.get("confidence", 0.5)
    try:
        conf_f = float(conf)
    except (TypeError, ValueError):
        conf_f = 0.5
    return TradeAdvice(
        decision=decision,  # type: ignore[arg-type]
        confidence=max(0.0, min(1.0, conf_f)),
        risk_notes=[str(n) for n in notes][:6],
        size_hint=size_hint,  # type: ignore[arg-type]
        headline=str(payload.get("headline") or "")[:200],
        sources_summary=str(payload.get("sources_summary") or "")[:400],
        raw_text=raw,
        fallback=fallback,
    )


def _equity_band(equity: float) -> str:
    if equity < 1_000:
        return "micro"
    if equity < 10_000:
        return "small"
    if equity < 50_000:
        return "medium"
    return "large"


class GeminiAdvisor:
    """
    Pre-trade advisor + professional Telegram composer.

    Fail-open: API errors return approve/full so scanner+risk still run.
    """

    def __init__(self, cfg: GeminiConfig | None = None) -> None:
        self.cfg = cfg or GEMINI
        self._client = None
        self._cache: dict[str, tuple[float, TradeAdvice]] = {}
        if self.cfg.enabled and self.cfg.api_key and genai is not None:
            try:
                self._client = genai.Client(api_key=self.cfg.api_key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Gemini client init failed: %s", exc)
                self._client = None

    @property
    def available(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.api_key and self._client is not None)

    def _cache_get(self, key: str) -> TradeAdvice | None:
        hit = self._cache.get(key)
        if not hit:
            return None
        ts, advice = hit
        if time.time() - ts > self.cfg.cache_seconds:
            self._cache.pop(key, None)
            return None
        advice.from_cache = True
        return advice

    def _cache_set(self, key: str, advice: TradeAdvice) -> None:
        self._cache[key] = (time.time(), advice)

    def _generate(
        self,
        prompt: str,
        *,
        use_search: bool,
        max_output_tokens: int = 512,
        temperature: float = 0.2,
    ) -> str:
        if self._client is None or genai_types is None:
            raise RuntimeError("Gemini client unavailable")

        config_kwargs: dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
        }
        if use_search and self.cfg.grounded_search:
            try:
                config_kwargs["tools"] = [genai_types.Tool(google_search=genai_types.GoogleSearch())]
            except Exception:  # noqa: BLE001
                # Older SDK shapes
                try:
                    config_kwargs["tools"] = [{"google_search": {}}]
                except Exception:  # noqa: BLE001
                    pass

        # google-genai generate_content is sync; enforce soft timeout via wall clock check after
        t0 = time.time()
        response = self._client.models.generate_content(
            model=self.cfg.model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(**config_kwargs)
            if hasattr(genai_types, "GenerateContentConfig")
            else None,
        )
        elapsed = time.time() - t0
        if elapsed > self.cfg.timeout_seconds:
            logger.warning("Gemini call slow (%.1fs)", elapsed)
        text = getattr(response, "text", None) or ""
        if not text and getattr(response, "candidates", None):
            parts = []
            for cand in response.candidates:
                content = getattr(cand, "content", None)
                for part in getattr(content, "parts", None) or []:
                    if getattr(part, "text", None):
                        parts.append(part.text)
            text = "\n".join(parts)
        return text

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        use_search: bool = False,
        max_output_tokens: int = 900,
        temperature: float = 0.45,
    ) -> str:
        """
        Multi-turn chat helper for the desk coach.

        ``messages`` items: {"role": "system"|"user"|"model", "content": "..."}.
        """
        if not self.available:
            raise RuntimeError("Gemini chat unavailable")
        blocks: list[str] = []
        for m in messages:
            role = str(m.get("role") or "user").lower()
            content = str(m.get("content") or "").strip()
            if not content:
                continue
            label = {"system": "SYSTEM", "model": "ASSISTANT", "assistant": "ASSISTANT"}.get(
                role, "USER"
            )
            blocks.append(f"{label}:\n{content}")
        blocks.append("ASSISTANT:")
        prompt = "\n\n".join(blocks)
        return self._generate(
            prompt,
            use_search=use_search,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )

    def analyze_vision(
        self,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
        prompt: str = "Analyze this trading chart. Identify key levels, trend bias, and technical pattern.",
    ) -> str:
        """Analyze a chart image uploaded by the user via Telegram."""
        if not self.available or self._client is None or genai_types is None:
            raise RuntimeError("Gemini Vision unavailable")
        try:
            part = genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
            response = self._client.models.generate_content(
                model=self.cfg.model,
                contents=[part, prompt],
            )
            text = getattr(response, "text", None) or ""
            return text.strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Gemini Vision error: %s", exc)
            return f"Error analyzing image: {exc}"

    def advise_trade(self, context: dict[str, Any]) -> TradeAdvice:
        """
        Advise approve/veto/caution for one entry candidate.

        ``context`` keys: symbol, side, score, reasons, price, atr, rsi, ema_stack,
        macro, open_positions, equity.
        """
        symbol = str(context.get("symbol", ""))
        side = str(context.get("side", ""))
        cache_key = f"{symbol}:{side}:{round(float(context.get('score', 0)), 2)}"

        if not self.cfg.enabled:
            return TradeAdvice(
                decision="approve",
                size_hint="full",
                headline="Gemini disabled",
                fallback=True,
            )
        if not self.cfg.api_key or self._client is None:
            logger.warning("Gemini unavailable (no key/client); fail-open approve.")
            return TradeAdvice(
                decision="approve",
                size_hint="full",
                headline="Gemini offline — risk engine only",
                fallback=True,
            )

        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        equity = float(context.get("equity") or 0)
        prompt = f"""You are a professional FX/crypto risk advisor for an automated trading desk.
Hard risk limits (1.5% risk/trade, ATR stops, session/news blackouts) are enforced separately.
Your job: given live context + web search if available, decide whether this ENTRY is prudent NOW.

Return ONLY valid JSON with keys:
decision: "approve" | "veto" | "caution"
confidence: number 0-1
risk_notes: string array (max 4 short bullets)
size_hint: "full" | "half" | "skip"
headline: one short thesis line
sources_summary: one short sentence on macro/news takeaway

Rules:
- veto if imminent high-impact risk or thesis broken
- caution/half if mixed signals or elevated event risk
- approve/full if aligned and session healthy
- never invent account credentials; do not increase size above full

CONTEXT:
{json.dumps({
    "symbol": symbol,
    "side": side,
    "scanner_score": context.get("score"),
    "scanner_reasons": context.get("reasons"),
    "price": context.get("price"),
    "atr": context.get("atr"),
    "rsi": context.get("rsi"),
    "ema20": context.get("ema20"),
    "ema50": context.get("ema50"),
    "ema200": context.get("ema200"),
    "macro": context.get("macro"),
    "open_positions": context.get("open_positions"),
    "equity_band": _equity_band(equity),
}, default=str)}
"""
        try:
            raw = self._generate(prompt, use_search=True)
            advice = _parse_advice(_extract_json(raw), raw)
            self._cache_set(cache_key, advice)
            logger.info(
                "Gemini advice %s %s → %s/%s conf=%.2f",
                symbol,
                side,
                advice.decision,
                advice.size_hint,
                advice.confidence,
            )
            return advice
        except Exception as exc:  # noqa: BLE001
            logger.warning("Gemini advise_trade failed (%s); fail-open approve.", exc)
            return TradeAdvice(
                decision="approve",
                size_hint="full",
                headline="Advisor error — proceeding under risk engine",
                risk_notes=[str(exc)[:120]],
                fallback=True,
            )

    def compose_alert(self, event: str, details: dict[str, Any]) -> str:
        """
        Compose professional Telegram HTML for entry/exit/veto/summary.

        Falls back to a clean local template if Gemini is unavailable.
        """
        fallback = self._fallback_alert(event, details)
        if not self.available:
            return fallback

        prompt = f"""Write a concise professional Telegram trade desk alert in HTML (Telegram subset).
Use <b> sparingly. No emojis spam (at most one). No hype. Max 12 lines.
Event type: {event}
Details JSON: {json.dumps(details, default=str)}

Return ONLY the message body text (HTML allowed), no markdown fences."""
        try:
            raw = self._generate(prompt, use_search=False)
            text = (raw or "").strip()
            if text.startswith("```"):
                text = re.sub(r"^```\w*\n?", "", text)
                text = re.sub(r"\n?```$", "", text)
            return text.strip() or fallback
        except Exception as exc:  # noqa: BLE001
            logger.warning("Gemini compose_alert failed: %s", exc)
            return fallback

    def _fallback_alert(self, event: str, details: dict[str, Any]) -> str:
        symbol = details.get("symbol", "")
        if event == "entry":
            return (
                f"<b>{str(details.get('side', '')).upper()}</b> {symbol}\n"
                f"Entry: {details.get('entry')}\n"
                f"SL: {details.get('stop_loss')} | TP: {details.get('take_profit')}\n"
                f"Vol: {details.get('volume')} | Score: {details.get('score')}\n"
                f"{details.get('headline') or ''}\n"
                f"Equity: {details.get('equity')} ({details.get('mode')})"
            ).strip()
        if event == "exit":
            return (
                f"<b>EXIT</b> {symbol}\n"
                f"{details.get('reason')}\n"
                f"PnL: {details.get('pnl')} | Equity: {details.get('equity')}"
            )
        if event == "veto":
            return (
                f"<b>VETO</b> {symbol} {details.get('side')}\n"
                f"{details.get('headline')}\n"
                f"{'; '.join(details.get('risk_notes') or [])}"
            )
        return f"<b>{event}</b>\n{json.dumps(details, default=str)[:500]}"


# Module singleton
default_advisor = GeminiAdvisor()


def advise_trade(context: dict[str, Any]) -> TradeAdvice:
    return default_advisor.advise_trade(context)


def compose_alert(event: str, details: dict[str, Any]) -> str:
    return default_advisor.compose_alert(event, details)


def size_hint_to_lot_mult(hint: SizeHint | str, decision: Decision | str = "approve") -> float:
    if decision == "veto" or hint == "skip":
        return 0.0
    if hint == "half" or decision == "caution":
        return 0.5
    return 1.0
