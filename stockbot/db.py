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


def insert_pick(conn: sqlite3.Connection, pick: dict) -> bool:
    """Insert a new ACTIVE pick. Returns False if the ticker already has one."""
    cols = (
        "ticker entry_date entry_price target_price stop_price pivot r1 r2 s1 s2 "
        "rsi_at_entry macd_hist_at_entry sentiment_at_entry rationale channel"
    ).split()
    try:
        conn.execute(
            f"INSERT INTO picks ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [pick.get(c) for c in cols],
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False  # ACTIVE pick already exists for this ticker


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
