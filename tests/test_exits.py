"""Exit engine — the soft-exit grace period (MIN_HOLD_BEFORE_SOFT_EXIT).

Regression guard for the day-1 SETUP_BROKEN bug: `closes_below_sma20` is the
stock's absolute streak (predates entry), so without a grace period a dip-buy
entry was guillotined on its first evaluation bar citing a streak it was born
into. Hard stop/target must still fire any day; soft exits must wait.
"""
import pandas as pd

import config
from stockbot import db, exits
from stockbot.indicators import Snapshot

DATES = ["2026-07-06", "2026-07-07", "2026-07-08"]  # 3 trading bars
RUN_DATE = DATES[-1]


def _snap(**overrides) -> Snapshot:
    base = dict(
        ticker="TESTX.NS", date=RUN_DATE,
        close=100.0, high=102.0, low=99.6,          # inside a wide stop/target
        rsi=55.0,
        macd=0.5, macd_signal=0.6, macd_hist=-0.1, macd_hist_prev=-0.3,
        macd_bullish_cross_recent=False, macd_bearish_cross_today=False,
        sma20=100.0, sma50=95.0, closes_below_sma20=5,   # setup "broken" by absolute state
        mom_5d=-2.0, mom_20d=6.0,
        vol_ratio=1.0, avg_turnover_20d=500e6,
        swing_low_10d=97.0, swing_low_10d_prior=97.0, close_prev=99.0,
        cmf=0.1, cmf_prev=0.05, fvg_bull_bottom=None, fvg_bull_top=None,
        anchored_vwap=98.0, volume_poc=98.0, high_252d=110.0,
        pivot=100.5, r1=103.0, r2=105.0, s1=98.0, s2=96.0, r3=107.0,
        weekly_r1=104.0, weekly_r2=106.0, weekly_r3=108.0,
    )
    base.update(overrides)
    return Snapshot(**base)


def _setup(tmp_path, entry_date: str, snap: Snapshot):
    conn = db.connect(tmp_path / "t.db")
    db.insert_pick(conn, {
        "ticker": "TESTX.NS", "entry_date": entry_date,
        "entry_price": 100.0, "target_price": 110.0, "stop_price": 90.0,
        "rsi_at_entry": 55.0, "channel": "TECHNICAL", "rationale": "test",
    })
    histories = {"TESTX.NS": pd.DataFrame(index=pd.to_datetime(DATES))}
    snapshots = {"TESTX.NS": snap}
    return conn, snapshots, histories


def test_soft_exit_suppressed_within_grace(tmp_path):
    # entered yesterday -> days_held = 1 (< MIN_HOLD_BEFORE_SOFT_EXIT) -> no soft exit
    assert config.MIN_HOLD_BEFORE_SOFT_EXIT >= 2
    conn, snaps, hist = _setup(tmp_path, "2026-07-07", _snap())
    closed = exits.evaluate_active_picks(conn, snaps, hist, {}, RUN_DATE, [])
    assert closed == []  # broken streak predates entry; held too briefly to act


def test_soft_exit_fires_after_grace(tmp_path):
    # entered 2 bars ago -> days_held = 2 (>= grace) -> SETUP_BROKEN now eligible
    conn, snaps, hist = _setup(tmp_path, "2026-07-06", _snap())
    closed = exits.evaluate_active_picks(conn, snaps, hist, {}, RUN_DATE, [])
    assert len(closed) == 1
    assert closed[0]["status"] == "SETUP_BROKEN"


def test_hard_stop_fires_within_grace(tmp_path):
    # a real stop breach must NOT be delayed by the grace period
    conn, snaps, hist = _setup(tmp_path, "2026-07-07", _snap(low=89.0))
    closed = exits.evaluate_active_picks(conn, snaps, hist, {}, RUN_DATE, [])
    assert len(closed) == 1
    assert closed[0]["status"] == "STOPPED_OUT"


def test_hard_target_fires_within_grace(tmp_path):
    conn, snaps, hist = _setup(tmp_path, "2026-07-07", _snap(high=111.0))
    closed = exits.evaluate_active_picks(conn, snaps, hist, {}, RUN_DATE, [])
    assert len(closed) == 1
    assert closed[0]["status"] == "TARGET_HIT"
