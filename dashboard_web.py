"""Read-only web dashboard — observe the paper book, strategy ledger, picks,
and market context without touching the trading pipeline.

Run with:  .venv\\Scripts\\python.exe -m streamlit run dashboard_web.py

Opens its own read-only SQLite connection (mode=ro) so it can never write to
or lock the database that run_daily.py is using — this is purely a viewer.
All figures are "as of the last run" (twice-daily by default), not live
intraday prices; there is no separate price-fetching here.
"""
from __future__ import annotations

import sqlite3

import pandas as pd
import streamlit as st

import config
from stockbot import db, strategy_engine

st.set_page_config(page_title="StockBot Dashboard", page_icon="📊", layout="wide")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def rows_to_df(rows) -> pd.DataFrame:
    return pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()


conn = get_connection()

st.title("📊 StockBot Dashboard")

top_left, top_right = st.columns([4, 1])
last_run = db.get_last_run(conn)
with top_left:
    if last_run:
        st.caption(
            f"Last run: **{last_run['run_date']}** finished {last_run['finished_at']} · "
            f"{last_run['tickers_scanned']} tickers scanned · "
            f"{last_run['new_picks']} new picks · {last_run['exits']} exits"
        )
    else:
        st.caption("No runs logged yet.")
with top_right:
    if st.button("🔄 Refresh"):
        st.rerun()

# ---------------------------------------------------------------------------
# Paper book summary
# ---------------------------------------------------------------------------
st.header("Paper Book")
equity_row = db.get_latest_equity_row(conn)
book = db.get_paper_book(conn)

if equity_row and book:
    total_return_pct = (equity_row["equity"] - book["starting_cash"]) / book["starting_cash"] * 100
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Equity", f"₹{equity_row['equity']:,.0f}", f"{total_return_pct:+.2f}%")
    c2.metric("Cash", f"₹{equity_row['cash']:,.0f}")
    c3.metric("Positions Value", f"₹{equity_row['positions_value']:,.0f}")
    c4.metric("Unrealized P&L", f"₹{equity_row['unrealized_pnl']:+,.0f}")
    c5.metric("Realized P&L (cum)", f"₹{equity_row['realized_pnl_cum']:+,.0f}")
    st.caption(f"as of {equity_row['date']} {equity_row['run_slot']} · "
              f"started with ₹{book['starting_cash']:,.0f}")
else:
    st.info("No paper book activity yet.")

equity_curve = rows_to_df(db.get_equity_curve(conn))
if not equity_curve.empty:
    equity_curve["run"] = equity_curve["date"] + " " + equity_curve["run_slot"]
    st.line_chart(equity_curve.set_index("run")[["equity", "cash"]])

# ---------------------------------------------------------------------------
# Market context
# ---------------------------------------------------------------------------
st.header("Market Context")
ctx = db.get_latest_market_context(conn)
if ctx:
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Nifty 50", f"{ctx['nifty_close']:,.0f}" if ctx["nifty_close"] else "-",
              f"{ctx['nifty_return_pct']:+.2f}%" if ctx["nifty_return_pct"] is not None else None)
    m1.caption(f"Regime: {ctx['nifty_regime'] or '-'}")
    m2.metric("Bank Nifty", f"{ctx['banknifty_close']:,.0f}" if ctx["banknifty_close"] else "-",
              f"{ctx['banknifty_return_pct']:+.2f}%" if ctx["banknifty_return_pct"] is not None else None)
    m2.caption(f"Regime: {ctx['banknifty_regime'] or '-'}")
    m3.metric("India VIX", f"{ctx['india_vix']:.2f}" if ctx["india_vix"] else "-")
    m3.caption(f"Regime: {ctx['vix_regime'] or '-'}")
    m4.metric("Avg Sentiment", f"{ctx['avg_market_sentiment']:+.2f}"
              if ctx["avg_market_sentiment"] is not None else "-")
    m4.caption(f"S&P {ctx['sp500_return_pct']:+.2f}% · Nasdaq {ctx['nasdaq_return_pct']:+.2f}%"
              if ctx["sp500_return_pct"] is not None else "Global: -")

    flags = []
    if ctx["nifty_crash"]:
        flags.append("🔴 Nifty crash")
    if ctx["banknifty_crash"]:
        flags.append("🔴 Bank Nifty crash")
    if ctx["global_crash"]:
        flags.append("🔴 Global crash")
    st.write(" · ".join(flags) if flags else "🟢 No crash conditions flagged")
    st.caption(f"as of {ctx['date']} {ctx['run_slot']}")
else:
    st.info("No market context recorded yet.")

# ---------------------------------------------------------------------------
# Strategy ledger
# ---------------------------------------------------------------------------
st.header("Strategy Ledger")
ledger_stats = db.get_strategy_ledger_stats(conn)
all_active = [s for ch in (*config.EVOLVING_CHANNELS, "NEWS")
              for s in db.get_active_strategies(conn, channel=ch)]
weights = strategy_engine.current_capital_weights(conn)

ledger_rows = []
for s in all_active:
    key = s["variant_key"]
    stats = ledger_stats.get(key, {"closed": 0, "win_rate": 0.0, "realized_pnl": 0.0,
                                    "profit_factor": None})
    ledger_rows.append({
        "Variant": key,
        "Channel": s["channel"],
        "Closed": stats["closed"],
        "Win Rate %": round(stats["win_rate"], 1),
        "Realized P&L": round(stats["realized_pnl"], 0),
        "Profit Factor": (round(stats["profit_factor"], 2) if stats["profit_factor"] is not None
                         else float("nan")),
        "Capital Weight %": round(weights.get(key, 0.0), 1),
        "Origin": s["origin"],
        "Graduate": "🏆" if s["graduate_candidate"] else "",
    })
ledger_df = pd.DataFrame(ledger_rows).sort_values(["Channel", "Variant"]) if ledger_rows else pd.DataFrame()
st.dataframe(ledger_df, width="stretch", hide_index=True)

retired = rows_to_df([s for s in db.get_all_strategies(conn) if s["status"] == "RETIRED"])
if not retired.empty:
    with st.expander(f"Retired variants ({len(retired)})"):
        st.dataframe(
            retired[["channel", "variant_key", "retired_at", "retired_reason", "parent_variant_key"]],
            width="stretch", hide_index=True,
        )

# ---------------------------------------------------------------------------
# Open paper positions & active picks
# ---------------------------------------------------------------------------
left, right = st.columns(2)

with left:
    st.subheader("Open Paper Positions")
    open_pos = rows_to_df(db.get_open_paper_positions(conn))
    if not open_pos.empty:
        st.dataframe(
            open_pos[["ticker", "strategy", "qty", "entry_fill_price", "cost_basis",
                     "target_price", "stop_price", "entry_date"]],
            width="stretch", hide_index=True,
        )
    else:
        st.caption("No open paper positions.")

with right:
    st.subheader("Active Picks")
    active_picks = rows_to_df(db.get_active_picks(conn))
    if not active_picks.empty:
        st.dataframe(
            active_picks[["ticker", "channel", "entry_price", "target_price",
                         "stop_price", "entry_date"]],
            width="stretch", hide_index=True,
        )
    else:
        st.caption("No active picks.")

# ---------------------------------------------------------------------------
# Recent activity
# ---------------------------------------------------------------------------
st.header("Recent Activity")
r1, r2 = st.columns(2)

with r1:
    st.subheader("Recently Closed Picks")
    closed_picks = rows_to_df(db.get_recent_closed_picks(conn, limit=15))
    if not closed_picks.empty:
        st.dataframe(
            closed_picks[["ticker", "channel", "status", "entry_price", "exit_price",
                         "exit_date", "exit_reason"]],
            width="stretch", hide_index=True,
        )
    else:
        st.caption("No closed picks yet.")

with r2:
    st.subheader("Recently Closed Paper Trades")
    closed_paper = rows_to_df(db.get_recent_closed_paper_positions(conn, limit=15))
    if not closed_paper.empty:
        st.dataframe(
            closed_paper[["ticker", "strategy", "qty", "realized_pnl", "exit_date", "exit_reason"]],
            width="stretch", hide_index=True,
        )
    else:
        st.caption("No closed paper trades yet.")

# ---------------------------------------------------------------------------
# Holdings
# ---------------------------------------------------------------------------
st.header("Real Holdings")
holdings = rows_to_df(db.get_holdings(conn))
if not holdings.empty:
    st.dataframe(holdings, width="stretch", hide_index=True)
else:
    st.caption("No holdings synced.")

conn.close()
