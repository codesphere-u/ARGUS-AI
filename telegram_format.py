"""Beautiful, copy-trade-ready Telegram message templates (HTML)."""

from __future__ import annotations

from datetime import datetime, timezone
from config import BRAND_NAME, BRAND_SHORT
from html import escape
from typing import Sequence


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


def fmt_price(symbol: str, price: float) -> str:
    return f"{price:.{_digits(symbol)}f}"


def fmt_money(value: float, *, signed: bool = False) -> str:
    if signed:
        return f"{value:+,.2f}"
    return f"{value:,.2f}"


def mode_badge(mode: str | None = None, *, paper: bool | None = None) -> str:
    """Small PAPER / LIVE chip for alert headers."""
    if paper is True:
        return "📄 PAPER"
    if paper is False:
        return "🔴 LIVE"
    m = (mode or "").strip().upper()
    if "PAPER" in m:
        return "📄 PAPER"
    if m:
        return f"🔴 {escape(m)}"
    return "🔴 LIVE"


def fmt_lot(volume: float) -> str:
    if abs(volume - round(volume)) < 1e-9:
        return f"{int(round(volume))}"
    text = f"{volume:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def score_bar(score: float, width: int = 8) -> str:
    score = max(0.0, min(1.0, float(score)))
    filled = int(round(score * width))
    return "▓" * filled + "░" * (width - filled)


def _rr(entry: float, sl: float, tp: float) -> str:
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 0:
        return "—"
    return f"1:{reward / risk:.2f}"


def _side_emoji(side: str) -> str:
    s = side.lower()
    if s in {"buy", "long"}:
        return "🟢"
    if s in {"sell", "short"}:
        return "🔴"
    return "⚪"


def _side_label(side: str) -> str:
    s = side.lower()
    if s in {"buy", "long"}:
        return "BUY"
    if s in {"sell", "short"}:
        return "SELL"
    return side.upper()


def _reasons_line(reasons: Sequence[str], limit: int = 3) -> str:
    clean = [escape(r.strip()) for r in reasons if r and str(r).strip()]
    if not clean:
        return ""
    return " · ".join(clean[:limit])


def copy_line(
    *,
    side: str,
    symbol: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    volume: float | None = None,
) -> str:
    """Single-line signal — long-press / tap to copy on Telegram."""
    parts = [
        _side_label(side),
        symbol,
        f"@ {fmt_price(symbol, entry)}",
        f"SL {fmt_price(symbol, stop_loss)}",
        f"TP {fmt_price(symbol, take_profit)}",
    ]
    if volume is not None and volume > 0:
        parts.append(f"{fmt_lot(volume)} lot")
    return " | ".join(parts)


def format_startup(*, mode: str, symbols: Sequence[str], equity: float | None = None) -> str:
    pairs = " · ".join(escape(s) for s in symbols)
    lines = [
        f"⚡ <b>{escape(BRAND_NAME)}</b> is online",
        "",
        f"{mode_badge(mode)} · Mode · <b>{escape(mode)}</b>",
        f"Pairs · {pairs}",
    ]
    if equity is not None:
        lines.append(f"Equity · <b>${fmt_money(equity)}</b>")
    lines.extend(
        [
            "",
            "<i>Live signals arrive here automatically.</i>",
            "<i>Tap copy lines to paste into MT5.</i>",
            "",
            "📱 Control · use the <b>buttons</b> below",
        ]
    )
    return "\n".join(lines)


def format_desk_pulse(
    *,
    mode: str,
    equity: float,
    snap: dict,
    open_count: int,
    max_open: int,
    streak: str = "",
    top_symbol: str = "",
    top_score: float | None = None,
    paused: bool = False,
    idle_note: str = "",
) -> str:
    """Hourly private monitoring card."""
    pause_line = "⏸ <b>PAUSED</b>" if paused else "▶ Running"
    lines = [
        "🖥 <b>DESK PULSE</b>",
        "",
        pause_line,
        f"{mode_badge(mode)} · Mode · <b>{escape(mode)}</b>",
        f"Equity · <b>${fmt_money(equity)}</b>",
        f"Today · {fmt_money(float(snap.get('today_pnl') or 0), signed=True)}",
        f"Win rate · <b>{float(snap.get('win_rate') or 0):.0f}%</b> "
        f"({int(snap.get('wins') or 0)}W / {int(snap.get('losses') or 0)}L)",
        f"Open · {open_count}/{max_open}",
    ]
    if streak:
        lines.append(f"Streak · {escape(streak)}")
    if top_symbol and top_score is not None:
        lines.append(
            f"Top scan · <b>{escape(top_symbol)}</b> {score_bar(top_score)} {top_score:.2f}"
        )
    if idle_note:
        lines.extend(["", f"<i>{escape(idle_note)}</i>"])
    return "\n".join(lines)


def format_copy_desk(*args, **kwargs):  # noqa: ANN002, ANN003
    """Re-export deterministic copy card from desk_coach."""
    from desk_coach import format_copy_desk as _fmt

    return _fmt(*args, **kwargs)


def format_shutdown(*, mode: str, equity: float) -> str:
    return (
        f"⏹ <b>{escape(BRAND_NAME)}</b> stopped\n"
        f"Mode · {escape(mode)}\n"
        f"Final equity · <b>${fmt_money(equity)}</b>"
    )


def truncate_caption(text: str, limit: int = 1024) -> str:
    """Telegram photo captions max out at 1024 characters."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def sanitize_telegram_html(text: str, *, limit: int = 3900) -> str:
    """
    Make Gemini/coach HTML safe for Telegram parse_mode=HTML.

    Keeps: b, i, u, s, code, pre, a(href). Converts <br> to newlines.
    Escapes stray < and bare & so sendMessage does not 400.
    """
    import re

    if not text:
        return ""
    s = str(text)
    if s.strip().startswith("```"):
        s = re.sub(r"^```\w*\n?", "", s.strip())
        s = re.sub(r"\n?```$", "", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</p\s*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<p\b[^>]*>", "", s, flags=re.IGNORECASE)
    s = re.sub(
        r"</?(?:div|span|ul|ol|li|h[1-6]|table|tr|td|th|html|body|head)\b[^>]*>",
        "",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"</?strong\b[^>]*>",
        lambda m: "</b>" if m.group(0).startswith("</") else "<b>",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"</?em\b[^>]*>",
        lambda m: "</i>" if m.group(0).startswith("</") else "<i>",
        s,
        flags=re.IGNORECASE,
    )

    allowed = re.compile(
        r"<(b|i|u|s|code|pre)(\s[^>]*)?>|</(b|i|u|s|code|pre)>|"
        r"<a\s+href=\"[^\"]+\">|</a>",
        re.IGNORECASE,
    )
    out: list[str] = []
    i = 0
    while i < len(s):
        if s[i] == "<":
            m = allowed.match(s, i)
            if m:
                out.append(m.group(0))
                i = m.end()
                continue
            out.append("&lt;")
            i += 1
            continue
        if s[i] == "&":
            ent = re.match(r"&(?:amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);", s[i:])
            if ent:
                out.append(ent.group(0))
                i += ent.end()
            else:
                out.append("&amp;")
                i += 1
            continue
        out.append(s[i])
        i += 1
    cleaned = re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned


def format_entry(
    *,
    side: str,
    symbol: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    volume: float,
    score: float,
    reasons: Sequence[str] = (),
    equity: float | None = None,
    mode: str = "LIVE",
    compact: bool = False,
    confluence_mtf: str | None = None,
    smc_tags: Sequence[str] = (),
) -> str:
    emoji = _side_emoji(side)
    label = _side_label(side)
    entry_s = fmt_price(symbol, entry)
    sl_s = fmt_price(symbol, stop_loss)
    tp_s = fmt_price(symbol, take_profit)
    lot_s = fmt_lot(volume)
    rr = _rr(entry, stop_loss, take_profit)
    clip = copy_line(
        side=side,
        symbol=symbol,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        volume=volume,
    )
    why = _reasons_line(reasons)
    badge = mode_badge(mode)

    tag_line = ""
    if smc_tags:
        tag_line = " ".join(f"<code>[{escape(t)}]</code>" for t in smc_tags)

    mtf_line = f"📐 <b>Align:</b> <code>{escape(confluence_mtf)}</code>" if confluence_mtf else ""

    if compact:
        lines = [
            f"{emoji} <b>{label}</b> · <b>{escape(symbol)}</b> · {badge}",
            f"{score_bar(score)} <b>{score:.2f}</b> · R:R <b>{rr}</b>",
        ]
        if mtf_line:
            lines.append(mtf_line)
        if tag_line:
            lines.append(tag_line)
        lines.extend([
            "<pre>"
            f"Entry  {entry_s}\n"
            f"SL     {sl_s}\n"
            f"TP     {tp_s}\n"
            f"Lot    {lot_s}"
            "</pre>",
            f"📋 <code>{escape(clip)}</code>",
        ])
        if why:
            lines.append(f"<i>{why}</i>")
        if equity is not None:
            lines.append(f"💰 <b>${fmt_money(equity)}</b>")
        return truncate_caption("\n".join(lines))

    lines = [
        f"{emoji} <b>{label} SIGNAL</b> · {badge}",
        "",
        f"<b>{escape(symbol)}</b>  ·  {score_bar(score)}  <b>{score:.2f}</b>",
    ]
    if mtf_line:
        lines.append(mtf_line)
    if tag_line:
        lines.append(tag_line)
    lines.extend([
        "",
        "<pre>"
        f"Side   {label}\n"
        f"Entry  {entry_s}\n"
        f"SL     {sl_s}\n"
        f"TP     {tp_s}\n"
        f"Lot    {lot_s}\n"
        f"R:R    {rr}"
        "</pre>",
        "",
        f"📋 <code>{escape(clip)}</code>",
    ])
    if why:
        lines.extend(["", f"💡 <i>{why}</i>"])
    if equity is not None:
        lines.extend(["", f"💰 Equity · <b>${fmt_money(equity)}</b>"])
    return "\n".join(lines)


def format_exit(
    *,
    symbol: str,
    reason: str,
    exit_price: float,
    pnl: float,
    equity: float,
    fill_status: str | None = None,
    side: str | None = None,
    entry: float | None = None,
    compact: bool = False,
    r_multiple: float | None = None,
    win_rate: float | None = None,
    mode: str = "LIVE",
) -> str:
    rocket = "Rocket" in reason or "rocket" in reason.lower()
    win = pnl >= 0
    badge = mode_badge(mode)
    if rocket and win:
        header = f"🚀 <b>ROCKET EXIT</b> · {badge}"
    elif win:
        header = f"✅ <b>TAKE PROFIT</b> · {badge}"
    else:
        header = f"🛑 <b>STOP / EXIT</b> · {badge}"

    if compact:
        lines = [
            f"{header} · <b>{escape(symbol)}</b>",
            f"<i>{escape(reason)}</i>",
            "<pre>",
        ]
        if side:
            lines.append(f"Side   {_side_label(side)}")
        if entry is not None:
            lines.append(f"Entry  {fmt_price(symbol, entry)}")
        lines.append(f"Exit   {fmt_price(symbol, exit_price)}")
        lines.append(f"PnL    {fmt_money(pnl, signed=True)}")
        if r_multiple is not None:
            lines.append(f"R      {r_multiple:+.2f}R")
        if win_rate is not None:
            lines.append(f"WR     {win_rate:.0f}%")
        lines.append("</pre>")
        lines.append(f"💰 <b>${fmt_money(equity)}</b>")
        return truncate_caption("\n".join(lines))

    body = [
        header,
        "",
        f"<b>{escape(symbol)}</b>",
        f"<i>{escape(reason)}</i>",
        "",
        "<pre>",
    ]
    if side:
        body.append(f"Side   {_side_label(side)}")
    if entry is not None:
        body.append(f"Entry  {fmt_price(symbol, entry)}")
    body.append(f"Exit   {fmt_price(symbol, exit_price)}")
    body.append(f"PnL    {fmt_money(pnl, signed=True)}")
    if r_multiple is not None:
        body.append(f"R      {r_multiple:+.2f}R")
    if win_rate is not None:
        body.append(f"WR     {win_rate:.0f}%")
    if fill_status:
        body.append(f"Fill   {fill_status}")
    body.append("</pre>")
    body.extend(["", f"💰 Equity · <b>${fmt_money(equity)}</b>"])
    return "\n".join(body)


def format_opportunity(
    *,
    symbol: str,
    side: str,
    score: float,
    price: float,
    atr: float,
    reasons: Sequence[str] = (),
    sl_atr_mult: float = 1.2,
    tp_atr_mult: float = 1.8,
) -> str:
    label = _side_label(side)
    emoji = _side_emoji(side)
    if atr > 0 and price > 0 and label in {"BUY", "SELL"}:
        if label == "BUY":
            sl = price - sl_atr_mult * atr
            tp = price + tp_atr_mult * atr
        else:
            sl = price + sl_atr_mult * atr
            tp = price - tp_atr_mult * atr
        clip = copy_line(
            side=side, symbol=symbol, entry=price, stop_loss=sl, take_profit=tp
        )
        card = (
            "<pre>"
            f"Side   {label}\n"
            f"Watch  {fmt_price(symbol, price)}\n"
            f"SL     {fmt_price(symbol, sl)}\n"
            f"TP     {fmt_price(symbol, tp)}\n"
            f"R:R    {_rr(price, sl, tp)}"
            "</pre>"
            f"\n\n📋 <code>{escape(clip)}</code>"
        )
    else:
        card = f"<pre>Side   {label}\nPrice  {fmt_price(symbol, price)}</pre>"

    why = _reasons_line(reasons)
    lines = [
        f"{emoji} <b>SETUP</b> · {escape(symbol)}",
        f"{score_bar(score)}  <b>{score:.2f}</b>",
        "",
        card,
    ]
    if why:
        lines.extend(["", f"💡 <i>{why}</i>"])
    return "\n".join(lines)


def format_opportunities_alert(
    opps: Sequence[object],
    *,
    sl_atr_mult: float = 1.2,
    tp_atr_mult: float = 1.8,
    limit: int = 3,
) -> str:
    """Bundle hot opportunities into one stunning alert."""
    stamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
    blocks = [
        f"🎯 <b>HOT SETUPS</b> · {stamp}",
        "<i>Preview levels — execute only after your own check.</i>",
    ]
    for opp in list(opps)[:limit]:
        blocks.append("──────────────")
        blocks.append(
            format_opportunity(
                symbol=getattr(opp, "symbol"),
                side=getattr(opp, "side"),
                score=float(getattr(opp, "score", 0.0)),
                price=float(getattr(opp, "price", 0.0)),
                atr=float(getattr(opp, "atr", 0.0)),
                reasons=list(getattr(opp, "reasons", []) or []),
                sl_atr_mult=sl_atr_mult,
                tp_atr_mult=tp_atr_mult,
            )
        )
    return "\n".join(blocks)


def format_error(exc: object) -> str:
    return f"⚠️ <b>Bot error</b>\n<code>{escape(str(exc))}</code>"


def format_train_complete(*, timesteps: int, model_path: str) -> str:
    return (
        "✅ <b>Training complete</b>\n"
        f"Steps · <b>{timesteps:,}</b>\n"
        f"Model · <code>{escape(str(model_path))}</code>"
    )


def format_learn_complete(
    *,
    steps: int,
    trades: int,
    baseline_sharpe: float,
    new_sharpe: float,
) -> str:
    delta = new_sharpe - baseline_sharpe
    arrow = "↑" if delta >= 0 else "↓"
    return (
        "🧠 <b>Online learn complete</b>\n"
        f"Steps · {steps:,}\n"
        f"Trades window · {trades}\n"
        f"Sharpe · <b>{baseline_sharpe:.3f}</b> → <b>{new_sharpe:.3f}</b> "
        f"({arrow}{abs(delta):.3f})"
    )


def format_learn_error(exc: object) -> str:
    return f"⚠️ <b>Online learn error</b>\n<code>{escape(str(exc))}</code>"


def format_backtest(results: dict) -> str:
    lines = ["📊 <b>Backtest complete</b>", ""]
    for key, value in results.items():
        if isinstance(value, float):
            shown = f"{value:.4f}" if abs(value) < 100 else f"{value:,.2f}"
        else:
            shown = str(value)
        lines.append(f"• <b>{escape(str(key))}</b> · <code>{escape(shown)}</code>")
    return "\n".join(lines)


def format_channel_progress(
    stats: dict,
    *,
    event: str = "digest",
    mode: str = "LIVE",
) -> str:
    """
    Public channel progress card.

    event: digest | milestone | daily_wrap
    """
    invested = float(stats.get("invested") or 0)
    equity = float(stats.get("equity") or 0)
    total_pnl = float(stats.get("total_pnl") or (equity - invested))
    ret = float(stats.get("total_return_pct") or 0)
    today = float(stats.get("today_pnl") or 0)
    win_rate = float(stats.get("win_rate") or 0)
    closed = int(stats.get("closed_trades") or 0)
    open_n = int(stats.get("open_positions") or 0)
    stamp = datetime.now(timezone.utc).strftime("%d %b · %H:%M UTC")
    badge = mode_badge(mode)

    if event == "milestone":
        header = f"🏆 <b>{escape(BRAND_NAME)}</b> · New equity high · {badge}"
        blurb = "<i>Fresh peak on the equity curve — discipline over noise.</i>"
    elif event == "daily_wrap":
        header = f"🌙 <b>{escape(BRAND_NAME)}</b> · Daily wrap · {badge}"
        blurb = "<i>End-of-day snapshot for followers tracking the book.</i>"
    else:
        # Rotate digest intros by hour bucket so posts feel varied
        hour = datetime.now(timezone.utc).hour
        intros = (
            f"📡 <b>{escape(BRAND_NAME)}</b> · Progress pulse · {badge}",
            f"📈 <b>{escape(BRAND_NAME)}</b> · Book update · {badge}",
            f"🧭 <b>{escape(BRAND_NAME)}</b> · Capital check-in · {badge}",
            f"⚙️ <b>{escape(BRAND_NAME)}</b> · System status · {badge}",
        )
        header = intros[hour % len(intros)]
        blurbs = (
            "<i>Quiet compounding — no hype, just the numbers.</i>",
            "<i>Risk-managed multi-pair book · transparent progress.</i>",
            "<i>Capital protected first. Opportunity second.</i>",
            "<i>Automated desk update for our channel community.</i>",
        )
        blurb = blurbs[hour % len(blurbs)]

    pnl_sign = "+" if total_pnl >= 0 else ""
    today_sign = "+" if today >= 0 else ""
    ret_sign = "+" if ret >= 0 else ""

    lines = [
        header,
        blurb,
        f"<i>{stamp}</i> · {escape(mode)}",
        "",
        "<pre>"
        f"Invested   ${fmt_money(invested)}\n"
        f"Equity     ${fmt_money(equity)}\n"
        f"Profit     {pnl_sign}${fmt_money(abs(total_pnl))}\n"
        f"Return     {ret_sign}{ret:.2f}%\n"
        f"Today      {today_sign}${fmt_money(abs(today))}\n"
        f"Win rate   {win_rate:.0f}%  ({closed} closed)\n"
        f"Open now   {open_n}"
        "</pre>",
        "",
        "<i>Free high-confidence setups + transparent progress for followers.</i>",
    ]
    return truncate_caption("\n".join(lines), 1024)


def format_channel_prediction(
    *,
    symbol: str,
    side: str,
    score: float,
    price: float,
    atr: float,
    reasons: Sequence[str] = (),
    sl_atr_mult: float = 1.2,
    tp_atr_mult: float = 1.8,
) -> str:
    """Public free prediction card — high-score setups only."""
    label = _side_label(side)
    emoji = _side_emoji(side)
    stamp = datetime.now(timezone.utc).strftime("%d %b · %H:%M UTC")
    why = _reasons_line(reasons, limit=3)
    conf = "HIGH" if score >= 0.85 else "STRONG"

    smc_tags = [
        r.upper() for r in (reasons or [])
        if any(k in r.lower() for k in ("order block", "fvg", "bos", "structure", "zone"))
    ]
    tag_str = " ".join(f"<code>[{escape(t)}]</code>" for t in smc_tags) if smc_tags else ""

    sl = tp = None
    if atr > 0 and price > 0 and label in {"BUY", "SELL"}:
        if label == "BUY":
            sl = price - sl_atr_mult * atr
            tp = price + tp_atr_mult * atr
        else:
            sl = price + sl_atr_mult * atr
            tp = price - tp_atr_mult * atr

    lines = [
        f"{emoji} <b>FREE PREDICTION</b> · {conf}",
        f"<b>{escape(symbol)}</b>  ·  {score_bar(score)}  <b>{score:.2f}</b>",
        f"<i>{stamp}</i>",
    ]
    if tag_str:
        lines.append(tag_str)
    lines.append("")
    if sl is not None and tp is not None:
        clip = copy_line(
            side=side, symbol=symbol, entry=price, stop_loss=sl, take_profit=tp
        )
        lines.append(
            "<pre>"
            f"Bias    {label}\n"
            f"Watch   {fmt_price(symbol, price)}\n"
            f"SL      {fmt_price(symbol, sl)}\n"
            f"TP      {fmt_price(symbol, tp)}\n"
            f"R:R     {_rr(price, sl, tp)}"
            "</pre>"
        )
        lines.extend(["", f"📋 <code>{escape(clip)}</code>"])
    else:
        lines.append(
            "<pre>"
            f"Bias    {label}\n"
            f"Watch   {fmt_price(symbol, price)}"
            "</pre>"
        )
    if why:
        lines.extend(["", f"💡 <i>{why}</i>"])
    lines.extend(
        [
            "",
            "<i>Model + scanner confluence. Not financial advice — manage your own risk.</i>",
        ]
    )
    return truncate_caption("\n".join(lines), 1024)


def format_journal_card(trades: Sequence[dict], *, limit: int = 10) -> str:
    """Last N closed trades for /journal."""
    rows = list(trades)[-limit:]
    rows = list(reversed(rows))
    if not rows:
        return "📓 <b>JOURNAL</b>\nNo closed trades in the recent window."
    lines = [f"📓 <b>JOURNAL</b> · last {len(rows)}", ""]
    for t in rows:
        sym = escape(str(t.get("symbol") or "?"))
        side = str(t.get("side") or "").upper()
        pnl = float(t.get("pnl") or 0)
        reason = escape(str(t.get("exit_reason") or t.get("reason") or "")[:40])
        closed = str(t.get("closed_at") or "")[:16].replace("T", " ")
        mark = "✅" if pnl >= 0 else "❌"
        lines.append(
            f"{mark} <b>{sym}</b> {side} · {fmt_money(pnl, signed=True)}"
            + (f"\n<code>{closed}</code> · <i>{reason}</i>" if closed or reason else "")
        )
    return "\n".join(lines)


def format_pnl_card(
    snap: dict,
    *,
    mode: str = "LIVE",
    open_count: int | None = None,
) -> str:
    """Today + total + WR card for /pnl."""
    open_n = (
        open_count
        if open_count is not None
        else int(snap.get("open_positions") or 0)
    )
    return "\n".join(
        [
            f"💰 <b>PnL</b> · {mode_badge(mode)}",
            "",
            f"Today · <b>{fmt_money(float(snap.get('today_pnl') or 0), signed=True)}</b>",
            f"Total · <b>{fmt_money(float(snap.get('total_pnl') or 0), signed=True)}</b> "
            f"({float(snap.get('total_return_pct') or 0):+.2f}%)",
            f"Win rate · <b>{float(snap.get('win_rate') or 0):.0f}%</b> "
            f"({int(snap.get('wins') or 0)}W / {int(snap.get('losses') or 0)}L)",
            f"Closed · {int(snap.get('closed_trades') or 0)} · Open · {open_n}",
            f"Equity · <b>${fmt_money(float(snap.get('equity') or 0))}</b>",
        ]
    )


def format_lot_calculator(
    *,
    symbol: str,
    equity: float,
    risk_pct: float,
    entry: float,
    stop_loss: float,
    computed_lot: float,
    risk_money: float,
) -> str:
    """Lot size calculator output card."""
    sym = escape(symbol)
    sl_dist = abs(entry - stop_loss)
    return "\n".join(
        [
            f"🧮 <b>LOT SIZE CALCULATOR</b>",
            f"Symbol · <b>{sym}</b>",
            "",
            f"Account Equity · <b>${fmt_money(equity)}</b>",
            f"Target Risk · <b>{risk_pct:.1f}%</b> (${fmt_money(risk_money)})",
            f"Entry Price · <code>{fmt_price(symbol, entry)}</code>",
            f"Stop Loss · <code>{fmt_price(symbol, stop_loss)}</code> (Distance: {fmt_price(symbol, sl_dist)})",
            "",
            f"👉 Recommended Lot · <b><code>{fmt_lot(computed_lot)}</code> lot</b>",
            "",
            "<i>Always confirm broker contract size before placing orders.</i>",
        ]
    )


def format_daily_digest(
    *,
    equity: float,
    today_pnl: float,
    win_rate: float,
    wins: int,
    losses: int,
    total_trades: int,
    best_trade_symbol: str = "",
    mode: str = "LIVE",
) -> str:
    """Daily summary digest (evening recap)."""
    pnl_str = fmt_money(today_pnl, signed=True)
    mark = "🚀" if today_pnl >= 0 else "📉"
    lines = [
        f"{mark} <b>DAILY DESK RECAP</b> · {mode_badge(mode)}",
        f"<i>{datetime.now(timezone.utc).strftime('%d %b %Y')}</i>",
        "",
        f"Today PnL · <b>{pnl_str}</b>",
        f"Win Rate · <b>{win_rate:.0f}%</b> ({wins}W / {losses}L)",
        f"Total Trades · {total_trades}",
        f"Closing Equity · <b>${fmt_money(equity)}</b>",
    ]
    if best_trade_symbol:
        lines.append(f"Top Performer · <b>{escape(best_trade_symbol)}</b>")
    lines.extend(["", "<i>Disciplined execution. Capital preserved.</i>"])
    return "\n".join(lines)


def format_news_warning_card(
    *,
    event_title: str,
    currency: str,
    minutes_until: int,
    open_positions_count: int = 0,
) -> str:
    """Pre-news blackout warning card."""
    return "\n".join(
        [
            "⚠️ <b>HIGH-IMPACT NEWS WARNING</b>",
            f"Event · <b>{escape(event_title)}</b> ({escape(currency)})",
            f"Time · <b>In {minutes_until} minutes</b>",
            "",
            f"Active Trades · <b>{open_positions_count} open position(s)</b>",
            "",
            "<i>High volatility expected. Consider locking Break-Even or taking partial profits.</i>",
        ]
    )
