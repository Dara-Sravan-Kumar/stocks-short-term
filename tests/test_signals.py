"""Unit tests for the PULLBACK entry gate using synthetic Snapshots."""
import config
from stockbot.indicators import Snapshot
from stockbot.signals import _passes_pullback


def make_snapshot(**overrides) -> Snapshot:
    """A liquid, textbook pullback-to-SMA20 setup; override fields to break it."""
    base = dict(
        ticker="TESTX.NS", date="2026-07-06",
        close=100.0, high=102.0, low=99.6,
        rsi=45.0,
        macd=0.5, macd_signal=0.6, macd_hist=-0.1, macd_hist_prev=-0.3,
        macd_bullish_cross_recent=False, macd_bearish_cross_today=False,
        sma20=100.0, sma50=95.0, closes_below_sma20=0,
        mom_5d=-2.0, mom_20d=6.0,
        vol_ratio=1.0, avg_turnover_20d=500e6,
        swing_low_10d=97.0,
        pivot=100.5, r1=103.0, r2=105.0, s1=98.0, s2=96.0, r3=107.0,
        weekly_r1=104.0, weekly_r2=106.0, weekly_r3=108.0,
    )
    base.update(overrides)
    return Snapshot(**base)


def test_textbook_pullback_passes():
    assert _passes_pullback(make_snapshot())


def test_rejects_illiquid():
    assert not _passes_pullback(make_snapshot(avg_turnover_20d=1e6))


def test_rejects_downtrend():
    assert not _passes_pullback(make_snapshot(sma20=94.0, sma50=95.0))
    assert not _passes_pullback(make_snapshot(close=94.0))  # below SMA50


def test_rejects_weak_20d_momentum():
    assert not _passes_pullback(
        make_snapshot(mom_20d=config.PULLBACK_MIN_MOM20 - 0.1))


def test_rejects_rally_not_dip():
    assert not _passes_pullback(make_snapshot(mom_5d=1.5))


def test_rejects_when_low_never_tags_sma20():
    # low stays >1% above SMA20 — no touch, no pullback entry
    assert not _passes_pullback(make_snapshot(low=101.5))


def test_rejects_sma20_breakdown():
    assert not _passes_pullback(make_snapshot(close=98.5))          # >1% below
    assert not _passes_pullback(make_snapshot(closes_below_sma20=2))


def test_rejects_rsi_out_of_band():
    assert not _passes_pullback(make_snapshot(rsi=config.PULLBACK_RSI_MIN - 1))
    assert not _passes_pullback(make_snapshot(rsi=config.PULLBACK_RSI_MAX + 1))


def test_rejects_macd_still_deteriorating():
    assert not _passes_pullback(
        make_snapshot(macd_hist=-0.4, macd_hist_prev=-0.3))


def test_pullback_band_edges_pass():
    assert _passes_pullback(make_snapshot(rsi=config.PULLBACK_RSI_MIN))
    assert _passes_pullback(make_snapshot(rsi=config.PULLBACK_RSI_MAX))
    assert _passes_pullback(make_snapshot(mom_5d=0.0))
    assert _passes_pullback(make_snapshot(closes_below_sma20=1, close=99.5))
