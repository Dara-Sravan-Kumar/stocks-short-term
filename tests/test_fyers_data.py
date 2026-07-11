"""Fyers NSE adapter: symbol resolution, candle parsing, market_data dispatch
and holdings mapping — all HTTP is mocked, no network calls."""
from datetime import datetime, timedelta

import pytest

import config
from stockbot import broker, fyers_data, market_data


def _master_row(ticker: str) -> list[str]:
    return ["10100000001", "SOME COMPANY LTD", "0", "1", "0.05", "INE000X01",
            "0916-1530", "2026-07-10", "-1", ticker, "10", "10", "1", "SYM",
            "1", "-1", "XX", "10100000001"]


def _candles(n: int, base: float = 100.0) -> list[list[float]]:
    start = datetime(2026, 1, 1)
    return [[int((start + timedelta(days=i)).timestamp()),
             base, base + 2, base - 2, base + 1, 10000] for i in range(n)]


# ------------------------------------------------------------------ symbols
def test_resolve_prefers_eq_series(monkeypatch):
    rows = [_master_row("NSE:TCS-EQ"), _master_row("NSE:TCS-BE")]
    monkeypatch.setattr(fyers_data, "_load_master", lambda w: rows)
    warnings = []
    out = fyers_data.resolve_symbols(["TCS.NS"], warnings)
    assert out == {"TCS.NS": "NSE:TCS-EQ"} and not warnings


def test_resolve_falls_back_to_be_series(monkeypatch):
    monkeypatch.setattr(fyers_data, "_load_master",
                        lambda w: [_master_row("NSE:SMALLCO-BE")])
    out = fyers_data.resolve_symbols(["SMALLCO.NS"], [])
    assert out == {"SMALLCO.NS": "NSE:SMALLCO-BE"}


def test_resolve_handles_special_characters(monkeypatch):
    rows = [_master_row("NSE:M&M-EQ"), _master_row("NSE:BAJAJ-AUTO-EQ")]
    monkeypatch.setattr(fyers_data, "_load_master", lambda w: rows)
    out = fyers_data.resolve_symbols(["M&M.NS", "BAJAJ-AUTO.NS"], [])
    assert out == {"M&M.NS": "NSE:M&M-EQ", "BAJAJ-AUTO.NS": "NSE:BAJAJ-AUTO-EQ"}


def test_resolve_warns_on_unknown_ticker(monkeypatch):
    monkeypatch.setattr(fyers_data, "_load_master",
                        lambda w: [_master_row("NSE:TCS-EQ")])
    warnings = []
    out = fyers_data.resolve_symbols(["NOPE.NS"], warnings)
    assert out == {} and "NOPE.NS" in warnings[0]


# ------------------------------------------------------------------ candles
def test_fetch_history_parses_candles(monkeypatch):
    monkeypatch.setattr(fyers_data, "ensure_token",
                        lambda creds, w, force_refresh=False: "tok")
    monkeypatch.setattr(fyers_data, "resolve_symbols",
                        lambda tickers, w: {"TCS.NS": "NSE:TCS-EQ"})
    monkeypatch.setattr(config, "MIN_HISTORY_BARS", 5)

    class FakeResp:
        status_code = 200
        def json(self):
            return {"s": "ok", "candles": _candles(10)}

    monkeypatch.setattr(fyers_data.requests, "get",
                        lambda *a, **k: FakeResp())
    warnings = []
    out = fyers_data.fetch_history(["TCS.NS"], warnings)
    df = out["TCS.NS"]
    assert len(df) == 10 and list(df.columns) == ["Open", "High", "Low",
                                                  "Close", "Volume"]
    assert not warnings


def test_short_history_skipped_with_warning(monkeypatch):
    monkeypatch.setattr(fyers_data, "ensure_token",
                        lambda creds, w, force_refresh=False: "tok")
    monkeypatch.setattr(fyers_data, "resolve_symbols",
                        lambda tickers, w: {"TCS.NS": "NSE:TCS-EQ"})

    class FakeResp:
        status_code = 200
        def json(self):
            return {"s": "ok", "candles": _candles(3)}

    monkeypatch.setattr(fyers_data.requests, "get", lambda *a, **k: FakeResp())
    warnings = []
    out = fyers_data.fetch_history(["TCS.NS"], warnings)
    assert out == {} and any("only 3 Fyers bars" in w for w in warnings)


# ----------------------------------------------------------------- dispatch
def test_market_data_prefers_fyers_and_fills_gaps(monkeypatch):
    monkeypatch.setenv("FYERS_APP_ID", "AB01234-100")
    monkeypatch.setenv("FYERS_SECRET_ID", "SECRET")
    monkeypatch.setattr(market_data.fyers_data, "fetch_history",
                        lambda tickers, w: {"TCS.NS": "FYERS_DF"})
    monkeypatch.setattr(market_data, "_fetch_yfinance",
                        lambda tickers, w: {t: "YF_DF" for t in tickers})
    warnings = []
    out = market_data.fetch_history(["TCS.NS", "INFY.NS"], warnings)
    assert out == {"TCS.NS": "FYERS_DF", "INFY.NS": "YF_DF"}
    assert any("missed 1 of 2" in w for w in warnings)


def test_market_data_full_fallback_when_fyers_empty(monkeypatch):
    monkeypatch.setenv("FYERS_APP_ID", "AB01234-100")
    monkeypatch.setenv("FYERS_SECRET_ID", "SECRET")
    monkeypatch.setattr(market_data.fyers_data, "fetch_history",
                        lambda tickers, w: {})
    monkeypatch.setattr(market_data, "_fetch_yfinance",
                        lambda tickers, w: {t: "YF_DF" for t in tickers})
    warnings = []
    out = market_data.fetch_history(["TCS.NS"], warnings)
    assert out == {"TCS.NS": "YF_DF"}
    assert any("falling back to yfinance" in w for w in warnings)


def test_market_data_skips_fyers_without_creds(monkeypatch):
    monkeypatch.delenv("FYERS_APP_ID", raising=False)
    monkeypatch.delenv("FYERS_SECRET_ID", raising=False)
    monkeypatch.setattr(market_data.fyers_data, "fetch_history",
                        lambda tickers, w: pytest.fail("should not be called"))
    monkeypatch.setattr(market_data, "_fetch_yfinance",
                        lambda tickers, w: {t: "YF_DF" for t in tickers})
    assert market_data.fetch_history(["TCS.NS"], []) == {"TCS.NS": "YF_DF"}


# ----------------------------------------------------------------- holdings
def test_map_fyers_symbol():
    assert broker.map_fyers_symbol("NSE:TCS-EQ") == "TCS.NS"
    assert broker.map_fyers_symbol("NSE:BAJAJ-AUTO-EQ") == "BAJAJ-AUTO.NS"
    assert broker.map_fyers_symbol("BSE:TCS-A") is None
    assert broker.map_fyers_symbol("") is None


def test_sync_prefers_fyers_even_when_empty(monkeypatch, tmp_path):
    import sqlite3
    from stockbot import db
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    conn = db.connect()
    monkeypatch.setattr(broker, "fetch_holdings_fyers", lambda w: [])
    sync = broker.sync_holdings(conn, [])
    assert sync["source"] == "FYERS" and sync["count"] == 0
    assert not sync["stale"]
    conn.close()


def test_sync_falls_back_when_fyers_unavailable(monkeypatch, tmp_path):
    from stockbot import db
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.delenv("OPENALGO_HOST", raising=False)
    monkeypatch.delenv("OPENALGO_API_KEY", raising=False)
    conn = db.connect()
    monkeypatch.setattr(broker, "fetch_holdings_fyers", lambda w: None)
    sync = broker.sync_holdings(conn, [])
    assert sync["source"] == "MOCK"   # unconfigured OpenAlgo seeds mock rows
    conn.close()
