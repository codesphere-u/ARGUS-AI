"""Money-path unit tests: risk engine sizing + circuit breaker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config import RISK
from risk_engine import MultiSymbolRiskManager, RiskEngine


def test_normalize_volume_clamps_and_steps():
    eng = RiskEngine(symbol="EURUSDm", enforce_macro=False)
    eng.min_lot = 0.01
    eng.max_lot = 1.0
    eng.lot_step = 0.01
    assert eng.normalize_volume(0.0) == 0.0
    assert eng.normalize_volume(0.005) == 0.0 or eng.normalize_volume(0.005) == 0.01
    assert eng.normalize_volume(0.019) == 0.01
    assert eng.normalize_volume(5.0) <= 1.0


def test_compute_lot_size_respects_money_cap():
    eng = RiskEngine(symbol="EURUSDm", enforce_macro=False)
    eng.contract_size = 100_000.0
    eng.risk_money_override = 50.0
    # 50 pips SL on EURUSD-ish: 0.0050 price distance
    lots = eng.compute_lot_size(equity=100_000.0, entry_price=1.1000, stop_loss=1.0950)
    # risk_amount=50; loss_per_lot = 100000*0.005 = 500 → 0.1 lots
    assert 0.09 <= lots <= 0.11


def test_initial_stops_atr_multiples():
    eng = RiskEngine(symbol="EURUSDm", enforce_macro=False)
    sl, tp = eng.initial_stops("buy", 1.1000, 0.0010)
    assert abs((1.1000 - sl) - RISK.stop_loss_atr_mult * 0.0010) < 1e-9
    assert abs((tp - 1.1000) - RISK.take_profit_atr_mult * 0.0010) < 1e-9
    sl2, tp2 = eng.initial_stops("sell", 1.1000, 0.0010)
    assert sl2 > 1.1000
    assert tp2 < 1.1000


def test_circuit_breaker_triggers_and_reports_newly():
    mgr = MultiSymbolRiskManager(
        symbols=("EURUSDm",),
        max_open_positions=2,
        risk_cfg=RISK,
    )
    mgr.day_start_balance = 10_000.0
    mgr.day_anchor = datetime.now(timezone.utc)
    # Force enabled path — RISK already has circuit_breaker_enabled from env/default
    if not RISK.circuit_breaker_enabled:
        return
    # Drop equity by more than daily_loss_limit
    equity = 10_000.0 * (1.0 - RISK.daily_loss_limit - 0.01)
    halted, newly = mgr.sync_circuit_breaker(equity)
    assert halted is True
    assert newly is True
    assert mgr.halt_until is not None
    # Second call while still halted → newly=False
    halted2, newly2 = mgr.sync_circuit_breaker(equity)
    assert halted2 is True
    assert newly2 is False


def test_circuit_breaker_clears_after_halt_until():
    mgr = MultiSymbolRiskManager(symbols=("EURUSDm",), max_open_positions=2)
    mgr.halt_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    mgr.day_start_balance = 10_000.0
    mgr.day_anchor = datetime.now(timezone.utc)
    halted, newly = mgr.sync_circuit_breaker(10_000.0)
    assert halted is False
    assert newly is False
    assert mgr.halt_until is None


def test_partial_close_multi_percentages():
    eng = RiskEngine(symbol="EURUSDm", enforce_macro=False)
    eng.register_fill("buy", volume=1.00, price=1.1000, stop_loss=1.0950, take_profit=1.1100, atr=0.0010)
    
    # 25% close of 1.00 lot -> 0.25 closed, 0.75 remaining
    closed = eng.partial_close(0.25)
    assert abs(closed - 0.25) < 1e-9
    assert abs(eng.position.volume - 0.75) < 1e-9

    # 50% close of 0.75 lot -> 0.38 closed, 0.37 remaining
    closed2 = eng.partial_close(0.50)
    assert closed2 > 0
    assert eng.position.volume > 0


def test_move_sl_to_be_buy_and_sell():
    eng = RiskEngine(symbol="EURUSDm", enforce_macro=False)
    eng.register_fill("buy", volume=0.50, price=1.1000, stop_loss=1.0950, take_profit=1.1100, atr=0.0010)
    ok = eng.move_sl_to_be(spread_pips_buffer=1.0)
    assert ok is True
    assert eng.position.stop_loss >= 1.1000

    eng_sell = RiskEngine(symbol="GBPUSDm", enforce_macro=False)
    eng_sell.register_fill("sell", volume=0.50, price=1.3000, stop_loss=1.3050, take_profit=1.2900, atr=0.0010)
    ok_sell = eng_sell.move_sl_to_be(spread_pips_buffer=1.0)
    assert ok_sell is True
    assert eng_sell.position.stop_loss <= 1.3000


def test_currency_exposure():
    mgr = MultiSymbolRiskManager(symbols=("EURUSDm", "GBPUSDm", "USDJPYm"))
    mgr.engine("EURUSDm").register_fill("buy", volume=0.1, price=1.1000, stop_loss=1.0950, take_profit=1.1100, atr=0.001)
    mgr.engine("GBPUSDm").register_fill("buy", volume=0.1, price=1.3000, stop_loss=1.2950, take_profit=1.3100, atr=0.001)
    exp = mgr.get_currency_exposure()
    assert exp.get("USD") == 2
    assert exp.get("EUR") == 1
    assert exp.get("GBP") == 1
