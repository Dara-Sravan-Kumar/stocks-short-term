"""Daily OHLCV for the scan universe: Fyers (real broker data) when
configured, with the original batched yfinance download as gap-filler and
full fallback. Failures are warnings, never exceptions."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

import config
from stockbot import fyers_data

IST = ZoneInfo("Asia/Kolkata")


def today_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def fetch_history(tickers: list[str], warnings: list[str]) -> dict[str, pd.DataFrame]:
    """~1y of daily bars per ticker: Fyers first, yfinance for the rest.

    Returns {ticker: DataFrame[Open, High, Low, Close, Volume]} keeping only
    tickers with enough clean history.
    """
    tickers = sorted(set(tickers))
    if not tickers:
        return {}
    creds = config.fyers_settings()
    if creds["app_id"] and creds["secret_id"]:
        try:
            out = fyers_data.fetch_history(tickers, warnings)
        except Exception as exc:
            warnings.append(f"Fyers provider crashed ({exc}) - using yfinance")
            out = {}
        if out:
            missing = [t for t in tickers if t not in out]
            if missing:
                warnings.append(f"Fyers missed {len(missing)} of {len(tickers)} "
                                "tickers - filling from yfinance")
                out.update(_fetch_yfinance(missing, warnings))
            return out
        warnings.append("Fyers returned no data - falling back to yfinance")
    return _fetch_yfinance(tickers, warnings)


def _fetch_yfinance(tickers: list[str],
                    warnings: list[str]) -> dict[str, pd.DataFrame]:
    """Original one-shot batched yfinance download."""
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
