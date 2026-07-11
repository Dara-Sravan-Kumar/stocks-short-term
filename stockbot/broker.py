"""Broker bridge — holdings sync (Fyers primary) and a future order hook.

Since 2026-07-11 Fyers is the broker of record: holdings come straight from
its /holdings API using the same shared token as market data. The OpenAlgo
path below (unified REST bridge that carried INDmoney holdings before the
move) remains only as a fallback while its creds are still in .env; a failed
sync keeps the last snapshot and flags staleness instead of failing the run.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import requests

import config
from stockbot import db, fyers_data


def map_fyers_symbol(symbol: str) -> str | None:
    """'NSE:TCS-EQ' -> 'TCS.NS'. Returns None for non-NSE symbols."""
    sym = (symbol or "").strip().upper()
    if not sym.startswith("NSE:"):
        return None
    body = sym[len("NSE:"):]
    root = body.rsplit("-", 1)[0] if "-" in body else body
    return f"{root}.NS" if root else None


def fetch_holdings_fyers(warnings: list[str]) -> list[dict] | None:
    """Fetch holdings straight from Fyers. Returns None on any failure
    (missing creds/token, HTTP or payload errors) so the caller can fall back.
    An empty list is a VALID result — a fresh account simply holds nothing."""
    creds = config.fyers_settings()
    if not creds["app_id"] or not creds["secret_id"]:
        return None
    token = fyers_data.ensure_token(creds, warnings)
    if token is None:
        return None
    try:
        resp = requests.get(f"{config.FYERS_API_BASE}/holdings",
                            headers={"Authorization": f"{creds['app_id']}:{token}"},
                            timeout=config.FYERS_TIMEOUT)
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        warnings.append(f"Fyers holdings unreachable: {exc}")
        return None
    if resp.status_code >= 400 or payload.get("s") != "ok":
        warnings.append(f"Fyers holdings error: "
                        f"{str(payload.get('message', payload))[:150]}")
        return None

    holdings = []
    for h in payload.get("holdings") or []:
        ticker = map_fyers_symbol(h.get("symbol", ""))
        qty = int(h.get("quantity") or 0)
        avg = float(h.get("costPrice") or 0.0)
        if ticker and qty > 0 and avg > 0:
            holdings.append({"ticker": ticker, "avg_buy_price": avg, "quantity": qty})
        elif not ticker and h.get("symbol"):
            warnings.append(f"Fyers holding {h['symbol']} skipped - not NSE")
    return holdings


def map_symbol(oa_symbol: str, exchange: str) -> str | None:
    """OpenAlgo symbol -> yfinance ticker. Returns None for non-NSE segments."""
    sym = (oa_symbol or "").strip().upper()
    if not sym:
        return None
    override = config.BROKER_SYMBOL_OVERRIDES.get(sym)
    if override:
        return override
    if (exchange or "").strip().upper() not in ("NSE", ""):
        return None  # BSE-only / other segments aren't in the yfinance universe
    return f"{sym}.NS"


def fetch_holdings(warnings: list[str]) -> list[dict] | None:
    """Fetch holdings via OpenAlgo. Returns None on any failure."""
    cfg = config.openalgo_settings()
    if not cfg["host"] or not cfg["api_key"]:
        return None
    try:
        resp = requests.post(f"{cfg['host']}/api/v1/holdings",
                             json={"apikey": cfg["api_key"]},
                             timeout=config.OPENALGO_TIMEOUT)
        if resp.status_code >= 400:
            warnings.append(f"OpenAlgo holdings failed ({resp.status_code}): "
                            f"{resp.text[:150]}")
            return None
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        warnings.append(f"OpenAlgo unreachable: {exc}")
        return None

    if payload.get("status") != "success":
        warnings.append(f"OpenAlgo holdings error: {str(payload)[:150]} "
                        "(IndMoney token expired? refresh it in the OpenAlgo UI)")
        return None

    data = payload.get("data") or {}
    rows = data.get("holdings") if isinstance(data, dict) else data
    holdings = []
    for h in rows or []:
        ticker = map_symbol(h.get("symbol", ""), h.get("exchange", "NSE"))
        qty = int(h.get("quantity") or 0)
        avg = float(h.get("average_price") or h.get("avgprice") or 0.0)
        if ticker and qty > 0 and avg > 0:
            holdings.append({"ticker": ticker, "avg_buy_price": avg, "quantity": qty})
        elif not ticker and h.get("symbol"):
            warnings.append(f"OpenAlgo holding {h['symbol']} ({h.get('exchange')}) "
                            "skipped - no NSE mapping (add to BROKER_SYMBOL_OVERRIDES)")
    return holdings


def sync_holdings(conn: sqlite3.Connection, warnings: list[str]) -> dict:
    """Refresh the holdings table: Fyers first, OpenAlgo fallback, then the
    last snapshot with staleness flagging.

    Returns {source, synced_at, stale, count} for dashboard/Discord display.
    """
    now = datetime.now().isoformat(timespec="seconds")

    fyers_rows = fetch_holdings_fyers(warnings)
    if fyers_rows is not None:
        db.replace_holdings(conn, fyers_rows, source="FYERS", synced_at=now)
        db.log_broker_sync(conn, now, "FYERS", "OK", len(fyers_rows), None)
        return {"source": "FYERS", "synced_at": now, "stale": False,
                "count": len(fyers_rows)}

    cfg = config.openalgo_settings()

    if not cfg["host"] or not cfg["api_key"]:
        # not configured: keep the existing mock-seed behavior untouched
        if db.seed_mock_holdings(conn):
            warnings.append("Holdings table was empty - seeded mock holdings "
                            "(set OPENALGO_HOST/OPENALGO_API_KEY for real sync)")
        db.log_broker_sync(conn, now, "MOCK", "NOT_CONFIGURED",
                           len(db.get_holdings(conn)), None)
        prov = db.get_holdings_provenance(conn)
        return {"source": prov["source"] or "MOCK", "synced_at": prov["synced_at"],
                "stale": False, "count": len(db.get_holdings(conn))}

    rows = fetch_holdings(warnings)
    if rows is not None:
        db.replace_holdings(conn, rows, source="OPENALGO", synced_at=now)
        db.log_broker_sync(conn, now, "OPENALGO", "OK", len(rows), None)
        return {"source": "OPENALGO", "synced_at": now, "stale": False,
                "count": len(rows)}

    # failed sync: fall back to the last snapshot and assess staleness
    error = warnings[-1] if warnings else "unknown"
    db.log_broker_sync(conn, now, "OPENALGO", "FAILED", None, error)
    prov = db.get_holdings_provenance(conn)
    stale = True
    if prov["synced_at"]:
        age = datetime.now() - datetime.fromisoformat(prov["synced_at"])
        stale = age > timedelta(hours=config.HOLDINGS_STALE_HOURS)
        if stale:
            warnings.append(
                f"Holdings snapshot is {age.total_seconds() / 3600:.0f}h old "
                f"(sync failing - refresh the IndMoney token in OpenAlgo)")
    else:
        warnings.append("Broker sync failed and no previous holdings snapshot exists")
    return {"source": prov["source"] or "CACHE", "synced_at": prov["synced_at"],
            "stale": stale, "count": len(db.get_holdings(conn))}


def analyzer_mode_on(warnings: list[str]) -> bool:
    """True only when OpenAlgo explicitly confirms Analyzer (sandbox) mode.

    Any doubt — unreachable, error payload, unexpected shape — returns False,
    because a wrong answer here would route mirrored orders to the real broker.
    """
    cfg = config.openalgo_settings()
    if not cfg["host"] or not cfg["api_key"]:
        return False
    try:
        resp = requests.post(f"{cfg['host']}/api/v1/analyzer",
                             json={"apikey": cfg["api_key"]},
                             timeout=config.OPENALGO_TIMEOUT)
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        warnings.append(f"OpenAlgo analyzer status check failed: {exc}")
        return False
    if resp.status_code >= 400 or payload.get("status") != "success":
        warnings.append(f"OpenAlgo analyzer status error: {str(payload)[:150]}")
        return False
    data = payload.get("data") or {}
    return bool(data.get("analyze_mode")) or data.get("mode") == "analyze"


def mirror_paper_orders(actions: list[dict], warnings: list[str]) -> int:
    """Send paper BUY/SELL actions to OpenAlgo's sandbox orderbook.

    Lets the user watch the paper book in OpenAlgo's trading UI (orderbook,
    positions, P&L). Hard-gated on analyzer mode — never mirrors when the
    server is in live mode. Returns the number of orders mirrored.
    """
    orders = [a for a in actions if a.get("action") in ("BUY", "SELL")]
    if not orders or not config.PAPER_MIRROR_TO_OPENALGO:
        return 0
    cfg = config.openalgo_settings()
    if not cfg["host"] or not cfg["api_key"]:
        return 0
    if not analyzer_mode_on(warnings):
        warnings.append("Paper orders NOT mirrored to OpenAlgo (analyzer mode "
                        "off or server unreachable) - toggle Analyzer ON in its UI")
        return 0

    mirrored = 0
    for a in orders:
        symbol = a["ticker"].removesuffix(".NS")
        try:
            resp = requests.post(
                f"{cfg['host']}/api/v1/placeorder",
                json={"apikey": cfg["api_key"],
                      "strategy": f"PAPER-{a['strategy']}",
                      "symbol": symbol, "exchange": "NSE",
                      "action": a["action"], "quantity": a["qty"],
                      "pricetype": "MARKET", "product": "CNC"},
                timeout=config.OPENALGO_TIMEOUT)
            payload = resp.json()
            if resp.status_code < 400 and payload.get("status") == "success":
                mirrored += 1
            else:
                warnings.append(f"OpenAlgo mirror {a['action']} {a['ticker']} "
                                f"failed: {str(payload)[:120]}")
        except (requests.RequestException, ValueError) as exc:
            warnings.append(f"OpenAlgo mirror {a['action']} {a['ticker']} failed: {exc}")
    return mirrored


def place_order(ticker: str, side: str, qty: int, warnings: list[str]) -> dict | None:
    """Future automation hook — hard-disabled in v1 (config.PLACE_ORDER_ENABLED).

    When enabled, posts to OpenAlgo /api/v1/placeorder which routes to the
    connected broker (IndMoney). Paper trading never calls this.
    """
    if not config.PLACE_ORDER_ENABLED:
        warnings.append(f"place_order({ticker}, {side}, {qty}) blocked: "
                        "PLACE_ORDER_ENABLED is False")
        return None
    cfg = config.openalgo_settings()
    if not cfg["host"] or not cfg["api_key"]:
        warnings.append("place_order blocked: OpenAlgo not configured")
        return None
    symbol = ticker.removesuffix(".NS")
    try:
        resp = requests.post(
            f"{cfg['host']}/api/v1/placeorder",
            json={"apikey": cfg["api_key"], "strategy": "stockbot",
                  "symbol": symbol, "exchange": "NSE", "action": side.upper(),
                  "quantity": qty, "pricetype": "MARKET", "product": "CNC"},
            timeout=config.OPENALGO_TIMEOUT)
        payload = resp.json()
        if resp.status_code >= 400 or payload.get("status") != "success":
            warnings.append(f"place_order failed: {str(payload)[:150]}")
            return None
        return payload
    except (requests.RequestException, ValueError) as exc:
        warnings.append(f"place_order failed: {exc}")
        return None
