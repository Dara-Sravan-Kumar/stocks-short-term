"""Fundamental quality gate via yfinance ticker.info, cached per (ticker, day).

.info is flaky for NSE tickers, so the gate is lenient: missing fields warn
but don't fail; only clearly-present bad values hard-fail.
"""
from __future__ import annotations

import sqlite3

import yfinance as yf

import config
from stockbot import db


def _get(info: dict, key: str):
    v = info.get(key)
    return v if isinstance(v, (int, float)) else None


def check_fundamentals(conn: sqlite3.Connection, ticker: str, date: str,
                       warnings: list[str]) -> bool:
    """Return True if the ticker passes the quality gate (cached daily)."""
    cached = db.get_fundamentals(conn, ticker, date)
    if cached is not None:
        return bool(cached["passed"])

    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as exc:
        warnings.append(f"{ticker}: fundamentals fetch failed ({exc}) — lenient pass")
        db.upsert_fundamentals(conn, ticker, date, True, None, None, None, None, None,
                               "fetch failed - lenient pass")
        return True

    mcap = _get(info, "marketCap")
    pe = _get(info, "trailingPE")
    roe = _get(info, "returnOnEquity")
    dte = _get(info, "debtToEquity")
    eps_g = _get(info, "earningsGrowth")

    reasons: list[str] = []

    tier = config.TIER.get(ticker, "LARGE")
    min_mcap = config.MIN_MARKET_CAP_BY_TIER[tier]
    if mcap is not None and mcap < min_mcap:
        reasons.append(f"mcap {mcap/1e9:.0f}B < {min_mcap/1e9:.0f}B ({tier} tier)")
    if pe is not None and (pe <= 0 or pe > config.MAX_PE):
        reasons.append(f"PE {pe:.1f} outside (0, {config.MAX_PE:.0f}]")
    if roe is not None and roe < config.MIN_ROE:
        reasons.append(f"ROE {roe:.2%} < {config.MIN_ROE:.0%}")
    if (dte is not None and ticker not in config.FINANCIALS
            and dte > config.MAX_DEBT_TO_EQUITY):
        reasons.append(f"D/E {dte:.0f} > {config.MAX_DEBT_TO_EQUITY:.0f}")
    if eps_g is not None and eps_g <= config.MIN_EARNINGS_GROWTH:
        reasons.append(f"EPS growth {eps_g:.1%} <= {config.MIN_EARNINGS_GROWTH:.0%}")

    passed = not reasons
    detail = "; ".join(reasons) if reasons else "passed"
    db.upsert_fundamentals(conn, ticker, date, passed, pe, roe, dte, mcap, eps_g, detail)
    return passed
