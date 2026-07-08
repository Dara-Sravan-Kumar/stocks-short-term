"""Unit tests for the PULLBACK entry gate using synthetic Snapshots."""
import config
from stockbot import strategy_engine
from stockbot.indicators import Snapshot
from stockbot.signals import _passes_pullback, _passes_technicals

PARAMS = strategy_engine.resolve_params("PULLBACK", None)


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
        swing_low_10d=97.0, swing_low_10d_prior=97.0, close_prev=99.0,
        cmf=0.1, cmf_prev=0.05, fvg_bull_bottom=None, fvg_bull_top=None,
        anchored_vwap=98.0, volume_poc=98.0, high_252d=110.0,
        pivot=100.5, r1=103.0, r2=105.0, s1=98.0, s2=96.0, r3=107.0,
        weekly_r1=104.0, weekly_r2=106.0, weekly_r3=108.0,
    )
    base.update(overrides)
    return Snapshot(**base)


def test_textbook_pullback_passes():
    assert _passes_pullback(make_snapshot(), PARAMS)


def test_rejects_illiquid():
    assert not _passes_pullback(make_snapshot(avg_turnover_20d=1e6), PARAMS)


def test_rejects_downtrend():
    assert not _passes_pullback(make_snapshot(sma20=94.0, sma50=95.0), PARAMS)
    assert not _passes_pullback(make_snapshot(close=94.0), PARAMS)  # below SMA50


def test_rejects_weak_20d_momentum():
    assert not _passes_pullback(
        make_snapshot(mom_20d=config.PULLBACK_MIN_MOM20 - 0.1), PARAMS)


def test_rejects_rally_not_dip():
    assert not _passes_pullback(make_snapshot(mom_5d=1.5), PARAMS)


def test_rejects_when_low_never_tags_sma20():
    # low stays >1% above SMA20 — no touch, no pullback entry
    assert not _passes_pullback(make_snapshot(low=101.5), PARAMS)


def test_rejects_sma20_breakdown():
    assert not _passes_pullback(make_snapshot(close=98.5), PARAMS)          # >1% below
    assert not _passes_pullback(make_snapshot(closes_below_sma20=2), PARAMS)


def test_rejects_rsi_out_of_band():
    assert not _passes_pullback(make_snapshot(rsi=config.PULLBACK_RSI_MIN - 1), PARAMS)
    assert not _passes_pullback(make_snapshot(rsi=config.PULLBACK_RSI_MAX + 1), PARAMS)


def test_rejects_macd_still_deteriorating():
    assert not _passes_pullback(
        make_snapshot(macd_hist=-0.4, macd_hist_prev=-0.3), PARAMS)


def test_pullback_band_edges_pass():
    assert _passes_pullback(make_snapshot(rsi=config.PULLBACK_RSI_MIN), PARAMS)
    assert _passes_pullback(make_snapshot(rsi=config.PULLBACK_RSI_MAX), PARAMS)
    assert _passes_pullback(make_snapshot(mom_5d=0.0), PARAMS)
    assert _passes_pullback(make_snapshot(closes_below_sma20=1, close=99.5), PARAMS)


# ---------------------------------------------------------------------------
# Params-driven variants: an overridden params dict must change gate behavior,
# not just be accepted as a no-op — this is the whole point of the strategy
# fleet (stockbot/strategy_engine.py resolves each variant's params_json).
# ---------------------------------------------------------------------------

def test_pullback_variant_with_wider_rsi_accepts_what_default_rejects():
    snap = make_snapshot(rsi=config.PULLBACK_RSI_MAX + 3)
    assert not _passes_pullback(snap, PARAMS)
    wide_params = dict(PARAMS, pullback_rsi_max=config.PULLBACK_RSI_MAX + 5)
    assert _passes_pullback(snap, wide_params)


def test_pullback_variant_with_tighter_mom20_rejects_what_default_accepts():
    snap = make_snapshot(mom_20d=config.PULLBACK_MIN_MOM20 + 0.5)
    assert _passes_pullback(snap, PARAMS)
    strict_params = dict(PARAMS, pullback_min_mom20=config.PULLBACK_MIN_MOM20 + 1.0)
    assert not _passes_pullback(snap, strict_params)


def test_technical_variant_with_wider_rsi_accepts_what_default_rejects():
    tech_params = strategy_engine.resolve_params("TECHNICAL", None)
    snap = make_snapshot(
        close=101.0, sma20=100.0, sma50=95.0,
        rsi=config.RSI_ENTRY_MAX + 3, macd=0.6, macd_signal=0.4,
        macd_hist=0.1, macd_hist_prev=0.05, pivot=99.0,
    )
    assert not _passes_technicals(snap, tech_params)
    wide_params = dict(tech_params, rsi_entry_max=config.RSI_ENTRY_MAX + 5)
    assert _passes_technicals(snap, wide_params)


def test_toggle_require_volume_surge_gates_on_context():
    snap = make_snapshot(vol_ratio=1.0)  # below VOL_RATIO_BONUS_THRESHOLD
    params = dict(PARAMS, _toggles=["require_volume_surge"])
    assert not _passes_pullback(snap, params, context={})
    surging = make_snapshot(vol_ratio=config.VOL_RATIO_BONUS_THRESHOLD + 0.5)
    assert _passes_pullback(surging, params, context={})
