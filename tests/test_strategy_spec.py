"""Tests for the strategy-as-data spec + safe expression evaluator.

The safety tests matter most: an entry expression is untrusted (it may come
from an LLM translating a web page), so it must never execute arbitrary code
and must never crash a scan.
"""
import pytest

from stockbot.indicators import Snapshot
from stockbot import strategy_spec as ss


def _snap(**over) -> Snapshot:
    base = dict(
        ticker="TEST.NS", date="2026-07-12", close=100.0, high=101.0, low=98.0,
        rsi=60.0, macd=1.0, macd_signal=0.5, macd_hist=0.5, macd_hist_prev=0.2,
        macd_bullish_cross_recent=True, macd_bearish_cross_today=False,
        sma20=95.0, sma50=90.0, closes_below_sma20=0, mom_5d=1.0, mom_20d=8.0,
        vol_ratio=1.8, avg_turnover_20d=5e8, swing_low_10d=94.0,
        swing_low_10d_prior=93.0, close_prev=99.0, cmf=0.15, cmf_prev=0.05,
        fvg_bull_bottom=None, fvg_bull_top=None, anchored_vwap=96.0,
        volume_poc=97.0, high_252d=105.0, pivot=99.0, r1=102.0, r2=104.0,
        s1=97.0, s2=95.0, r3=106.0, weekly_r1=103.0, weekly_r2=107.0, weekly_r3=110.0,
    )
    base.update(over)
    return Snapshot(**base)


# --------------------------------------------------------------------------- #
# Correctness
# --------------------------------------------------------------------------- #
def test_simple_entry_true_and_false():
    s = _snap(close=100.0, sma20=95.0, rsi=60.0)
    assert ss.evaluate_entry("close > sma20 and rsi > 50", s) is True
    assert ss.evaluate_entry("close < sma20", s) is False


def test_ranges_and_safe_functions():
    s = _snap(rsi=60.0, vol_ratio=1.8, mom_20d=8.0)
    assert ss.evaluate_entry("rsi > 50 and rsi < 70 and vol_ratio >= 1.5", s) is True
    # min/max/abs are the only callables allowed
    assert ss.evaluate_entry("max(mom_20d, 5) > 7 and abs(rsi - 60) < 1", s) is True


def test_chained_comparison():
    s = _snap(rsi=55.0)
    assert ss.evaluate_entry("50 < rsi < 60", s) is True
    assert ss.evaluate_entry("50 < rsi < 52", s) is False


def test_realistic_published_strategy_maps_to_vocabulary():
    # "buy strength above the 20-SMA with momentum, order flow and volume" — a
    # typical swing setup expressed purely over existing indicators
    expr = "close > sma20 > sma50 and rsi > 55 and cmf > 0.1 and vol_ratio > 1.3"
    assert ss.evaluate_entry(expr, _snap()) is True


# --------------------------------------------------------------------------- #
# Safety — these must be REJECTED at validation and must NOT execute
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("expr", [
    "__import__('os').system('echo hacked')",
    "close.__class__.__mro__",
    "().__class__.__bases__",
    "open('/etc/passwd').read()",
    "[x for x in range(10)]",
    "lambda: 1",
    "close if True else low",
    "data[0]",
    "exec('x=1')",
    "close > unknown_field",
    "print(close)",
])
def test_malicious_or_unknown_expr_rejected(expr):
    ok, reason = ss.validate_expr(expr)
    assert ok is False, f"should have rejected: {expr} ({reason})"
    # and even if it somehow reached the evaluator, it must not fire / not raise
    assert ss.evaluate_entry(expr, _snap()) is False


def test_evaluator_swallows_runtime_errors():
    # comparing against a None field (no unfilled FVG) must yield False, not crash
    s = _snap(fvg_bull_bottom=None)
    assert ss.evaluate_entry("close > fvg_bull_bottom", s) is False


# --------------------------------------------------------------------------- #
# Spec model + horizon enforcement
# --------------------------------------------------------------------------- #
def test_spec_roundtrip_and_valid():
    spec = ss.StrategySpec(
        name="momo_v1", entry_expr="close > sma20 and rsi > 55",
        source="web_discovered", rationale="trend + momentum")
    ok, reason = ss.validate_spec(spec)
    assert ok, reason
    again = ss.StrategySpec.from_json(spec.to_json())
    assert again.entry_expr == spec.entry_expr and again.horizon == "SWING"


def test_spec_rejects_non_swing_horizon():
    spec = ss.StrategySpec(name="scalp", entry_expr="close > sma20", horizon="INTRADAY")
    ok, reason = ss.validate_spec(spec)
    assert not ok and "swing" in reason.lower()


def test_spec_rejects_bad_expr():
    spec = ss.StrategySpec(name="bad", entry_expr="close > __import__('os')")
    ok, _ = ss.validate_spec(spec)
    assert not ok


def test_spec_gate_and_target_stop_integrate():
    s = _snap()
    spec = ss.StrategySpec(name="t", entry_expr="close > sma20 and rsi > 50")
    assert ss.spec_matches(spec, s) is True
    target, stop = ss.spec_target_stop(spec, s)
    assert stop < s.close and (target is None or target > s.close)
