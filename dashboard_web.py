"""StockBot web dashboard — multi-page, with a top navigation bar and a global
Paper/Live mode switch.

Pages:  🏠 Summary · 💼 Book · 🧾 All Trades · 📒 Ledger · 📜 History · 🧪 Backtest

Run with:  .venv\\Scripts\\python.exe -m streamlit run dashboard_web.py

Read-only: every page opens SQLite with mode=ro, so the dashboard can never
write to or lock the database run_daily.py uses. "Live" figures come from
Fyers quotes (60-second cache); everything else is as of the last run.
"""
from __future__ import annotations

import streamlit as st

import config
from dashboard_pages import (backtests, book, common, fleet, history, ledger,
                             summary, trades)

st.set_page_config(page_title="StockBot Dashboard", page_icon="📊", layout="wide")

# ------------------------------------------------------------- mode switch
with st.sidebar:
    st.markdown("### Mode")
    default_mode = "🔴 Live" if config.TRADING_MODE == "LIVE" else "📄 Paper"
    picked = st.radio(
        "Trading book to view", ["📄 Paper", "🔴 Live"],
        index=1 if default_mode == "🔴 Live" else 0,
        label_visibility="collapsed",
        help="Paper: the virtual book the bot trades every run. "
             "Live: real Fyers holdings + the live-order audit trail.")
    st.session_state[common.MODE_KEY] = "LIVE" if picked == "🔴 Live" else "PAPER"

    st.divider()
    gate = "ENABLED ⚠️" if (config.TRADING_MODE == "LIVE"
                            and config.PLACE_ORDER_ENABLED) else "off"
    st.caption(f"TRADING_MODE: **{config.TRADING_MODE}**\n\n"
               f"Real orders: **{gate}**")
    if st.button("🔄 Refresh data"):
        st.cache_data.clear()
        st.rerun()

# ---------------------------------------------------------------- navigation
nav = st.navigation(
    [
        st.Page(summary.page, title="Summary", icon="🏠", default=True),
        st.Page(book.page, title="Book", icon="💼", url_path="book"),
        st.Page(trades.page, title="All Trades", icon="🧾", url_path="trades"),
        st.Page(ledger.page, title="Ledger", icon="📒", url_path="ledger"),
        st.Page(fleet.page, title="Fleet", icon="🛰️", url_path="fleet"),
        st.Page(history.page, title="History", icon="📜", url_path="history"),
        st.Page(backtests.page, title="Backtest", icon="🧪", url_path="backtest"),
    ],
    position="top",
)
nav.run()
