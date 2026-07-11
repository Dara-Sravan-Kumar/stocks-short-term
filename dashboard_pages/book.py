"""Book — current positions for the selected mode.

Paper: virtual book, open paper positions marked to live Fyers quotes, active
signal picks. Live: real Fyers holdings with live P&L and the live-order
audit trail (live_trades).
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

import config
from stockbot import db
from dashboard_pages import common


def _paper_book(conn) -> None:
    equity_row = db.get_latest_equity_row(conn)
    book = db.get_paper_book(conn)
    if equity_row and book:
        total_return = (equity_row["equity"] - book["starting_cash"]) / book["starting_cash"] * 100
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Equity", common.inr(equity_row["equity"]), f"{total_return:+.2f}%")
        c2.metric("Cash", common.inr(equity_row["cash"]))
        c3.metric("Positions Value", common.inr(equity_row["positions_value"]))
        c4.metric("Unrealized P&L", common.inr(equity_row["unrealized_pnl"], signed=True))
        c5.metric("Realized P&L (cum)", common.inr(equity_row["realized_pnl_cum"], signed=True))
        st.caption(f"as of {equity_row['date']} {equity_row['run_slot']} · "
                   f"started with {common.inr(book['starting_cash'])}")
    else:
        st.info("No paper book activity yet.")

    st.subheader("Open Paper Positions")
    open_pos = common.rows_to_df(db.get_open_paper_positions(conn))
    if open_pos.empty:
        st.caption("No open paper positions.")
    else:
        quotes = common.live_quotes(tuple(open_pos["ticker"]))
        open_pos["ltp (live)"] = open_pos["ticker"].map(
            lambda t: (quotes.get(t) or {}).get("ltp"))
        open_pos["live P&L"] = ((open_pos["ltp (live)"] - open_pos["entry_fill_price"])
                                * open_pos["qty"]).round(0)
        open_pos["live %"] = ((open_pos["ltp (live)"] / open_pos["entry_fill_price"] - 1)
                              * 100).round(2)
        st.dataframe(
            open_pos[["ticker", "strategy", "qty", "entry_fill_price", "cost_basis",
                      "ltp (live)", "live P&L", "live %",
                      "target_price", "stop_price", "entry_date"]],
            width="stretch", hide_index=True)
        st.caption("live columns use Fyers quotes (60s cache)" if quotes else
                   "⚠️ live quotes unavailable — showing last-run data only")

    st.subheader("Active Signal Picks")
    active_picks = common.rows_to_df(db.get_active_picks(conn))
    if not active_picks.empty:
        st.dataframe(active_picks[["ticker", "channel", "entry_price",
                                   "target_price", "stop_price", "entry_date"]],
                     width="stretch", hide_index=True)
    else:
        st.caption("No active picks.")

    st.subheader("Recently Closed Paper Trades")
    closed = common.rows_to_df(db.get_recent_closed_paper_positions(conn, limit=15))
    if not closed.empty:
        st.dataframe(closed[["ticker", "strategy", "qty", "realized_pnl",
                             "exit_date", "exit_reason"]],
                     width="stretch", hide_index=True)
    else:
        st.caption("No closed paper trades yet.")


def _live_book(conn) -> None:
    st.subheader("Real Holdings (Fyers)")
    holdings = common.rows_to_df(db.get_holdings(conn))
    prov = db.get_holdings_provenance(conn)
    if holdings.empty:
        st.info("No holdings synced. They sync from Fyers on every run.")
    else:
        quotes = common.live_quotes(tuple(holdings["ticker"]))
        holdings["ltp (live)"] = holdings["ticker"].map(
            lambda t: (quotes.get(t) or {}).get("ltp"))
        holdings["day %"] = holdings["ticker"].map(
            lambda t: (quotes.get(t) or {}).get("change_pct"))
        holdings["invested"] = (holdings["avg_buy_price"] * holdings["quantity"]).round(0)
        holdings["value"] = (holdings["ltp (live)"] * holdings["quantity"]).round(0)
        holdings["P&L"] = holdings["value"] - holdings["invested"]
        holdings["P&L %"] = ((holdings["value"] / holdings["invested"] - 1) * 100).round(2)
        st.dataframe(
            holdings[["ticker", "quantity", "avg_buy_price", "ltp (live)", "day %",
                      "invested", "value", "P&L", "P&L %"]],
            width="stretch", hide_index=True)
        st.caption(f"source: {prov['source'] or '-'} · synced {prov['synced_at'] or 'never'}"
                   + ("" if quotes else " · ⚠️ live quotes unavailable"))

    st.subheader("Live Order Audit Trail")
    st.caption("Every real-order attempt in TRADING_MODE=LIVE lands here — "
               "SUBMITTED (sent to Fyers), BLOCKED (safety gate off), or FAILED.")
    if common.table_exists(conn, "live_trades"):
        trades = common.rows_to_df(db.get_live_trades(conn, limit=200))
        if not trades.empty:
            st.dataframe(trades[["ts", "ticker", "side", "qty", "strategy",
                                 "status", "order_id", "detail"]],
                         width="stretch", hide_index=True)
        else:
            st.caption("No live-order activity yet.")
    else:
        st.caption("No live-order activity yet (table appears after the next run).")

    st.subheader("Broker Sync Log")
    sync_log = common.sql_df(conn, """
        SELECT synced_at, source, status, holdings_count, error
        FROM broker_sync_log ORDER BY synced_at DESC LIMIT 20""")
    if not sync_log.empty:
        st.dataframe(sync_log, width="stretch", hide_index=True)
    else:
        st.caption("No broker syncs logged yet.")


def page() -> None:
    conn = common.get_conn()
    st.title("💼 Book")
    common.last_run_caption(conn)
    common.mode_badge()
    if common.mode() == "LIVE":
        _live_book(conn)
    else:
        _paper_book(conn)
    conn.close()
