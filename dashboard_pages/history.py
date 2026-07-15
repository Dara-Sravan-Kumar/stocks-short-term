"""History — everything since day one: equity curve, P&L by strategy and
channel, per-ticker tracking series, closed picks, and the run log."""
from __future__ import annotations

import streamlit as st

from stockbot import db
from dashboard_pages import common


def _equity(conn) -> None:
    curve = common.rows_to_df(db.get_equity_curve(conn))
    if curve.empty:
        st.info("No equity history yet.")
        return
    curve["run"] = curve["date"] + " " + curve["run_slot"]
    st.line_chart(curve.set_index("run")[["equity", "cash"]],
                  color=common.SERIES[:2])
    st.line_chart(curve.set_index("run")[["realized_pnl_cum", "unrealized_pnl"]],
                  color=common.SERIES[:2])


def _pnl_by_strategy(conn) -> None:
    pnl = common.sql_df(conn, """
        SELECT strategy, COUNT(*) AS trades,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
               ROUND(SUM(realized_pnl), 0) AS net_pnl,
               ROUND(AVG(realized_pnl), 0) AS avg_pnl,
               MIN(exit_date) AS first_exit, MAX(exit_date) AS last_exit
        FROM paper_positions WHERE status='CLOSED'
        GROUP BY strategy ORDER BY net_pnl DESC""")
    if pnl.empty:
        st.info("No closed paper trades yet.")
        return
    pnl["win_rate_%"] = (pnl["wins"] / pnl["trades"] * 100).round(1)
    st.dataframe(pnl, width="stretch", hide_index=True)

    st.subheader("Cumulative Realized P&L")
    daily = common.sql_df(conn, """
        SELECT exit_date, SUM(realized_pnl) AS pnl FROM paper_positions
        WHERE status='CLOSED' GROUP BY exit_date ORDER BY exit_date""")
    daily["cumulative"] = daily["pnl"].cumsum()
    st.line_chart(daily.set_index("exit_date")[["cumulative"]],
                  color=common.SERIES[0])


def _tracking_explorer(conn) -> None:
    tracked = common.sql_df(conn, "SELECT DISTINCT ticker FROM tracking_log ORDER BY 1")
    if tracked.empty:
        st.info("No tracking history yet.")
        return
    ticker = st.selectbox("Ticker", tracked["ticker"].tolist())
    series = common.sql_df(conn, """
        SELECT date || ' ' || run_slot AS run, price, return_pct, sentiment,
               kind, note, catalyst
        FROM tracking_log WHERE ticker = ? ORDER BY date, run_slot""", (ticker,))
    if series.empty:
        return
    s1, s2 = st.columns(2)
    with s1:
        st.caption("Price")
        st.line_chart(series.set_index("run")[["price"]], color=common.SERIES[0])
    with s2:
        st.caption("Return % vs entry · Sentiment")
        st.line_chart(series.set_index("run")[["return_pct", "sentiment"]],
                      color=common.SERIES[:2])
    with st.expander("Snapshots (note + news catalyst per run)"):
        st.dataframe(series[["run", "kind", "price", "return_pct",
                             "sentiment", "note", "catalyst"]],
                     width="stretch", hide_index=True)


def _closed_picks(conn) -> None:
    picks = common.sql_df(conn, """
        SELECT ticker, channel, status, entry_date, entry_price, exit_date,
               exit_price, ROUND((exit_price-entry_price)/entry_price*100, 2)
                   AS pnl_pct, exit_reason
        FROM picks WHERE status != 'ACTIVE'
        ORDER BY exit_date DESC LIMIT 500""")
    if picks.empty:
        st.caption("No closed picks yet.")
        return
    st.dataframe(picks, width="stretch", hide_index=True)


def _run_log(conn) -> None:
    runs = common.sql_df(conn, """
        SELECT run_date, started_at, finished_at, tickers_scanned,
               new_picks, exits, warnings
        FROM run_log ORDER BY started_at DESC LIMIT 50""")
    if runs.empty:
        st.caption("No runs logged yet.")
        return
    st.dataframe(runs, width="stretch", hide_index=True)


def render(conn) -> None:
    """Body of the History view (no title/conn management)."""
    st.header("Equity Curve")
    _equity(conn)
    st.header("Realized P&L by Strategy")
    _pnl_by_strategy(conn)
    st.header("Ticker Tracking Explorer")
    _tracking_explorer(conn)
    st.header("Closed Picks (signal-level)")
    _closed_picks(conn)
    st.header("Run Log")
    _run_log(conn)


def page() -> None:
    conn = common.get_conn()
    st.title("📜 History")
    common.last_run_caption(conn)
    render(conn)
    conn.close()
