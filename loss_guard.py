"""Trade stats tracker with optional size-down after loss streaks (no auto-pause)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class LossGuard:
    """
    Tracks win/loss streaks.

    Never auto-pauses entries. After consecutive losses, returns a size-down
    ``risk_scale`` so the live loop can trade smaller until a win resets.
    """

    consecutive_losses: int = 0
    consecutive_wins: int = 0
    pause_until: datetime | None = None
    session_peak_equity: float = 0.0
    defensive: bool = False
    closed_pnls: list[float] = field(default_factory=list)
    size_down_after_losses: int = 2
    size_down_scale: float = 0.5

    def on_session_start(self, equity: float) -> None:
        self.session_peak_equity = max(equity, self.session_peak_equity or equity)

    def update_equity(self, equity: float) -> None:
        self.session_peak_equity = max(self.session_peak_equity, equity)

    def record_close(self, pnl: float, *, now: datetime | None = None) -> dict[str, Any]:
        """Update streak counters after a realized close (no pauses)."""
        del now
        self.closed_pnls.append(float(pnl))
        if len(self.closed_pnls) > 50:
            self.closed_pnls = self.closed_pnls[-40:]

        if pnl > 0:
            self.consecutive_wins += 1
            self.consecutive_losses = 0
            self.defensive = False
            streak = f"W{self.consecutive_wins}"
        else:
            self.consecutive_losses += 1
            self.consecutive_wins = 0
            self.defensive = self.consecutive_losses >= self.size_down_after_losses
            streak = f"L{self.consecutive_losses}"
        scale = self.size_down_scale if self.defensive else 1.0
        logger.info(
            "Trade result PnL=%.2f streak=%s risk_scale=%.2f",
            pnl,
            streak,
            scale,
        )
        return {"pnl": pnl, "streak": streak, "defensive": self.defensive, "risk_scale": scale}

    def allow_new_entry(
        self,
        *,
        equity: float,
        score: float,
        open_count: int,
        now: datetime | None = None,
    ) -> tuple[bool, str, float]:
        """Always allow entries; shrink size after loss streak."""
        del score, open_count, now
        self.update_equity(equity)
        if self.consecutive_losses >= self.size_down_after_losses:
            self.defensive = True
            return True, f"size-down after L{self.consecutive_losses}", self.size_down_scale
        self.defensive = False
        return True, "ok", 1.0


def default_loss_guard_from_env() -> LossGuard:
    return LossGuard(
        size_down_after_losses=_env_int("LOSS_GUARD_SIZE_DOWN_AFTER", 2),
        size_down_scale=_env_float("LOSS_GUARD_SIZE_DOWN_SCALE", 0.5),
    )


default_loss_guard = default_loss_guard_from_env()
