"""Fleet Analysis — a complete cross-channel view of the self-evolving strategy
fleet (all channels incl. DISCOVERED), aggregate health, per-channel breakdown,
capital allocation, the discovered/mixer spotlight, and an on-demand backtest
runner. Complements the per-variant Ledger page.
"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

import config
from stockbot import backtest, db, strategy_engine
from dashboard_pages import common

_CHANNELS = (*config.EVOLVING_CHANNELS, "NEWS", "DISCOVERED")


def _stat(ledger_stats, key):
    return ledger_stats.get(key, {"closed": 0, "win_rate": 0.0,
                                  "realized_pnl": 0.0, "profit_factor": None})


def _overview(active, ledger_stats):
    closed = sum(_stat(ledger_stats, s["variant_key"])["closed"] for s in active)
    wr = (sum((_stat(ledger_stats, s["variant_key"])["win_rate"] or 0)
              * _stat(ledger_stats, s["variant_key"])["closed"] for s in active)
          / closed) if closed else 0.0
    pnl = sum(_stat(ledger_stats, s["variant_key"])["realized_pnl"] or 0 for s in active)
    grads = sum(1 for s in active if s["graduate_candidate"])
    disc = sum(1 for s in active if s["channel"] == "DISCOVERED")

    c = st.columns(6)
    c[0].metric("Active variants", len(active))
    c[1].metric("Channels", len({s["channel"] for s in active}))
    c[2].metric("Closed trades", closed)
    c[3].metric("Fleet win rate", f"{wr:.0f}%")
    c[4].metric("Realized P&L", common.inr(pnl, signed=True))
    c[5].metric("Discovered 🔬", disc, help="Strategies found by the web "
                "discoverer / genetic mixer that cleared the backtest gate")
    if grads:
        st.caption(f"🏆 {grads} graduate candidate(s) — proven edge over "
                   f"{config.STRATEGY_GRADUATE_MIN_TRADES}+ trades.")


def _per_channel(active, ledger_stats, weights):
    agg = {}
    for s in active:
        st_ = _stat(ledger_stats, s["variant_key"])
        d = agg.setdefault(s["channel"], {"Variants": 0, "Closed": 0, "pnl": 0.0,
                                          "wr": 0.0, "Capital %": 0.0})
        d["Variants"] += 1
        d["Closed"] += st_["closed"]
        d["pnl"] += st_["realized_pnl"] or 0
        d["wr"] += (st_["win_rate"] or 0) * st_["closed"]
        d["Capital %"] += weights.get(s["variant_key"], 0.0)
    rows = [{"Channel": ch, "Variants": d["Variants"], "Closed": d["Closed"],
             "Win %": round(d["wr"] / d["Closed"], 1) if d["Closed"] else 0.0,
             "Realized ₹": round(d["pnl"], 0),
             "Capital %": round(d["Capital %"], 1)} for ch, d in agg.items()]
    df = pd.DataFrame(rows).sort_values("Capital %", ascending=False)
    st.subheader("Per-channel breakdown")
    st.dataframe(df, width="stretch", hide_index=True)


def _variant_table(active, ledger_stats, weights):
    rows = []
    for s in active:
        st_ = _stat(ledger_stats, s["variant_key"])
        expr = ""
        if s["params_json"]:
            expr = (json.loads(s["params_json"]) or {}).get("entry_expr", "")
        rows.append({
            "Variant": s["variant_key"], "Channel": s["channel"],
            "Closed": st_["closed"], "Win %": round(st_["win_rate"], 1),
            "Realized ₹": round(st_["realized_pnl"], 0),
            "PF": (round(st_["profit_factor"], 2)
                   if st_["profit_factor"] is not None else float("nan")),
            "Capital %": round(weights.get(s["variant_key"], 0.0), 1),
            "Origin": s["origin"],
            "🏆": "🏆" if s["graduate_candidate"] else "",
            "Entry expr (discovered)": expr,
        })
    df = pd.DataFrame(rows).sort_values(["Channel", "Capital %"], ascending=[True, False])
    st.subheader(f"All active variants ({len(df)})")
    st.dataframe(df, width="stretch", hide_index=True)

    if not df.empty:
        st.subheader("Capital allocation")
        alloc = df.set_index("Variant")["Capital %"].sort_values()
        st.bar_chart(alloc, horizontal=True, height=max(220, 22 * len(alloc)),
                     color=common.SERIES[0])


def _discovered_spotlight(active, ledger_stats):
    disc = [s for s in active if s["channel"] == "DISCOVERED"]
    st.subheader("🔬 Discovered & mixed strategies")
    if not disc:
        st.info("No discovered strategies yet. The evening (18:30) run proposes "
                "published swing strategies and breeds genetic-mixer offspring; "
                "only those clearing the out-of-sample backtest gate land here — "
                "expect empty days, it's a strict filter.")
        return
    rows = []
    for s in disc:
        st_ = _stat(ledger_stats, s["variant_key"])
        params = json.loads(s["params_json"] or "{}")
        rows.append({
            "Variant": s["variant_key"], "Source": s["origin"],
            "Entry expression": params.get("entry_expr", ""),
            "R:R": params.get("min_reward_risk", ""),
            "Closed": st_["closed"], "Win %": round(st_["win_rate"], 1),
            "Realized ₹": round(st_["realized_pnl"], 0),
            "Rationale": s["generation_rationale"] or "",
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def _backtest_runner():
    st.subheader("🧪 Run a backtest now")
    st.caption("Replays the active fleet over historical daily bars through the "
               "live gate/exit/cost code, and saves it to the Backtest page. "
               "Heavy: fetches history + replays, so it can take a few minutes.")
    c = st.columns(4)
    # label -> (replay sessions, yfinance fetch depth). Longer periods pull
    # deeper history on demand; the extra fetch depth over the replay window
    # gives the indicators their warm-up bars.
    periods = {
        "3 Months (~63)": (63, "1y"),
        "6 Months (~126)": (126, "1y"),
        "1 Year (~240)": (240, "2y"),
        "2 Years (~480)": (480, "3y"),
        "3 Years (~720)": (720, "5y"),
        "5 Years (max)": (1300, "max"),
        "Custom sessions…": (None, "5y"),
    }
    label = c[0].selectbox("Period", list(periods), index=2,
                           help="How far back to replay. Longer periods fetch "
                                "deeper history on demand (yfinance).")
    days, fetch_period = periods[label]
    if days is None:
        days = c[0].number_input("Sessions", 20, 1500, 240, step=20)
    tcap = c[1].number_input("Max tickers", 10, 500, 60, step=10,
                             help="Cap the universe for a faster run")
    capital = c[2].number_input("₹ per trade", 10_000, 1_000_000, 100_000, step=10_000)
    seeds = c[3].checkbox("Seeds only", value=False,
                          help="Test each channel's seed defaults, skip DB variants")
    st.caption("Multi-year runs fetch deeper history on demand (via yfinance); "
               "the daily runs still use ~1 year, so this never slows them. "
               "Bigger period × more tickers = longer wait.")

    if st.button("▶ Run backtest", type="primary"):
        with st.spinner("Fetching history and replaying the fleet…"):
            try:
                payload = backtest.run_and_save(
                    days=int(days), tickers_cap=int(tcap),
                    capital=float(capital), seeds_only=bool(seeds),
                    period=fetch_period)
                st.session_state["fleet_bt"] = payload
            except Exception as exc:  # never let a backtest crash the page
                st.session_state["fleet_bt"] = {"error": str(exc)}

    payload = st.session_state.get("fleet_bt")
    if not payload:
        return
    if payload.get("error"):
        st.error(f"Backtest failed: {payload['error']}")
        return

    st.success(f"Replayed {payload['days']} sessions × {payload['tickers']} "
               f"tickers · saved to {payload['path']}")
    results = payload["results"]
    score = pd.DataFrame([{
        "Variant": k, "Channel": r["channel"], "Trades": r["trades"],
        "Win %": r["win_rate_pct"], "Net ₹": r["net_inr"],
        "PF": r["profit_factor"], "Max DD ₹": r["max_drawdown_inr"],
        "Graduate?": "🏆" if r["meets_graduation_gate"] else "",
    } for k, r in results.items()]).sort_values("Net ₹", ascending=False)
    st.dataframe(score, width="stretch", hide_index=True)
    if not score.empty:
        st.bar_chart(score.set_index("Variant")["Net ₹"].sort_values(),
                     horizontal=True, height=max(220, 22 * len(score)),
                     color=common.SERIES[0])


def page() -> None:
    conn = common.get_conn()
    st.title("🛰️ Fleet Analysis")
    common.last_run_caption(conn)

    ledger_stats = db.get_strategy_ledger_stats(conn)
    weights = strategy_engine.current_capital_weights(conn)
    active = [s for ch in _CHANNELS for s in db.get_active_strategies(conn, channel=ch)]

    if not active:
        st.info("No active strategies yet.")
        conn.close()
        return

    _overview(active, ledger_stats)
    st.divider()
    _per_channel(active, ledger_stats, weights)
    _variant_table(active, ledger_stats, weights)
    st.divider()
    _discovered_spotlight(active, ledger_stats)
    st.divider()
    _backtest_runner()
    conn.close()
