"""Backtest engine: deterministic replay mechanics on fabricated bars —
entry/exit ordering, stop-before-target, expiry, and report stats."""
from datetime import datetime, timedelta

import pandas as pd
import pytest

import config
from stockbot import backtest
from stockbot.indicators import compute_snapshot


def _df(closes: list[float], spread: float = 1.0) -> pd.DataFrame:
    idx = pd.DatetimeIndex([datetime(2026, 1, 1) + timedelta(days=i)
                            for i in range(len(closes))])
    return pd.DataFrame({
        "Open": closes, "High": [c + spread for c in closes],
        "Low": [c - spread for c in closes], "Close": closes,
        "Volume": [1_000_000] * len(closes),
    }, index=idx)


def _variant(channel="TECHNICAL", key="TECHNICAL_seed"):
    from stockbot import strategy_engine
    return {"variant_key": key, "channel": channel,
            "params": strategy_engine.resolve_params(channel, None)}


def test_stop_beats_target_on_same_bar():
    book = backtest._Book(_variant())
    df = _df([100.0] * 70)
    snap = compute_snapshot("T.NS", df, config.MACD_CROSS_LOOKBACK)
    book.open("T.NS", "2026-03-01", snap, target=104.0, stop=97.0,
              capital=100_000)
    assert "T.NS" in book.positions

    # a wide bar touching BOTH stop and target must exit at the stop
    wide = _df([100.0] * 70, spread=10.0)
    snap2 = compute_snapshot("T.NS", wide, config.MACD_CROSS_LOOKBACK)
    backtest._evaluate_exits(book, "2026-03-02", {"T.NS": snap2})
    assert not book.positions
    assert book.trades[0]["status"] == "STOPPED_OUT"


def test_no_exit_on_entry_day():
    book = backtest._Book(_variant())
    wide = _df([100.0] * 70, spread=10.0)
    snap = compute_snapshot("T.NS", wide, config.MACD_CROSS_LOOKBACK)
    book.open("T.NS", "2026-03-01", snap, target=104.0, stop=97.0,
              capital=100_000)
    backtest._evaluate_exits(book, "2026-03-01", {"T.NS": snap})
    assert "T.NS" in book.positions and not book.trades


def test_costs_and_slippage_reduce_pnl():
    book = backtest._Book(_variant())
    df = _df([100.0] * 70, spread=0.5)
    snap = compute_snapshot("T.NS", df, config.MACD_CROSS_LOOKBACK)
    book.open("T.NS", "2026-03-01", snap, target=100.4, stop=95.0,
              capital=100_000)
    up = _df([100.0] * 69 + [100.5], spread=0.5)  # high 101.0 >= target
    snap2 = compute_snapshot("T.NS", up, config.MACD_CROSS_LOOKBACK)
    backtest._evaluate_exits(book, "2026-03-02", {"T.NS": snap2})
    t = book.trades[0]
    assert t["status"] == "TARGET_HIT"
    # gross move is +0.4% but slippage (2 x 0.05%) + charges must eat into it
    gross = (100.4 - 100.0) / 100.0 * 100
    assert t["pnl_pct"] < gross


def test_report_aggregates_and_graduation_gate():
    book = backtest._Book(_variant())
    for i in range(60):
        book.trades.append({"ticker": f"T{i}.NS", "entry_date": "d", "exit_date": "d",
                            "status": "TARGET_HIT", "reason": "", "qty": 1,
                            "entry_fill": 100.0, "exit_fill": 103.0, "bars_held": 3,
                            "net_inr": 300.0 if i % 5 else -100.0,  # 48W / 12L
                            "pnl_pct": 3.0 if i % 5 else -1.0})
    r = backtest._report(book, eval_dates=list(range(100)))
    assert r["trades"] == 60 and r["win_rate_pct"] == 80.0
    assert r["meets_graduation_gate"] is True
    assert r["exit_breakdown"]["TARGET_HIT"] == 60


def test_build_variant_list_seeds_only():
    variants = backtest.build_variant_list(None, channels=["TECHNICAL",
                                                           "PULLBACK"])
    keys = {v["variant_key"] for v in variants}
    assert keys == {"TECHNICAL_seed", "PULLBACK_seed"}
