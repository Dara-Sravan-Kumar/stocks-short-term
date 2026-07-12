"""Ledger — the self-evolving strategy fleet: every active variant with its
paper-trade record and capital weight, capital allocation chart, per-variant
drill-down, and the retired-variant graveyard."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

import config
from stockbot import db, strategy_engine
from dashboard_pages import common


def _fleet_table(conn) -> pd.DataFrame:
    ledger_stats = db.get_strategy_ledger_stats(conn)
    all_active = [s for ch in (*config.EVOLVING_CHANNELS, "NEWS", "DISCOVERED")
                  for s in db.get_active_strategies(conn, channel=ch)]
    weights = strategy_engine.current_capital_weights(conn)

    rows = []
    for s in all_active:
        key = s["variant_key"]
        stats = ledger_stats.get(key, {"closed": 0, "win_rate": 0.0,
                                       "realized_pnl": 0.0, "profit_factor": None})
        rows.append({
            "Variant": key,
            "Channel": s["channel"],
            "Closed": stats["closed"],
            "Win Rate %": round(stats["win_rate"], 1),
            "Realized P&L": round(stats["realized_pnl"], 0),
            "Profit Factor": (round(stats["profit_factor"], 2)
                              if stats["profit_factor"] is not None else float("nan")),
            "Capital Weight %": round(weights.get(key, 0.0), 1),
            "Origin": s["origin"],
            "Created": s["created_at"],
            "Graduate": "🏆" if s["graduate_candidate"] else "",
        })
    df = pd.DataFrame(rows).sort_values(["Channel", "Variant"]) if rows else pd.DataFrame()
    st.dataframe(df, width="stretch", hide_index=True)

    if not df.empty:
        st.subheader("Capital Allocation")
        alloc = df.set_index("Variant")["Capital Weight %"].sort_values()
        st.bar_chart(alloc, horizontal=True, height=max(220, 24 * len(alloc)),
                     color=common.SERIES[0])
    return df


def _variant_drilldown(conn, fleet: pd.DataFrame) -> None:
    st.subheader("Variant Drill-down")
    all_keys = common.sql_df(
        conn, "SELECT DISTINCT variant_key FROM strategies ORDER BY 1")
    if all_keys.empty:
        st.caption("No strategies registered yet.")
        return
    pick = st.selectbox("Variant", all_keys["variant_key"].tolist())
    row = conn.execute("SELECT * FROM strategies WHERE variant_key = ?",
                       (pick,)).fetchone()
    if row is None:
        return

    meta = dict(row)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Status", meta.get("status", "-"))
    c2.metric("Channel", meta.get("channel", "-"))
    c3.metric("Origin", meta.get("origin", "-"))
    c4.metric("Parent", meta.get("parent_variant_key") or "—", delta_color="off")
    if meta.get("retired_reason"):
        st.caption(f"Retired {meta.get('retired_at')}: {meta['retired_reason']}")

    params = meta.get("params_json")
    with st.expander("Parameters"):
        if params:
            st.json(json.loads(params))
        else:
            st.caption("Seed variant — uses config.py defaults.")
    if meta.get("generation_rationale"):
        with st.expander("LLM rationale"):
            st.write(meta["generation_rationale"])

    trades = common.sql_df(conn, """
        SELECT ticker, status, qty, entry_date, entry_fill_price, exit_date,
               exit_fill_price, ROUND(realized_pnl, 0) AS realized_pnl, exit_reason
        FROM paper_positions WHERE strategy = ?
        ORDER BY COALESCE(exit_date, entry_date) DESC LIMIT 500""", (pick,))
    if trades.empty:
        st.caption("No paper trades recorded for this variant yet.")
        return
    closed = trades.dropna(subset=["exit_date"]).sort_values("exit_date")
    if not closed.empty:
        closed = closed.assign(cumulative=closed["realized_pnl"].cumsum())
        st.line_chart(closed.set_index("exit_date")[["cumulative"]],
                      height=200, color=common.SERIES[0])
    st.dataframe(trades, width="stretch", hide_index=True)


def _retired(conn) -> None:
    retired = common.rows_to_df(
        [s for s in db.get_all_strategies(conn) if s["status"] == "RETIRED"])
    if retired.empty:
        return
    with st.expander(f"Retired variants ({len(retired)})"):
        st.dataframe(
            retired[["channel", "variant_key", "retired_at", "retired_reason",
                     "parent_variant_key"]],
            width="stretch", hide_index=True)


def page() -> None:
    conn = common.get_conn()
    st.title("📒 Strategy Ledger")
    common.last_run_caption(conn)

    st.caption(f"Fleets evolve automatically: a variant retires below "
               f"{config.STRATEGY_RETIREMENT_WIN_RATE_FLOOR:.0f}% win rate after "
               f"{config.STRATEGY_MIN_TRADES_FOR_RETIREMENT} closed trades and is "
               f"replaced immediately; a wildcard variant is added every "
               f"{config.STRATEGY_WILDCARD_INTERVAL_DAYS} days; "
               f"{config.STRATEGY_GRADUATE_WIN_RATE:.0f}% win rate over "
               f"{config.STRATEGY_GRADUATE_MIN_TRADES}+ trades flags a graduate 🏆.")

    fleet = _fleet_table(conn)
    _variant_drilldown(conn, fleet)
    _retired(conn)
    conn.close()
