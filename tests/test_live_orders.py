"""Live-order safety gates and Fyers quote parsing — all HTTP mocked."""
import pytest

import config
from stockbot import broker, db, fyers_data


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


def _no_network(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("network call attempted through a closed gate")
    monkeypatch.setattr(broker.requests, "post", boom)
    monkeypatch.setattr(broker.requests, "get", boom)


ACTIONS = [
    {"action": "SELL", "ticker": "TCS.NS", "qty": 5, "strategy": "TECHNICAL_seed"},
    {"action": "BUY", "ticker": "INFY.NS", "qty": 10, "strategy": "PULLBACK_seed"},
]


# ------------------------------------------------------------------- gates
def test_paper_mode_is_a_complete_noop(conn, monkeypatch):
    monkeypatch.setattr(config, "TRADING_MODE", "PAPER")
    _no_network(monkeypatch)
    result = broker.execute_live_orders(conn, ACTIONS, "2026-07-11", "AM", [])
    assert result == {"submitted": 0, "blocked": 0, "failed": 0}
    assert db.get_live_trades(conn) == []


def test_live_mode_blocked_without_place_order_flag(conn, monkeypatch):
    monkeypatch.setattr(config, "TRADING_MODE", "LIVE")
    monkeypatch.setattr(config, "PLACE_ORDER_ENABLED", False)
    _no_network(monkeypatch)
    warnings = []
    result = broker.execute_live_orders(conn, ACTIONS, "2026-07-11", "AM", warnings)
    assert result == {"submitted": 0, "blocked": 2, "failed": 0}
    rows = db.get_live_trades(conn)
    assert {r["status"] for r in rows} == {"BLOCKED"}
    assert {r["ticker"] for r in rows} == {"TCS.NS", "INFY.NS"}
    assert any("PLACE_ORDER_ENABLED" in w for w in warnings)


def test_live_mode_submits_when_both_gates_open(conn, monkeypatch):
    monkeypatch.setattr(config, "TRADING_MODE", "LIVE")
    monkeypatch.setattr(config, "PLACE_ORDER_ENABLED", True)
    placed = []

    def fake_place(ticker, side, qty, warnings):
        placed.append((ticker, side, qty))
        return {"s": "ok", "id": "24070100001", "message": "Order submitted"}

    monkeypatch.setattr(broker, "place_order_fyers", fake_place)
    result = broker.execute_live_orders(conn, ACTIONS, "2026-07-11", "AM", [])
    assert result == {"submitted": 2, "blocked": 0, "failed": 0}
    # exits (SELL) must reach the broker before entries (BUY)
    assert placed == [("TCS.NS", "SELL", 5), ("INFY.NS", "BUY", 10)]
    rows = db.get_live_trades(conn)
    assert all(r["status"] == "SUBMITTED" and r["order_id"] for r in rows)


def test_failed_order_is_recorded(conn, monkeypatch):
    monkeypatch.setattr(config, "TRADING_MODE", "LIVE")
    monkeypatch.setattr(config, "PLACE_ORDER_ENABLED", True)
    monkeypatch.setattr(broker, "place_order_fyers",
                        lambda ticker, side, qty, w: None)
    result = broker.execute_live_orders(conn, ACTIONS[:1], "2026-07-11", "AM", [])
    assert result == {"submitted": 0, "blocked": 0, "failed": 1}
    assert db.get_live_trades(conn)[0]["status"] == "FAILED"


# ------------------------------------------------------------------- quotes
class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_fetch_quotes_parses_ltps(monkeypatch):
    monkeypatch.setattr(fyers_data, "ensure_token",
                        lambda creds, w, force_refresh=False: "tok")
    monkeypatch.setattr(fyers_data, "resolve_symbols",
                        lambda tickers, w: {"TCS.NS": "NSE:TCS-EQ",
                                            "INFY.NS": "NSE:INFY-EQ"})
    monkeypatch.setattr(config, "fyers_settings",
                        lambda: {"app_id": "APP-100", "secret_id": "S", "pin": "",
                                 "redirect_uri": ""})
    payload = {"s": "ok", "d": [
        {"n": "NSE:TCS-EQ", "s": "ok",
         "v": {"lp": 3500.5, "chp": 1.2, "open_price": 3450, "high_price": 3510,
               "low_price": 3440, "prev_close_price": 3458, "volume": 12345}},
        {"n": "NSE:INFY-EQ", "s": "ok", "v": {"lp": 0}},  # no price -> skipped
    ]}
    monkeypatch.setattr(fyers_data.requests, "get",
                        lambda *a, **k: _Resp(payload))
    out = fyers_data.fetch_quotes(["TCS.NS", "INFY.NS"], [])
    assert out == {"TCS.NS": {"ltp": 3500.5, "change_pct": 1.2, "open": 3450,
                              "high": 3510, "low": 3440, "prev_close": 3458,
                              "volume": 12345}}


def test_fetch_quotes_survives_http_error(monkeypatch):
    monkeypatch.setattr(fyers_data, "ensure_token",
                        lambda creds, w, force_refresh=False: "tok")
    monkeypatch.setattr(fyers_data, "resolve_symbols",
                        lambda tickers, w: {"TCS.NS": "NSE:TCS-EQ"})
    monkeypatch.setattr(config, "fyers_settings",
                        lambda: {"app_id": "APP-100", "secret_id": "S", "pin": "",
                                 "redirect_uri": ""})
    monkeypatch.setattr(fyers_data.requests, "get",
                        lambda *a, **k: _Resp({"s": "error", "message": "boom"}, 500))
    warnings = []
    assert fyers_data.fetch_quotes(["TCS.NS"], warnings) == {}
    assert warnings and "quotes" in warnings[0].lower()
