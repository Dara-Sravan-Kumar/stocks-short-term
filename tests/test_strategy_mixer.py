"""Tests for Phase 4 — the genetic mixer.

Key safety property: mutation must nudge numeric thresholds only, never corrupt
field names that contain digits (mom_20d, high_252d, sma20). Offspring must
always be valid expressions over the known vocabulary."""
import json
import random

from stockbot import db
from stockbot import strategy_discovery as sd
from stockbot import strategy_mixer as sm
from stockbot.strategy_spec import validate_expr

import pytest


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def test_clauses_splits_conjunction():
    assert sm._clauses("close > sma20 and rsi > 55 and cmf > 0.1") == \
        ["close > sma20", "rsi > 55", "cmf > 0.1"]
    assert sm._clauses("close > sma20") == ["close > sma20"]


def test_crossover_yields_valid_expression():
    child = sm._crossover("close > sma20 and rsi > 55",
                          "cmf > 0.1 and vol_ratio > 1.3", random.Random(1))
    ok, reason = validate_expr(child)
    assert ok, (child, reason)


def test_mutate_nudges_constant_without_corrupting_fields():
    original = "rsi > 55.0 and mom_20d > 3.0 and high_252d > close"
    out = sm._mutate(original, random.Random(3))
    ok, _ = validate_expr(out)
    assert ok
    assert "mom_20d" in out and "high_252d" in out   # digit-bearing names intact
    assert out != original                            # a literal actually changed


def test_mix_registers_valid_offspring(conn, monkeypatch):
    monkeypatch.setattr(sd, "backtest_gate", lambda s, h, w: {"passed": True, "reasons": []})
    report = sm.mix_and_register(conn, {}, [], max_offspring=4, rng=random.Random(7))
    assert report["proposed"] >= 1 and len(report["registered"]) >= 1
    rows = db.get_active_strategies(conn, channel="DISCOVERED")
    assert rows and all(r["origin"] == "mixer" for r in rows)
    for r in rows:
        expr = json.loads(r["params_json"])["entry_expr"]
        assert validate_expr(expr)[0], expr


def test_mix_gate_can_block(conn, monkeypatch):
    monkeypatch.setattr(sd, "backtest_gate",
                        lambda s, h, w: {"passed": False, "reasons": ["weak OOS"]})
    report = sm.mix_and_register(conn, {}, [], max_offspring=3, rng=random.Random(1))
    assert report["registered"] == []
    assert db.get_active_strategies(conn, channel="DISCOVERED") == []


def test_mix_is_deterministic_with_seed(tmp_path, monkeypatch):
    monkeypatch.setattr(sd, "backtest_gate", lambda s, h, w: {"passed": True, "reasons": []})
    c1 = db.connect(tmp_path / "a.db")
    r1 = sm.mix_and_register(c1, {}, [], max_offspring=3, rng=random.Random(99))
    c1.close()
    c2 = db.connect(tmp_path / "b.db")
    r2 = sm.mix_and_register(c2, {}, [], max_offspring=3, rng=random.Random(99))
    c2.close()
    assert sorted(x["entry_expr"] for x in r1["registered"]) == \
        sorted(x["entry_expr"] for x in r2["registered"])


def test_gene_pool_includes_discovered_specs(conn):
    db.insert_strategy(conn, {
        "channel": "DISCOVERED", "variant_key": "disc_seed_gene",
        "params_json": json.dumps({"entry_expr": "close > anchored_vwap and cmf > 0.2",
                                   "min_reward_risk": 1.6}),
        "retirable": 1, "origin": "web_discovered",
        "parent_variant_key": None, "generation_rationale": ""})
    pool = sm._gene_pool(conn)
    exprs = [g["expr"] for g in pool]
    assert "close > anchored_vwap and cmf > 0.2" in exprs   # ledger spec
    assert len(pool) == len(sm.SEED_GENES) + 1              # + seed genes
