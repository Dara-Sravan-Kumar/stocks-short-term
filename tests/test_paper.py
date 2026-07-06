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
    qty, note = paper.size_position(equity=10_000, cash=10_000,
                                    entry_ref=500.0, stop=482.0)
    # risk budget 150 / 18 per share = 8, but 40% position cap => 7
    assert qty == 7
    assert "position cap" in note


def test_size_position_risk_binding():
    # wide book, tight stop: risk is the binding constraint
    qty, note = paper.size_position(equity=10_000, cash=10_000,
                                    entry_ref=100.0, stop=98.0)
    # risk budget 150 / 2 = 75; cap 4000/100.05 = 39; cash ~ 9930/100.2 = 99
    assert qty == 39 or "cap" in note  # cap binds at 39
    assert qty == 39


def test_size_position_insufficient_cash():
    qty, note = paper.size_position(equity=10_000, cash=100.0,
                                    entry_ref=500.0, stop=482.0)
    assert qty == 0
    assert "cash" in note


def test_size_position_rejects_bad_stop():
    qty, note = paper.size_position(10_000, 10_000, entry_ref=500.0, stop=500.0)
    assert qty == 0


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
    assert buy["qty"] == 7

    book = db.get_paper_book(conn)
    assert book["cash"] == pytest.approx(10_000 - buy["invested"], abs=0.01)

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
        10_000 - buy["invested"] + sell["net_proceeds"], abs=0.02)

    # ledger has exactly one BUY and one SELL whose net_amounts sum to cash delta
    trades = conn.execute("SELECT * FROM paper_trades ORDER BY id").fetchall()
    assert [t["side"] for t in trades] == ["BUY", "SELL"]
    net = sum(t["net_amount"] for t in trades)
    assert book["cash"] == pytest.approx(10_000 + net, abs=0.02)

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
    # exhaust the book with an affordable large position first
    paper.open_positions_for_picks(conn, [_pick("AAA.NS")], {}, "2026-07-06",
                                   "AM", warnings)
    paper.open_positions_for_picks(conn, [_pick("BBB.NS")], {}, "2026-07-06",
                                   "AM", warnings)
    paper.open_positions_for_picks(conn, [_pick("CCC.NS")], {}, "2026-07-06",
                                   "AM", warnings)
    # after three ~3.5k positions, a fourth must be skipped for cash
    actions = paper.open_positions_for_picks(conn, [_pick("DDD.NS")], {},
                                             "2026-07-06", "AM", warnings)
    assert actions[0]["action"] == "SKIP"
    assert "cash" in actions[0]["note"]
    book = db.get_paper_book(conn)
    assert book["cash"] > 0  # never negative


def test_duplicate_open_position_rejected(conn):
    warnings = []
    paper.open_positions_for_picks(conn, [_pick("AAA.NS")], {}, "2026-07-06",
                                   "AM", warnings)
    cash_before = db.get_paper_book(conn)["cash"]
    paper.open_positions_for_picks(conn, [_pick("AAA.NS")], {}, "2026-07-06",
                                   "AM", warnings)
    assert db.get_paper_book(conn)["cash"] == cash_before
    assert any("already exists" in w for w in warnings)


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
