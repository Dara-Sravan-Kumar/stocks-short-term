"""Summary — the landing page: headline numbers for the current mode, market
context, today's activity, and a fleet snapshot."""
from __future__ import annotations

import pandas as pd
import streamlit as st

import config
from stockbot import db
from dashboard_pages import common


def _paper_tiles(conn) -> None:
    equity_row = db.get_latest_equity_row(conn)
    book = db.get_paper_book(conn)
    if not (equity_row and book):
        st.info("No paper book activity yet — it appears after the first run.")
        return
    total_return = (equity_row["equity"] - book["starting_cash"]) / book["starting_cash"] * 100
    overall = conn.execute(
        """SELECT COUNT(*) AS n,
                  SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins
           FROM paper_positions WHERE status='CLOSED'""").fetchone()
    win_rate = (overall["wins"] / overall["n"] * 100) if overall["n"] else None

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Equity", common.inr(equity_row["equity"]), f"{total_return:+.2f}%")
    c2.metric("Cash", common.inr(equity_row["cash"]))
    c3.metric("In Positions", common.inr(equity_row["positions_value"]))
    c4.metric("Unrealized P&L", common.inr(equity_row["unrealized_pnl"], signed=True))
    c5.metric("Realized P&L", common.inr(equity_row["realized_pnl_cum"], signed=True))
    c6.metric("Win Rate", f"{win_rate:.0f}%" if win_rate is not None else "-",
              f"{overall['n']} closed trades", delta_color="off")
    st.caption(f"as of {equity_row['date']} {equity_row['run_slot']} · "
               f"started with {common.inr(book['starting_cash'])}")

    curve = common.rows_to_df(db.get_equity_curve(conn))
    if len(curve) > 1:
        curve["run"] = curve["date"] + " " + curve["run_slot"]
        st.line_chart(curve.set_index("run")[["equity"]], height=180,
                      color=common.SERIES[0])


def _live_tiles(conn) -> None:
    holdings = common.rows_to_df(db.get_holdings(conn))
    prov = db.get_holdings_provenance(conn)
    if holdings.empty:
        st.info("No real holdings synced yet.")
        return
    quotes = common.live_quotes(tuple(holdings["ticker"]))
    holdings["ltp"] = holdings["ticker"].map(
        lambda t: (quotes.get(t) or {}).get("ltp"))
    holdings["invested"] = holdings["avg_buy_price"] * holdings["quantity"]
    holdings["value"] = holdings["ltp"] * holdings["quantity"]
    priced = holdings.dropna(subset=["ltp"])
    invested = priced["invested"].sum()
    value = priced["value"].sum()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Holdings Value", common.inr(value) if len(priced) else "-",
              f"{(value - invested) / invested * 100:+.2f}%" if invested else None)
    c2.metric("Invested", common.inr(invested) if len(priced) else "-")
    c3.metric("Unrealized P&L", common.inr(value - invested, signed=True)
              if invested else "-")
    c4.metric("Positions", f"{len(holdings)}")
    source_note = f"source: {prov['source'] or '-'} · synced {prov['synced_at'] or 'never'}"
    if quotes:
        st.caption(f"live Fyers quotes ({len(quotes)}/{len(holdings)} priced) · {source_note}")
    else:
        st.caption(f"⚠️ live quotes unavailable — values need a Fyers token · {source_note}")

    if common.table_exists(conn, "live_trades"):
        today_orders = conn.execute(
            """SELECT status, COUNT(*) AS n FROM live_trades
               WHERE date = (SELECT MAX(date) FROM live_trades) GROUP BY status"""
        ).fetchall()
        if today_orders:
            st.caption("Latest live-order day: " + " · ".join(
                f"{r['status']} {r['n']}" for r in today_orders))


def _market_context(conn) -> None:
    ctx = db.get_latest_market_context(conn)
    if not ctx:
        st.info("No market context recorded yet.")
        return
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
    flags = [label for key, label in
             [("nifty_crash", "🔴 Nifty crash"), ("banknifty_crash", "🔴 Bank Nifty crash"),
              ("global_crash", "🔴 Global crash")] if ctx[key]]
    st.write(" · ".join(flags) if flags else "🟢 No crash conditions flagged")
    st.caption(f"as of {ctx['date']} {ctx['run_slot']}")


def _todays_activity(conn) -> None:
    last_run = db.get_last_run(conn)
    if not last_run:
        return
    date = last_run["run_date"]
    left, right = st.columns(2)
    with left:
        st.subheader(f"New picks · {date}")
        picks = common.rows_to_df(db.get_todays_new_picks(conn, date))
        if not picks.empty:
            st.dataframe(picks[["ticker", "channel", "entry_price",
                                "target_price", "stop_price"]],
                         width="stretch", hide_index=True)
        else:
            st.caption("None.")
    with right:
        st.subheader(f"Exits · {date}")
        exits = common.rows_to_df(db.get_todays_exits(conn, date))
        if not exits.empty:
            st.dataframe(exits[["ticker", "channel", "status", "entry_price",
                                "exit_price", "exit_reason"]],
                         width="stretch", hide_index=True)
        else:
            st.caption("None.")


def _fleet_snapshot(conn) -> None:
    stats = db.get_strategy_ledger_stats(conn)
    active = db.get_active_strategies(conn)
    by_channel = pd.Series([s["channel"] for s in active]).value_counts()
    graduates = [s["variant_key"] for s in active if s["graduate_candidate"]]

    c1, c2, c3 = st.columns(3)
    c1.metric("Active Variants", len(active), f"{len(by_channel)} channels",
              delta_color="off")
    ranked = sorted(((k, v) for k, v in stats.items() if v["closed"] > 0),
                    key=lambda kv: kv[1]["realized_pnl"], reverse=True)
    c2.metric("Best Variant", ranked[0][0] if ranked else "-",
              common.inr(ranked[0][1]["realized_pnl"], signed=True) if ranked else None)
    c3.metric("Graduate Candidates", len(graduates),
              ", ".join(graduates) if graduates else None, delta_color="off")
    st.caption("Variants per channel: " + " · ".join(
        f"{ch} {n}" for ch, n in by_channel.items()))


def _fyers_connection() -> tuple[bool, str]:
    """Read-only Fyers login status for the Summary banner. Mirrors EXACTLY the
    condition fyers_data._ensure_token uses (token cached AND issued today AND
    access_token present) — so this banner can never disagree with the book's
    actual freeze behaviour. Pure display; reads the shared token file only."""
    from datetime import datetime

    from stockbot import fyers_data
    cache = fyers_data.load_token_cache()
    today = datetime.now().strftime("%Y-%m-%d")
    if cache and cache.get("access_token") and cache.get("issued") == today:
        return True, f"token issued {cache['issued']}"
    if cache and cache.get("access_token"):
        return False, f"stale — last login {cache.get('issued') or '?'}"
    return False, "no token cached"


def _fyers_banner() -> None:
    ok, detail = _fyers_connection()
    if ok:
        st.success(f"🟢 **Connected to Fyers** — {detail}. Runs book on real "
                   "market data.")
    else:
        st.error(f"🔴 **Not connected to Fyers** — {detail}. Run the daily Fyers "
                 "login; the paper book stays frozen (fallback runs don't book) "
                 "until you log in.")


def render(conn) -> None:
    """Body of the Summary view (no title/conn management) — reused as a tab by
    dashboard_web.py's MCX-style 2-view layout."""
    _fyers_banner()
    st.header("Book at a Glance")
    if common.mode() == "LIVE":
        _live_tiles(conn)
    else:
        _paper_tiles(conn)

    st.header("Market Context")
    _market_context(conn)

    st.header("Today's Activity")
    _todays_activity(conn)

    st.header("Strategy Fleet")
    _fleet_snapshot(conn)


def page() -> None:
    conn = common.get_conn()
    st.title("📊 StockBot")
    common.last_run_caption(conn)
    common.mode_badge()
    render(conn)
    conn.close()
