"""Daily re-evaluation of ACTIVE picks — the exit engine.

Order of checks per pick (conservative: stop before target on the same bar):
  1. STOPPED_OUT   low <= stop
  2. TARGET_HIT    high >= target
  3. SETUP_BROKEN  trend/momentum/sentiment breakdown
  4. EXPIRED       held > MAX_HOLDING_DAYS trading days
"""
from __future__ import annotations

import sqlite3

import config
from stockbot import db
from stockbot.indicators import Snapshot


def _trading_days_held(entry_date: str, snap: Snapshot, history_index) -> int:
    """Count trading bars since entry using the ticker's own bar index."""
    dates = [d.strftime("%Y-%m-%d") for d in history_index]
    try:
        start = dates.index(entry_date)
        return len(dates) - 1 - start
    except ValueError:
        # entry date not a bar (holiday insert) — approximate by counting later bars
        return sum(1 for d in dates if d > entry_date)


def evaluate_active_picks(conn: sqlite3.Connection, snapshots: dict[str, Snapshot],
                          histories: dict, sentiments: dict[str, dict],
                          date: str, warnings: list[str]) -> list[dict]:
    """Apply exit logic to every ACTIVE pick. Returns list of closed picks."""
    closed = []
    for pick in db.get_active_picks(conn):
        ticker = pick["ticker"]
        snap = snapshots.get(ticker)
        if snap is None:
            warnings.append(f"{ticker}: no fresh data — active pick left untouched")
            continue
        if pick["entry_date"] == date:
            continue  # suggested today; evaluate from tomorrow

        status = exit_price = reason = None

        if snap.low <= pick["stop_price"]:
            status = "STOPPED_OUT"
            exit_price = pick["stop_price"]
            reason = "Hit stop loss (support broken)"
        elif snap.high >= pick["target_price"]:
            status = "TARGET_HIT"
            exit_price = pick["target_price"]
            reason = "Target resistance reached - book profit"
        else:
            sent = (sentiments.get(ticker) or {}).get("score", 0.0) or 0.0
            # pullback entries sit AT SMA20, so they get more room before the
            # consecutive-closes-below rule fires
            sma_bars = (config.PULLBACK_SETUP_BROKEN_SMA_BARS
                        if pick["channel"] == "PULLBACK"
                        else config.SETUP_BROKEN_SMA_BARS)
            if snap.closes_below_sma20 >= sma_bars:
                status = "SETUP_BROKEN"
                exit_price = snap.close
                reason = (f"Setup broken: closed below SMA20 for "
                          f"{snap.closes_below_sma20} consecutive days")
            elif snap.macd_bearish_cross_today and snap.rsi < config.SETUP_BROKEN_RSI:
                status = "SETUP_BROKEN"
                exit_price = snap.close
                reason = f"Setup broken: MACD bearish crossover with RSI {snap.rsi:.0f}"
            elif sent <= config.SENTIMENT_EXIT_MAX:
                status = "SETUP_BROKEN"
                exit_price = snap.close
                reason = f"News sentiment breakdown ({sent:+.2f})"
            else:
                hist = histories.get(ticker)
                days = _trading_days_held(pick["entry_date"], snap, hist.index) if hist is not None else 0
                if days > config.MAX_HOLDING_DAYS:
                    status = "EXPIRED"
                    exit_price = snap.close
                    reason = (f"Max holding period ({config.MAX_HOLDING_DAYS} trading days) "
                              "- no longer required")

        if status:
            db.close_pick(conn, pick["id"], status, date, round(float(exit_price), 2), reason)
            pnl = (exit_price - pick["entry_price"]) / pick["entry_price"] * 100
            closed.append({
                "ticker": ticker, "status": status, "entry_date": pick["entry_date"],
                "entry_price": pick["entry_price"], "exit_price": round(float(exit_price), 2),
                "exit_reason": reason, "pnl_pct": round(pnl, 2),
            })
    return closed
