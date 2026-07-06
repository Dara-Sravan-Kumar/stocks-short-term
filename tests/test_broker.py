"""Broker sync tests against a fake OpenAlgo HTTP server."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import config
from stockbot import broker, db

CANNED_HOLDINGS = {
    "status": "success",
    "data": {
        "holdings": [
            {"symbol": "RELIANCE", "exchange": "NSE",
             "quantity": 4, "average_price": 1290.5},
            {"symbol": "TATAMOTORS", "exchange": "BSE",   # non-NSE -> skipped
             "quantity": 2, "average_price": 700.0},
            {"symbol": "IDEA", "exchange": "NSE",         # zero qty -> skipped
             "quantity": 0, "average_price": 9.0},
        ],
    },
}


class _Handler(BaseHTTPRequestHandler):
    payload = CANNED_HOLDINGS
    analyzer_mode = "analyze"          # or "live"
    placed_orders: list[dict] = []

    def do_POST(self):
        # must drain the request body — replying with it unread makes Windows
        # abort the client connection (WinError 10053) intermittently
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/api/v1/analyzer":
            reply = {"status": "success",
                     "data": {"mode": self.analyzer_mode,
                              "analyze_mode": self.analyzer_mode == "analyze"}}
        elif self.path == "/api/v1/placeorder":
            _Handler.placed_orders.append(json.loads(raw))
            reply = {"status": "success", "orderid": "SB-TEST-1"}
        else:
            reply = self.payload
        body = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence test output
        pass


@pytest.fixture
def fake_openalgo(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("OPENALGO_HOST", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("OPENALGO_API_KEY", "test-key")
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


def test_map_symbol():
    assert broker.map_symbol("RELIANCE", "NSE") == "RELIANCE.NS"
    assert broker.map_symbol("tcs", "nse") == "TCS.NS"
    assert broker.map_symbol("XYZ", "BSE") is None
    assert broker.map_symbol("", "NSE") is None


def test_map_symbol_override(monkeypatch):
    monkeypatch.setattr(config, "BROKER_SYMBOL_OVERRIDES", {"MNM": "M&M.NS"})
    assert broker.map_symbol("MNM", "NSE") == "M&M.NS"


def test_not_configured_seeds_mock(conn, monkeypatch):
    monkeypatch.delenv("OPENALGO_HOST", raising=False)
    monkeypatch.delenv("OPENALGO_API_KEY", raising=False)
    warnings = []
    sync = broker.sync_holdings(conn, warnings)
    assert sync["source"] == "MOCK"
    assert sync["count"] == len(config.MOCK_HOLDINGS)
    row = conn.execute("SELECT * FROM broker_sync_log").fetchone()
    assert row["status"] == "NOT_CONFIGURED"


def test_live_sync_replaces_holdings(conn, fake_openalgo):
    warnings = []
    sync = broker.sync_holdings(conn, warnings)
    assert sync["source"] == "OPENALGO" and not sync["stale"]
    holdings = db.get_holdings(conn)
    # only the valid NSE row survives mapping/quantity filters
    assert [h["ticker"] for h in holdings] == ["RELIANCE.NS"]
    assert holdings[0]["quantity"] == 4
    assert holdings[0]["avg_buy_price"] == pytest.approx(1290.5)
    assert holdings[0]["source"] == "OPENALGO"
    assert any("TATAMOTORS" in w for w in warnings)  # unmapped symbol surfaced
    row = conn.execute(
        "SELECT * FROM broker_sync_log ORDER BY id DESC").fetchone()
    assert row["status"] == "OK" and row["holdings_count"] == 1


def test_failed_sync_keeps_snapshot(conn, fake_openalgo, monkeypatch):
    warnings = []
    first = broker.sync_holdings(conn, warnings)  # live sync first
    assert first["source"] == "OPENALGO", f"first sync failed: {warnings}"
    fake_openalgo.shutdown()              # then the "token expired" reality
    monkeypatch.setenv("OPENALGO_HOST", "http://127.0.0.1:1")  # dead port
    sync = broker.sync_holdings(conn, warnings)
    assert sync["source"] == "OPENALGO", f"warnings: {warnings}"  # kept snapshot
    assert sync["count"] == 1             # snapshot preserved, not wiped
    assert not sync["stale"]              # just synced moments ago
    row = conn.execute(
        "SELECT * FROM broker_sync_log ORDER BY id DESC").fetchone()
    assert row["status"] == "FAILED"


def test_error_payload_treated_as_failure(conn, fake_openalgo, monkeypatch):
    monkeypatch.setattr(_Handler, "payload",
                        {"status": "error", "message": "Invalid openalgo apikey"})
    warnings = []
    sync = broker.sync_holdings(conn, warnings)
    assert sync["source"] in ("CACHE", "MOCK") or sync["count"] == 0
    assert any("token expired" in w or "error" in w.lower() for w in warnings)


_BUY_ACTION = {"action": "BUY", "ticker": "GROWW.NS", "strategy": "TECHNICAL",
               "qty": 19}
_SELL_ACTION = {"action": "SELL", "ticker": "GROWW.NS", "strategy": "TECHNICAL",
                "qty": 19}


def test_mirror_sends_orders_in_analyzer_mode(fake_openalgo, monkeypatch):
    monkeypatch.setattr(config, "PAPER_MIRROR_TO_OPENALGO", True)
    monkeypatch.setattr(_Handler, "analyzer_mode", "analyze")
    monkeypatch.setattr(_Handler, "placed_orders", [])
    warnings = []
    n = broker.mirror_paper_orders([_SELL_ACTION, _BUY_ACTION,
                                    {"action": "SKIP", "ticker": "X.NS"}], warnings)
    assert n == 2
    sides = [(o["action"], o["symbol"], o["quantity"], o["strategy"])
             for o in _Handler.placed_orders]
    assert sides == [("SELL", "GROWW", 19, "PAPER-TECHNICAL"),
                     ("BUY", "GROWW", 19, "PAPER-TECHNICAL")]


def test_mirror_refuses_in_live_mode(fake_openalgo, monkeypatch):
    monkeypatch.setattr(config, "PAPER_MIRROR_TO_OPENALGO", True)
    monkeypatch.setattr(_Handler, "analyzer_mode", "live")
    monkeypatch.setattr(_Handler, "placed_orders", [])
    warnings = []
    n = broker.mirror_paper_orders([_BUY_ACTION], warnings)
    assert n == 0
    assert _Handler.placed_orders == []       # nothing reached the order API
    assert any("NOT mirrored" in w for w in warnings)


def test_mirror_refuses_when_unreachable(monkeypatch):
    monkeypatch.setattr(config, "PAPER_MIRROR_TO_OPENALGO", True)
    monkeypatch.setenv("OPENALGO_HOST", "http://127.0.0.1:1")
    monkeypatch.setenv("OPENALGO_API_KEY", "k")
    warnings = []
    assert broker.mirror_paper_orders([_BUY_ACTION], warnings) == 0
    assert any("NOT mirrored" in w for w in warnings)


def test_mirror_disabled_by_config(fake_openalgo, monkeypatch):
    monkeypatch.setattr(config, "PAPER_MIRROR_TO_OPENALGO", False)
    monkeypatch.setattr(_Handler, "placed_orders", [])
    assert broker.mirror_paper_orders([_BUY_ACTION], []) == 0
    assert _Handler.placed_orders == []


def test_place_order_hard_disabled(monkeypatch):
    warnings = []
    assert not config.PLACE_ORDER_ENABLED
    result = broker.place_order("RELIANCE.NS", "BUY", 1, warnings)
    assert result is None
    assert any("blocked" in w for w in warnings)
