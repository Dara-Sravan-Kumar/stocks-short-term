"""Daily health check of personal holdings vs fresh levels + sentiment."""
from __future__ import annotations

import sqlite3

import config
from stockbot import db
from stockbot.indicators import Snapshot


def health_check(conn: sqlite3.Connection, snapshots: dict[str, Snapshot],
                 sentiments: dict[str, dict], warnings: list[str]) -> list[dict]:
    """Return one report row per holding with a color-coded signal."""
    reports = []
    for h in db.get_holdings(conn):
        ticker = h["ticker"]
        snap = snapshots.get(ticker)
        if snap is None:
            warnings.append(f"{ticker}: no fresh data for holdings check")
            reports.append({
                "ticker": ticker, "avg_buy": h["avg_buy_price"], "qty": h["quantity"],
                "ltp": None, "pnl_pct": None, "level": "yellow",
                "signal": "No fresh market data - check manually",
            })
            continue

        ltp = snap.close
        pnl = (ltp - h["avg_buy_price"]) / h["avg_buy_price"] * 100
        sent = (sentiments.get(ticker) or {}).get("score", 0.0) or 0.0
        sent_summary = (sentiments.get(ticker) or {}).get("summary", "")

        level, signal = "green", "Healthy: above key averages, no risk flags"

        near_r1 = abs(snap.r1 - ltp) / ltp * 100 <= config.RESISTANCE_PROXIMITY_PCT
        near_r2 = abs(snap.r2 - ltp) / ltp * 100 <= config.RESISTANCE_PROXIMITY_PCT
        near_s1 = abs(ltp - snap.s1) / ltp * 100 <= config.SUPPORT_PROXIMITY_PCT

        if snap.low <= snap.s1 or ltp < snap.sma50:
            level = "red"
            what = "broke pivot support S1" if snap.low <= snap.s1 else "closed below SMA50"
            signal = f"RISK: {what} - consider exit or tighten stop"
        elif sent <= config.SENTIMENT_EXIT_MAX:
            level = "red"
            signal = f"RISK: strongly bearish news ({sent:+.2f}). {sent_summary}".strip()
        elif near_r1 or near_r2 or snap.rsi >= config.RSI_OVERBOUGHT:
            level = "yellow"
            what = f"RSI {snap.rsi:.0f} overbought" if snap.rsi >= config.RSI_OVERBOUGHT \
                else "approaching resistance " + ("R1" if near_r1 else "R2")
            signal = f"OPPORTUNITY: {what} - consider booking partial profit"
        elif near_s1 and snap.rsi <= config.RSI_OVERSOLD:
            level = "yellow"
            signal = f"WATCH: near support S1 with RSI {snap.rsi:.0f} oversold - hold, watch for bounce"

        reports.append({
            "ticker": ticker, "avg_buy": h["avg_buy_price"], "qty": h["quantity"],
            "ltp": round(ltp, 2), "pnl_pct": round(pnl, 2),
            "level": level, "signal": signal,
            "s1": round(snap.s1, 2), "r1": round(snap.r1, 2), "rsi": round(snap.rsi, 1),
        })
    return reports
