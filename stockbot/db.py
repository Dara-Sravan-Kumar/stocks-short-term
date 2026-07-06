"""SQLite persistence layer — the only module that touches the database."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS picks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker TEXT NOT NULL,
  entry_date TEXT NOT NULL,
  entry_price REAL NOT NULL,
  target_price REAL NOT NULL,
  stop_price REAL NOT NULL,
  pivot REAL, r1 REAL, r2 REAL, s1 REAL, s2 REAL,
  rsi_at_entry REAL, macd_hist_at_entry REAL,
  sentiment_at_entry REAL,
  rationale TEXT,
  channel TEXT NOT NULL DEFAULT 'TECHNICAL',
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  exit_date TEXT, exit_price REAL, exit_reason TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_pick
  ON picks(ticker) WHERE status = 'ACTIVE';

CREATE TABLE IF NOT EXISTS holdings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker TEXT NOT NULL UNIQUE,
  avg_buy_price REAL NOT NULL,
  quantity INTEGER NOT NULL,
  added_date TEXT
);

CREATE TABLE IF NOT EXISTS sentiment_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker TEXT NOT NULL,
  date TEXT NOT NULL,
  score REAL NOT NULL,
  confidence REAL,
  summary TEXT,
  headline_count INTEGER,
  source TEXT NOT NULL,
  UNIQUE(ticker, date)
);

CREATE TABLE IF NOT EXISTS fundamentals_cache (
  ticker TEXT NOT NULL,
  date TEXT NOT NULL,
  passed INTEGER,
  pe REAL, roe REAL, debt_to_equity REAL, market_cap REAL, eps_growth REAL,
  detail TEXT,
  PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS universe (
  ticker TEXT PRIMARY KEY,
  name TEXT,
  tier TEXT,
  industry TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS tracking_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,
  run_slot TEXT NOT NULL,          -- AM / PM
  kind TEXT NOT NULL,              -- PICK / HOLDING
  ticker TEXT NOT NULL,
  channel TEXT,                    -- NEWS / TECHNICAL (picks only)
  price REAL,
  return_pct REAL,                 -- vs entry price (picks) / avg buy (holdings)
  sentiment REAL,
  catalyst TEXT,                   -- news sentiment summary at this snapshot
  note TEXT,                       -- status / health signal
  UNIQUE(date, run_slot, kind, ticker)
);

CREATE TABLE IF NOT EXISTS run_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_date TEXT,
  started_at TEXT,
  finished_at TEXT,
  tickers_scanned INTEGER,
  new_picks INTEGER,
  exits INTEGER,
  warnings TEXT
);

CREATE TABLE IF NOT EXISTS paper_book (
  id INTEGER PRIMARY KEY CHECK (id = 1),   -- single shared book
  starting_cash REAL NOT NULL,
  cash REAL NOT NULL,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS paper_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy TEXT NOT NULL,          -- TECHNICAL / NEWS / PULLBACK
  ticker TEXT NOT NULL,
  pick_id INTEGER REFERENCES picks(id),
  qty INTEGER NOT NULL,
  entry_date TEXT NOT NULL,
  entry_ref_price REAL NOT NULL,   -- signal close the fill was derived from
  entry_fill_price REAL NOT NULL,  -- ref * (1 + slippage)
  entry_charges REAL NOT NULL,
  cost_basis REAL NOT NULL,        -- qty * fill + charges
  target_price REAL NOT NULL,
  stop_price REAL NOT NULL,
  rationale TEXT,
  status TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN / CLOSED
  exit_date TEXT, exit_fill_price REAL, exit_charges REAL,
  net_proceeds REAL, realized_pnl REAL, exit_reason TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_paper_pos
  ON paper_positions(ticker) WHERE status = 'OPEN';

CREATE TABLE IF NOT EXISTS paper_trades (   -- immutable ledger, one row per order
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL REFERENCES paper_positions(id),
  strategy TEXT NOT NULL,
  ticker TEXT NOT NULL,
  side TEXT NOT NULL,              -- BUY / SELL
  trade_date TEXT NOT NULL,
  run_slot TEXT NOT NULL,
  qty INTEGER NOT NULL,
  ref_price REAL NOT NULL,
  fill_price REAL NOT NULL,
  gross_value REAL NOT NULL,
  brokerage REAL, stt REAL, exch_txn REAL, sebi REAL, stamp REAL, gst REAL, dp REAL,
  total_charges REAL NOT NULL,
  net_amount REAL NOT NULL,        -- BUY: -(gross+charges); SELL: +(gross-charges)
  cash_after REAL NOT NULL,
  reason TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS paper_equity_log (   -- per-run equity curve
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,
  run_slot TEXT NOT NULL,
  cash REAL,
  positions_value REAL,
  equity REAL,
  unrealized_pnl REAL,
  realized_pnl_cum REAL,
  open_positions INTEGER,
  UNIQUE(date, run_slot)
);

CREATE TABLE IF NOT EXISTS broker_sync_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  synced_at TEXT NOT NULL,
  source TEXT NOT NULL,            -- OPENALGO / MOCK
  status TEXT NOT NULL,            -- OK / FAILED / NOT_CONFIGURED
  holdings_count INTEGER,
  error TEXT
);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for databases created by older versions."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(picks)")}
    if "channel" not in cols:
        conn.execute("ALTER TABLE picks ADD COLUMN channel TEXT NOT NULL DEFAULT 'TECHNICAL'")
        conn.commit()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(holdings)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE holdings ADD COLUMN source TEXT NOT NULL DEFAULT 'MOCK'")
        conn.execute("ALTER TABLE holdings ADD COLUMN synced_at TEXT")
        conn.commit()


# ---------------------------------------------------------------------------
# Holdings
# ---------------------------------------------------------------------------

def seed_mock_holdings(conn: sqlite3.Connection) -> bool:
    """Insert mock holdings if the table is empty. Returns True if seeded."""
    if conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] > 0:
        return False
    today = datetime.now().strftime("%Y-%m-%d")
    conn.executemany(
        "INSERT INTO holdings (ticker, avg_buy_price, quantity, added_date) VALUES (?,?,?,?)",
        [(t, p, q, today) for t, p, q in config.MOCK_HOLDINGS],
    )
    conn.commit()
    return True


def get_holdings(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM holdings ORDER BY ticker").fetchall()


# ---------------------------------------------------------------------------
# Picks
# ---------------------------------------------------------------------------

def get_active_picks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM picks WHERE status = 'ACTIVE' ORDER BY entry_date"
    ).fetchall()


def insert_pick(conn: sqlite3.Connection, pick: dict) -> int | None:
    """Insert a new ACTIVE pick. Returns its id, or None if the ticker already has one."""
    cols = (
        "ticker entry_date entry_price target_price stop_price pivot r1 r2 s1 s2 "
        "rsi_at_entry macd_hist_at_entry sentiment_at_entry rationale channel"
    ).split()
    try:
        cur = conn.execute(
            f"INSERT INTO picks ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [pick.get(c) for c in cols],
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None  # ACTIVE pick already exists for this ticker


def close_pick(conn: sqlite3.Connection, pick_id: int, status: str,
               exit_date: str, exit_price: float, exit_reason: str) -> None:
    conn.execute(
        "UPDATE picks SET status=?, exit_date=?, exit_price=?, exit_reason=? WHERE id=?",
        (status, exit_date, exit_price, exit_reason, pick_id),
    )
    conn.commit()


def get_closed_picks_stats(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """SELECT status, exit_price, entry_price FROM picks
           WHERE status != 'ACTIVE' AND exit_price IS NOT NULL"""
    ).fetchall()
    if not rows:
        return {"closed": 0, "wins": 0, "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0}
    pnls = [(r["exit_price"] - r["entry_price"]) / r["entry_price"] * 100 for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    return {
        "closed": len(pnls),
        "wins": len(wins),
        "win_rate": len(wins) / len(pnls) * 100,
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
    }


def get_todays_exits(conn: sqlite3.Connection, date: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM picks WHERE exit_date = ? ORDER BY ticker", (date,)
    ).fetchall()


def get_todays_new_picks(conn: sqlite3.Connection, date: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM picks WHERE entry_date = ? AND status = 'ACTIVE' ORDER BY ticker",
        (date,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Sentiment cache
# ---------------------------------------------------------------------------

def get_sentiment(conn: sqlite3.Connection, ticker: str, date: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sentiment_log WHERE ticker=? AND date=?", (ticker, date)
    ).fetchone()


def upsert_sentiment(conn: sqlite3.Connection, ticker: str, date: str, score: float,
                     confidence: float, summary: str, headline_count: int, source: str) -> None:
    conn.execute(
        """INSERT INTO sentiment_log (ticker, date, score, confidence, summary, headline_count, source)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(ticker, date) DO UPDATE SET
             score=excluded.score, confidence=excluded.confidence, summary=excluded.summary,
             headline_count=excluded.headline_count, source=excluded.source""",
        (ticker, date, score, confidence, summary, headline_count, source),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Fundamentals cache
# ---------------------------------------------------------------------------

def get_fundamentals(conn: sqlite3.Connection, ticker: str, date: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM fundamentals_cache WHERE ticker=? AND date=?", (ticker, date)
    ).fetchone()


def upsert_fundamentals(conn: sqlite3.Connection, ticker: str, date: str, passed: bool,
                        pe, roe, dte, mcap, eps_growth, detail: str) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO fundamentals_cache
           (ticker, date, passed, pe, roe, debt_to_equity, market_cap, eps_growth, detail)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (ticker, date, int(passed), pe, roe, dte, mcap, eps_growth, detail),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Tracking history (returns % + catalyst per pick/holding per run)
# ---------------------------------------------------------------------------

def upsert_tracking(conn: sqlite3.Connection, date: str, run_slot: str, kind: str,
                    ticker: str, channel: str | None, price, return_pct,
                    sentiment, catalyst: str | None, note: str | None) -> None:
    conn.execute(
        """INSERT INTO tracking_log
             (date, run_slot, kind, ticker, channel, price, return_pct,
              sentiment, catalyst, note)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(date, run_slot, kind, ticker) DO UPDATE SET
             channel=excluded.channel, price=excluded.price,
             return_pct=excluded.return_pct, sentiment=excluded.sentiment,
             catalyst=excluded.catalyst, note=excluded.note""",
        (date, run_slot, kind, ticker, channel, price, return_pct,
         sentiment, catalyst, note),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------

def log_run(conn: sqlite3.Connection, run_date: str, started_at: str, finished_at: str,
            tickers_scanned: int, new_picks: int, exits: int, warnings: list[str]) -> None:
    conn.execute(
        """INSERT INTO run_log (run_date, started_at, finished_at, tickers_scanned,
                                new_picks, exits, warnings) VALUES (?,?,?,?,?,?,?)""",
        (run_date, started_at, finished_at, tickers_scanned, new_picks, exits,
         "; ".join(warnings) if warnings else None),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Paper trading book (single shared cash pool, positions tagged by strategy)
# ---------------------------------------------------------------------------

def ensure_paper_book(conn: sqlite3.Connection) -> bool:
    """Seed the shared paper book once. Returns True if created."""
    if conn.execute("SELECT COUNT(*) FROM paper_book").fetchone()[0] > 0:
        return False
    conn.execute(
        "INSERT INTO paper_book (id, starting_cash, cash) VALUES (1, ?, ?)",
        (config.PAPER_STARTING_CASH, config.PAPER_STARTING_CASH),
    )
    conn.commit()
    return True


def get_paper_book(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM paper_book WHERE id = 1").fetchone()


def get_open_paper_positions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM paper_positions WHERE status = 'OPEN' ORDER BY entry_date, ticker"
    ).fetchall()


def get_open_paper_position(conn: sqlite3.Connection, ticker: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM paper_positions WHERE status = 'OPEN' AND ticker = ?", (ticker,)
    ).fetchone()


_POSITION_COLS = (
    "strategy ticker pick_id qty entry_date entry_ref_price entry_fill_price "
    "entry_charges cost_basis target_price stop_price rationale"
).split()

_TRADE_COLS = (
    "position_id strategy ticker side trade_date run_slot qty ref_price fill_price "
    "gross_value brokerage stt exch_txn sebi stamp gst dp total_charges "
    "net_amount cash_after reason"
).split()


def open_paper_position(conn: sqlite3.Connection, pos: dict, trade: dict) -> int | None:
    """Insert position + BUY ledger row + cash debit atomically.

    Returns the new position id, or None if the ticker already has an OPEN
    paper position (mirrors insert_pick's duplicate handling).
    """
    try:
        cur = conn.execute(
            f"INSERT INTO paper_positions ({','.join(_POSITION_COLS)}) "
            f"VALUES ({','.join('?' * len(_POSITION_COLS))})",
            [pos.get(c) for c in _POSITION_COLS],
        )
        position_id = cur.lastrowid
        trade = dict(trade, position_id=position_id)
        conn.execute(
            f"INSERT INTO paper_trades ({','.join(_TRADE_COLS)}) "
            f"VALUES ({','.join('?' * len(_TRADE_COLS))})",
            [trade.get(c) for c in _TRADE_COLS],
        )
        conn.execute(
            "UPDATE paper_book SET cash = ?, updated_at = datetime('now') WHERE id = 1",
            (trade["cash_after"],),
        )
        conn.commit()
        return position_id
    except sqlite3.IntegrityError:
        conn.rollback()
        return None


def close_paper_position(conn: sqlite3.Connection, position_id: int,
                         exit_fields: dict, trade: dict) -> None:
    """Close a position + SELL ledger row + cash credit atomically."""
    conn.execute(
        """UPDATE paper_positions SET status='CLOSED', exit_date=?, exit_fill_price=?,
             exit_charges=?, net_proceeds=?, realized_pnl=?, exit_reason=?
           WHERE id=?""",
        (exit_fields["exit_date"], exit_fields["exit_fill_price"],
         exit_fields["exit_charges"], exit_fields["net_proceeds"],
         exit_fields["realized_pnl"], exit_fields["exit_reason"], position_id),
    )
    trade = dict(trade, position_id=position_id)
    conn.execute(
        f"INSERT INTO paper_trades ({','.join(_TRADE_COLS)}) "
        f"VALUES ({','.join('?' * len(_TRADE_COLS))})",
        [trade.get(c) for c in _TRADE_COLS],
    )
    conn.execute(
        "UPDATE paper_book SET cash = ?, updated_at = datetime('now') WHERE id = 1",
        (trade["cash_after"],),
    )
    conn.commit()


def upsert_paper_equity(conn: sqlite3.Connection, date: str, run_slot: str,
                        cash: float, positions_value: float, equity: float,
                        unrealized_pnl: float, realized_pnl_cum: float,
                        open_positions: int) -> None:
    conn.execute(
        """INSERT INTO paper_equity_log
             (date, run_slot, cash, positions_value, equity, unrealized_pnl,
              realized_pnl_cum, open_positions)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(date, run_slot) DO UPDATE SET
             cash=excluded.cash, positions_value=excluded.positions_value,
             equity=excluded.equity, unrealized_pnl=excluded.unrealized_pnl,
             realized_pnl_cum=excluded.realized_pnl_cum,
             open_positions=excluded.open_positions""",
        (date, run_slot, cash, positions_value, equity, unrealized_pnl,
         realized_pnl_cum, open_positions),
    )
    conn.commit()


def get_paper_stats(conn: sqlite3.Connection) -> dict[str, dict]:
    """Per-strategy realized performance (attribution despite the shared book)."""
    stats = {s: {"closed": 0, "wins": 0, "win_rate": 0.0, "realized_pnl": 0.0}
             for s in config.PAPER_STRATEGIES}
    rows = conn.execute(
        """SELECT strategy, COUNT(*) AS closed,
                  SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                  SUM(realized_pnl) AS realized
           FROM paper_positions WHERE status = 'CLOSED' GROUP BY strategy"""
    ).fetchall()
    for r in rows:
        stats[r["strategy"]] = {
            "closed": r["closed"], "wins": r["wins"] or 0,
            "win_rate": (r["wins"] or 0) / r["closed"] * 100 if r["closed"] else 0.0,
            "realized_pnl": r["realized"] or 0.0,
        }
    return stats


def get_realized_pnl_cum(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0.0) FROM paper_positions WHERE status='CLOSED'"
    ).fetchone()
    return float(row[0])


# ---------------------------------------------------------------------------
# Broker holdings sync (OpenAlgo -> holdings table)
# ---------------------------------------------------------------------------

def replace_holdings(conn: sqlite3.Connection, rows: list[dict],
                     source: str, synced_at: str) -> None:
    """Replace the holdings table with a fresh broker snapshot (one txn)."""
    conn.execute("DELETE FROM holdings")
    conn.executemany(
        """INSERT INTO holdings (ticker, avg_buy_price, quantity, added_date, source, synced_at)
           VALUES (?,?,?,?,?,?)""",
        [(r["ticker"], r["avg_buy_price"], r["quantity"],
          r.get("added_date"), source, synced_at) for r in rows],
    )
    conn.commit()


def get_holdings_provenance(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT source, synced_at FROM holdings ORDER BY synced_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return {"source": None, "synced_at": None}
    return {"source": row["source"], "synced_at": row["synced_at"]}


def log_broker_sync(conn: sqlite3.Connection, synced_at: str, source: str,
                    status: str, holdings_count: int | None, error: str | None) -> None:
    conn.execute(
        """INSERT INTO broker_sync_log (synced_at, source, status, holdings_count, error)
           VALUES (?,?,?,?,?)""",
        (synced_at, source, status, holdings_count, error),
    )
    conn.commit()
