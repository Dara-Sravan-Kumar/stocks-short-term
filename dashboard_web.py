"""StockBot web dashboard — TWO top-level views over the same SQLite state,
each with its own neatly-organised tab bar (mirrors mcx-short-term's layout):

  🟢 Live  — REAL trading. Real Fyers holdings, the live-order audit trail, and
             the paper→real graduation gate. Real order placement stays OFF
             unless TRADING_MODE=LIVE and PLACE_ORDER_ENABLED=True. Tabs:
               • Holdings & Orders — real Fyers holdings + live_trades audit.
               • Readiness & Gate  — graduation readiness + the hard order gate.
  📝 Paper — the virtual book the bot trades every run. Tabs:
               • Summary     — realized & unrealized headline + market context.
               • Open Book    — open positions (entry, live price, live P&L, qty).
               • Closed Book  — realized paper trades + per-strategy attribution.
               • Fleet        — the complete self-evolving strategy fleet.
               • Ledger       — per-variant record + capital weights.
               • History      — equity curve, run log, per-ticker tracking.
               • Backtest     — Fyers-sourced walk-forward strategy comparison.
               • Discovery    — discovered / genetically-mixed strategies.

Run with:  .venv\\Scripts\\python.exe -m streamlit run dashboard_web.py

Read-only: every connection is opened with mode=ro, so the dashboard can never
write to or lock the database run_daily.py is using — purely a viewer. Paper /
History figures are "as of the last run"; "live" columns come from Fyers quotes
(60s cache). Backtest figures are as of the last `python backtest.py`.
"""
from __future__ import annotations

import streamlit as st

import config
from stockbot import db
from dashboard_pages import (backtests, book, common, fleet, history, ledger,
                             summary, trades)

st.set_page_config(page_title="StockBot Dashboard", page_icon="📊", layout="wide")


# ===========================================================================
# 📝 PAPER tab bodies (Summary / Open Book / Closed Book / … reuse page modules)
# ===========================================================================
def paper_closed_book(conn) -> None:
    st.caption("Every realized paper trade, filterable and exportable, plus "
               "per-strategy P&L attribution.")
    trades._paper_trades(conn)
    st.divider()
    st.subheader("Realized P&L by strategy (attribution)")
    history._pnl_by_strategy(conn)


def paper_discovery(conn) -> None:
    st.caption(
        "Strategies proposed from published swing setups (Claude CLI) and bred "
        "by the genetic mixer, expressed as safe whitelist-only entry specs "
        "(DISCOVERED channel). Every candidate must clear an out-of-sample Fyers "
        "backtest before it trades. Discovery proposes; paper trades judge.")
    ledger_stats = db.get_strategy_ledger_stats(conn)
    active = [s for ch in (*config.EVOLVING_CHANNELS, "NEWS", "DISCOVERED")
              for s in db.get_active_strategies(conn, channel=ch)]
    fleet._discovered_spotlight(active, ledger_stats)


# ===========================================================================
# 🟢 LIVE tab bodies
# ===========================================================================
def live_readiness_gate(conn) -> None:
    st.caption("Real orders route through Fyers only for variants that "
               "**graduate** off the paper book — the path from paper → real.")
    gate_open = config.TRADING_MODE == "LIVE" and config.PLACE_ORDER_ENABLED
    active = [s for ch in (*config.EVOLVING_CHANNELS, "NEWS", "DISCOVERED")
              for s in db.get_active_strategies(conn, channel=ch)]
    graduated = [s for s in active if s["graduate_candidate"]]

    c1, c2, c3 = st.columns(3)
    c1.metric("Order gate", "OPEN ⚠️" if gate_open else "CLOSED (safe)")
    c2.metric("Graduated variants", f"{len(graduated)}",
              help=f"≥{config.STRATEGY_GRADUATE_WIN_RATE:.0f}% win over "
                   f"{config.STRATEGY_GRADUATE_MIN_TRADES}+ trades, positive P&L")
    c3.metric("Variants tracked", f"{len(active)}")

    if gate_open:
        st.error("⚠️ Real order placement is ENABLED (TRADING_MODE=LIVE + "
                 "PLACE_ORDER_ENABLED=True) — graduated variants can route live "
                 "Fyers orders.")
    else:
        off = []
        if config.TRADING_MODE != "LIVE":
            off.append("TRADING_MODE=PAPER")
        if not config.PLACE_ORDER_ENABLED:
            off.append("PLACE_ORDER_ENABLED=False")
        st.info(f"Real trading is **disabled** ({', '.join(off)}). When enabled, "
                "only graduated variants place live Fyers orders; everything else "
                "keeps paper-trading.")

    st.subheader("Graduation Readiness")
    st.caption(f"Graduate at ≥{config.STRATEGY_GRADUATE_WIN_RATE:.0f}% win rate over "
               f"{config.STRATEGY_GRADUATE_MIN_TRADES}+ closed trades with positive "
               "realized P&L.")
    ledger_stats = db.get_strategy_ledger_stats(conn)

    def _status(s, stt) -> str:
        if s["graduate_candidate"]:
            return "🏆 Graduated (allowlisted)"
        closed = stt["closed"]
        if closed < config.STRATEGY_GRADUATE_MIN_TRADES:
            return f"⏳ {config.STRATEGY_GRADUATE_MIN_TRADES - closed} more trades"
        if stt["win_rate"] < config.STRATEGY_GRADUATE_WIN_RATE:
            return (f"❌ win rate {stt['win_rate']:.0f}% < "
                    f"{config.STRATEGY_GRADUATE_WIN_RATE:.0f}%")
        if stt["realized_pnl"] <= 0:
            return "❌ realized P&L not positive"
        return "✅ eligible (pending flag)"

    import pandas as pd
    rows = []
    for s in active:
        stt = ledger_stats.get(s["variant_key"], {"closed": 0, "win_rate": 0.0,
                                                   "realized_pnl": 0.0,
                                                   "profit_factor": None})
        rows.append({
            "Variant": s["variant_key"], "Channel": s["channel"],
            "Closed": stt["closed"], "Win Rate %": round(stt["win_rate"], 1),
            "Realized P&L": round(stt["realized_pnl"], 0),
            "Status": _status(s, stt),
        })
    df = (pd.DataFrame(rows).sort_values(["Closed", "Win Rate %"], ascending=False)
          if rows else pd.DataFrame())
    st.dataframe(df, width="stretch", hide_index=True)


# ---------------------------------------------------------------------------
# Sidebar: 2-option view selector + run banner + order-gate note
# ---------------------------------------------------------------------------
st.sidebar.title("📊 StockBot")
view = st.sidebar.radio(
    "View", ["🟢 Live", "📝 Paper"], index=1, label_visibility="collapsed",
    help="Live: real Fyers holdings + the paper→real gate. "
         "Paper: the virtual book the bot trades every run.")
st.session_state[common.MODE_KEY] = "LIVE" if view.startswith("🟢") else "PAPER"

if st.sidebar.button("🔄 Refresh data"):
    st.cache_data.clear()
    st.rerun()

_sidebar_conn = common.get_conn()
_last = db.get_last_run(_sidebar_conn)
if _last:
    st.sidebar.caption(
        f"**Last run** {_last['run_date']} · finished {_last['finished_at']}\n\n"
        f"provider {_last['provider'] or '-'} · {_last['tickers_scanned']} tickers · "
        f"{_last['new_picks']} picks · {_last['exits']} exits")
else:
    st.sidebar.caption("No runs logged yet.")
_gate = "ENABLED ⚠️" if (config.TRADING_MODE == "LIVE"
                         and config.PLACE_ORDER_ENABLED) else "off"
st.sidebar.caption(f"TRADING_MODE: **{config.TRADING_MODE}** · real orders: **{_gate}**")
_sidebar_conn.close()


# ---------------------------------------------------------------------------
# Dispatch — one shared read-only connection; each view holds a neat tab bar
# ---------------------------------------------------------------------------
conn = common.get_conn()

if view.startswith("🟢"):
    st.title("🟢 Live — real trading")
    common.mode_badge()
    tab_hold, tab_ready = st.tabs(["Holdings & Orders", "Readiness & Gate"])
    with tab_hold:
        book.render(conn)
    with tab_ready:
        live_readiness_gate(conn)
else:
    st.title("📝 Paper")
    common.mode_badge()
    (tab_summary, tab_open, tab_closed, tab_fleet, tab_ledger,
     tab_history, tab_backtest, tab_discovery) = st.tabs(
        ["Summary", "Open Book", "Closed Book", "🛰️ Fleet", "📒 Ledger",
         "📜 History", "🧪 Backtest", "🔬 Discovery"])
    with tab_summary:
        summary.render(conn)
    with tab_open:
        book.render(conn)
    with tab_closed:
        paper_closed_book(conn)
    with tab_fleet:
        fleet.render(conn)
    with tab_ledger:
        ledger.render(conn)
    with tab_history:
        history.render(conn)
    with tab_backtest:
        backtests.render()
    with tab_discovery:
        paper_discovery(conn)

conn.close()
