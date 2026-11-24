"""External venue and calibration integrations for ARGUS AI."""

from argus_ai.integrations.headline_arena import (
    CalibrationSummary,
    HeadlineArenaChallenge,
    HeadlineArenaClient,
    HeadlineArenaDesk,
    HeadlineArenaError,
    HeadlineArenaRunResult,
    TernaryForecast,
    compute_calibration_summary,
    generate_offline_macro_challenges,
    passport_to_ternary_forecast,
)

__all__ = [
    "CalibrationSummary",
    "HeadlineArenaChallenge",
    "HeadlineArenaClient",
    "HeadlineArenaDesk",
    "HeadlineArenaError",
    "HeadlineArenaRunResult",
    "TernaryForecast",
    "compute_calibration_summary",
    "generate_offline_macro_challenges",
    "passport_to_ternary_forecast",
]
