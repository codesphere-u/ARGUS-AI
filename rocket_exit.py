"""Rocket profit-lock and in-trade loss minimization."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

from config import MULTI, MultiPairConfig
from risk_engine import PositionState

RocketAction = Literal["hold", "close", "tighten_sl", "partial_close"]


@dataclass
class RocketState:
    """Tracks peak profit and bar age for one open position."""

    symbol: str
    peak_unrealized: float = 0.0
    initial_risk_money: float = 0.0
    bars_open: int = 0
    last_bar_time: datetime | None = None
    breakeven_done: bool = False
    partial_done: bool = False
    rocket_armed: bool = False


@dataclass
class RocketDecision:
    """Exit / SL advice from the rocket manager."""

    action: RocketAction = "hold"
    reason: str = ""
    new_stop_loss: float | None = None
    partial_close_pct: float = 0.5


@dataclass
class RocketExitManager:
    """
    Protect open profits and cut losses early — positive-expectancy ladder.

    Rules (priority order)
    -----
    1. Hard loss cut at −rocket_hard_loss_r × initial risk money.
    2. Breakeven after +rocket_be_atr × ATR profit.
    3. Trail stop ~rocket_trail_atr × ATR behind price after BE is armed
       (lets winners run toward structural TP / 2–4R).
    4. Rocket pullback close if drawdown from peak ≥ pullback_pct.
    5. Scalp TP only as a large-move lock (default ~2×ATR — not a micro scalp).
    6. Time-profit / time-decay: stalled-trade fallback only after many bars.
    """

    cfg: MultiPairConfig = field(default_factory=lambda: MULTI)
    states: dict[str, RocketState] = field(default_factory=dict)

    def register_open(
        self,
        symbol: str,
        *,
        entry: float,
        stop_loss: float,
        volume: float,
        contract_size: float,
        bar_time: datetime | None = None,
    ) -> None:
        risk_dist = abs(entry - stop_loss)
        initial_risk = risk_dist * volume * contract_size
        self.states[symbol] = RocketState(
            symbol=symbol,
            peak_unrealized=0.0,
            initial_risk_money=max(initial_risk, 1e-8),
            bars_open=0,
            last_bar_time=bar_time,
        )

    def register_close(self, symbol: str) -> None:
        self.states.pop(symbol, None)

    def _unrealized(
        self,
        pos: PositionState,
        price: float,
        contract_size: float,
    ) -> float:
        if pos.side == "buy":
            return (price - pos.entry_price) * pos.volume * contract_size
        if pos.side == "sell":
            return (pos.entry_price - price) * pos.volume * contract_size
        return 0.0

    def _price_profit(self, pos: PositionState, price: float) -> float:
        if pos.side == "buy":
            return price - pos.entry_price
        if pos.side == "sell":
            return pos.entry_price - price
        return 0.0

    def _advance_bar(self, st: RocketState, bar_time: datetime | None) -> None:
        """Increment bars_open only when the M15 (or env) bar timestamp advances."""
        if bar_time is None:
            # Fallback: elapsed-minute proxy (~15m bars) so poll ticks never count as bars
            now = datetime.now(timezone.utc)
            if st.last_bar_time is None:
                st.last_bar_time = now
                return
            elapsed_min = (now - st.last_bar_time).total_seconds() / 60.0
            if elapsed_min >= 15.0:
                steps = int(elapsed_min // 15.0)
                st.bars_open += steps
                st.last_bar_time = st.last_bar_time + timedelta(minutes=15 * steps)
            return

        if bar_time.tzinfo is None:
            bar_time = bar_time.replace(tzinfo=timezone.utc)
        if st.last_bar_time is None:
            st.last_bar_time = bar_time
            return
        last = st.last_bar_time
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if bar_time > last:
            st.bars_open += 1
            st.last_bar_time = bar_time

    def evaluate(
        self,
        symbol: str,
        pos: PositionState,
        *,
        price: float,
        atr: float,
        contract_size: float,
        now: datetime | None = None,
        bar_time: datetime | None = None,
    ) -> RocketDecision:
        """Return hold / close / tighten_sl for the open position."""
        if pos.side == "flat":
            return RocketDecision(action="hold", reason="flat")

        st = self.states.get(symbol)
        if st is None:
            self.register_open(
                symbol,
                entry=pos.entry_price,
                stop_loss=pos.stop_loss or pos.entry_price,
                volume=pos.volume,
                contract_size=contract_size,
                bar_time=bar_time or now,
            )
            st = self.states[symbol]

        self._advance_bar(st, bar_time if bar_time is not None else now)
        atr = max(atr, 1e-12)
        unreal = self._unrealized(pos, price, contract_size)
        price_pnl = self._price_profit(pos, price)
        st.peak_unrealized = max(st.peak_unrealized, unreal)

        # 1) Hard loss cut — real emergency exit (bounded risk)
        if unreal <= -self.cfg.rocket_hard_loss_r * st.initial_risk_money:
            return RocketDecision(
                action="close",
                reason=f"Hard loss cut ({unreal:.2f} <= -{self.cfg.rocket_hard_loss_r:.1f}R)",
            )

        # 2) Breakeven SL after meaningful progress
        be_trigger = self.cfg.rocket_be_atr * atr
        if not st.breakeven_done and price_pnl >= be_trigger:
            buffer = 0.05 * atr
            if pos.side == "buy":
                new_sl = pos.entry_price + buffer
                if new_sl > pos.stop_loss:
                    st.breakeven_done = True
                    return RocketDecision(
                        action="tighten_sl",
                        reason="Breakeven SL armed",
                        new_stop_loss=new_sl,
                    )
            else:
                new_sl = pos.entry_price - buffer
                if pos.stop_loss == 0 or new_sl < pos.stop_loss:
                    st.breakeven_done = True
                    return RocketDecision(
                        action="tighten_sl",
                        reason="Breakeven SL armed",
                        new_stop_loss=new_sl,
                    )

        # 3) Trail after breakeven — primary mechanism to let winners run
        trail_atr = float(getattr(self.cfg, "rocket_trail_atr", 1.0))
        if st.breakeven_done and price_pnl >= be_trigger:
            trail = trail_atr * atr
            if pos.side == "buy":
                new_sl = price - trail
                if new_sl > pos.stop_loss:
                    return RocketDecision(
                        action="tighten_sl",
                        reason=f"Trail SL ({trail_atr:.2f} ATR)",
                        new_stop_loss=new_sl,
                    )
            else:
                new_sl = price + trail
                if pos.stop_loss == 0 or new_sl < pos.stop_loss:
                    return RocketDecision(
                        action="tighten_sl",
                        reason=f"Trail SL ({trail_atr:.2f} ATR)",
                        new_stop_loss=new_sl,
                    )

        # 4) Rocket pullback close from peak (only after meaningful peak)
        min_peak = self.cfg.rocket_min_pullback_atr * atr * pos.volume * contract_size
        if st.peak_unrealized >= min_peak and st.peak_unrealized > 0:
            st.rocket_armed = True
            pullback = st.peak_unrealized - unreal
            if pullback >= self.cfg.rocket_pullback_pct * st.peak_unrealized:
                return RocketDecision(
                    action="close",
                    reason=(
                        f"Rocket close: pullback {pullback:.2f} from peak "
                        f"{st.peak_unrealized:.2f} ({self.cfg.rocket_pullback_pct:.0%})"
                    ),
                )

        # 5) Large-move scalp lock (default ~2 ATR — not a micro scalp)
        scalp_trigger = self.cfg.rocket_scalp_atr * atr
        if price_pnl >= scalp_trigger:
            return RocketDecision(
                action="close",
                reason=f"Scalp TP at +{self.cfg.rocket_scalp_atr:.2f} ATR",
            )

        # 6a) Time-profit — stalled-trade fallback only after many bars
        if (
            st.bars_open >= self.cfg.rocket_time_profit_close_bars
            and price_pnl >= self.cfg.rocket_time_profit_min_atr * atr
        ):
            return RocketDecision(
                action="close",
                reason=(
                    f"Time-profit after {st.bars_open} bars "
                    f"(>={self.cfg.rocket_time_profit_min_atr:.2f} ATR)"
                ),
            )

        # 6b) Time decay — tighten trail if stagnating after N real bars
        if (
            st.bars_open >= self.cfg.rocket_time_bars
            and 0 < price_pnl < self.cfg.rocket_stale_profit_atr * atr
        ):
            trail = 0.75 * atr
            if pos.side == "buy":
                new_sl = price - trail
                if new_sl > pos.stop_loss:
                    return RocketDecision(
                        action="tighten_sl",
                        reason="Time-decay trail tighten",
                        new_stop_loss=new_sl,
                    )
            else:
                new_sl = price + trail
                if pos.stop_loss == 0 or new_sl < pos.stop_loss:
                    return RocketDecision(
                        action="tighten_sl",
                        reason="Time-decay trail tighten",
                        new_stop_loss=new_sl,
                    )

        return RocketDecision(action="hold", reason="ok")
