"""Batched OHLCV download via yfinance with per-ticker resilience."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

import config

IST = ZoneInfo("Asia/Kolkata")


def today_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def fetch_history(tickers: list[str], warnings: list[str]) -> dict[str, pd.DataFrame]:
    """Download ~1y of daily bars for all tickers in one batched call.

    Returns {ticker: DataFrame[Open, High, Low, Close, Volume]} keeping only
    tickers with enough clean history. Failures are recorded as warnings,
    never raised.
    """
    tickers = sorted(set(tickers))
    if not tickers:
        return {}
    try:
        raw = yf.download(
            tickers,
            period=config.HISTORY_PERIOD,
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            threads=True,
            progress=False,
        )
    except Exception as exc:  # network failure etc.
        warnings.append(f"yfinance batch download failed: {exc}")
        return {}

    out: dict[str, pd.DataFrame] = {}
    for t in tickers:
        try:
            df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(how="all")
            df = df[df["Close"].notna()]
            if len(df) < config.MIN_HISTORY_BARS:
                warnings.append(f"{t}: only {len(df)} bars of history - skipped")
                continue
            out[t] = df
        except Exception as exc:
            warnings.append(f"{t}: data extraction failed ({exc}) - skipped")
    return out


def latest_bar_date(history: dict[str, pd.DataFrame]) -> str:
    """Most recent bar date across all tickers (YYYY-MM-DD)."""
    dates = [df.index[-1] for df in history.values() if len(df)]
    if not dates:
        return today_ist()
    return max(dates).strftime("%Y-%m-%d")
