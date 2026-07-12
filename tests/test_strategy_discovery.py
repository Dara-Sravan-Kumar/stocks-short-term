"""Tests for Phase 2 — the backtest gate, spec registration, and the DISCOVERED
channel wiring into the live scan.

Gate-logic tests monkeypatch run_backtest so the pass/fail DECISION is exercised
deterministically (independent of a full historical simulation)."""
import json

import pandas as pd
import pytest

import config
from stockbot import db, signals, strategy_engine, backtest
from stockbot import strategy_discovery as sd
from stockbot.indicators import Snapshot
from stockbot.strategy_spec import StrategySpec


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def _dates(n=300):
    return pd.date_range("2025-01-01", periods=n, freq="D")


def _histories(n=300):
    idx = _dates(n)
    df = pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0,
                       "Close": 100.0, "Volume": 1e6}, index=idx)
    return {"RELIANCE.NS": df}


def _report(trades, win_rate, pf, net):
    return {"channel": "DISCOVERED", "params": {}, "sessions": 100,
            "trades": trades, "win_rate_pct": win_rate, "net_inr": net,
            "avg_pnl_pct": 1.0, "profit_factor": pf, "avg_hold_bars": 5.0,
            "max_drawdown_inr": 0, "exit_breakdown": {}, "open_at_end": 0,
            "meets_graduation_gate": False, "trades_detail": []}


def _snap(ticker="RELIANCE.NS", **over):
    base = dict(
        ticker=ticker, date="2026-07-12", close=100.0, high=101.0, low=98.0,
        rsi=58.0, macd=1.0, macd_signal=0.5, macd_hist=0.5, macd_hist_prev=0.2,
        macd_bullish_cross_recent=True, macd_bearish_cross_today=False,
        sma20=95.0, sma50=90.0, closes_below_sma20=0, mom_5d=1.0, mom_20d=8.0,
        vol_ratio=1.8, avg_turnover_20d=5e8, swing_low_10d=97.0,
        swing_low_10d_prior=96.0, close_prev=99.0, cmf=0.15, cmf_prev=0.05,
        fvg_bull_bottom=None, fvg_bull_top=None, anchored_vwap=96.0,
        volume_poc=97.0, high_252d=120.0, pivot=99.0, r1=106.0, r2=110.0,
        s1=97.0, s2=95.0, r3=112.0, weekly_r1=107.0, weekly_r2=111.0, weekly_r3=115.0,
    )
    base.update(over)
    return Snapshot(**base)


# --------------------------------------------------------------------------- #
# Backtest gate decision logic
# --------------------------------------------------------------------------- #
def test_gate_passes_on_good_oos(monkeypatch):
    monkeypatch.setattr(backtest, "run_backtest",
                        lambda h, v, eval_days, **k: {"d1": _report(30, 60.0, 1.8, 50000)})
    res = sd.backtest_gate(StrategySpec(name="d1", entry_expr="close > sma20 and rsi > 55"),
                           _histories(), [])
    assert res["passed"], res["reasons"]


def test_gate_fails_on_too_few_oos_trades(monkeypatch):
    monkeypatch.setattr(backtest, "run_backtest",
                        lambda h, v, eval_days, **k: {"d2": _report(3, 70.0, 2.0, 9000)})
    res = sd.backtest_gate(StrategySpec(name="d2", entry_expr="close > sma20"), _histories(), [])
    assert not res["passed"] and any("few trades" in r for r in res["reasons"])


def test_gate_fails_on_low_profit_factor(monkeypatch):
    monkeypatch.setattr(backtest, "run_backtest",
                        lambda h, v, eval_days, **k: {"d3": _report(40, 60.0, 1.0, 5000)})
    res = sd.backtest_gate(StrategySpec(name="d3", entry_expr="close > sma20"), _histories(), [])
    assert not res["passed"] and any("profit factor" in r for r in res["reasons"])


def test_gate_fails_when_not_enough_history(monkeypatch):
    # only a handful of dates -> cannot form both windows
    res = sd.backtest_gate(StrategySpec(name="d4", entry_expr="close > sma20"),
                           _histories(n=1), [])
    assert not res["passed"] and "history" in res["reasons"][0]


# --------------------------------------------------------------------------- #
# register_spec
# --------------------------------------------------------------------------- #
def test_register_rejects_invalid_spec(conn):
    res = sd.register_spec(conn, StrategySpec(name="bad", entry_expr="__import__('os')"),
                           {}, [])
    assert not res["registered"] and res["stage"] == "validate"
    assert db.get_active_strategies(conn, channel="DISCOVERED") == []


def test_register_rejects_non_swing(conn):
    res = sd.register_spec(conn, StrategySpec(name="scalp", entry_expr="close > sma20",
                                              horizon="INTRADAY"), {}, [])
    assert not res["registered"] and res["stage"] == "validate"


def test_register_inserts_when_gate_passes(conn, monkeypatch):
    monkeypatch.setattr(sd, "backtest_gate",
                        lambda s, h, w: {"passed": True, "reasons": [],
                                         "in_sample": None, "out_of_sample": None})
    spec = StrategySpec(name="momo_x", entry_expr="close > sma20 and rsi > 55",
                        source="web_discovered", rationale="trend momentum")
    res = sd.register_spec(conn, spec, {}, [])
    assert res["registered"]
    rows = db.get_active_strategies(conn, channel="DISCOVERED")
    assert len(rows) == 1 and rows[0]["variant_key"] == "momo_x"
    assert rows[0]["origin"] == "web_discovered"
    assert json.loads(rows[0]["params_json"])["entry_expr"] == "close > sma20 and rsi > 55"


def test_register_blocks_when_gate_fails(conn, monkeypatch):
    monkeypatch.setattr(sd, "backtest_gate",
                        lambda s, h, w: {"passed": False, "reasons": ["OOS too few trades"]})
    res = sd.register_spec(conn, StrategySpec(name="weak", entry_expr="close > sma20"), {}, [])
    assert not res["registered"] and res["stage"] == "backtest_gate"
    assert db.get_active_strategies(conn, channel="DISCOVERED") == []


def test_register_rejects_duplicate(conn, monkeypatch):
    monkeypatch.setattr(sd, "backtest_gate", lambda s, h, w: {"passed": True, "reasons": []})
    spec = StrategySpec(name="dup", entry_expr="close > sma20")
    assert sd.register_spec(conn, spec, {}, [])["registered"]
    res2 = sd.register_spec(conn, spec, {}, [])
    assert not res2["registered"] and res2["stage"] == "duplicate"


def test_register_respects_fleet_cap(conn, monkeypatch):
    monkeypatch.setattr(sd, "backtest_gate", lambda s, h, w: {"passed": True, "reasons": []})
    monkeypatch.setattr(config, "DISCOVERED_FLEET_MAX", 2)
    for i in range(2):
        assert sd.register_spec(conn, StrategySpec(name=f"s{i}", entry_expr="close > sma20"),
                                {}, [])["registered"]
    res = sd.register_spec(conn, StrategySpec(name="s2", entry_expr="close > sma20"), {}, [])
    assert not res["registered"] and res["stage"] == "fleet_cap"


# --------------------------------------------------------------------------- #
# resolve_params + live-scan wiring
# --------------------------------------------------------------------------- #
def test_resolve_params_preserves_entry_expr():
    p = strategy_engine.resolve_params(
        "DISCOVERED", json.dumps({"entry_expr": "close > sma20", "min_reward_risk": 1.7}))
    assert p["entry_expr"] == "close > sma20" and p["min_reward_risk"] == 1.7


def test_discovered_scan_produces_pick(conn, monkeypatch):
    # a registered spec whose entry matches a liquid, in-uptrend snapshot should
    # surface as a DISCOVERED pick tagged with the variant_key
    monkeypatch.setattr(signals.fundamentals, "check_fundamentals",
                        lambda *a, **k: True)
    db.insert_strategy(conn, {
        "channel": "DISCOVERED", "variant_key": "d_scan",
        "params_json": json.dumps({"entry_expr": "close > sma20 and rsi > 50",
                                   "min_reward_risk": 1.5}),
        "retirable": 1, "origin": "manual",
        "parent_variant_key": None, "generation_rationale": ""})
    snap = _snap("RELIANCE.NS")
    picks = signals.scan_discovered_picks(
        conn, {"RELIANCE.NS": snap}, {"RELIANCE.NS": {"score": 0.5}}, "2026-07-12", [])
    assert any(p["channel"] == "d_scan" and p["ticker"] == "RELIANCE.NS" for p in picks)


def test_discovered_scan_skips_when_expr_false(conn, monkeypatch):
    monkeypatch.setattr(signals.fundamentals, "check_fundamentals", lambda *a, **k: True)
    db.insert_strategy(conn, {
        "channel": "DISCOVERED", "variant_key": "d_none",
        "params_json": json.dumps({"entry_expr": "rsi > 90", "min_reward_risk": 1.5}),
        "retirable": 1, "origin": "manual",
        "parent_variant_key": None, "generation_rationale": ""})
    snap = _snap("RELIANCE.NS", rsi=58.0)  # rsi>90 is false
    picks = signals.scan_discovered_picks(
        conn, {"RELIANCE.NS": snap}, {"RELIANCE.NS": {"score": 0.5}}, "2026-07-12", [])
    assert not any(p["channel"] == "d_none" for p in picks)


# --------------------------------------------------------------------------- #
# Phase 3 — the discoverer's routing
# --------------------------------------------------------------------------- #
def test_discover_registers_valid_and_rejects_malicious(conn, monkeypatch):
    canned = {"strategies": [
        {"name": "MA Pullback", "entry_expr": "close > sma20 and rsi > 45 and rsi < 60",
         "min_reward_risk": 1.8, "rationale": "moving-average pullback"},
        {"name": "evil", "entry_expr": "__import__('os').system('x')",
         "min_reward_risk": 2.0, "rationale": "nope"},
    ]}
    monkeypatch.setattr(strategy_engine, "_call_claude_cli", lambda p, w, **k: canned)
    monkeypatch.setattr(sd, "backtest_gate",
                        lambda s, h, w: {"passed": True, "reasons": []})
    report = sd.discover_and_register(conn, {}, [])
    assert report["proposed"] == 2
    assert len(report["registered"]) == 1
    assert report["registered"][0]["name"].startswith("disc_ma_pullback")
    assert report["rejected"][0]["stage"] == "validate"
    # the good one is now a live DISCOVERED variant
    assert len(db.get_active_strategies(conn, channel="DISCOVERED")) == 1


def test_discover_noop_without_llm(conn):
    assert sd.discover_and_register(conn, {}, [], use_llm=False)["proposed"] == 0


def test_discover_handles_no_proposals(conn, monkeypatch):
    monkeypatch.setattr(strategy_engine, "_call_claude_cli", lambda p, w, **k: None)
    report = sd.discover_and_register(conn, {}, [])
    assert report["proposed"] == 0 and report["registered"] == []


def test_discover_backtest_gate_can_block(conn, monkeypatch):
    canned = {"strategies": [{"name": "weak", "entry_expr": "close > sma20",
                              "min_reward_risk": 1.5, "rationale": "x"}]}
    monkeypatch.setattr(strategy_engine, "_call_claude_cli", lambda p, w, **k: canned)
    monkeypatch.setattr(sd, "backtest_gate",
                        lambda s, h, w: {"passed": False, "reasons": ["OOS profit factor 0.9"]})
    report = sd.discover_and_register(conn, {}, [])
    assert report["registered"] == [] and report["rejected"][0]["stage"] == "backtest_gate"
    assert db.get_active_strategies(conn, channel="DISCOVERED") == []
