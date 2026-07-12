"""Dynamic scan universe from NSE's official index constituent files.

Sources (updated by NSE on every index rebalance):
  - NIFTY 100          -> LARGE tier
  - NIFTY Midcap 150   -> MID tier
  - NIFTY Smallcap 250 -> SMALL tier
  - NIFTY Microcap 250 -> MICRO tier

Constituents are cached in the `universe` table and refreshed weekly.
Fallback chain: fresh NSE fetch -> last cached copy in DB -> static
lists in config.py. A run never dies because NSE blocked a request.
"""
from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime, timedelta

import requests

import config

INDEX_SOURCES = {
    "LARGE": "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv",
    "MID": "https://nsearchives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
    "SMALL": "https://nsearchives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
    # Microcap 250's file name has an underscore before "list" (the others don't).
    "MICRO": "https://nsearchives.nseindia.com/content/indices/ind_niftymicrocap250_list.csv",
}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "text/csv,application/csv,text/plain,*/*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

FINANCIAL_INDUSTRY_KEYWORDS = ("financial", "bank")


def _fetch_index_csv(url: str) -> list[dict]:
    """Download and parse one NSE index constituent CSV."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    text = resp.content.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    # Expected columns: Company Name, Industry, Symbol, Series, ISIN Code
    parsed = []
    for row in rows:
        row = { (k or "").strip(): (v or "").strip() for k, v in row.items() }
        symbol = row.get("Symbol")
        if not symbol or row.get("Series", "EQ") not in ("EQ", "BE", ""):
            continue
        parsed.append({
            "symbol": symbol,
            "name": row.get("Company Name", symbol),
            "industry": row.get("Industry", ""),
        })
    if len(parsed) < 20:  # sanity: a constituent file has dozens of rows
        raise ValueError(f"only {len(parsed)} usable rows parsed from {url}")
    return parsed


def _refresh_from_nse(conn: sqlite3.Connection, warnings: list[str]) -> bool:
    """Fetch all three index files. All-or-nothing table replace."""
    fetched: list[tuple] = []
    now = datetime.now().isoformat(timespec="seconds")
    for tier, url in INDEX_SOURCES.items():
        try:
            for row in _fetch_index_csv(url):
                fetched.append((row["symbol"] + ".NS", row["name"], tier,
                                row["industry"], now))
        except Exception as exc:
            warnings.append(f"NSE universe fetch failed for {tier} tier ({exc}) "
                            "- using cached/static universe")
            return False
    conn.execute("DELETE FROM universe")
    conn.executemany(
        "INSERT OR REPLACE INTO universe (ticker, name, tier, industry, updated_at) "
        "VALUES (?,?,?,?,?)", fetched)
    conn.commit()
    return True


def load(conn: sqlite3.Connection, warnings: list[str],
         force_refresh: bool = False) -> dict:
    """Return the active universe, refreshing from NSE if stale.

    Returns {"tickers": [...], "tier": {t: TIER}, "names": {t: name},
             "financials": {t, ...}, "source": "nse"|"cached"|"static"}
    """
    row = conn.execute("SELECT MAX(updated_at) AS u, COUNT(*) AS n FROM universe").fetchone()
    stale = True
    if row["n"] and row["u"]:
        age = datetime.now() - datetime.fromisoformat(row["u"])
        stale = age > timedelta(days=config.UNIVERSE_REFRESH_DAYS)

    source = "cached"
    if force_refresh or stale or not row["n"]:
        if _refresh_from_nse(conn, warnings):
            source = "nse"

    rows = conn.execute("SELECT * FROM universe").fetchall()
    if not rows:
        warnings.append("Universe table empty and NSE unreachable - using static "
                        f"fallback watchlist ({len(config.WATCHLIST)} tickers)")
        return {
            "tickers": list(config.WATCHLIST),
            "tier": dict(config.TIER),
            "names": dict(config.COMPANY_NAMES),
            "financials": set(config.FINANCIALS),
            "source": "static",
        }

    tickers, tier, names, financials = [], {}, {}, set()
    for r in rows:
        t = r["ticker"]
        tickers.append(t)
        tier[t] = r["tier"]
        names[t] = r["name"]
        industry = (r["industry"] or "").lower()
        if any(k in industry for k in FINANCIAL_INDUSTRY_KEYWORDS):
            financials.add(t)
    return {"tickers": tickers, "tier": tier, "names": names,
            "financials": financials, "source": source}


def apply(uni: dict) -> None:
    """Install the loaded universe as the runtime config (modules read
    config.WATCHLIST / TIER / COMPANY_NAMES / FINANCIALS at call time)."""
    config.WATCHLIST = uni["tickers"]
    config.TIER = uni["tier"]
    config.COMPANY_NAMES = uni["names"]
    config.FINANCIALS = uni["financials"]
