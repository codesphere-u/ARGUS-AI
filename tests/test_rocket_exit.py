"""Rocket exit: real-bar timing, hard cut, scalp, breakeven, trail."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from config import MULTI
from risk_engine import PositionState
from rocket_exit import RocketExitManager


def _pos(side="buy", entry=1.10, sl=1.088, vol=0.1) -> PositionState:
    return PositionState(
        side=side,
        volume=vol,
        entry_price=entry,
        stop_loss=sl,
        take_profit=1.13,
        atr_at_entry=0.01,
    )


def test_bars_open_only_on_new_bar_time():
    r = RocketExitManager()
    pos = _pos()
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    r.register_open(
        "EURUSDm",
        entry=1.10,
        stop_loss=1.088,
        volume=0.1,
        contract_size=100_000,
        bar_time=t0,
    )
    r.evaluate(
        "EURUSDm", pos, price=1.101, atr=0.01, contract_size=100_000, bar_time=t0
    )
    assert r.states["EURUSDm"].bars_open == 0
    r.evaluate(
        "EURUSDm",
        pos,
        price=1.101,
        atr=0.01,
        contract_size=100_000,
        bar_time=t0 + timedelta(minutes=15),
    )
    assert r.states["EURUSDm"].bars_open == 1
    # Same bar again — no increment
    r.evaluate(
        "EURUSDm",
        pos,
        price=1.101,
        atr=0.01,
        contract_size=100_000,
        bar_time=t0 + timedelta(minutes=15),
    )
    assert r.states["EURUSDm"].bars_open == 1


def test_hard_loss_cut():
    cfg = replace(MULTI, rocket_hard_loss_r=1.0)
    r = RocketExitManager(cfg=cfg)
    pos = _pos(entry=1.10, sl=1.088)  # risk dist 0.012
    r.register_open(
        "EURUSDm",
        entry=1.10,
        stop_loss=1.088,
        volume=0.1,
        contract_size=100_000,
    )
    d = r.evaluate(
        "EURUSDm",
        pos,
        price=1.07,
        atr=0.01,
        contract_size=100_000,
        bar_time=datetime.now(timezone.utc),
    )
    assert d.action == "close"
    assert "Hard loss" in d.reason


def test_scalp_at_configured_atr():
    # BE fires before scalp on first touch; second call at scalp level closes
    cfg = replace(MULTI, rocket_be_atr=0.5, rocket_scalp_atr=2.0, rocket_trail_atr=5.0)
    r = RocketExitManager(cfg=cfg)
    atr = 0.01
    pos = _pos(entry=1.10, sl=1.088)
    r.register_open(
        "EURUSDm",
        entry=1.10,
        stop_loss=1.088,
        volume=0.1,
        contract_size=100_000,
    )
    # Arm BE first
    d1 = r.evaluate(
        "EURUSDm",
        pos,
        price=1.10 + 0.6 * atr,
        atr=atr,
        contract_size=100_000,
        bar_time=datetime.now(timezone.utc),
    )
    assert d1.action == "tighten_sl"
    if d1.new_stop_loss:
        pos.stop_loss = d1.new_stop_loss
    # Now hit large scalp
    d = r.evaluate(
        "EURUSDm",
        pos,
        price=1.10 + 2.0 * atr + 0.0001,
        atr=atr,
        contract_size=100_000,
        bar_time=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    assert d.action == "close"
    assert "Scalp" in d.reason


def test_breakeven_arms():
    r = RocketExitManager()
    atr = 0.01
    pos = _pos(entry=1.10, sl=1.088)
    r.register_open(
        "EURUSDm",
        entry=1.10,
        stop_loss=1.088,
        volume=0.1,
        contract_size=100_000,
    )
    be = r.cfg.rocket_be_atr * atr
    price = 1.10 + be + 0.0001
    assert (price - 1.10) < r.cfg.rocket_scalp_atr * atr
    d = r.evaluate(
        "EURUSDm",
        pos,
        price=price,
        atr=atr,
        contract_size=100_000,
        bar_time=datetime.now(timezone.utc),
    )
    assert d.action == "tighten_sl"
    assert d.new_stop_loss is not None
    assert d.new_stop_loss > pos.stop_loss


def test_trail_after_breakeven():
    cfg = replace(MULTI, rocket_be_atr=0.5, rocket_trail_atr=1.0, rocket_scalp_atr=5.0)
    r = RocketExitManager(cfg=cfg)
    atr = 0.01
    pos = _pos(entry=1.10, sl=1.088)
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    r.register_open(
        "EURUSDm",
        entry=1.10,
        stop_loss=1.088,
        volume=0.1,
        contract_size=100_000,
        bar_time=t0,
    )
    d1 = r.evaluate(
        "EURUSDm",
        pos,
        price=1.10 + 0.6 * atr,
        atr=atr,
        contract_size=100_000,
        bar_time=t0,
    )
    assert d1.action == "tighten_sl"
    pos.stop_loss = float(d1.new_stop_loss or pos.stop_loss)
    d2 = r.evaluate(
        "EURUSDm",
        pos,
        price=1.10 + 1.5 * atr,
        atr=atr,
        contract_size=100_000,
        bar_time=t0 + timedelta(minutes=15),
    )
    assert d2.action == "tighten_sl"
    assert "Trail" in d2.reason
    assert d2.new_stop_loss is not None
    assert d2.new_stop_loss > pos.stop_loss


def test_time_profit_close_after_bars():
    cfg = replace(
        MULTI,
        rocket_time_profit_close_bars=4,
        rocket_time_profit_min_atr=0.3,
        rocket_be_atr=2.0,
        rocket_scalp_atr=5.0,
        rocket_time_bars=50,
    )
    r = RocketExitManager(cfg=cfg)
    atr = 0.01
    min_pnl = cfg.rocket_time_profit_min_atr * atr
    pos = _pos(entry=1.10, sl=1.088)
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    r.register_open(
        "EURUSDm",
        entry=1.10,
        stop_loss=1.088,
        volume=0.1,
        contract_size=100_000,
        bar_time=t0,
    )
    price = 1.10 + min_pnl + 0.00005
    d = None
    for i in range(1, cfg.rocket_time_profit_close_bars + 1):
        d = r.evaluate(
            "EURUSDm",
            pos,
            price=price,
            atr=atr,
            contract_size=100_000,
            bar_time=t0 + timedelta(minutes=15 * i),
        )
    assert d is not None
    assert d.action == "close"
    assert "Time-profit" in d.reason
