"""Professional dark candlestick chart PNGs for Telegram trade alerts."""

from __future__ import annotations

import logging
from io import BytesIO
from typing import Any

import numpy as np
import pandas as pd

from config import BRAND_SHORT

logger = logging.getLogger(__name__)

# TradingView Pro dark trading palette
BG = "#131722"
PANEL = "#1e222d"
GRID = "#2a2e39"
TEXT = "#d1d4dc"
MUTED = "#787b86"
UP = "#00e676"  # Vibrant TradingView Green
DOWN = "#ff5252"  # Vibrant TradingView Red
ENTRY = "#2962ff"  # TradingView Electric Blue
SL = "#ff5252"
TP = "#00e676"
EXIT = "#ff9800"  # Vibrant Gold
WATERMARK = "#434651"


def _digits(symbol: str) -> int:
    s = symbol.upper()
    if any(
        t in s
        for t in (
            "XAU",
            "XAG",
            "GOLD",
            "BTC",
            "ETH",
            "SOL",
            "XRP",
            "LTC",
            "BNB",
            "ADA",
            "DOGE",
            "LINK",
            "AVAX",
        )
    ):
        return 2
    if "JPY" in s:
        return 3
    return 5


def _prepare_ohlc(df: pd.DataFrame, bars: int = 72) -> pd.DataFrame | None:
    need = {"open", "high", "low", "close"}
    if df is None or df.empty or not need.issubset(set(df.columns)):
        return None
    cols = ["open", "high", "low", "close"]
    for extra in ("time", "ema_20", "ema_50", "ema_200", "rsi_14"):
        if extra in df.columns:
            cols.append(extra)
    out = df.loc[:, cols].copy()
    if "time" in df.columns:
        out["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
    else:
        out["time"] = pd.RangeIndex(len(out))
    out = out.dropna(subset=["open", "high", "low", "close"]).tail(bars)
    if len(out) < 5:
        return None
    return out.reset_index(drop=True)


def render_trade_chart(
    df: pd.DataFrame,
    *,
    symbol: str,
    side: str | None = None,
    entry: float | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    exit_price: float | None = None,
    title_suffix: str | None = None,
    bars: int = 72,
) -> bytes | None:
    """
    Render a dark candlestick chart with trade levels as PNG bytes.

    Returns None on failure (caller should fall back to text-only alert).
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except ImportError:
        logger.warning("matplotlib not installed; skipping chart render.")
        return None

    ohlc = _prepare_ohlc(df, bars=bars)
    if ohlc is None:
        return None

    try:
        n = len(ohlc)
        x = np.arange(n)
        opens = ohlc["open"].to_numpy(dtype=float)
        highs = ohlc["high"].to_numpy(dtype=float)
        lows = ohlc["low"].to_numpy(dtype=float)
        closes = ohlc["close"].to_numpy(dtype=float)

        fig, ax = plt.subplots(figsize=(10.5, 5.6), dpi=140)
        fig.patch.set_facecolor(BG)
        ax.set_facecolor(PANEL)

        width = 0.62
        for i in range(n):
            bull = closes[i] >= opens[i]
            color = UP if bull else DOWN
            ax.vlines(i, lows[i], highs[i], color=color, linewidth=1.0, zorder=2)
            body_bottom = min(opens[i], closes[i])
            body_h = max(abs(closes[i] - opens[i]), (highs[i] - lows[i]) * 0.002)
            ax.add_patch(
                Rectangle(
                    (i - width / 2, body_bottom),
                    width,
                    body_h,
                    facecolor=color,
                    edgecolor=color,
                    linewidth=0.6,
                    zorder=3,
                )
            )

        if "ema_20" in ohlc.columns:
            ema20 = ohlc["ema_20"].to_numpy(dtype=float)
            if not np.all(np.isnan(ema20)):
                ax.plot(x, ema20, color="#00e5ff", linewidth=1.1, alpha=0.75, label="EMA 20", zorder=3)
        if "ema_50" in ohlc.columns:
            ema50 = ohlc["ema_50"].to_numpy(dtype=float)
            if not np.all(np.isnan(ema50)):
                ax.plot(x, ema50, color="#ffd700", linewidth=1.1, alpha=0.75, label="EMA 50", zorder=3)

        levels: list[tuple[float, str, str, str]] = []
        if entry is not None and entry > 0:
            levels.append((float(entry), "ENTRY", ENTRY, "--"))
        if stop_loss is not None and stop_loss > 0:
            levels.append((float(stop_loss), "SL", SL, "-"))
        if take_profit is not None and take_profit > 0:
            levels.append((float(take_profit), "TP", TP, "-"))
        if exit_price is not None and exit_price > 0:
            levels.append((float(exit_price), "EXIT", EXIT, "--"))

        dig = _digits(symbol)
        ymin = float(np.min(lows))
        ymax = float(np.max(highs))
        for price, *_ in levels:
            ymin = min(ymin, price)
            ymax = max(ymax, price)
        pad = (ymax - ymin) * 0.08 or abs(ymax) * 0.001 or 0.0001
        ax.set_ylim(ymin - pad, ymax + pad)

        for price, label, color, style in levels:
            ax.axhline(price, color=color, linestyle=style, linewidth=1.4, alpha=0.95, zorder=4)
            ax.text(
                n - 0.5,
                price,
                f"  {label} {price:.{dig}f}",
                color=color,
                fontsize=8.5,
                fontweight="bold",
                va="center",
                ha="left",
                clip_on=False,
                zorder=5,
            )

        if entry is not None and entry > 0:
            if take_profit is not None and take_profit > 0:
                ax.axhspan(min(entry, take_profit), max(entry, take_profit), color=TP, alpha=0.08, zorder=1)
            if stop_loss is not None and stop_loss > 0:
                ax.axhspan(min(entry, stop_loss), max(entry, stop_loss), color=SL, alpha=0.08, zorder=1)

        side_txt = (side or "").upper()
        if side_txt in {"BUY", "LONG"}:
            side_txt = "BUY"
        elif side_txt in {"SELL", "SHORT"}:
            side_txt = "SELL"
        title = f"{symbol}"
        if side_txt in {"BUY", "SELL"}:
            title += f"  ·  {side_txt}"
        if title_suffix:
            title += f"  ·  {title_suffix}"
        ax.set_title(title, color=TEXT, fontsize=13, fontweight="bold", pad=12, loc="left")

        ax.text(
            0.99,
            0.02,
            BRAND_SHORT,
            transform=ax.transAxes,
            ha="right",
            va="bottom",
            fontsize=9,
            color=WATERMARK,
            fontweight="bold",
            alpha=0.85,
        )

        ax.grid(True, color=GRID, linewidth=0.6, alpha=0.7)
        ax.set_xlim(-1, n + max(6, int(n * 0.12)))
        ax.tick_params(colors=MUTED, labelsize=8)
        for spine in ax.spines.values():
            spine.set_color(GRID)

        # Sparse time ticks
        step = max(1, n // 6)
        tick_idx = list(range(0, n, step))
        if n - 1 not in tick_idx:
            tick_idx.append(n - 1)
        labels: list[str] = []
        for i in tick_idx:
            t = ohlc.loc[i, "time"]
            if hasattr(t, "strftime"):
                labels.append(t.strftime("%m-%d %H:%M"))
            else:
                labels.append(str(i))
        ax.set_xticks(tick_idx)
        ax.set_xticklabels(labels, rotation=0, ha="center")

        fig.tight_layout(pad=0.6)
        buf = BytesIO()
        fig.savefig(
            buf,
            format="png",
            facecolor=fig.get_facecolor(),
            edgecolor="none",
            bbox_inches="tight",
            pad_inches=0.15,
        )
        plt.close(fig)
        buf.seek(0)
        data = buf.getvalue()
        return data if data else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Chart render failed: %s", exc)
        try:
            plt.close("all")
        except Exception:  # noqa: BLE001
            pass
        return None


def levels_from_atr(
    *,
    side: str,
    price: float,
    atr: float,
    sl_atr_mult: float = 1.5,
    tp_atr_mult: float = 3.0,
) -> tuple[float, float] | None:
    """Estimate SL/TP from ATR for opportunity preview charts."""
    if price <= 0 or atr <= 0:
        return None
    s = side.lower()
    if s in {"buy", "long"}:
        return price - sl_atr_mult * atr, price + tp_atr_mult * atr
    if s in {"sell", "short"}:
        return price + sl_atr_mult * atr, price - tp_atr_mult * atr
    return None


def append_advisor_note(caption: str, note: str | None, *, max_total: int = 1024) -> str:
    """Append a short Gemini/advisor line without replacing the card."""
    note = (note or "").strip()
    if not note:
        return caption[:max_total]
    from html import escape

    extra = f"\n\n🧠 <i>{escape(note[:180])}</i>"
    combined = caption + extra
    return combined[:max_total]


def render_equity_chart(
    equity_points: list[float] | Any,
    *,
    title: str = f"{BRAND_SHORT} · Equity",
    invested: float | None = None,
) -> bytes | None:
    """Render a dark equity curve PNG for channel progress posts."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        logger.warning("matplotlib not installed; skipping equity chart.")
        return None

    try:
        pts = [float(x) for x in list(equity_points) if x is not None]
        if len(pts) < 2:
            return None

        fig, ax = plt.subplots(figsize=(10.5, 4.8), dpi=140)
        fig.patch.set_facecolor(BG)
        ax.set_facecolor(PANEL)

        xs = np.arange(len(pts))
        ys = np.asarray(pts, dtype=float)
        color = UP if ys[-1] >= ys[0] else DOWN
        ax.plot(xs, ys, color=color, linewidth=2.2, zorder=3)
        ax.fill_between(xs, ys, ys.min(), color=color, alpha=0.12, zorder=2)

        if invested is not None and invested > 0:
            ax.axhline(float(invested), color=ENTRY, linestyle="--", linewidth=1.2, alpha=0.8)
            ax.text(
                len(pts) - 0.5,
                float(invested),
                f"  Capital {float(invested):,.0f}",
                color=ENTRY,
                fontsize=8,
                va="bottom",
                ha="left",
            )

        ax.set_title(title, color=TEXT, fontsize=13, fontweight="bold", pad=12, loc="left")
        ax.text(
            0.99,
            0.02,
            BRAND_SHORT,
            transform=ax.transAxes,
            ha="right",
            va="bottom",
            fontsize=9,
            color=WATERMARK,
            fontweight="bold",
            alpha=0.85,
        )
        ax.grid(True, color=GRID, linewidth=0.6, alpha=0.7)
        ax.tick_params(colors=MUTED, labelsize=8)
        for spine in ax.spines.values():
            spine.set_color(GRID)
        ax.set_xlim(0, max(len(pts) - 1, 1))
        pad = (ys.max() - ys.min()) * 0.08 or abs(ys[-1]) * 0.001 or 1.0
        ax.set_ylim(ys.min() - pad, ys.max() + pad)
        ax.set_xticks([])

        fig.tight_layout(pad=0.6)
        buf = BytesIO()
        fig.savefig(
            buf,
            format="png",
            facecolor=fig.get_facecolor(),
            edgecolor="none",
            bbox_inches="tight",
            pad_inches=0.15,
        )
        plt.close(fig)
        buf.seek(0)
        data = buf.getvalue()
        return data if data else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Equity chart render failed: %s", exc)
        try:
            plt.close("all")
        except Exception:  # noqa: BLE001
            pass
        return None
