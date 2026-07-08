"""Unit tests for the new daily-bar proxy indicators (order flow, FVG, anchored
VWAP, volume POC) against small hand-built OHLCV frames with known expected
values — these are the algorithmically novel pieces most worth pinning down.
"""
import numpy as np
import pandas as pd
import pytest

from stockbot.indicators import (
    anchored_vwap, chaikin_money_flow, compute_snapshot, find_bullish_fvg, volume_poc,
)


# ---------------------------------------------------------------------------
# Chaikin Money Flow (order-flow proxy)
# ---------------------------------------------------------------------------

def _flat_series(n, high, low, close, volume):
    return (pd.Series([close] * n), pd.Series([high] * n),
            pd.Series([low] * n), pd.Series([volume] * n))


def test_cmf_maximally_bullish_when_close_at_high():
    close, high, low, vol = _flat_series(25, high=110.0, low=100.0, close=110.0, volume=1000.0)
    cmf = chaikin_money_flow(close, high, low, vol, period=20)
    assert cmf.iloc[-1] == pytest.approx(1.0)


def test_cmf_maximally_bearish_when_close_at_low():
    close, high, low, vol = _flat_series(25, high=110.0, low=100.0, close=100.0, volume=1000.0)
    cmf = chaikin_money_flow(close, high, low, vol, period=20)
    assert cmf.iloc[-1] == pytest.approx(-1.0)


def test_cmf_neutral_when_close_at_midpoint():
    close, high, low, vol = _flat_series(25, high=110.0, low=100.0, close=105.0, volume=1000.0)
    cmf = chaikin_money_flow(close, high, low, vol, period=20)
    assert cmf.iloc[-1] == pytest.approx(0.0)


def test_cmf_handles_zero_range_bar_without_nan():
    # a no-range day (high == low) would divide by zero in the multiplier -
    # rolling sum must skip it, not poison the whole window with NaN
    n = 25
    high = pd.Series([110.0] * n)
    low = pd.Series([100.0] * n)
    close = pd.Series([105.0] * n)
    vol = pd.Series([1000.0] * n)
    high.iloc[-3] = low.iloc[-3] = close.iloc[-3] = 103.0  # zero-range bar
    cmf = chaikin_money_flow(close, high, low, vol, period=20)
    assert not np.isnan(cmf.iloc[-1])


# ---------------------------------------------------------------------------
# Fair Value Gap detection
# ---------------------------------------------------------------------------

def test_find_bullish_fvg_detects_the_gap():
    high = pd.Series([95, 95, 95, 100, 95, 108, 95, 95, 95, 95], dtype=float)
    low = pd.Series([90, 90, 90, 95, 90, 105, 90, 90, 90, 90], dtype=float)
    bottom, top = find_bullish_fvg(high, low, lookback=15)
    assert bottom == pytest.approx(100.0)
    assert top == pytest.approx(105.0)


def test_find_bullish_fvg_returns_none_when_no_gap():
    # every bar overlaps the one two bars back - no imbalance anywhere
    high = pd.Series([100.0] * 10)
    low = pd.Series([95.0] * 10)
    bottom, top = find_bullish_fvg(high, low, lookback=15)
    assert bottom is None and top is None


def test_find_bullish_fvg_picks_the_most_recent_of_two():
    # an older gap at i=3 (high[1]=90 < low[3]=95) and a newer one at i=8
    # (high[6]=100 < low[8]=106) - scanning backward must hit i=8 first
    high = pd.Series([95, 90, 95, 95, 95, 95, 100, 95, 95, 95], dtype=float)
    low = pd.Series([90, 88, 90, 95, 90, 90, 90, 90, 106, 90], dtype=float)
    bottom, top = find_bullish_fvg(high, low, lookback=15)
    assert (bottom, top) == pytest.approx((100.0, 106.0))


# ---------------------------------------------------------------------------
# Anchored VWAP (anchored to the most recent swing low)
# ---------------------------------------------------------------------------

def test_anchored_vwap_anchors_to_the_swing_low_and_averages_from_there():
    dates = pd.bdate_range("2026-01-01", periods=10)
    # bars 0-2: higher lows (not the anchor); bars 3-9: tied lowest low (100),
    # idxmin() picks the FIRST occurrence -> bar 3 is the anchor
    highs = [115, 115, 115] + [110] * 7
    lows = [105, 105, 105] + [100] * 7
    closes = [110, 110, 110] + [105] * 7   # constant typical price for bars 3-9
    vols = [500, 500, 500] + [1000] * 7
    df = pd.DataFrame({"High": highs, "Low": lows, "Close": closes, "Volume": vols},
                      index=dates)
    # typical price for bars 3-9 is (110+100+105)/3 = 105 exactly, constant,
    # so the volume-weighted average from the anchor to today is just 105
    assert anchored_vwap(df, lookback=60) == pytest.approx(105.0)


def test_anchored_vwap_never_negative_or_nan_on_flat_volume():
    dates = pd.bdate_range("2026-01-01", periods=15)
    df = pd.DataFrame({
        "High": [101.0] * 15, "Low": [99.0] * 15, "Close": [100.0] * 15,
        "Volume": [0.0] * 15,   # degenerate: zero volume throughout
    }, index=dates)
    result = anchored_vwap(df, lookback=60)
    assert result == pytest.approx(100.0)  # falls back to last close, not NaN/crash


# ---------------------------------------------------------------------------
# Volume Profile Point-of-Control proxy
# ---------------------------------------------------------------------------

def test_volume_poc_finds_the_dominant_volume_price_zone():
    dates = pd.bdate_range("2026-01-01", periods=10)
    typical_targets = [100 + i for i in range(10)]  # 100..109, one bar each
    highs = [t + 0.5 for t in typical_targets]
    lows = [t - 0.5 for t in typical_targets]
    closes = list(typical_targets)
    vols = [100.0] * 10
    vols[7] = 100_000.0   # bar 7 (typical price 107) massively dominates volume
    df = pd.DataFrame({"High": highs, "Low": lows, "Close": closes, "Volume": vols},
                      index=dates)
    poc = volume_poc(df, lookback=60, bins=20)
    assert poc == pytest.approx(107.0, abs=1.0)


def test_volume_poc_handles_flat_price_range():
    dates = pd.bdate_range("2026-01-01", periods=10)
    df = pd.DataFrame({
        "High": [100.5] * 10, "Low": [99.5] * 10, "Close": [100.0] * 10,
        "Volume": [1000.0] * 10,
    }, index=dates)
    poc = volume_poc(df, lookback=60, bins=20)
    assert poc == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Full compute_snapshot integration — no crash, values in sane ranges
# ---------------------------------------------------------------------------

def test_compute_snapshot_populates_all_new_fields_sanely():
    rng = np.random.default_rng(42)
    n = 300
    dates = pd.bdate_range("2025-01-01", periods=n)
    close = 100 + np.cumsum(rng.normal(0.1, 1.5, n))
    high = close + np.abs(rng.normal(1, 0.5, n))
    low = close - np.abs(rng.normal(1, 0.5, n))
    vol = rng.integers(100_000, 500_000, n).astype(float)
    df = pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close,
                       "Volume": vol}, index=dates)

    snap = compute_snapshot("TEST.NS", df)

    assert -1.0 <= snap.cmf <= 1.0
    assert -1.0 <= snap.cmf_prev <= 1.0
    assert snap.swing_low_10d_prior <= snap.high_252d
    assert snap.anchored_vwap > 0
    assert snap.volume_poc > 0
    assert snap.high_252d >= snap.close or snap.high_252d >= snap.high
    assert snap.close_prev > 0
    if snap.fvg_bull_bottom is not None:
        assert snap.fvg_bull_bottom < snap.fvg_bull_top
