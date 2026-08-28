"""Append-only trade journal for online learning."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import JOURNAL_DIR

logger = logging.getLogger(__name__)


@dataclass
class JournalTrade:
    """Closed (or open) trade record."""

    id: int | None
    opened_at: str
    closed_at: str | None
    symbol: str
    side: str
    volume: float
    entry: float
    exit: float | None
    pnl: float | None
    action: int
    obs_json: str
    in_blackout: float
    in_london: float
    in_ny: float
    in_overlap: float
    status: str  # open | closed


class TradeJournal:
    """SQLite journal under ``data/journal/trades.db``."""

    def __init__(self, db_path: Path | None = None) -> None:
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path or (JOURNAL_DIR / "trades.db")
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    volume REAL NOT NULL,
                    entry REAL NOT NULL,
                    exit REAL,
                    pnl REAL,
                    action INTEGER NOT NULL,
                    obs_json TEXT NOT NULL,
                    in_blackout REAL DEFAULT 0,
                    in_london REAL DEFAULT 0,
                    in_ny REAL DEFAULT 0,
                    in_overlap REAL DEFAULT 0,
                    status TEXT NOT NULL,
                    ticket INTEGER
                )
                """
            )
            # Migrate older DBs missing ticket column
            cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
            if "ticket" not in cols:
                conn.execute("ALTER TABLE trades ADD COLUMN ticket INTEGER")
            conn.commit()

    def log_open(
        self,
        *,
        symbol: str,
        side: str,
        volume: float,
        entry: float,
        action: int,
        obs: list[float] | None = None,
        macro: dict[str, float] | None = None,
        opened_at: datetime | None = None,
        ticket: int | None = None,
    ) -> int:
        """Record a newly opened position; returns row id."""
        opened_at = opened_at or datetime.now(timezone.utc)
        macro = macro or {}
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO trades (
                    opened_at, closed_at, symbol, side, volume, entry, exit, pnl,
                    action, obs_json, in_blackout, in_london, in_ny, in_overlap, status, ticket
                ) VALUES (?, NULL, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    opened_at.isoformat(),
                    symbol,
                    side,
                    float(volume),
                    float(entry),
                    int(action),
                    json.dumps(list(obs or [])),
                    float(macro.get("in_blackout", 0.0)),
                    float(macro.get("in_london", 0.0)),
                    float(macro.get("in_ny", 0.0)),
                    float(macro.get("in_overlap", 0.0)),
                    int(ticket) if ticket is not None else None,
                ),
            )
            conn.commit()
            row_id = int(cur.lastrowid)
        logger.info("Journal open id=%s %s %s @ %s ticket=%s", row_id, side, symbol, entry, ticket)
        return row_id

    def find_open_id(self, symbol: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM trades
                WHERE status='open' AND symbol=?
                ORDER BY id DESC LIMIT 1
                """,
                (symbol,),
            ).fetchone()
            return int(row["id"]) if row else None

    def set_ticket(self, trade_id: int, ticket: int | None) -> None:
        if ticket is None:
            return
        with self._connect() as conn:
            conn.execute(
                "UPDATE trades SET ticket=? WHERE id=?",
                (int(ticket), int(trade_id)),
            )
            conn.commit()

    def log_close(
        self,
        *,
        trade_id: int | None = None,
        symbol: str | None = None,
        exit_price: float,
        pnl: float,
        closed_at: datetime | None = None,
    ) -> None:
        """Close the latest open trade for ``symbol`` (required when multi-pair)."""
        closed_at = closed_at or datetime.now(timezone.utc)
        with self._connect() as conn:
            if trade_id is None:
                if not symbol:
                    logger.warning("Journal close needs trade_id or symbol in multi-pair mode")
                    return
                row = conn.execute(
                    """
                    SELECT id FROM trades
                    WHERE status='open' AND symbol=?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (symbol,),
                ).fetchone()
                if row is None:
                    logger.warning("Journal close: no open trade for %s", symbol)
                    return
                trade_id = int(row["id"])
            conn.execute(
                """
                UPDATE trades
                SET closed_at=?, exit=?, pnl=?, status='closed'
                WHERE id=? AND status='open'
                """,
                (closed_at.isoformat(), float(exit_price), float(pnl), trade_id),
            )
            conn.commit()
        logger.info("Journal close id=%s exit=%s pnl=%.2f", trade_id, exit_price, pnl)

    def open_trade_id(self, symbol: str) -> int | None:
        """Return the open journal id for a symbol, if any."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM trades
                WHERE status='open' AND symbol=?
                ORDER BY id DESC LIMIT 1
                """,
                (symbol,),
            ).fetchone()
            return int(row["id"]) if row else None

    def open_symbols(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM trades WHERE status='open'"
            ).fetchall()
            return [str(r["symbol"]) for r in rows]

    def count_closed(self, days: int = 7) -> int:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE status='closed' AND closed_at >= ?",
                (since,),
            ).fetchone()
            return int(row["n"] if row else 0)

    def load_recent(self, days: int = 7) -> list[dict[str, Any]]:
        """Load closed trades from the last ``days`` days."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM trades
                WHERE status='closed' AND closed_at >= ?
                ORDER BY closed_at ASC
                """,
                (since,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                d["obs"] = json.loads(d.get("obs_json") or "[]")
            except json.JSONDecodeError:
                d["obs"] = []
            out.append(d)
        return out

    def has_open_position(self, symbol: str | None = None) -> bool:
        with self._connect() as conn:
            if symbol:
                row = conn.execute(
                    "SELECT 1 FROM trades WHERE status='open' AND symbol=? LIMIT 1",
                    (symbol,),
                ).fetchone()
            else:
                row = conn.execute("SELECT 1 FROM trades WHERE status='open' LIMIT 1").fetchone()
            return row is not None

    def count_open(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE status='open'"
            ).fetchone()
            return int(row["n"] if row else 0)

    def orphan_stale_opens(
        self,
        *,
        live_tickets: set[int] | None = None,
        live_symbols: set[str] | None = None,
    ) -> int:
        """
        Mark journal rows still ``status='open'`` as ``orphaned`` when they no
        longer match a live MT5 ticket / managed symbol.

        Returns the number of rows orphaned. Orphaned rows are excluded from
        open-position counts and public performance snapshots.
        """
        live_tickets = live_tickets or set()
        live_symbols = {s.upper() for s in (live_symbols or set())}
        now = datetime.now(timezone.utc).isoformat()
        orphaned = 0
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, symbol, ticket FROM trades WHERE status='open'"
            ).fetchall()
            for row in rows:
                ticket = row["ticket"]
                sym = str(row["symbol"] or "").upper()
                keep = False
                if ticket is not None and int(ticket) in live_tickets:
                    keep = True
                elif ticket is None and sym in live_symbols:
                    # Paper / no-ticket but still managed in-process
                    keep = True
                if keep:
                    continue
                conn.execute(
                    """
                    UPDATE trades
                    SET status='orphaned', closed_at=COALESCE(closed_at, ?)
                    WHERE id=? AND status='open'
                    """,
                    (now, int(row["id"])),
                )
                orphaned += 1
            conn.commit()
        if orphaned:
            logger.info("Journal orphaned %s stale open row(s)", orphaned)
        return orphaned

    def performance_snapshot(
        self,
        *,
        equity: float,
        balance: float,
        initial_balance: float,
    ) -> dict[str, Any]:
        """
        Aggregate public performance stats for channel progress posts.

        Invested baseline = initial_balance. Realized PnL from closed trades.
        """
        today = datetime.now(timezone.utc).date().isoformat()
        with self._connect() as conn:
            closed = conn.execute(
                """
                SELECT pnl FROM trades
                WHERE status='closed' AND pnl IS NOT NULL
                """
            ).fetchall()
            today_rows = conn.execute(
                """
                SELECT pnl FROM trades
                WHERE status='closed' AND pnl IS NOT NULL
                  AND closed_at IS NOT NULL AND substr(closed_at, 1, 10) = ?
                """,
                (today,),
            ).fetchall()
            open_n = conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE status='open'"
            ).fetchone()

        pnls = [float(r["pnl"]) for r in closed]
        today_pnls = [float(r["pnl"]) for r in today_rows]
        wins = [p for p in pnls if p > 0]
        realized = float(sum(pnls)) if pnls else 0.0
        today_pnl = float(sum(today_pnls)) if today_pnls else 0.0
        invested = float(initial_balance)
        eq = float(equity)
        bal = float(balance)
        total_return_pct = ((eq - invested) / invested * 100.0) if invested > 0 else 0.0
        win_rate = (len(wins) / len(pnls) * 100.0) if pnls else 0.0

        return {
            "invested": invested,
            "equity": eq,
            "balance": bal,
            "realized_pnl": realized,
            "unrealized_pnl": eq - bal,
            "total_pnl": eq - invested,
            "total_return_pct": total_return_pct,
            "today_pnl": today_pnl,
            "closed_trades": len(pnls),
            "wins": len(wins),
            "losses": len(pnls) - len(wins),
            "win_rate": win_rate,
            "open_positions": int(open_n["n"] if open_n else 0),
        }

    def detailed_analytics(self) -> dict[str, float]:
        """Compute advanced trading metrics: Profit Factor, Expectancy, Sharpe Ratio, Profit/Loss Ratio."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT pnl FROM trades WHERE status='closed' AND pnl IS NOT NULL"
            ).fetchall()
        pnls = [float(r["pnl"]) for r in rows]
        if not pnls:
            return {
                "profit_factor": 0.0,
                "expectancy": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "win_rate": 0.0,
                "sharpe": 0.0,
            }
        wins = [p for p in pnls if p > 0]
        losses = [abs(p) for p in pnls if p < 0]
        gross_profit = sum(wins)
        gross_loss = sum(losses)
        pf = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)
        avg_w = (gross_profit / len(wins)) if wins else 0.0
        avg_l = (gross_loss / len(losses)) if losses else 0.0
        wr = len(wins) / len(pnls)
        exp = (wr * avg_w) - ((1.0 - wr) * avg_l)

        import numpy as np
        std_pnl = float(np.std(pnls)) if len(pnls) > 1 else 1.0
        mean_pnl = float(np.mean(pnls))
        sharpe = (mean_pnl / std_pnl * np.sqrt(252)) if std_pnl > 0 else 0.0
        return {
            "profit_factor": round(pf, 2),
            "expectancy": round(exp, 2),
            "avg_win": round(avg_w, 2),
            "avg_loss": round(avg_l, 2),
            "win_rate": round(wr * 100.0, 1),
            "sharpe": round(sharpe, 2),
        }


default_journal = TradeJournal()
