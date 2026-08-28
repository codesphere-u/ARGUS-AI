"""Free economic-calendar blackouts and London/NY session filters."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from config import CALENDAR_CACHE_PATH, DATA_DIR, MACRO, MacroConfig

logger = logging.getLogger(__name__)

MACRO_FEATURE_KEYS = (
    "in_blackout",
    "mins_to_event_scaled",
    "in_london",
    "in_ny",
    "in_overlap",
)


@dataclass(frozen=True)
class CalendarEvent:
    """Normalized high-impact (or any) calendar event."""

    title: str
    country: str
    impact: str
    when: datetime


def _parse_event_time(raw: str) -> datetime | None:
    """Parse Forex Factory JSON date strings into UTC datetimes."""
    if not raw:
        return None
    raw = raw.strip()
    # Common formats: "07-28-2026T08:30:00-04:00", "2026-07-28T12:30:00Z"
    for fmt in (
        "%m-%d-%YT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%m-%d-%Y %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(raw.replace("Z", "+0000"), fmt) if "Z" in raw and "%z" in fmt else datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        # Fallback: fromisoformat with minor cleanup
        cleaned = raw.replace("Z", "+00:00")
        # Normalize -0400 → -04:00
        cleaned = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", cleaned)
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        logger.debug("Unparseable calendar date: %s", raw)
        return None


def _normalize_impact(value: str) -> str:
    v = (value or "").strip().lower()
    if v in {"high", "red", "3"}:
        return "High"
    if v in {"medium", "orange", "2"}:
        return "Medium"
    if v in {"low", "yellow", "1"}:
        return "Low"
    return value or "Unknown"


class MacroFilter:
    """
    Session + high-impact economic calendar gate.

    On calendar fetch failure, new entries are blocked (fail closed) when
    ``block_on_calendar_error`` is True.
    """

    def __init__(self, cfg: MacroConfig | None = None, cache_path: Path | None = None) -> None:
        self.cfg = cfg or MACRO
        self.cache_path = cache_path or CALENDAR_CACHE_PATH
        self._events: list[CalendarEvent] = []
        self._loaded_at: datetime | None = None
        self._calendar_ok = False
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    def _cache_fresh(self, now: datetime) -> bool:
        if self._loaded_at is not None and self._calendar_ok:
            age = (now - self._loaded_at).total_seconds()
            if age < self.cfg.cache_refresh_seconds:
                return True
        # Prefer fresh on-disk cache to avoid rate limits during training
        if self.cache_path.exists():
            try:
                mtime = datetime.fromtimestamp(self.cache_path.stat().st_mtime, tz=timezone.utc)
                if (now - mtime).total_seconds() < self.cfg.cache_refresh_seconds:
                    return False  # not in-memory fresh, but disk is — load disk without network
            except OSError:
                pass
        return False

    def _disk_is_fresh(self, now: datetime) -> bool:
        if not self.cache_path.exists():
            return False
        try:
            mtime = datetime.fromtimestamp(self.cache_path.stat().st_mtime, tz=timezone.utc)
            return (now - mtime).total_seconds() < self.cfg.cache_refresh_seconds
        except OSError:
            return False

    def _load_from_disk(self) -> list[dict[str, Any]] | None:
        if not self.cache_path.exists():
            return None
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and "events" in payload:
                return payload["events"]
            if isinstance(payload, list):
                return payload
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Calendar cache read failed: %s", exc)
        return None

    def _save_to_disk(self, events: list[dict[str, Any]]) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "events": events}),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Calendar cache write failed: %s", exc)

    def _fetch_remote(self) -> list[dict[str, Any]]:
        resp = requests.get(self.cfg.calendar_url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            raise ValueError("Unexpected calendar JSON shape")
        return data

    def _ingest_raw(self, raw_events: list[dict[str, Any]], now: datetime, ok: bool) -> None:
        parsed: list[CalendarEvent] = []
        for item in raw_events or []:
            country = str(item.get("country") or item.get("currency") or "").upper()
            impact = _normalize_impact(str(item.get("impact") or ""))
            when = _parse_event_time(str(item.get("date") or item.get("datetime") or ""))
            if when is None:
                continue
            title = str(item.get("title") or item.get("event") or "Event")
            parsed.append(CalendarEvent(title=title, country=country, impact=impact, when=when))
        self._events = sorted(parsed, key=lambda e: e.when)
        self._loaded_at = now
        self._calendar_ok = ok

    def refresh(self, now: datetime | None = None, force: bool = False) -> None:
        """Refresh calendar events from cache/network."""
        now = now or datetime.now(timezone.utc)
        if not force and self._loaded_at is not None and self._calendar_ok:
            age = (now - self._loaded_at).total_seconds()
            if age < self.cfg.cache_refresh_seconds:
                return

        # Use on-disk cache when fresh (critical during RL training loops)
        if not force and self._disk_is_fresh(now):
            raw = self._load_from_disk()
            if raw is not None:
                self._ingest_raw(raw, now, ok=True)
                return

        raw_events: list[dict[str, Any]] | None = None
        try:
            raw_events = self._fetch_remote()
            self._save_to_disk(raw_events)
            self._ingest_raw(raw_events, now, ok=True)
            logger.info("Calendar loaded: %s events (ok=%s)", len(self._events), self._calendar_ok)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("Calendar fetch failed (%s); trying disk cache.", exc)
            raw_events = self._load_from_disk()
            if raw_events is None:
                self._events = []
                self._loaded_at = now
                self._calendar_ok = False
                return
            self._ingest_raw(raw_events, now, ok=True)
            logger.info("Calendar loaded from disk: %s events", len(self._events))

    def currencies_for_symbol(self, symbol: str) -> set[str]:
        """
        Infer quote currencies from a symbol like EURUSDm / BTCUSDm.

        Always-liquid crypto/metals use only their own legs (no default EUR union),
        so BTC is not blocked by EUR high-impact news.
        FX pairs still union with MACRO.currencies.
        """
        from live_brain import currency_codes_for_symbol, is_always_liquid

        codes = currency_codes_for_symbol(symbol)
        if is_always_liquid(symbol):
            return codes if codes else set(self.cfg.currencies)

        found = set(self.cfg.currencies)
        if codes:
            found |= codes
        else:
            base = re.sub(r"[^A-Za-z]", "", symbol).upper()
            if len(base) >= 6:
                found.add(base[:3])
                found.add(base[3:6])
        return found

    def high_impact_events(
        self,
        now: datetime | None = None,
        symbol: str = "EURUSDm",
    ) -> list[CalendarEvent]:
        now = now or datetime.now(timezone.utc)
        self.refresh(now)
        currencies = self.currencies_for_symbol(symbol)
        return [
            e
            for e in self._events
            if e.impact == "High" and (e.country in currencies or e.country.upper() in currencies)
        ]

    def session_flags(self, now: datetime | None = None) -> dict[str, bool]:
        now = now or datetime.now(timezone.utc)
        hour = now.astimezone(timezone.utc).hour + now.minute / 60.0
        in_london = self.cfg.london_start_hour <= hour < self.cfg.london_end_hour
        in_ny = self.cfg.ny_start_hour <= hour < self.cfg.ny_end_hour
        return {
            "in_london": in_london,
            "in_ny": in_ny,
            "in_overlap": in_london and in_ny,
        }

    def in_session(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        mode = (self.cfg.session_mode or "london_ny").lower()
        flags = self.session_flags(now)
        if mode in {"off", "always", "all"}:
            return True
        if mode == "overlap":
            return flags["in_overlap"]
        # default london_ny
        return flags["in_london"] or flags["in_ny"]

    @staticmethod
    def is_fx_market_open(now: datetime | None = None) -> bool:
        """
        FX weekend gate (UTC).

        Closed from Friday 20:00 UTC through Sunday 22:00 UTC
        (pre-close buffer + illiquid reopen buffer). Crypto/gold
        callers should bypass via ``is_always_liquid``.
        """
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        wd = now.weekday()  # Mon=0 … Sun=6
        hour = now.hour + now.minute / 60.0
        if wd == 4 and hour >= 20.0:  # Friday after 20:00
            return False
        if wd == 5:  # Saturday
            return False
        if wd == 6 and hour < 22.0:  # Sunday before 22:00
            return False
        return True

    def blackout_status(
        self,
        now: datetime | None = None,
        symbol: str = "EURUSDm",
    ) -> tuple[bool, str | None, float]:
        """
        Return (in_blackout, event_title, minutes_to_nearest_high).

        ``minutes_to_nearest_high`` is signed (negative = after event) for the
        nearest high-impact event in time; large sentinel if none.
        """
        now = now or datetime.now(timezone.utc)
        self.refresh(now)
        if not self._calendar_ok and self.cfg.block_on_calendar_error:
            return True, "calendar_unavailable", 0.0

        window = timedelta(minutes=self.cfg.blackout_minutes)
        nearest_mins = 1e9
        active_title: str | None = None
        in_blackout = False

        for event in self.high_impact_events(now, symbol):
            delta = event.when - now
            mins = delta.total_seconds() / 60.0
            if abs(mins) < abs(nearest_mins):
                nearest_mins = mins
            if event.when - window <= now <= event.when + window:
                in_blackout = True
                active_title = event.title

        if nearest_mins >= 1e8:
            nearest_mins = 24 * 60.0  # no event → far away
        return in_blackout, active_title, float(nearest_mins)

    def is_trading_allowed(
        self,
        now: datetime | None = None,
        symbol: str = "EURUSDm",
        *,
        allow_exits: bool = False,
    ) -> tuple[bool, str]:
        """
        Gate for new entries.

        Protective exits should bypass this (``allow_exits`` is informational;
        callers decide). Returns (allowed, reason).
        """
        del allow_exits  # documented for callers
        now = now or datetime.now(timezone.utc)
        from live_brain import is_always_liquid

        if not is_always_liquid(symbol) and not self.is_fx_market_open(now):
            return False, "FX market closed (weekend gate)"
        if not is_always_liquid(symbol) and not self.in_session(now):
            return False, "Outside London/NY session"
        in_blackout, title, _ = self.blackout_status(now, symbol)
        if in_blackout:
            why = title or "high-impact event"
            return False, f"Calendar blackout: {why}"
        return True, "ok"

    def macro_features(
        self,
        now: datetime | None = None,
        symbol: str = "EURUSDm",
    ) -> dict[str, float]:
        """
        Observation extras for the RL policy.

        Keys: in_blackout, mins_to_event_scaled, in_london, in_ny, in_overlap.
        """
        now = now or datetime.now(timezone.utc)
        flags = self.session_flags(now)
        in_blackout, _, mins = self.blackout_status(now, symbol)
        # Scale minutes into roughly [-1, 1] over ±blackout*4 window (clip)
        scale = max(self.cfg.blackout_minutes * 4, 1)
        mins_scaled = float(max(-1.0, min(1.0, mins / scale)))
        return {
            "in_blackout": 1.0 if in_blackout else 0.0,
            "mins_to_event_scaled": mins_scaled,
            "in_london": 1.0 if flags["in_london"] else 0.0,
            "in_ny": 1.0 if flags["in_ny"] else 0.0,
            "in_overlap": 1.0 if flags["in_overlap"] else 0.0,
        }

    def macro_feature_vector(
        self,
        now: datetime | None = None,
        symbol: str = "EURUSDm",
    ) -> list[float]:
        feats = self.macro_features(now, symbol)
        return [feats[k] for k in MACRO_FEATURE_KEYS]


# Module-level singleton for convenience
default_macro_filter = MacroFilter()


def is_trading_allowed(
    now: datetime | None = None,
    symbol: str = "EURUSDm",
) -> tuple[bool, str]:
    return default_macro_filter.is_trading_allowed(now, symbol)


def macro_features(
    now: datetime | None = None,
    symbol: str = "EURUSDm",
) -> dict[str, float]:
    return default_macro_filter.macro_features(now, symbol)
