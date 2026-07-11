"""Shared helpers for all dashboard pages: read-only DB access, live Fyers
quotes (cached 60s), the Paper/Live mode switch state, and chart colors.

The dashboard NEVER writes to the database — every connection is opened with
mode=ro so it can't lock or corrupt the DB while run_daily.py is working.
"""
from __future__ import annotations

import sqlite3

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import config

load_dotenv(config.PROJECT_ROOT / ".env")

# Validated categorical palette (dataviz reference instance, light-mode steps).
# Fixed slot order — series always take colors in this order, never cycled.
SERIES = ["#2a78d6", "#1baf7a", "#eda100", "#008300", "#4a3aa7", "#e34948"]
GOOD, CRITICAL = "#0ca30c", "#d03b3b"

MODE_KEY = "trading_mode_view"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def sql_df(conn, query: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(query, conn, params=params)


def rows_to_df(rows) -> pd.DataFrame:
    return pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()


def table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def mode() -> str:
    """Current dashboard view mode: 'PAPER' or 'LIVE' (sidebar switch)."""
    return st.session_state.get(MODE_KEY, "PAPER")


def mode_badge() -> None:
    """One-line banner stating the view mode and the real-order gate status."""
    if mode() == "LIVE":
        gates = (config.TRADING_MODE == "LIVE", config.PLACE_ORDER_ENABLED)
        if all(gates):
            st.error("🔴 **LIVE view** — real order placement is **ENABLED** "
                     "(TRADING_MODE=LIVE + PLACE_ORDER_ENABLED=True)")
        else:
            off = []
            if config.TRADING_MODE != "LIVE":
                off.append("TRADING_MODE=PAPER")
            if not config.PLACE_ORDER_ENABLED:
                off.append("PLACE_ORDER_ENABLED=False")
            st.warning(f"🔴 **LIVE view** — real orders are **OFF** ({', '.join(off)}). "
                       "Showing real Fyers holdings and the live-order audit trail.")
    else:
        st.info("📄 **PAPER view** — virtual book, no real money. "
                "Switch modes in the sidebar.")


@st.cache_data(ttl=60, show_spinner=False)
def live_quotes(tickers: tuple[str, ...]) -> dict[str, dict]:
    """Live LTPs from Fyers, cached 60s. Empty dict when Fyers is unavailable
    (dashboard then falls back to last-run prices)."""
    if not tickers:
        return {}
    try:
        from stockbot import fyers_data
        return fyers_data.fetch_quotes(list(tickers), [])
    except Exception:
        return {}


def inr(x: float | None, signed: bool = False) -> str:
    if x is None:
        return "-"
    return f"₹{x:+,.0f}" if signed else f"₹{x:,.0f}"


def last_run_caption(conn) -> None:
    from stockbot import db
    last_run = db.get_last_run(conn)
    if last_run:
        st.caption(
            f"Last run: **{last_run['run_date']}** finished {last_run['finished_at']} · "
            f"{last_run['tickers_scanned']} tickers · {last_run['new_picks']} new picks · "
            f"{last_run['exits']} exits"
        )
    else:
        st.caption("No runs logged yet.")
