"""Central configuration for the RL Forex trading bot."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
MODELS_DIR = BASE_DIR / "models"
DATA_DIR = BASE_DIR / "data"
JOURNAL_DIR = DATA_DIR / "journal"
CALENDAR_CACHE_PATH = DATA_DIR / "ff_calendar_cache.json"

# Public brand shown in Telegram / charts
BRAND_NAME = "Alvin Hacks FX"
BRAND_SHORT = "Alvin Hacks FX"


def _env_bool(key: str, default: bool = True) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class RiskConfig:
    """Hard risk-management constants (mandatory override layer)."""

    max_risk_per_trade: float = field(
        default_factory=lambda: _env_float("MAX_RISK_PER_TRADE", 0.01)
    )  # 1% of equity (capital protection)
    stop_loss_atr_mult: float = field(
        default_factory=lambda: _env_float("STOP_LOSS_ATR_MULT", 1.5)
    )
    take_profit_atr_mult: float = field(
        default_factory=lambda: _env_float("TAKE_PROFIT_ATR_MULT", 2.5)
    )
    trailing_activation_atr_mult: float = 1.0
    daily_loss_limit: float = field(
        default_factory=lambda: _env_float("DAILY_LOSS_LIMIT", 0.03)
    )  # 3% daily loss before halt when CB enabled
    circuit_breaker_hours: int = field(
        default_factory=lambda: _env_int("CIRCUIT_BREAKER_HOURS", 1)
    )
    circuit_breaker_enabled: bool = field(
        default_factory=lambda: _env_bool("CIRCUIT_BREAKER_ENABLED", True)
    )
    circuit_breaker_flatten: bool = field(
        default_factory=lambda: _env_bool("CIRCUIT_BREAKER_FLATTEN", True)
    )
    transaction_fee: float = 0.00005  # ~0.5 pip round-ish (fractional)
    max_lot: float = field(default_factory=lambda: _env_float("MAX_LOT", 2.0))
    max_risk_money: float = field(
        default_factory=lambda: _env_float("MAX_RISK_MONEY", 500.0)
    )  # hard $ cap so huge demo balances don't blow lot size
    max_spread_atr_fraction: float = field(
        default_factory=lambda: _env_float("MAX_SPREAD_ATR_FRACTION", 0.15)
    )
    max_currency_exposure: int = field(
        default_factory=lambda: _env_int("MAX_CURRENCY_EXPOSURE", 2)
    )


@dataclass(frozen=True)
class QualityConfig:
    """Strict entry confluence for higher win-rate targeting."""

    min_entry_score: float = field(
        default_factory=lambda: _env_float("MIN_ENTRY_SCORE", 0.72)
    )
    max_extension_atr: float = 1.0
    rsi_buy_low: float = 42.0
    rsi_buy_high: float = 65.0
    rsi_sell_low: float = 35.0
    rsi_sell_high: float = 58.0
    require_overlap: bool = field(
        default_factory=lambda: _env_bool("QUALITY_REQUIRE_OVERLAP", False)
    )
    fast_entry_score: float = field(
        default_factory=lambda: _env_float("FAST_ENTRY_SCORE", 0.88)
    )
    gemini_fast_skip: bool = field(
        default_factory=lambda: _env_bool("GEMINI_FAST_SKIP", True)
    )
    hot_grab_score: float = field(
        default_factory=lambda: _env_float("HOT_GRAB_SCORE", 0.85)
    )


@dataclass(frozen=True)
class DataConfig:
    """Market data and feature-engineering settings."""

    symbol: str = field(default_factory=lambda: os.getenv("SYMBOL", "EURUSDm"))
    timeframe_primary: str = "15m"
    timeframe_context: str = "1h"
    ema_periods: tuple[int, ...] = (20, 50, 200)
    rsi_period: int = 14
    atr_period: int = 14
    volatility_window: int = 20
    history_months: int = 12
    lookback_bars: int = 500


@dataclass(frozen=True)
class MacroConfig:
    """Economic calendar blackout and session filters."""

    calendar_url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    blackout_minutes: int = field(
        default_factory=lambda: _env_int("CALENDAR_BLACKOUT_MINUTES", 30)
    )
    session_mode: str = field(
        default_factory=lambda: os.getenv("SESSION_MODE", "always")
    )
    # London 07:00–16:00 UTC, New York 12:00–21:00 UTC
    london_start_hour: int = 7
    london_end_hour: int = 16
    ny_start_hour: int = 12
    ny_end_hour: int = 21
    cache_refresh_seconds: int = 3600
    # Fail closed: block new entries if calendar cannot be loaded
    block_on_calendar_error: bool = True
    currencies: tuple[str, ...] = ("USD", "EUR")


@dataclass(frozen=True)
class OnlineLearnConfig:
    """Daily online PPO fine-tuning settings."""

    enabled: bool = field(default_factory=lambda: _env_bool("ONLINE_LEARN_ENABLED", True))
    max_steps: int = field(default_factory=lambda: _env_int("ONLINE_LEARN_MAX_STEPS", 4096))
    min_trades: int = field(default_factory=lambda: _env_int("ONLINE_LEARN_MIN_TRADES", 5))
    learning_rate: float = field(default_factory=lambda: _env_float("ONLINE_LEARN_LR", 1e-5))
    lookback_days: int = 7
    smoke_bars: int = 500
    sharpe_collapse_delta: float = 0.5  # rollback if new Sharpe is worse by this much
    micro_steps: int = field(default_factory=lambda: _env_int("ONLINE_MICRO_STEPS", 2048))
    closes_trigger: int = field(
        default_factory=lambda: _env_int("ONLINE_LEARN_ON_CLOSES", 3)
    )
    background: bool = field(
        default_factory=lambda: _env_bool("ONLINE_LEARN_BACKGROUND", True)
    )


def _env_symbols(key: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(key, default)
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return tuple(parts) if parts else tuple(p.strip() for p in default.split(","))


@dataclass(frozen=True)
class MultiPairConfig:
    """Multi-symbol scanning, opportunity alerts, and rocket exits."""

    symbols: tuple[str, ...] = field(
        default_factory=lambda: _env_symbols(
            "SYMBOLS",
            "EURUSDm,GBPUSDm,USDJPYm,AUDUSDm,USDCADm,NZDUSDm,USDCHFm,EURJPYm,GBPJPYm,"
            "XAUUSDm,XAGUSDm,BTCUSDm,ETHUSDm,SOLUSDm,XRPUSDm,LTCUSDm,BNBUSDm",
        )
    )
    max_open_positions: int = field(
        default_factory=lambda: _env_int("MAX_OPEN_POSITIONS", 4)
    )
    opportunity_alert_min: float = field(
        default_factory=lambda: _env_float("OPPORTUNITY_ALERT_MIN", 0.85)
    )
    rocket_pullback_pct: float = field(
        default_factory=lambda: _env_float("ROCKET_PULLBACK_PCT", 0.30)
    )
    rocket_be_atr: float = field(default_factory=lambda: _env_float("ROCKET_BE_ATR", 0.8))
    rocket_scalp_atr: float = field(
        default_factory=lambda: _env_float("ROCKET_SCALP_ATR", 2.0)
    )
    rocket_min_pullback_atr: float = 0.5
    rocket_hard_loss_r: float = field(
        default_factory=lambda: _env_float("ROCKET_HARD_LOSS_R", 0.8)
    )
    rocket_time_bars: int = 16
    rocket_stale_profit_atr: float = 0.3
    # Stalled-trade fallback only (not the primary win mechanism)
    rocket_time_profit_close_bars: int = field(
        default_factory=lambda: _env_int("ROCKET_TIME_PROFIT_BARS", 24)
    )
    rocket_time_profit_min_atr: float = field(
        default_factory=lambda: _env_float("ROCKET_TIME_PROFIT_MIN_ATR", 0.4)
    )
    rocket_trail_atr: float = field(
        default_factory=lambda: _env_float("ROCKET_TRAIL_ATR", 1.0)
    )
    entries_per_cycle: int = field(
        default_factory=lambda: _env_int("ENTRIES_PER_CYCLE", 2)
    )
    opportunity_alert_cooldown_min: float = field(
        default_factory=lambda: _env_float("OPPORTUNITY_ALERT_COOLDOWN_MIN", 30.0)
    )
    reentry_cooldown_loss_min: float = field(
        default_factory=lambda: _env_float("REENTRY_COOLDOWN_MIN", 20.0)
    )
    reentry_cooldown_win_min: float = field(
        default_factory=lambda: _env_float("REENTRY_COOLDOWN_WIN_MIN", 5.0)
    )
    desk_pulse_hours: float = field(
        default_factory=lambda: _env_float("DESK_PULSE_HOURS", 1.0)
    )


@dataclass(frozen=True)
class GeminiConfig:
    """Gemini pre-trade advisor, alert composer, and desk coach chat."""

    enabled: bool = field(default_factory=lambda: _env_bool("GEMINI_ENABLED", True))
    api_key: str | None = field(default_factory=lambda: os.getenv("GEMINI_API_KEY") or None)
    model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-3.6-flash"))
    cache_seconds: int = field(default_factory=lambda: _env_int("GEMINI_CACHE_SECONDS", 180))
    grounded_search: bool = field(
        default_factory=lambda: _env_bool("GEMINI_GROUNDED_SEARCH", True)
    )
    timeout_seconds: float = 12.0
    desk_coach_enabled: bool = field(
        default_factory=lambda: _env_bool("DESK_COACH_ENABLED", True)
    )


@dataclass(frozen=True)
class ChannelConfig:
    """Public Telegram channel progress updates (non-spam digests)."""

    enabled: bool = field(default_factory=lambda: _env_bool("CHANNEL_UPDATES_ENABLED", True))
    channel_id: str | None = field(
        default_factory=lambda: os.getenv("TELEGRAM_CHANNEL_ID") or None
    )
    digest_hours: float = field(
        default_factory=lambda: _env_float("CHANNEL_DIGEST_HOURS", 4.0)
    )
    milestone_pct: float = 0.005  # 0.5% above last posted high
    daily_wrap_hour_utc: int = 21
    state_path: Path = field(default_factory=lambda: DATA_DIR / "channel_state.json")
    # Free high-confidence predictions (public, rate-limited)
    pred_enabled: bool = field(
        default_factory=lambda: _env_bool("CHANNEL_PRED_ENABLED", True)
    )
    pred_min_score: float = field(
        default_factory=lambda: _env_float("CHANNEL_PRED_MIN_SCORE", 0.75)
    )
    pred_cooldown_hours: float = field(
        default_factory=lambda: _env_float("CHANNEL_PRED_COOLDOWN_HOURS", 3.0)
    )
    pred_max_per_day: int = field(
        default_factory=lambda: _env_int("CHANNEL_PRED_MAX_PER_DAY", 3)
    )


@dataclass(frozen=True)
class BotConfig:
    """Runtime bot settings loaded from environment."""

    paper_trading: bool = field(default_factory=lambda: _env_bool("PAPER_TRADING", True))
    symbol: str = field(default_factory=lambda: os.getenv("SYMBOL", "EURUSDm"))
    model_path: Path = field(
        default_factory=lambda: Path(os.getenv("MODEL_PATH", str(MODELS_DIR / "ppo_forex.zip")))
    )
    mt5_login: int | None = field(
        default_factory=lambda: int(os.getenv("MT5_LOGIN")) if os.getenv("MT5_LOGIN") else None
    )
    mt5_password: str | None = field(default_factory=lambda: os.getenv("MT5_PASSWORD") or None)
    mt5_server: str | None = field(default_factory=lambda: os.getenv("MT5_SERVER") or None)
    mt5_path: str | None = field(default_factory=lambda: os.getenv("MT5_PATH") or None)
    telegram_bot_token: str | None = field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN") or None
    )
    telegram_chat_id: str | None = field(
        default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID") or None
    )
    telegram_channel_id: str | None = field(
        default_factory=lambda: os.getenv("TELEGRAM_CHANNEL_ID") or None
    )
    telegram_bot_username: str | None = field(
        default_factory=lambda: (os.getenv("TELEGRAM_BOT_USERNAME") or "").lstrip("@") or None
    )
    poll_seconds: int = field(default_factory=lambda: _env_int("POLL_SECONDS", 10))
    initial_balance: float = 10_000.0
    max_retries: int = 5
    retry_base_delay: float = 1.0


DESK_SETTINGS_PATH = DATA_DIR / "desk_settings.json"


RISK = RiskConfig()
QUALITY = QualityConfig()
DATA = DataConfig()
MACRO = MacroConfig()
ONLINE = OnlineLearnConfig()
MULTI = MultiPairConfig()
GEMINI = GeminiConfig()
CHANNEL = ChannelConfig()
BOT = BotConfig()
