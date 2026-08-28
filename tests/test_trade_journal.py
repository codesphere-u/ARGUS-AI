"""Trade journal open/close/ticket round-trip."""

from __future__ import annotations

from pathlib import Path

from trade_journal import TradeJournal


def test_journal_open_close_ticket(tmp_path: Path):
    db = tmp_path / "trades.db"
    j = TradeJournal(db_path=db)
    tid = j.log_open(
        symbol="EURUSDm",
        side="buy",
        volume=0.1,
        entry=1.1000,
        action=1,
        ticket=12345,
    )
    assert tid > 0
    assert j.find_open_id("EURUSDm") == tid
    j.set_ticket(tid, 99999)
    j.log_close(trade_id=tid, symbol="EURUSDm", exit_price=1.1050, pnl=50.0)
    assert j.find_open_id("EURUSDm") is None
    recent = j.load_recent(days=7)
    assert any(r.get("symbol") == "EURUSDm" for r in recent)
    snap = j.performance_snapshot(
        equity=10_050.0, balance=10_050.0, initial_balance=10_000.0
    )
    assert int(snap.get("closed_trades") or 0) >= 1


def test_orphan_stale_opens(tmp_path: Path):
    db = tmp_path / "trades.db"
    j = TradeJournal(db_path=db)
    keep = j.log_open(
        symbol="EURUSDm", side="buy", volume=0.1, entry=1.1, action=1, ticket=111
    )
    ghost = j.log_open(
        symbol="GBPUSDm", side="buy", volume=0.1, entry=1.3, action=1, ticket=None
    )
    n = j.orphan_stale_opens(live_tickets={111}, live_symbols=set())
    assert n == 1
    assert j.find_open_id("EURUSDm") == keep
    assert j.find_open_id("GBPUSDm") is None
    # ghost should be orphaned, not counted as open
    assert j.count_open() == 1
    del ghost
