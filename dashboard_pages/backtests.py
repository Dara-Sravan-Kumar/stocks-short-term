"""Backtest — scorecards from data/backtests/ with per-variant drill-down."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

import config
from dashboard_pages import common


def page() -> None:
    st.title("🧪 Backtest")
    bt_dir = Path(config.DATA_DIR) / "backtests"
    bt_files = sorted(bt_dir.glob("backtest_*.json"), reverse=True)
    if not bt_files:
        st.info("No backtests yet. Run one with:  `python backtest.py --days 120`")
        return

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
    score_df = pd.DataFrame(score_rows).sort_values("Net ₹", ascending=False)
    st.dataframe(score_df, width="stretch", hide_index=True)

    st.subheader("Net P&L by Variant")
    st.bar_chart(score_df.set_index("Variant")["Net ₹"].sort_values(),
                 horizontal=True, height=max(220, 24 * len(score_df)),
                 color=common.SERIES[0])

    st.subheader("Variant Drill-down")
    variant_pick = st.selectbox("Variant", list(results))
    r = results[variant_pick]
    trades = pd.DataFrame(r["trades_detail"])
    if trades.empty:
        st.caption("This variant produced no trades in the replay window.")
    else:
        trades = trades.sort_values("exit_date")
        trades["cumulative ₹"] = trades["net_inr"].cumsum()
        st.line_chart(trades.set_index("exit_date")[["cumulative ₹"]],
                      color=common.SERIES[0])
        e1, e2 = st.columns(2)
        with e1:
            st.caption("Exit breakdown")
            st.bar_chart(pd.Series(r["exit_breakdown"]), color=common.SERIES[0])
        with e2:
            st.caption("P&L % distribution")
            st.bar_chart(trades["pnl_pct"].value_counts(bins=15).sort_index(),
                         color=common.SERIES[0])
        with st.expander(f"All {len(trades)} trades"):
            st.dataframe(trades, width="stretch", hide_index=True)
    with st.expander("Variant parameters"):
        st.json(r["params"])
