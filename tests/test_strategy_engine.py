"""Unit tests for the self-evolving strategy fleet (stockbot/strategy_engine.py).

The LLM proposal call is always stubbed here (monkeypatched to return None,
forcing the deterministic bounds-midpoint fallback) — these are pure-function
and DB-logic tests, not a live Claude CLI integration test.
"""
import json
from datetime import datetime, timedelta

import pytest

import config
from stockbot import db, strategy_engine


@pytest.fixture(autouse=True)
def no_live_llm(monkeypatch):
    """Every test in this file gets a stubbed (unavailable) LLM by default."""
    monkeypatch.setattr(strategy_engine, "_call_claude_cli", lambda *a, **k: None)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


def _seed_closed_trade(conn, strategy: str, realized_pnl: float, ticker: str | None = None):
    conn.execute(
        """INSERT INTO paper_positions
             (strategy, ticker, qty, entry_date, entry_ref_price, entry_fill_price,
              entry_charges, cost_basis, target_price, stop_price, status,
              exit_date, exit_fill_price, exit_charges, net_proceeds,
              realized_pnl, exit_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?, 'CLOSED', ?,?,?,?,?,?)""",
        (strategy, ticker or f"{strategy}-{realized_pnl}.NS", 10, "2026-01-01",
         100.0, 100.2, 10.0, 1012.0, 110.0, 95.0,
         "2026-01-05", 105.0, 10.0, 1012.0 + realized_pnl, realized_pnl, "test"),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# resolve_params — merges overrides onto channel defaults, clamped to bounds
# ---------------------------------------------------------------------------

def test_resolve_params_defaults_match_config():
    params = strategy_engine.resolve_params("TECHNICAL", None)
    assert params["rsi_entry_min"] == config.RSI_ENTRY_MIN
    assert params["rsi_entry_max"] == config.RSI_ENTRY_MAX
    assert params["min_reward_risk"] == config.MIN_REWARD_RISK
    assert params["_toggles"] == []


def test_resolve_params_clamps_out_of_bounds():
    lo, hi = config.STRATEGY_PARAM_BOUNDS["TECHNICAL"]["rsi_entry_min"]
    overrides = json.dumps({"rsi_entry_min": hi + 100})
    params = strategy_engine.resolve_params("TECHNICAL", overrides)
    assert params["rsi_entry_min"] == hi


def test_resolve_params_ignores_unknown_keys():
    overrides = json.dumps({"not_a_real_param": 42, "rsi_entry_min": 50.0})
    params = strategy_engine.resolve_params("TECHNICAL", overrides)
    assert "not_a_real_param" not in params
    assert params["rsi_entry_min"] == 50.0


def test_resolve_params_filters_toggles_to_known_library():
    overrides = json.dumps({"_toggles": ["require_volume_surge", "not_a_real_toggle"]})
    params = strategy_engine.resolve_params("TECHNICAL", overrides)
    assert params["_toggles"] == ["require_volume_surge"]


def test_resolve_params_malformed_json_falls_back_to_defaults():
    params = strategy_engine.resolve_params("PULLBACK", "{not valid json")
    assert params == strategy_engine.resolve_params("PULLBACK", None)


# ---------------------------------------------------------------------------
# propose_new_variant — deterministic fallback when the CLI is unavailable
# ---------------------------------------------------------------------------

def test_propose_new_variant_fallback_stays_within_bounds(conn):
    warnings = []
    proposal = strategy_engine.propose_new_variant(
        conn, "TECHNICAL", {}, {"nifty_regime": "UPTREND"}, warnings,
        mode="parameter", parent_variant_key="TECHNICAL_seed")
    assert proposal["origin"] == "fallback_parameter_variant"  # honest, not pretending to be LLM
    assert proposal["variant_key"] == "TECHNICAL_v2"  # seed is v1-equivalent (count=1)
    params = json.loads(proposal["params_json"])
    for key, (lo, hi) in config.STRATEGY_PARAM_BOUNDS["TECHNICAL"].items():
        assert lo <= params[key] <= hi


def test_next_variant_key_never_collides_after_retirement(conn):
    warnings = []
    first = strategy_engine.propose_new_variant(conn, "PULLBACK", {}, {}, warnings)
    db.insert_strategy(conn, first)
    db.retire_strategy(conn, first["variant_key"], "test retirement", "2026-07-06")
    second = strategy_engine.propose_new_variant(conn, "PULLBACK", {}, {}, warnings)
    assert second["variant_key"] != first["variant_key"]


# ---------------------------------------------------------------------------
# _capital_weights — bounded, sums to ~100, untested variants get a baseline
# ---------------------------------------------------------------------------

def test_capital_weights_sum_to_100_and_respect_bounds():
    strategies = [
        {"variant_key": "A"}, {"variant_key": "B"}, {"variant_key": "C"},
    ]
    ledger_stats = {
        "A": {"closed": 40, "profit_factor": 5.0, "win_rate": 70.0, "realized_pnl": 500.0},
        "B": {"closed": 40, "profit_factor": 0.2, "win_rate": 20.0, "realized_pnl": -300.0},
        # C has no closed trades - untested
    }
    weights = strategy_engine._capital_weights(strategies, ledger_stats)
    assert weights.keys() == {"A", "B", "C"}
    for w in weights.values():
        assert config.STRATEGY_MIN_CAPITAL_WEIGHT_PCT - 0.01 <= w
        assert w <= config.STRATEGY_MAX_CAPITAL_WEIGHT_PCT + 0.01
    # the strong performer should outweigh the weak one
    assert weights["A"] > weights["B"]
    # untested variant gets a real (non-zero) baseline allocation
    assert weights["C"] >= config.STRATEGY_MIN_CAPITAL_WEIGHT_PCT - 0.01


def test_capital_weights_empty_returns_empty():
    assert strategy_engine._capital_weights([], {}) == {}


def test_capital_weights_floor_relaxes_at_high_strategy_count():
    """Regression: with the flat 5% floor, 25 strategies would want 125% of
    the book (n * floor > 100), an incoherent guarantee. The floor must relax
    to 100/n so it's always honorable regardless of fleet size."""
    n = 25
    strategies = [{"variant_key": f"S{i}"} for i in range(n)]
    weights = strategy_engine._capital_weights(strategies, {})  # all untested -> equal scores
    assert len(weights) == n
    expected_floor = 100.0 / n
    for w in weights.values():
        assert w == pytest.approx(expected_floor, abs=0.1)
        assert w < config.STRATEGY_MIN_CAPITAL_WEIGHT_PCT  # confirms the flat floor relaxed


# ---------------------------------------------------------------------------
# evaluate_and_evolve — retirement, stalled backstop, graduate flagging
# ---------------------------------------------------------------------------

def test_does_not_retire_before_min_trade_threshold(conn):
    variant = db.get_active_strategies(conn, channel="TECHNICAL")[0]
    for _ in range(config.STRATEGY_MIN_TRADES_FOR_RETIREMENT - 1):
        _seed_closed_trade(conn, variant["variant_key"], -10.0)

    today = datetime.now().strftime("%Y-%m-%d")
    result = strategy_engine.evaluate_and_evolve(conn, today, "AM", {}, [])

    active_keys = {s["variant_key"] for s in result["active_by_channel"]["TECHNICAL"]}
    assert variant["variant_key"] in active_keys
    assert not any(e["type"] == "retired" for e in result["events"])


def test_retires_underperforming_variant_and_backfills(conn):
    variant = db.get_active_strategies(conn, channel="TECHNICAL")[0]
    key = variant["variant_key"]
    for _ in range(config.STRATEGY_MIN_TRADES_FOR_RETIREMENT):
        _seed_closed_trade(conn, key, -10.0)  # every trade a loser

    today = datetime.now().strftime("%Y-%m-%d")
    result = strategy_engine.evaluate_and_evolve(conn, today, "AM", {}, [])

    retired_events = [e for e in result["events"] if e["type"] == "retired"]
    created_events = [e for e in result["events"] if e["type"] == "created"]
    assert any(e["variant_key"] == key for e in retired_events)
    assert any(e["parent"] == key for e in created_events)

    row = conn.execute(
        "SELECT status FROM strategies WHERE variant_key=?", (key,)
    ).fetchone()
    assert row["status"] == "RETIRED"

    active_keys = {s["variant_key"] for s in result["active_by_channel"]["TECHNICAL"]}
    assert key not in active_keys
    assert len(active_keys) >= 1  # replacement is active


def test_stalled_variant_is_retired_even_under_trade_minimum(conn):
    variant = db.get_active_strategies(conn, channel="PULLBACK")[0]
    key = variant["variant_key"]
    old_date = (datetime.now() - timedelta(days=config.STRATEGY_STALLED_DAYS + 5)).strftime(
        "%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE strategies SET created_at=? WHERE variant_key=?", (old_date, key))
    conn.commit()

    today = datetime.now().strftime("%Y-%m-%d")
    result = strategy_engine.evaluate_and_evolve(conn, today, "AM", {}, [])

    retired_events = [e for e in result["events"] if e["type"] == "retired"]
    assert any(e["variant_key"] == key and "stalled" in e["reason"] for e in retired_events)


def test_flags_graduate_candidate(conn):
    variant = db.get_active_strategies(conn, channel="TECHNICAL")[0]
    key = variant["variant_key"]
    n = config.STRATEGY_GRADUATE_MIN_TRADES
    wins = round(n * config.STRATEGY_GRADUATE_WIN_RATE / 100) + 1
    for _ in range(wins):
        _seed_closed_trade(conn, key, 50.0)
    for _ in range(n - wins):
        _seed_closed_trade(conn, key, -5.0)

    today = datetime.now().strftime("%Y-%m-%d")
    result = strategy_engine.evaluate_and_evolve(conn, today, "AM", {}, [])

    row = conn.execute(
        "SELECT graduate_candidate FROM strategies WHERE variant_key=?", (key,)
    ).fetchone()
    assert row["graduate_candidate"] == 1
    assert any(e["type"] == "graduate_candidate" and e["variant_key"] == key
              for e in result["events"])


def test_wildcard_cadence_not_reset_by_fallback_origin(conn):
    """Regression: the fallback path used to stamp origin="seed" regardless of
    mode, which made _last_wildcard_date() blind to any fallback-created
    wildcard — every subsequent run (with no LLM available) would then spawn
    ANOTHER wildcard instead of waiting out STRATEGY_WILDCARD_INTERVAL_DAYS.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    first = strategy_engine.evaluate_and_evolve(conn, today, "AM", {}, [])
    first_wildcards = [e for e in first["events"] if e["type"] == "wildcard_created"]
    assert len(first_wildcards) == len(config.EVOLVING_CHANNELS)  # one per evolving channel

    second = strategy_engine.evaluate_and_evolve(conn, today, "PM", {}, [])
    second_wildcards = [e for e in second["events"] if e["type"] == "wildcard_created"]
    assert second_wildcards == []

    for channel in config.EVOLVING_CHANNELS:
        n = conn.execute(
            "SELECT COUNT(*) FROM strategies WHERE channel=?", (channel,)
        ).fetchone()[0]
        assert n == 2  # seed + exactly one wildcard, not more


def test_evaluate_and_evolve_upserts_market_context(conn):
    today = datetime.now().strftime("%Y-%m-%d")
    strategy_engine.evaluate_and_evolve(conn, today, "AM",
                                       {"X.NS": {"score": 0.4}, "Y.NS": {"score": -0.2}}, [])
    row = conn.execute(
        "SELECT * FROM strategy_daily_context WHERE date=? AND run_slot='AM'", (today,)
    ).fetchone()
    assert row is not None
    assert row["avg_market_sentiment"] == pytest.approx(0.1)
