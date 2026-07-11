"""Unit tests for the paper trading engine (run: python -m pytest from repo root)."""
import math

import pytest

import config
from stockbot import db, paper


# ---------------------------------------------------------------------------
# Cost model — hand-computed worked example from the implementation plan:
# 7 shares filled at 500.25 (ref 500 + 0.05% slippage)
# ---------------------------------------------------------------------------

def test_buy_fill_applies_slippage():
    assert paper.buy_fill(500.0) == pytest.approx(500.25)
    assert paper.sell_fill(500.0) == pytest.approx(499.75)


def test_buy_costs_worked_example():
    costs = paper.buy_costs(7, 500.25)
    turnover = 7 * 500.25  # 3501.75
    assert costs.brokerage == 5.0
    assert costs.stt == pytest.approx(turnover * 0.001)          # 3.50
    assert costs.stamp == pytest.approx(turnover * 0.00015)      # 0.53
    assert costs.dp == 0.0
    assert costs.gst == pytest.approx(
        0.18 * (5.0 + turnover * (0.0000297 + 0.000001)))
    assert costs.total == pytest.approx(10.05, abs=0.02)
    # cost basis matches the plan's ₹3,511.80
    assert turnover + costs.total == pytest.approx(3511.80, abs=0.05)


def test_sell_costs_include_dp_no_stamp():
    costs = paper.sell_costs(7, 481.76)
    assert costs.stamp == 0.0
    assert costs.dp == config.PAPER_DP_CHARGE_SELL
    turnover = 7 * 481.76
    assert costs.stt == pytest.approx(turnover * 0.001)
    assert costs.gst == pytest.approx(
        0.18 * (5.0 + turnover * (0.0000297 + 0.000001) + 16.0))


# ---------------------------------------------------------------------------
# Risk-based sizing — plan's worked example: ₹10k book, ₹500 stock, stop 482
# ---------------------------------------------------------------------------

def test_size_position_worked_example():
    # Rs 5L book, Rs 500 stock, stop 482 (Rs 18/share risk):
    #   risk budget 1% x 500k = 5000 / 18 = 277 shares
    #   2% position cap = 10000 / 500.25 fill = 19 shares  <- binds
    qty, note = paper.size_position(equity=500_000, cash=500_000,
                                    entry_ref=500.0, stop=482.0)
    assert qty == 19
    assert "position cap" in note


def test_size_position_risk_binding():
    # A very wide stop makes risk the binding constraint even under the small
    # 2% position cap: entry 100, stop 40 => Rs 60/share risk.
    #   risk budget 1% x 500k = 5000 / 60 = 83 shares      <- binds
    #   2% position cap = 10000 / 100.05 fill = 99 shares
    qty, note = paper.size_position(equity=500_000, cash=500_000,
                                    entry_ref=100.0, stop=40.0)
    assert qty == 83
    assert "risk" in note


def test_size_position_insufficient_cash():
    qty, note = paper.size_position(equity=10_000, cash=100.0,
                                    entry_ref=500.0, stop=482.0)
    assert qty == 0
    assert "cash" in note


def test_size_position_rejects_bad_stop():
    qty, note = paper.size_position(10_000, 10_000, entry_ref=500.0, stop=500.0)
    assert qty == 0


# ---------------------------------------------------------------------------
# Per-strategy capital budget guard — "split money accordingly" so one
# strategy variant can't front-run the whole shared book (config.STRATEGY_*).
# ---------------------------------------------------------------------------

def test_size_position_respects_budget_cap():
    # Unconstrained, the 2% position cap binds at 19 (see worked example above).
    # A tighter strategy budget (Rs 6000, ~11 shares) becomes the new binding
    # constraint — chosen so the result still clears PAPER_MIN_POSITION_VALUE
    # (else it would be skipped as too small, not capped).
    qty, note = paper.size_position(equity=500_000, cash=500_000,
                                    entry_ref=500.0, stop=482.0, budget_cap=6000.0)
    assert qty == 11
    assert "strategy budget" in note


def test_size_position_budget_cap_exhausted_skips():
    qty, note = paper.size_position(equity=500_000, cash=500_000,
                                    entry_ref=500.0, stop=482.0, budget_cap=0.0)
    assert qty == 0
    assert "strategy budget exhausted" in note


# ---------------------------------------------------------------------------
# Ledger round trip on a scratch DB — cash, ledger, and P&L must reconcile
# ---------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    db.ensure_paper_book(c)
    yield c
    c.close()


def _pick(ticker="TESTX.NS", entry=500.0, stop=482.0, target=540.0,
          channel="TECHNICAL"):
    return {"ticker": ticker, "entry_price": entry, "stop_price": stop,
            "target_price": target, "channel": channel,
            "rationale": "unit test", "reward_risk": 2.2}


def test_open_close_round_trip_reconciles(conn):
    warnings = []
    buys = paper.open_positions_for_picks(conn, [_pick()], {}, "2026-07-06",
                                          "AM", warnings)
    assert len(buys) == 1 and buys[0]["action"] == "BUY"
    buy = buys[0]
    # 40%-of-equity position cap binds at this book size (see size_position tests
    # for the worked-example math) — assert the invariant rather than a magic number
    assert buy["invested"] <= config.PAPER_STARTING_CASH * config.PAPER_MAX_POSITION_PCT / 100

    book = db.get_paper_book(conn)
    assert book["cash"] == pytest.approx(
        config.PAPER_STARTING_CASH - buy["invested"], abs=0.01)

    sells = paper.close_positions_for_exits(
        conn, [{"ticker": "TESTX.NS", "status": "TARGET_HIT",
                "exit_price": 540.0, "exit_reason": "target reached"}],
        "2026-07-10", "PM", warnings)
    assert len(sells) == 1
    sell = sells[0]

    # realized P&L = net proceeds - cost basis, and cash reflects both legs
    assert sell["realized_pnl"] == pytest.approx(
        sell["net_proceeds"] - buy["invested"], abs=0.01)
    book = db.get_paper_book(conn)
    assert book["cash"] == pytest.approx(
        config.PAPER_STARTING_CASH - buy["invested"] + sell["net_proceeds"], abs=0.02)

    # ledger has exactly one BUY and one SELL whose net_amounts sum to cash delta
    trades = conn.execute("SELECT * FROM paper_trades ORDER BY id").fetchall()
    assert [t["side"] for t in trades] == ["BUY", "SELL"]
    net = sum(t["net_amount"] for t in trades)
    assert book["cash"] == pytest.approx(config.PAPER_STARTING_CASH + net, abs=0.02)

    # position closed with attribution intact
    pos = conn.execute("SELECT * FROM paper_positions").fetchone()
    assert pos["status"] == "CLOSED"
    assert pos["strategy"] == "TECHNICAL"
    assert pos["realized_pnl"] == pytest.approx(sell["realized_pnl"], abs=0.01)

    stats = db.get_paper_stats(conn)
    assert stats["TECHNICAL"]["closed"] == 1
    assert stats["TECHNICAL"]["wins"] == 1


def test_skip_when_cash_exhausted(conn):
    warnings = []
    # The Rs 5L book funds MANY small (~2%-of-equity) positions across distinct
    # tickers, then gracefully stops once it's drained — without ever going
    # negative. This is the "reasonable size, more trades" profile in action.
    opened, skipped = 0, None
    for i in range(200):
        actions = paper.open_positions_for_picks(
            conn, [_pick(f"T{i:03d}.NS")], {}, "2026-07-06", "AM", warnings)
        if actions[0]["action"] == "BUY":
            opened += 1
        else:
            skipped = actions[0]
            break
    assert opened > 10          # dozens of small positions, not a few big ones
    assert skipped is not None  # further picks are skipped once the book drains
    book = db.get_paper_book(conn)
    assert book["cash"] > 0     # never negative


def test_duplicate_open_position_rejected(conn):
    warnings = []
    paper.open_positions_for_picks(conn, [_pick("AAA.NS")], {}, "2026-07-06",
                                   "AM", warnings)
    cash_before = db.get_paper_book(conn)["cash"]
    paper.open_positions_for_picks(conn, [_pick("AAA.NS")], {}, "2026-07-06",
                                   "AM", warnings)
    assert db.get_paper_book(conn)["cash"] == cash_before
    assert any("already exists" in w for w in warnings)


def test_open_positions_for_picks_respects_capital_weights(conn):
    warnings = []
    # NEWS gets a tiny 5% weight of the shared book — far below what an
    # unconstrained position would want, so its size should be capped to fit.
    weights = {"NEWS": 5.0, "TECHNICAL": 95.0}
    actions = paper.open_positions_for_picks(
        conn, [_pick(channel="NEWS")], {}, "2026-07-06", "AM", warnings,
        capital_weights=weights)
    assert len(actions) == 1
    action = actions[0]
    budget = config.PAPER_STARTING_CASH * 0.05
    if action["action"] == "BUY":
        assert action["invested"] <= budget + 1.0  # small slack for fill rounding
    else:
        assert "budget" in action["note"]


def test_open_positions_for_picks_unweighted_strategy_is_unconstrained(conn):
    warnings = []
    actions = paper.open_positions_for_picks(
        conn, [_pick(channel="TECHNICAL")], {}, "2026-07-06", "AM", warnings,
        capital_weights={"NEWS": 50.0})  # TECHNICAL absent from the map - no cap
    assert actions[0]["action"] == "BUY"


def test_mark_to_market_logs_equity_curve(conn):
    warnings = []
    paper.open_positions_for_picks(conn, [_pick("AAA.NS")], {}, "2026-07-06",
                                   "AM", warnings)
    summary = paper.mark_to_market(conn, {}, "2026-07-06", "AM")
    # with no snapshot, positions are valued at entry fill: equity = cash + qty*fill
    assert summary["equity"] == pytest.approx(
        summary["cash"] + summary["positions_value"], abs=0.01)
    # unrealized equals -charges when valued at entry fill
    assert summary["unrealized_pnl"] == pytest.approx(
        -sum(p["cost_basis"] - p["qty"] * p["entry_fill_price"]
             for p in summary["open_positions"]), abs=0.01)
    row = conn.execute("SELECT * FROM paper_equity_log").fetchone()
    assert row["date"] == "2026-07-06" and row["run_slot"] == "AM"
    assert row["open_positions"] == 1
