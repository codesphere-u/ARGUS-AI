"""Data ingestion and feature engineering for MT5 Forex OHLCV."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from config import DATA, DataConfig

logger = logging.getLogger(__name__)

# Optional MT5 import — allows offline feature engineering / unit tests.
try:
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover
    mt5 = None  # type: ignore

TIMEFRAME_MAP = {
    "1m": "TIMEFRAME_M1",
    "5m": "TIMEFRAME_M5",
    "15m": "TIMEFRAME_M15",
    "30m": "TIMEFRAME_M30",
    "1h": "TIMEFRAME_H1",
    "4h": "TIMEFRAME_H4",
    "1d": "TIMEFRAME_D1",
}

FEATURE_COLUMNS = [
    "ema_20",
    "ema_50",
    "ema_200",
    "rsi_14",
    "atr_14",
    "volatility",
    "ema_20_1h",
    "ema_50_1h",
    "rsi_14_1h",
    "atr_14_1h",
    "close_return",
]


class MT5ConnectionError(RuntimeError):
    """Raised when MetaTrader 5 cannot be initialized or queried."""


def initialize_mt5(
    login: int | None = None,
    password: str | None = None,
    server: str | None = None,
    path: str | None = None,
) -> None:
    """Initialize a connection to the MetaTrader 5 terminal."""
    if mt5 is None:
        raise MT5ConnectionError("MetaTrader5 package is not installed.")

    kwargs: dict = {}
    if path:
        kwargs["path"] = path
    if not mt5.initialize(**kwargs):
        raise MT5ConnectionError(f"MT5 initialize failed: {mt5.last_error()}")

    if login is not None and password and server and not mt5.login(login, password=password, server=server):
        mt5.shutdown()
        raise MT5ConnectionError(f"MT5 login failed: {mt5.last_error()}")

    logger.info("MT5 initialized successfully.")


def fetch_account_state() -> dict[str, float | str]:
    """
    Read live balance / equity / margin from the connected MT5 account.

    Returns keys: balance, equity, margin, free_margin, profit, currency, login.
    """
    if mt5 is None:
        raise MT5ConnectionError("MetaTrader5 package is not installed.")
    info = mt5.account_info()
    if info is None:
        raise MT5ConnectionError(f"account_info failed: {mt5.last_error()}")
    return {
        "balance": float(info.balance),
        "equity": float(info.equity),
        "margin": float(getattr(info, "margin", 0) or 0),
        "free_margin": float(getattr(info, "margin_free", 0) or 0),
        "profit": float(getattr(info, "profit", 0) or 0),
        "currency": str(getattr(info, "currency", "") or ""),
        "login": str(getattr(info, "login", "") or ""),
    }


def shutdown_mt5() -> None:
    """Shut down the MT5 terminal connection if available."""
    if mt5 is not None:
        mt5.shutdown()


def _resolve_timeframe(tf: str):
    if mt5 is None:
        raise MT5ConnectionError("MetaTrader5 package is not installed.")
    key = TIMEFRAME_MAP.get(tf.lower())
    if key is None:
        raise ValueError(f"Unsupported timeframe: {tf}")
    return getattr(mt5, key)


def resolve_symbol(symbol: str) -> str:
    """
    Resolve a user symbol to the broker's exact Market Watch name.

    Exness and many brokers append suffixes (e.g. ``EURUSDm``). If the exact
    name is missing, pick the first visible symbol containing the base name.
    """
    if mt5 is None:
        raise MT5ConnectionError("MetaTrader5 package is not installed.")

    info = mt5.symbol_info(symbol)
    if info is not None:
        return info.name

    needle = symbol.upper().replace(".", "").replace("_", "")
    symbols = mt5.symbols_get() or ()
    candidates = []
    for s in symbols:
        name_u = s.name.upper().replace(".", "").replace("_", "")
        if needle in name_u or name_u.startswith(needle):
            candidates.append(s.name)
    if not candidates:
        raise MT5ConnectionError(
            f"Symbol '{symbol}' not found on this broker. "
            "Check Market Watch or set SYMBOL in .env (e.g. EURUSDm)."
        )
    resolved = candidates[0]
    logger.warning("Symbol '%s' not exact; using broker symbol '%s'.", symbol, resolved)
    return resolved


def try_resolve_symbol(symbol: str) -> str | None:
    """Resolve + select a symbol; return broker name or None if unavailable."""
    if mt5 is None:
        return None
    try:
        resolved = resolve_symbol(symbol)
        if not mt5.symbol_select(resolved, True):
            logger.warning(
                "Watchlist SKIP %s — select failed: %s",
                symbol,
                mt5.last_error(),
            )
            return None
        return resolved
    except Exception as exc:  # noqa: BLE001
        logger.warning("Watchlist SKIP %s — %s", symbol, exc)
        return None


def filter_watchlist_symbols(symbols: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """
    Keep configured symbols that resolve on this broker.

    Returns the configured names (not remapped) so engines/journal keys stay stable.
    """
    ok: list[str] = []
    skipped: list[str] = []
    for sym in symbols:
        resolved = try_resolve_symbol(sym)
        if resolved is None:
            skipped.append(sym)
            continue
        if resolved != sym:
            logger.info("Watchlist OK %s → broker %s", sym, resolved)
        else:
            logger.info("Watchlist OK %s", sym)
        ok.append(sym)
    if skipped:
        logger.warning(
            "Watchlist SKIP (not on broker): %s",
            ", ".join(skipped),
        )
    return tuple(ok)


def _ensure_symbol_selected(symbol: str) -> str:
    """Select symbol in Market Watch; return the resolved broker name."""
    resolved = resolve_symbol(symbol)
    if not mt5.symbol_select(resolved, True):
        raise MT5ConnectionError(
            f"Unable to select symbol {resolved}: {mt5.last_error()}. "
            "Open MT5, add the symbol to Market Watch, and ensure the terminal stays running."
        )
    return resolved


def fetch_ohlcv(
    symbol: str,
    timeframe: str,
    bars: int = 500,
    end: datetime | None = None,
) -> pd.DataFrame:
    """
    Fetch OHLCV candle data from MT5.

    Parameters
    ----------
    symbol:
        Broker symbol name (e.g. ``EURUSDm`` on Exness).
    timeframe:
        Candle timeframe key such as ``15m`` or ``1h``.
    bars:
        Number of candles to retrieve.
    end:
        Optional end datetime (UTC). Defaults to now.
    """
    if mt5 is None:
        raise MT5ConnectionError("MetaTrader5 package is not installed.")

    symbol = _ensure_symbol_selected(symbol)

    tf = _resolve_timeframe(timeframe)
    end = end or datetime.now(timezone.utc)
    rates = mt5.copy_rates_from(symbol, tf, end, bars)
    if rates is None or len(rates) == 0:
        raise MT5ConnectionError(f"No rates for {symbol} {timeframe}: {mt5.last_error()}")

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.rename(columns={"tick_volume": "volume"})
    return df[["time", "open", "high", "low", "close", "volume"]].copy()


def fetch_ohlcv_range(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime | None = None,
) -> pd.DataFrame:
    """Fetch OHLCV between ``start`` and ``end`` (UTC)."""
    if mt5 is None:
        raise MT5ConnectionError("MetaTrader5 package is not installed.")

    symbol = _ensure_symbol_selected(symbol)

    tf = _resolve_timeframe(timeframe)
    end = end or datetime.now(timezone.utc)
    rates = mt5.copy_rates_range(symbol, tf, start, end)
    if rates is None or len(rates) == 0:
        raise MT5ConnectionError(
            f"No rates in range for {symbol} {timeframe}: {mt5.last_error()}"
        )

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.rename(columns={"tick_volume": "volume"})
    return df[["time", "open", "high", "low", "close", "volume"]].copy()


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            (df["high"] - df["low"]).abs(),
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def add_indicators(df: pd.DataFrame, cfg: DataConfig | None = None, suffix: str = "") -> pd.DataFrame:
    """
    Add EMA, RSI, ATR and rolling volatility columns.

    Parameters
    ----------
    df:
        OHLCV frame with columns open/high/low/close.
    cfg:
        Data configuration; defaults to global ``DATA``.
    suffix:
        Optional column suffix (e.g. ``_1h`` for context timeframe).
    """
    cfg = cfg or DATA
    out = df.copy()
    close = out["close"]

    for period in cfg.ema_periods:
        out[f"ema_{period}{suffix}"] = _ema(close, period)

    out[f"rsi_{cfg.rsi_period}{suffix}"] = _rsi(close, cfg.rsi_period)
    out[f"atr_{cfg.atr_period}{suffix}"] = _atr(out, cfg.atr_period)
    out[f"volatility{suffix}"] = close.pct_change().rolling(cfg.volatility_window).std()
    out[f"close_return{suffix}"] = close.pct_change()

    # MACD (12, 26, 9)
    macd_line = _ema(close, 12) - _ema(close, 26)
    macd_sig = _ema(macd_line, 9)
    out[f"macd{suffix}"] = macd_line
    out[f"macd_signal{suffix}"] = macd_sig
    out[f"macd_hist{suffix}"] = macd_line - macd_sig

    # Bollinger Bands (20, 2)
    bb_mid = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    bb_up = bb_mid + 2.0 * bb_std
    bb_low = bb_mid - 2.0 * bb_std
    out[f"bb_upper{suffix}"] = bb_up
    out[f"bb_lower{suffix}"] = bb_low
    out[f"bb_pct_b{suffix}"] = (close - bb_low) / (bb_up - bb_low).replace(0, np.nan)

    return out


def merge_timeframes(
    primary: pd.DataFrame,
    context: pd.DataFrame,
    context_cols: Iterable[str],
) -> pd.DataFrame:
    """Align higher-timeframe features onto the primary timeframe via forward-fill."""
    ctx = context[["time", *context_cols]].copy()
    merged = pd.merge_asof(
        primary.sort_values("time"),
        ctx.sort_values("time"),
        on="time",
        direction="backward",
    )
    return merged


def normalize_features(
    df: pd.DataFrame,
    feature_cols: list[str] | None = None,
    window: int = 100,
) -> tuple[pd.DataFrame, list[str]]:
    """
    Rolling z-score normalize market features for model ingestion.

    Returns the enriched DataFrame and the list of normalized column names.
    """
    feature_cols = feature_cols or [c for c in FEATURE_COLUMNS if c in df.columns]
    out = df.copy()
    norm_cols: list[str] = []

    for col in feature_cols:
        mean = out[col].rolling(window=window, min_periods=max(10, window // 5)).mean()
        std = out[col].rolling(window=window, min_periods=max(10, window // 5)).std()
        norm_name = f"{col}_norm"
        out[norm_name] = ((out[col] - mean) / std.replace(0, np.nan)).clip(-5, 5)
        norm_cols.append(norm_name)

    return out, norm_cols


def build_feature_frame(
    primary: pd.DataFrame,
    context: pd.DataFrame,
    cfg: DataConfig | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """
    Full feature pipeline: indicators on both TFs, merge, normalize.

    Returns
    -------
    frame:
        Clean DataFrame indexed chronologically with raw + normalized features.
    feature_cols:
        Names of normalized columns suitable for the RL observation vector.
    """
    cfg = cfg or DATA
    primary_ind = add_indicators(primary, cfg, suffix="")
    context_ind = add_indicators(context, cfg, suffix="_1h")

    context_feature_cols = [
        "ema_20_1h",
        "ema_50_1h",
        f"rsi_{cfg.rsi_period}_1h",
        f"atr_{cfg.atr_period}_1h",
    ]
    merged = merge_timeframes(primary_ind, context_ind, context_feature_cols)

    raw_features = [
        "ema_20",
        "ema_50",
        "ema_200",
        f"rsi_{cfg.rsi_period}",
        f"atr_{cfg.atr_period}",
        "volatility",
        *context_feature_cols,
        "close_return",
    ]
    # Keep only columns that exist
    raw_features = [c for c in raw_features if c in merged.columns]
    framed, norm_cols = normalize_features(merged, raw_features)
    framed = framed.dropna().reset_index(drop=True)
    return framed, norm_cols


def load_live_features(
    symbol: str | None = None,
    bars: int | None = None,
    cfg: DataConfig | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Fetch recent 15m + 1h candles and return a normalized feature frame."""
    cfg = cfg or DATA
    symbol = symbol or cfg.symbol
    bars = bars or cfg.lookback_bars

    primary = fetch_ohlcv(symbol, cfg.timeframe_primary, bars=bars)
    context = fetch_ohlcv(symbol, cfg.timeframe_context, bars=max(bars // 4, 200))
    return build_feature_frame(primary, context, cfg)


def load_historical_features(
    symbol: str | None = None,
    months: int | None = None,
    cfg: DataConfig | None = None,
    end: datetime | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """
    Load roughly ``months`` of historical data for backtesting / training.

    Falls back to ``bars``-based fetch sizing when date-range retrieval is sparse.
    """
    cfg = cfg or DATA
    symbol = symbol or cfg.symbol
    months = months or cfg.history_months
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(days=30 * months)

    primary = fetch_ohlcv_range(symbol, cfg.timeframe_primary, start, end)
    context = fetch_ohlcv_range(symbol, cfg.timeframe_context, start, end)
    return build_feature_frame(primary, context, cfg)


def synthesize_ohlcv(
    bars: int = 2000,
    seed: int = 42,
    start_price: float = 1.1000,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Generate synthetic 15m / 1h OHLCV for offline demos when MT5 is unavailable.

    Useful for training smoke-tests and paper-mode dry runs without a terminal.
    """
    rng = np.random.default_rng(seed)
    times = pd.date_range(
        end=datetime.now(timezone.utc),
        periods=bars,
        freq="15min",
        tz="UTC",
    )
    returns = rng.normal(0, 0.0004, size=bars)
    close = start_price * np.cumprod(1 + returns)
    high = close * (1 + rng.uniform(0.0001, 0.0008, size=bars))
    low = close * (1 - rng.uniform(0.0001, 0.0008, size=bars))
    open_ = np.roll(close, 1)
    open_[0] = start_price
    volume = rng.integers(100, 1000, size=bars)

    primary = pd.DataFrame(
        {
            "time": times,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )

    # Aggregate to 1h
    ctx = primary.set_index("time").resample("1h").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    ).dropna().reset_index()
    return primary, ctx
