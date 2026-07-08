"""Market regime signal for the strategy ledger's daily context row.

Nifty 50 + Bank Nifty trend, India VIX level, and a "global markets" cue (prior
S&P 500 / Nasdaq session return — the standard practitioner proxy for global
sentiment when a dedicated index isn't available) via yfinance. Reuses
market_data.py's batched-download style. Degrades gracefully: a failed fetch
never blocks the run, fields come back None.
"""
from __future__ import annotations

import pandas as pd
import yfinance as yf

import config

NIFTY_TICKER = "^NSEI"
BANKNIFTY_TICKER = "^NSEBANK"
VIX_TICKER = "^INDIAVIX"
SP500_TICKER = "^GSPC"
NASDAQ_TICKER = "^IXIC"
REGIME_PERIOD = "6mo"   # enough daily bars for a 50-day SMA

VIX_LOW_MAX = 13.0      # India VIX below this = calm market
VIX_HIGH_MIN = 20.0     # above this = elevated fear


def _trend_regime(close: pd.Series) -> str | None:
    if len(close) < 50:
        return None
    sma20 = close.rolling(20).mean().iloc[-1]
    sma50 = close.rolling(50).mean().iloc[-1]
    last = close.iloc[-1]
    if last > sma20 > sma50:
        return "UPTREND"
    if last < sma20 < sma50:
        return "DOWNTREND"
    return "SIDEWAYS"


def _vix_regime(level: float | None) -> str | None:
    if level is None:
        return None
    if level < VIX_LOW_MAX:
        return "LOW"
    if level > VIX_HIGH_MIN:
        return "HIGH"
    return "NORMAL"


def _day_return_pct(close: pd.Series) -> float | None:
    """Latest bar's % change vs the prior bar — a day-over-day return."""
    if len(close) < 2:
        return None
    prev, last = float(close.iloc[-2]), float(close.iloc[-1])
    if prev == 0:
        return None
    return round((last / prev - 1) * 100, 2)


def _extract_close(raw, ticker: str, warnings: list[str], label: str) -> pd.Series | None:
    try:
        frame = raw[ticker] if isinstance(raw.columns, pd.MultiIndex) else raw
        close = frame["Close"].dropna()
        return close if len(close) else None
    except Exception as exc:
        warnings.append(f"market_regime: {label} extraction failed ({exc})")
        return None


def fetch_regime(warnings: list[str]) -> dict:
    """Returns nifty/bank nifty/VIX/global-markets fields; None on any failure
    (never blocks the run — this is context for the ledger, not a hard gate)."""
    result = {
        "nifty_close": None, "nifty_regime": None, "nifty_return_pct": None,
        "india_vix": None, "vix_regime": None,
        "banknifty_close": None, "banknifty_return_pct": None, "banknifty_regime": None,
        "sp500_return_pct": None, "nasdaq_return_pct": None,
        "nifty_crash": None, "banknifty_crash": None, "global_crash": None,
    }
    try:
        raw = yf.download(
            [NIFTY_TICKER, BANKNIFTY_TICKER, VIX_TICKER, SP500_TICKER, NASDAQ_TICKER],
            period=REGIME_PERIOD,
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            threads=True,
            progress=False,
        )
    except Exception as exc:  # network failure etc.
        warnings.append(f"market_regime: yfinance download failed ({exc}) - regime unavailable")
        return result

    nifty_close = _extract_close(raw, NIFTY_TICKER, warnings, "Nifty")
    if nifty_close is not None:
        result["nifty_close"] = round(float(nifty_close.iloc[-1]), 2)
        result["nifty_regime"] = _trend_regime(nifty_close)
        result["nifty_return_pct"] = _day_return_pct(nifty_close)
        if result["nifty_return_pct"] is not None:
            result["nifty_crash"] = result["nifty_return_pct"] <= config.MARKET_CRASH_THRESHOLD_PCT

    banknifty_close = _extract_close(raw, BANKNIFTY_TICKER, warnings, "Bank Nifty")
    if banknifty_close is not None:
        result["banknifty_close"] = round(float(banknifty_close.iloc[-1]), 2)
        result["banknifty_regime"] = _trend_regime(banknifty_close)
        result["banknifty_return_pct"] = _day_return_pct(banknifty_close)
        if result["banknifty_return_pct"] is not None:
            result["banknifty_crash"] = (
                result["banknifty_return_pct"] <= config.MARKET_CRASH_THRESHOLD_PCT)

    vix_close = _extract_close(raw, VIX_TICKER, warnings, "India VIX")
    if vix_close is not None:
        level = round(float(vix_close.iloc[-1]), 2)
        result["india_vix"] = level
        result["vix_regime"] = _vix_regime(level)

    sp500_close = _extract_close(raw, SP500_TICKER, warnings, "S&P 500")
    if sp500_close is not None:
        result["sp500_return_pct"] = _day_return_pct(sp500_close)

    nasdaq_close = _extract_close(raw, NASDAQ_TICKER, warnings, "Nasdaq")
    if nasdaq_close is not None:
        result["nasdaq_return_pct"] = _day_return_pct(nasdaq_close)

    if result["sp500_return_pct"] is not None and result["nasdaq_return_pct"] is not None:
        result["global_crash"] = (
            result["sp500_return_pct"] <= config.GLOBAL_CRASH_THRESHOLD_PCT
            and result["nasdaq_return_pct"] <= config.GLOBAL_CRASH_THRESHOLD_PCT)

    return result
