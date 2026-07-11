"""All Trades — every trade the bot has made, filterable, with CSV export.

Three ledgers: paper trades (executed on the virtual book), signal picks
(channel-level entries/exits, no sizing), and live orders (real-order audit
trail from TRADING_MODE=LIVE).
"""
from __future__ import annotations

import streamlit as st

from stockbot import db
from dashboard_pages import common


def _download(df, name: str) -> None:
    if not df.empty:
        st.download_button(f"⬇️ Export {name}.csv",
                           df.to_csv(index=False).encode("utf-8"),
                           file_name=f"{name}.csv", mime="text/csv")


def _paper_trades(conn) -> None:
    f1, f2, f3, f4 = st.columns(4)
    strategies = common.sql_df(conn, "SELECT DISTINCT strategy FROM paper_positions ORDER BY 1")
    tickers = common.sql_df(conn, "SELECT DISTINCT ticker FROM paper_positions ORDER BY 1")
    strat = f1.selectbox("Strategy", ["(all)"] + strategies["strategy"].tolist())
    status = f2.selectbox("Status", ["(all)", "CLOSED", "OPEN"])
    ticker = f3.selectbox("Ticker", ["(all)"] + tickers["ticker"].tolist())
    outcome = f4.selectbox("Outcome", ["(all)", "Winners", "Losers"])

    q = """SELECT ticker, strategy, status, qty, entry_date, entry_fill_price,
                  target_price, stop_price, exit_date, exit_fill_price,
                  ROUND(realized_pnl, 0) AS realized_pnl, exit_reason
           FROM paper_positions WHERE 1=1"""
    params: list = []
    if strat != "(all)":
        q += " AND strategy = ?"
        params.append(strat)
    if status != "(all)":
        q += " AND status = ?"
        params.append(status)
    if ticker != "(all)":
        q += " AND ticker = ?"
        params.append(ticker)
    if outcome == "Winners":
        q += " AND realized_pnl > 0"
    elif outcome == "Losers":
        q += " AND realized_pnl <= 0 AND status='CLOSED'"
    q += " ORDER BY COALESCE(exit_date, entry_date) DESC, id DESC LIMIT 1000"

    df = common.sql_df(conn, q, tuple(params))
    closed = df[df["status"] == "CLOSED"]
    if not closed.empty:
        wins = (closed["realized_pnl"] > 0).sum()
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Trades (closed)", len(closed))
        c2.metric("Win Rate", f"{wins / len(closed) * 100:.0f}%")
        c3.metric("Net P&L", common.inr(closed["realized_pnl"].sum(), signed=True))
        c4.metric("Avg P&L / trade", common.inr(closed["realized_pnl"].mean(), signed=True))
    st.dataframe(df, width="stretch", hide_index=True)
    _download(df, "paper_trades")


def _picks(conn) -> None:
    channels = common.sql_df(conn, "SELECT DISTINCT channel FROM picks ORDER BY 1")
    f1, f2 = st.columns(2)
    channel = f1.selectbox("Channel", ["(all)"] + channels["channel"].tolist())
    status = f2.selectbox("Status ", ["(all)", "ACTIVE", "TARGET_HIT", "STOPPED_OUT",
                                      "SETUP_BROKEN", "EXPIRED"])
    q = """SELECT ticker, channel, status, entry_date, entry_price,
                  target_price, stop_price, exit_date, exit_price,
                  ROUND((exit_price-entry_price)/entry_price*100, 2) AS pnl_pct,
                  exit_reason
           FROM picks WHERE 1=1"""
    params: list = []
    if channel != "(all)":
        q += " AND channel = ?"
        params.append(channel)
    if status != "(all)":
        q += " AND status = ?"
        params.append(status)
    q += " ORDER BY COALESCE(exit_date, entry_date) DESC, id DESC LIMIT 1000"
    df = common.sql_df(conn, q, tuple(params))
    st.dataframe(df, width="stretch", hide_index=True)
    _download(df, "signal_picks")


def _live_orders(conn) -> None:
    if not common.table_exists(conn, "live_trades"):
        st.caption("No live-order activity yet (table appears after the next run).")
        return
    df = common.rows_to_df(db.get_live_trades(conn, limit=1000))
    if df.empty:
        st.caption("No live-order activity yet. Orders appear here once "
                   "TRADING_MODE=LIVE (BLOCKED rows show intent while the "
                   "PLACE_ORDER_ENABLED gate is still off).")
        return
    st.dataframe(df[["ts", "date", "run_slot", "ticker", "side", "qty",
                     "strategy", "status", "order_id", "detail"]],
                 width="stretch", hide_index=True)
    _download(df, "live_orders")


def page() -> None:
    conn = common.get_conn()
    st.title("🧾 All Trades")
    common.last_run_caption(conn)

    tab_paper, tab_picks, tab_live = st.tabs(
        ["📄 Paper Trades", "🎯 Signal Picks", "🔴 Live Orders"])
    with tab_paper:
        _paper_trades(conn)
    with tab_picks:
        _picks(conn)
    with tab_live:
        _live_orders(conn)
    conn.close()
