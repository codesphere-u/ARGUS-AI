"""Currency exposure / correlation block tests."""

from __future__ import annotations

from datetime import datetime, timezone

from live_brain import (
    currency_codes_for_symbol,
    currency_exposure_block,
    is_always_liquid,
    usd_fx_long_cluster_block,
)
from macro_filter import CalendarEvent, MacroFilter
from risk_engine import default_contract_size


def test_currency_exposure_blocks_third_usd_long():
    opens = [
        {"symbol": "EURUSDm", "side": "buy"},
        {"symbol": "GBPUSDm", "side": "buy"},
    ]
    blocked, reason = currency_exposure_block(
        "AUDUSDm",
        "buy",
        open_positions=opens,
        score=0.80,
        max_exposure=2,
    )
    assert blocked
    assert "USD" in reason or "exposure" in reason.lower()


def test_currency_exposure_allows_elite_override():
    opens = [
        {"symbol": "EURUSDm", "side": "buy"},
        {"symbol": "GBPUSDm", "side": "buy"},
    ]
    blocked, _ = currency_exposure_block(
        "AUDUSDm",
        "buy",
        open_positions=opens,
        score=0.95,
        max_exposure=2,
        elite_override=0.92,
    )
    assert not blocked


def test_currency_exposure_sell_offsets_long():
    opens = [{"symbol": "EURUSDm", "side": "buy"}]
    # Selling EURUSD reduces EUR long / USD short — should be fine
    blocked, _ = currency_exposure_block(
        "EURUSDm",
        "sell",
        open_positions=opens,
        score=0.80,
        max_exposure=2,
    )
    assert not blocked


def test_legacy_wrapper_still_callable():
    blocked, _ = usd_fx_long_cluster_block(
        "AUDUSDm",
        "buy",
        open_symbols=["EURUSDm", "GBPUSDm"],
        score=0.80,
    )
    # With long-only open_symbols mapping, AUDUSD buy adds more USD-short exposure
    assert isinstance(blocked, bool)


def test_fx_weekend_gate():
    # Friday 21:00 UTC → closed
    fri = datetime(2026, 8, 7, 21, 0, tzinfo=timezone.utc)  # Friday
    assert fri.weekday() == 4
    assert MacroFilter.is_fx_market_open(fri) is False
    # Saturday → closed
    sat = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    assert MacroFilter.is_fx_market_open(sat) is False
    # Sunday 21:00 → still closed
    sun = datetime(2026, 8, 9, 21, 0, tzinfo=timezone.utc)
    assert MacroFilter.is_fx_market_open(sun) is False
    # Sunday 22:30 → open
    sun_open = datetime(2026, 8, 9, 22, 30, tzinfo=timezone.utc)
    assert MacroFilter.is_fx_market_open(sun_open) is True
    # Tuesday → open
    tue = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    assert MacroFilter.is_fx_market_open(tue) is True


def test_always_liquid_tags_and_legs():
    for sym in ("BTCUSDm", "ETHUSDm", "SOLUSDm", "XRPUSDm", "XAGUSDm", "XAUUSDm"):
        assert is_always_liquid(sym), sym
    assert currency_codes_for_symbol("SOLUSDm") == {"SOL", "USD"}
    assert currency_codes_for_symbol("XRPUSDm") == {"XRP", "USD"}
    assert currency_codes_for_symbol("XAGUSDm") == {"XAG", "USD"}
    assert currency_codes_for_symbol("EURJPYm") == {"EUR", "JPY"}


def test_always_liquid_skips_weekend_gate():
    fri = datetime(2026, 8, 7, 21, 0, tzinfo=timezone.utc)
    mf = MacroFilter()
    mf._events = []
    mf._calendar_ok = True
    for sym in ("SOLUSDm", "XRPUSDm", "XAGUSDm", "BTCUSDm"):
        allowed, reason = mf.is_trading_allowed(fri, sym)
        assert allowed, f"{sym}: {reason}"
    fx_ok, fx_reason = mf.is_trading_allowed(fri, "EURUSDm")
    assert not fx_ok
    assert "weekend" in fx_reason.lower()


def test_crypto_calendar_ignores_eur_news():
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    mf = MacroFilter()
    mf._calendar_ok = True
    # Prevent disk/network refresh from replacing injected events
    mf.refresh = lambda now=None: None  # type: ignore[method-assign]
    mf._events = [
        CalendarEvent(
            title="ECB Rate Decision",
            country="EUR",
            impact="High",
            when=now,
        )
    ]
    assert "EUR" not in mf.currencies_for_symbol("BTCUSDm")
    assert "BTC" in mf.currencies_for_symbol("BTCUSDm")
    assert mf.high_impact_events(now, "BTCUSDm") == []
    allowed, reason = mf.is_trading_allowed(now, "BTCUSDm")
    assert allowed, reason
    # EURUSD still blacked out by EUR event
    eur_allowed, eur_reason = mf.is_trading_allowed(now, "EURUSDm")
    assert not eur_allowed
    assert "blackout" in eur_reason.lower() or "ECB" in eur_reason


def test_crypto_usd_exposure_still_counts():
    opens = [
        {"symbol": "EURUSDm", "side": "buy"},
        {"symbol": "GBPUSDm", "side": "buy"},
    ]
    blocked, reason = currency_exposure_block(
        "BTCUSDm",
        "buy",
        open_positions=opens,
        score=0.80,
        max_exposure=2,
    )
    assert blocked
    assert "USD" in reason or "exposure" in reason.lower()


def test_default_contract_sizes_for_new_assets():
    assert default_contract_size("BTCUSDm") == 1.0
    assert default_contract_size("ETHUSDm") == 1.0
    assert default_contract_size("SOLUSDm") == 1.0
    assert default_contract_size("XAGUSDm") == 100.0
    assert default_contract_size("EURUSDm") == 100_000.0


def test_symbol_matching_aliases_and_suffixes():
    from main_bot import _build_command_listener, RuntimeControl
    from risk_engine import MultiSymbolRiskManager
    from rocket_exit import RocketExitManager
    from trade_journal import TradeJournal

    mgr = MultiSymbolRiskManager(symbols=("EURUSDm", "GBPUSDm", "USDJPYm", "XAUUSDm", "BTCUSDm", "ETHUSDm", "SOLUSDm"))
    rock = RocketExitManager()
    journ = TradeJournal()

    class DummyAccount:
        equity = 10000.0
        balance = 10000.0

    listener = _build_command_listener(
        control=RuntimeControl(),
        manager=mgr,
        rocket=rock,
        account=DummyAccount(),
        journal=journ,
        journal_ids={},
        loss_guard=None,
        cfg=None,
        mode="PAPER",
        paper=True,
        equity_history=[10000.0],
        session_start_equity=10000.0,
        get_live_context=lambda: {"frames": {}, "rows": {}, "opps": []},
    )
    assert listener is not None
    # Verify commands are registered without errors
    assert "chart" in listener.handlers
    assert "partial" in listener.handlers
    assert "be" in listener.handlers
    assert "be_all" in listener.handlers
    assert "digest" in listener.handlers
    assert "calc" in listener.handlers
