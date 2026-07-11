"""Read-only web dashboard — one link, three views:
🔴 Live (paper book, ledger, positions as of the last run), 📜 History
(equity curve, per-strategy P&L, full trade log, per-ticker tracking series),
🧪 Backtest (scorecards from data/backtests/ with per-variant drill-down).

Run with:  .venv\\Scripts\\python.exe -m streamlit run dashboard_web.py

Opens its own read-only SQLite connection (mode=ro) so it can never write to
or lock the database that run_daily.py is using — this is purely a viewer.
Live figures are "as of the last run", not intraday prices; there is no
separate price-fetching here.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

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


def sql_df(conn, query: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(query, conn, params=params)


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

tab_live, tab_history, tab_backtest = st.tabs(["🔴 Live", "📜 History", "🧪 Backtest"])

# ===========================================================================
# LIVE — state as of the last run
# ===========================================================================
with tab_live:
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

    st.header("Real Holdings")
    holdings = rows_to_df(db.get_holdings(conn))
    if not holdings.empty:
        st.dataframe(holdings, width="stretch", hide_index=True)
    else:
        st.caption("No holdings synced.")

# ===========================================================================
# HISTORY — everything the bot has done since day one
# ===========================================================================
with tab_history:
    st.header("Equity Curve")
    equity_curve = rows_to_df(db.get_equity_curve(conn))
    if not equity_curve.empty:
        equity_curve["run"] = equity_curve["date"] + " " + equity_curve["run_slot"]
        st.line_chart(equity_curve.set_index("run")[["equity", "cash"]])
        st.line_chart(equity_curve.set_index("run")[["realized_pnl_cum", "unrealized_pnl"]])
    else:
        st.info("No equity history yet.")

    st.header("Realized P&L by Strategy")
    pnl_by_strategy = sql_df(conn, """
        SELECT strategy, COUNT(*) AS trades,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
               ROUND(SUM(realized_pnl), 0) AS net_pnl,
               ROUND(AVG(realized_pnl), 0) AS avg_pnl,
               MIN(exit_date) AS first_exit, MAX(exit_date) AS last_exit
        FROM paper_positions WHERE status='CLOSED'
        GROUP BY strategy ORDER BY net_pnl DESC""")
    if not pnl_by_strategy.empty:
        pnl_by_strategy["win_rate_%"] = (pnl_by_strategy["wins"] / pnl_by_strategy["trades"] * 100).round(1)
        st.dataframe(pnl_by_strategy, width="stretch", hide_index=True)
        daily_pnl = sql_df(conn, """
            SELECT exit_date, SUM(realized_pnl) AS pnl FROM paper_positions
            WHERE status='CLOSED' GROUP BY exit_date ORDER BY exit_date""")
        daily_pnl["cumulative"] = daily_pnl["pnl"].cumsum()
        st.line_chart(daily_pnl.set_index("exit_date")[["cumulative"]])
    else:
        st.info("No closed paper trades yet.")

    st.header("Full Trade Log")
    f1, f2 = st.columns(2)
    strategies = sql_df(conn, "SELECT DISTINCT strategy FROM paper_positions ORDER BY 1")
    strat_pick = f1.selectbox("Strategy", ["(all)"] + strategies["strategy"].tolist())
    status_pick = f2.selectbox("Status", ["CLOSED", "OPEN", "(all)"])
    q = """SELECT ticker, strategy, status, qty, entry_date, entry_fill_price,
                  target_price, stop_price, exit_date, exit_fill_price,
                  realized_pnl, exit_reason
           FROM paper_positions WHERE 1=1"""
    params: list = []
    if strat_pick != "(all)":
        q += " AND strategy = ?"
        params.append(strat_pick)
    if status_pick != "(all)":
        q += " AND status = ?"
        params.append(status_pick)
    q += " ORDER BY COALESCE(exit_date, entry_date) DESC, id DESC LIMIT 500"
    st.dataframe(sql_df(conn, q, tuple(params)), width="stretch", hide_index=True)

    st.header("Ticker Tracking Explorer")
    tracked = sql_df(conn, "SELECT DISTINCT ticker FROM tracking_log ORDER BY 1")
    if not tracked.empty:
        ticker_pick = st.selectbox("Ticker", tracked["ticker"].tolist())
        series = sql_df(conn, """
            SELECT date || ' ' || run_slot AS run, price, return_pct, sentiment,
                   kind, note, catalyst
            FROM tracking_log WHERE ticker = ? ORDER BY date, run_slot""",
            (ticker_pick,))
        if not series.empty:
            s1, s2 = st.columns(2)
            with s1:
                st.caption("Price")
                st.line_chart(series.set_index("run")[["price"]])
            with s2:
                st.caption("Return % vs entry · Sentiment")
                st.line_chart(series.set_index("run")[["return_pct", "sentiment"]])
            with st.expander("Snapshots (note + news catalyst per run)"):
                st.dataframe(series[["run", "kind", "price", "return_pct",
                                     "sentiment", "note", "catalyst"]],
                             width="stretch", hide_index=True)
    else:
        st.info("No tracking history yet.")

    st.header("Closed Picks (signal-level history)")
    picks_hist = sql_df(conn, """
        SELECT ticker, channel, status, entry_date, entry_price, exit_date,
               exit_price, ROUND((exit_price-entry_price)/entry_price*100, 2)
                   AS pnl_pct, exit_reason
        FROM picks WHERE status != 'ACTIVE'
        ORDER BY exit_date DESC LIMIT 500""")
    if not picks_hist.empty:
        st.dataframe(picks_hist, width="stretch", hide_index=True)
    else:
        st.caption("No closed picks yet.")

# ===========================================================================
# BACKTEST — replay scorecards from data/backtests/
# ===========================================================================
with tab_backtest:
    st.header("Backtest Results")
    bt_dir = Path(config.DATA_DIR) / "backtests"
    bt_files = sorted(bt_dir.glob("backtest_*.json"), reverse=True)
    if not bt_files:
        st.info("No backtests yet. Run one with:  "
                "`python backtest.py --days 120`")
    else:
        file_pick = st.selectbox("Backtest run", [f.name for f in bt_files])
        data = json.loads((bt_dir / file_pick).read_text(encoding="utf-8"))
        st.caption(f"{data['days']} sessions · {data['tickers']} tickers · "
                   f"₹{data['capital_per_trade']:,.0f} per trade · sentiment "
                   "neutral (NEWS channel + sentiment exits not replayed)")

        results = data["results"]
        score_rows = []
        for key, r in results.items():
            score_rows.append({
                "Variant": key, "Channel": r["channel"], "Trades": r["trades"],
                "Win %": r["win_rate_pct"], "Net ₹": r["net_inr"],
                "Avg %": r["avg_pnl_pct"], "Profit Factor": r["profit_factor"],
                "Avg Hold (bars)": r["avg_hold_bars"],
                "Max DD ₹": r["max_drawdown_inr"],
                "Open @ end": r["open_at_end"],
                "Graduate?": "🏆" if r["meets_graduation_gate"] else "",
            })
        score_df = (pd.DataFrame(score_rows)
                    .sort_values("Net ₹", ascending=False))
        st.dataframe(score_df, width="stretch", hide_index=True)

        st.subheader("Variant drill-down")
        variant_pick = st.selectbox("Variant", list(results))
        r = results[variant_pick]
        trades = pd.DataFrame(r["trades_detail"])
        if not trades.empty:
            trades = trades.sort_values("exit_date")
            trades["cumulative ₹"] = trades["net_inr"].cumsum()
            st.line_chart(trades.set_index("exit_date")[["cumulative ₹"]])
            e1, e2 = st.columns(2)
            with e1:
                st.caption("Exit breakdown")
                st.bar_chart(pd.Series(r["exit_breakdown"]))
            with e2:
                st.caption("P&L % distribution")
                st.bar_chart(trades["pnl_pct"].value_counts(bins=15).sort_index())
            with st.expander(f"All {len(trades)} trades"):
                st.dataframe(trades, width="stretch", hide_index=True)
        else:
            st.caption("This variant produced no trades in the replay window.")
        with st.expander("Variant parameters"):
            st.json(r["params"])

conn.close()
