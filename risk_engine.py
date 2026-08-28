"""Deterministic risk-management overlay (mandatory override layer)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

from config import RISK, RiskConfig
from macro_filter import MacroFilter, default_macro_filter

Side = Literal["buy", "sell", "flat"]


@dataclass
class PositionState:
    """Tracks an open position and its protective stops."""

    side: Side = "flat"
    volume: float = 0.0
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    atr_at_entry: float = 0.0
    trailing_active: bool = False
    opened_at: datetime | None = None
    ticket: int | None = None


@dataclass
class TradeOrder:
    """Risk-approved order intent passed to the execution engine."""

    action: Literal["hold", "buy", "sell", "close"]
    volume: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    reason: str = ""
    approved: bool = True


@dataclass
class RiskEngine:
    """
    Hard risk constraints that always override the RL policy.

    Rules
    -----
    - Never risk more than ``max_risk_per_trade`` of account equity
      (capped by ``max_risk_money`` and optional runtime override).
    - Initial SL = stop_loss_atr_mult × ATR, TP = take_profit_atr_mult × ATR
      (defaults ~1.5 / 2.5 → ~1:1.67 R:R). Rocket BE+trail lets winners run.
    - Trailing stop activates after trailing_activation_atr_mult × ATR profit.
    - Circuit breaker (optional): halt new trades if daily loss >= daily_loss_limit.
    - Macro/session blackout blocks new entries (exits still allowed).
    """

    risk_cfg: RiskConfig = field(default_factory=lambda: RISK)
    day_start_balance: float = 0.0
    day_anchor: datetime | None = None
    halt_until: datetime | None = None
    position: PositionState = field(default_factory=PositionState)
    contract_size: float = 100_000.0  # standard FX lot
    min_lot: float = 0.01
    max_lot: float = 2.0
    lot_step: float = 0.01
    tick_size: float = 0.00001
    macro_filter: MacroFilter = field(default_factory=lambda: default_macro_filter)
    symbol: str = "EURUSDm"
    enforce_macro: bool = True
    # Optional live override from Telegram /setrisk (None = use config)
    risk_money_override: float | None = None

    def __post_init__(self) -> None:
        # Prefer config max_lot when engine still at default
        if self.max_lot <= 0 or self.max_lot == 2.0:
            self.max_lot = float(self.risk_cfg.max_lot)

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _ensure_day(self, equity: float, now: datetime | None = None) -> None:
        now = now or self._now()
        if self.day_anchor is None or now.date() != self.day_anchor.date():
            self.day_anchor = now
            self.day_start_balance = equity

    def check_circuit_breaker(self, equity: float, now: datetime | None = None) -> bool:
        """Return True if trading is halted by the daily loss circuit breaker."""
        now = now or self._now()
        if self.halt_until is not None:
            if now < self.halt_until:
                return True
            self.halt_until = None

        self._ensure_day(equity, now)
        if self.day_start_balance <= 0:
            return False

        daily_loss_pct = (self.day_start_balance - equity) / self.day_start_balance
        if (
            self.risk_cfg.circuit_breaker_enabled
            and daily_loss_pct >= self.risk_cfg.daily_loss_limit
        ):
            self.halt_until = now + timedelta(hours=self.risk_cfg.circuit_breaker_hours)
            return True
        return False

    def normalize_volume(self, lots: float) -> float:
        """Clamp volume to broker min/max/step (avoids MT5 10014 Invalid volume)."""
        if lots <= 0:
            return 0.0
        step = max(self.lot_step, 1e-8)
        capped = min(float(lots), float(self.max_lot))
        import math
        steps = int(math.floor(capped / step + 1e-9))
        floored = steps * step
        if floored < self.min_lot:
            if capped + 1e-12 >= self.min_lot:
                floored = self.min_lot
            else:
                return 0.0
        # round to step decimals
        decimals = max(0, len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0)
        return float(round(floored, max(2, decimals)))

    def compute_lot_size(
        self,
        equity: float,
        entry_price: float,
        stop_loss: float,
        pip_value_per_lot: float | None = None,
    ) -> float:
        """Size position so a full stop-loss hit risks <= max_risk (and $ cap)."""
        risk_amount = equity * self.risk_cfg.max_risk_per_trade
        # Hard money cap — critical for huge demo balances (e.g. $100M)
        money_cap = self.risk_cfg.max_risk_money
        if self.risk_money_override is not None and self.risk_money_override > 0:
            money_cap = float(self.risk_money_override)
        if money_cap > 0:
            risk_amount = min(risk_amount, money_cap)
        sl_distance = abs(entry_price - stop_loss)
        if sl_distance <= 0 or equity <= 0:
            return 0.0

        if pip_value_per_lot is None:
            loss_per_lot = self.contract_size * sl_distance
        else:
            pips = sl_distance / max(self.tick_size * 10, 1e-8)
            loss_per_lot = pip_value_per_lot * pips

        if loss_per_lot <= 0:
            return 0.0

        raw_lots = risk_amount / loss_per_lot
        return self.normalize_volume(raw_lots)

    def initial_stops(self, side: Side, entry_price: float, atr: float) -> tuple[float, float]:
        """Compute initial stop-loss and take-profit from ATR multiples."""
        atr = max(atr, self.tick_size)
        sl_dist = self.risk_cfg.stop_loss_atr_mult * atr
        tp_dist = self.risk_cfg.take_profit_atr_mult * atr

        if side == "buy":
            return entry_price - sl_dist, entry_price + tp_dist
        if side == "sell":
            return entry_price + sl_dist, entry_price - tp_dist
        return 0.0, 0.0

    def update_stops(self, mid_price: float, atr: float | None = None) -> PositionState:
        """Update trailing stop after price reaches +1.5x ATR in profit."""
        pos = self.position
        if pos.side == "flat" or pos.entry_price <= 0:
            return pos

        atr = atr if atr is not None else pos.atr_at_entry
        atr = max(atr, self.tick_size)
        activation = self.risk_cfg.trailing_activation_atr_mult * atr
        trail_dist = self.risk_cfg.stop_loss_atr_mult * atr

        if pos.side == "buy":
            unrealized = mid_price - pos.entry_price
            if unrealized >= activation:
                pos.trailing_active = True
                new_sl = mid_price - trail_dist
                pos.stop_loss = max(pos.stop_loss, new_sl)
        elif pos.side == "sell":
            unrealized = pos.entry_price - mid_price
            if unrealized >= activation:
                pos.trailing_active = True
                new_sl = mid_price + trail_dist
                if pos.stop_loss == 0:
                    pos.stop_loss = new_sl
                else:
                    pos.stop_loss = min(pos.stop_loss, new_sl)

        return pos

    def stop_hit(self, high: float, low: float) -> Literal["none", "sl", "tp"]:
        """Check whether SL or TP was touched by the latest candle range.

        When both are touched in the same bar, prefer the nearer target (TP under
        high-hit-rate geometry) since continuous price must pass it first.
        """
        pos = self.position
        if pos.side == "flat":
            return "none"

        if pos.side == "buy":
            hit_sl = bool(pos.stop_loss and low <= pos.stop_loss)
            hit_tp = bool(pos.take_profit and high >= pos.take_profit)
            if hit_sl and hit_tp:
                sl_dist = abs(pos.entry_price - pos.stop_loss)
                tp_dist = abs(pos.take_profit - pos.entry_price)
                return "tp" if tp_dist <= sl_dist else "sl"
            if hit_tp:
                return "tp"
            if hit_sl:
                return "sl"
        elif pos.side == "sell":
            hit_sl = bool(pos.stop_loss and high >= pos.stop_loss)
            hit_tp = bool(pos.take_profit and low <= pos.take_profit)
            if hit_sl and hit_tp:
                sl_dist = abs(pos.stop_loss - pos.entry_price)
                tp_dist = abs(pos.entry_price - pos.take_profit)
                return "tp" if tp_dist <= sl_dist else "sl"
            if hit_tp:
                return "tp"
            if hit_sl:
                return "sl"
        return "none"

    def approve_and_size(
        self,
        raw_action: int,
        equity: float,
        price: float,
        atr: float,
        now: datetime | None = None,
        lot_mult: float = 1.0,
    ) -> TradeOrder:
        """Map RL discrete action through risk + macro gates into an order."""
        now = now or self._now()
        lot_mult = float(max(0.25, min(1.0, lot_mult))) if lot_mult > 0 else 0.0

        if self.check_circuit_breaker(equity, now):
            return TradeOrder(
                action="hold",
                approved=False,
                reason=f"Circuit breaker active until {self.halt_until.isoformat() if self.halt_until else 'n/a'}",
            )

        if self.position.side != "flat":
            self.update_stops(price, atr)

        if raw_action == 0:
            return TradeOrder(action="hold", reason="Policy HOLD")

        if lot_mult <= 0:
            return TradeOrder(action="hold", approved=False, reason="lot_mult=0 (advisor skip)")

        # Macro/session gate — blocks NEW entries only; closes still allowed
        is_entry = (
            (raw_action == 1 and self.position.side == "flat")
            or (raw_action == 2 and self.position.side == "flat")
        )
        if self.enforce_macro and is_entry:
            allowed, reason = self.macro_filter.is_trading_allowed(now, self.symbol)
            if not allowed:
                return TradeOrder(action="hold", approved=False, reason=reason)

        if raw_action == 1:
            if self.position.side == "buy":
                return TradeOrder(action="hold", reason="Already long")
            if self.position.side == "sell":
                return TradeOrder(
                    action="close",
                    volume=self.position.volume,
                    reason="Close short before long",
                )

            sl, tp = self.initial_stops("buy", price, atr)
            volume = self.normalize_volume(self.compute_lot_size(equity, price, sl) * lot_mult)
            if volume < self.min_lot:
                return TradeOrder(action="hold", approved=False, reason="Computed lot below minimum")
            reason = "Risk-approved BUY" if lot_mult >= 0.999 else f"Risk-approved BUY (lot_mult={lot_mult:.2f})"
            return TradeOrder(
                action="buy",
                volume=volume,
                stop_loss=sl,
                take_profit=tp,
                reason=reason,
            )

        if raw_action == 2:
            if self.position.side == "buy":
                return TradeOrder(
                    action="close",
                    volume=self.position.volume,
                    reason="Close long",
                )
            if self.position.side == "sell":
                return TradeOrder(action="hold", reason="Already short")

            sl, tp = self.initial_stops("sell", price, atr)
            volume = self.normalize_volume(self.compute_lot_size(equity, price, sl) * lot_mult)
            if volume < self.min_lot:
                return TradeOrder(action="hold", approved=False, reason="Computed lot below minimum")
            reason = "Risk-approved SELL" if lot_mult >= 0.999 else f"Risk-approved SELL (lot_mult={lot_mult:.2f})"
            return TradeOrder(
                action="sell",
                volume=volume,
                stop_loss=sl,
                take_profit=tp,
                reason=reason,
            )

        return TradeOrder(action="hold", approved=False, reason=f"Unknown action {raw_action}")

    def register_fill(
        self,
        side: Side,
        volume: float,
        price: float,
        stop_loss: float,
        take_profit: float,
        atr: float,
        now: datetime | None = None,
        ticket: int | None = None,
    ) -> None:
        """Update internal position state after a successful fill."""
        now = now or self._now()
        if side == "flat":
            self.position = PositionState()
            return
        self.position = PositionState(
            side=side,
            volume=volume,
            entry_price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            atr_at_entry=atr,
            trailing_active=False,
            opened_at=now,
            ticket=int(ticket) if ticket else None,
        )

    def register_close(self) -> None:
        """Clear position state after a full close."""
        self.position = PositionState()

    def partial_close(self, percentage: float = 0.5) -> float:
        """
        Reduce position volume by percentage (e.g. 0.5 for 50%).
        Returns the closed volume.
        """
        if self.position.side == "flat" or self.position.volume <= 0:
            return 0.0
        pct = max(0.1, min(0.9, float(percentage)))
        close_vol = self.normalize_volume(self.position.volume * pct)
        if close_vol <= 0:
            return 0.0
        rem_vol = self.normalize_volume(self.position.volume - close_vol)
        if rem_vol < self.min_lot:
            # Cannot partially close leaving less than min lot; full close required
            close_vol = self.position.volume
            self.position = PositionState()
        else:
            self.position.volume = rem_vol
        return close_vol

    def move_sl_to_be(self, spread_pips_buffer: float = 1.0) -> bool:
        """
        Move stop loss to entry price plus/minus a small spread buffer.
        Returns True if SL was adjusted.
        """
        pos = self.position
        if pos.side == "flat" or pos.entry_price <= 0:
            return False
        buffer = self.tick_size * 10 * spread_pips_buffer
        if pos.side == "buy":
            new_sl = pos.entry_price + buffer
            if new_sl > pos.stop_loss:
                pos.stop_loss = new_sl
                return True
        elif pos.side == "sell":
            new_sl = pos.entry_price - buffer
            if pos.stop_loss == 0 or new_sl < pos.stop_loss:
                pos.stop_loss = new_sl
                return True
        return False

    def record_realized_pnl(self, equity_after: float, now: datetime | None = None) -> None:
        """Refresh day anchor / circuit breaker using post-trade equity."""
        self.check_circuit_breaker(equity_after, now)

    def apply_symbol_info(
        self,
        *,
        trade_contract_size: float | None = None,
        volume_min: float | None = None,
        volume_max: float | None = None,
        volume_step: float | None = None,
        point: float | None = None,
    ) -> None:
        """Apply MT5 symbol_info hints for sizing (BTC/XAU differ from FX)."""
        if trade_contract_size and trade_contract_size > 0:
            self.contract_size = float(trade_contract_size)
        if volume_min and volume_min > 0:
            self.min_lot = float(volume_min)
        if volume_step and volume_step > 0:
            self.lot_step = float(volume_step)
        if point and point > 0:
            self.tick_size = float(point)
        # Cap at broker max and config MAX_LOT
        broker_cap = float(volume_max) if volume_max and volume_max > 0 else self.max_lot
        self.max_lot = min(float(self.risk_cfg.max_lot), broker_cap)


def default_contract_size(symbol: str) -> float:
    """Fallback contract size when MT5 symbol_info is unavailable."""
    u = symbol.upper()
    for tag in ("BTC", "ETH", "SOL", "XRP", "LTC", "BNB", "ADA", "DOGE", "LINK", "AVAX"):
        if tag in u:
            return 1.0
    if "XAU" in u or "XAG" in u or "GOLD" in u:
        return 100.0
    return 100_000.0


@dataclass
class MultiSymbolRiskManager:
    """
    Per-symbol RiskEngine instances with shared equity circuit breaker
    and a global open-position cap.
    """

    symbols: tuple[str, ...]
    max_open_positions: int = 8
    macro_filter: MacroFilter = field(default_factory=lambda: default_macro_filter)
    risk_cfg: RiskConfig = field(default_factory=lambda: RISK)
    engines: dict[str, RiskEngine] = field(default_factory=dict)
    day_start_balance: float = 0.0
    day_anchor: datetime | None = None
    halt_until: datetime | None = None
    risk_money_override: float | None = None

    def __post_init__(self) -> None:
        for sym in self.symbols:
            if sym not in self.engines:
                eng = RiskEngine(
                    risk_cfg=self.risk_cfg,
                    macro_filter=self.macro_filter,
                    symbol=sym,
                    enforce_macro=True,
                    contract_size=default_contract_size(sym),
                )
                self.engines[sym] = eng

    def set_risk_money_override(self, amount: float | None) -> None:
        """Propagate Telegram /setrisk override to every per-symbol engine."""
        self.risk_money_override = amount
        for eng in self.engines.values():
            eng.risk_money_override = amount

    def engine(self, symbol: str) -> RiskEngine:
        if symbol not in self.engines:
            self.engines[symbol] = RiskEngine(
                risk_cfg=self.risk_cfg,
                macro_filter=self.macro_filter,
                symbol=symbol,
                enforce_macro=True,
                contract_size=default_contract_size(symbol),
                risk_money_override=self.risk_money_override,
            )
        return self.engines[symbol]

    def open_symbols(self) -> list[str]:
        return [s for s, e in self.engines.items() if e.position.side != "flat"]

    def open_count(self) -> int:
        return len(self.open_symbols())

    def has_capacity(self) -> bool:
        return self.open_count() < self.max_open_positions

    def sync_circuit_breaker(
        self, equity: float, now: datetime | None = None
    ) -> tuple[bool, bool]:
        """
        Shared daily loss halt across all symbols.

        Returns ``(halted, newly_triggered)``.
        ``newly_triggered`` is True only on the transition into halt
        (so callers can flatten once).
        """
        now = now or datetime.now(timezone.utc)
        if self.halt_until is not None:
            if now < self.halt_until:
                for e in self.engines.values():
                    e.halt_until = self.halt_until
                return True, False
            self.halt_until = None

        if self.day_anchor is None or now.date() != self.day_anchor.date():
            self.day_anchor = now
            self.day_start_balance = equity

        newly = False
        if self.day_start_balance > 0:
            daily_loss_pct = (self.day_start_balance - equity) / self.day_start_balance
            if (
                self.risk_cfg.circuit_breaker_enabled
                and daily_loss_pct >= self.risk_cfg.daily_loss_limit
            ):
                self.halt_until = now + timedelta(hours=self.risk_cfg.circuit_breaker_hours)
                newly = True
                for e in self.engines.values():
                    e.halt_until = self.halt_until
                    e.day_start_balance = self.day_start_balance
                    e.day_anchor = self.day_anchor
                return True, newly

        for e in self.engines.values():
            e.day_start_balance = self.day_start_balance
            e.day_anchor = self.day_anchor
            e.halt_until = self.halt_until
        return False, False

    def get_currency_exposure(self) -> dict[str, int]:
        """Count active position occurrences per currency code."""
        exposure: dict[str, int] = {}
        for sym in self.open_symbols():
            eng = self.engines[sym]
            if eng.position.side == "flat":
                continue
            clean = sym.rstrip("m").upper()
            base, quote = clean[:3], clean[3:6]
            if len(clean) >= 6:
                exposure[base] = exposure.get(base, 0) + 1
                exposure[quote] = exposure.get(quote, 0) + 1
        return exposure

    def approve_entry(
        self,
        symbol: str,
        raw_action: int,
        equity: float,
        price: float,
        atr: float,
        now: datetime | None = None,
        lot_mult: float = 1.0,
    ) -> TradeOrder:
        """Approve a new entry only if capacity and currency exposure limits allow."""
        now = now or datetime.now(timezone.utc)
        halted, _ = self.sync_circuit_breaker(equity, now)
        if halted:
            return TradeOrder(
                action="hold",
                approved=False,
                reason=f"Circuit breaker until {self.halt_until}",
            )
        eng = self.engine(symbol)
        if eng.position.side == "flat" and raw_action in (1, 2):
            if not self.has_capacity():
                return TradeOrder(
                    action="hold",
                    approved=False,
                    reason=f"Max open positions ({self.max_open_positions})",
                )
            max_curr = self.risk_cfg.max_currency_exposure
            if max_curr > 0:
                clean = symbol.rstrip("m").upper()
                base, quote = clean[:3], clean[3:6]
                exp = self.get_currency_exposure()
                if exp.get(base, 0) >= max_curr or exp.get(quote, 0) >= max_curr:
                    return TradeOrder(
                        action="hold",
                        approved=False,
                        reason=f"Currency exposure limit hit ({base}/{quote} >= {max_curr})",
                    )
        return eng.approve_and_size(
            raw_action, equity, price, atr, now=now, lot_mult=lot_mult
        )
