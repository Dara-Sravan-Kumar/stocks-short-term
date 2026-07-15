"""Technical indicators & pivot levels — pure pandas/numpy, no TA-Lib.

Some fields (cmf, fvg_bull_*, anchored_vwap, volume_poc) are DAILY-BAR PROXIES for
concepts that normally need intraday/tick data (order flow, fair value gaps, anchored
VWAP, volume profile) — they're documented approximations retail swing traders already
use when tick data isn't available, not attempts to fake real intraday indicators.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Snapshot:
    """Latest-bar technical snapshot for one ticker."""
    ticker: str
    date: str
    close: float
    high: float
    low: float
    rsi: float
    macd: float
    macd_signal: float
    macd_hist: float
    macd_hist_prev: float
    macd_bullish_cross_recent: bool  # bullish crossover within lookback bars
    macd_bearish_cross_today: bool
    sma20: float
    sma50: float
    closes_below_sma20: int          # consecutive closes below SMA20 (incl. today)
    mom_5d: float                    # % return over 5 bars
    mom_20d: float
    vol_ratio: float                 # today's volume / 20d avg volume
    avg_turnover_20d: float          # 20d avg of close*volume (INR) — liquidity
    swing_low_10d: float
    swing_low_10d_prior: float       # 10-bar low EXCLUDING today (vs swing_low_10d, which includes it)
    close_prev: float                # prior bar's close
    # daily-bar proxies for concepts that normally need intraday data — see
    # indicators.py module docstring for what each one actually measures
    cmf: float                       # Chaikin Money Flow (20d) — order-flow proxy
    cmf_prev: float                  # CMF 5 bars ago, for a "rising" check
    fvg_bull_bottom: float | None    # most recent unfilled bullish FVG zone (None if none)
    fvg_bull_top: float | None
    anchored_vwap: float             # VWAP anchored to the most recent 60-bar swing low
    volume_poc: float                # 60-bar volume-weighted price histogram peak
    high_252d: float                 # 252-day (52-week) rolling high
    # daily pivots (from prior day's H/L/C)
    pivot: float
    r1: float
    r2: float
    s1: float
    s2: float
    r3: float
    # weekly pivots (from prior week's H/L/C)
    weekly_r1: float
    weekly_r2: float
    weekly_r3: float
    atr: float = 0.0                 # Wilder ATR(14), absolute price units (0 if unknown)


def rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi.fillna(100.0)  # all-gain streak -> RSI 100


def macd_lines(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    sig = macd.ewm(span=signal, adjust=False).mean()
    return macd, sig, macd - sig


def atr_wilder(high: pd.Series, low: pd.Series, close: pd.Series,
               period: int = 14) -> pd.Series:
    """Average True Range (Wilder). True Range = max of the current high-low
    range and the gaps to the prior close, smoothed with Wilder's EMA. Absolute
    price units — a volatility yardstick for sizing stops outside daily noise."""
    prev_close = close.shift(1)
    tr = pd.concat([(high - low),
                    (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def classic_pivots(h: float, l: float, c: float) -> dict[str, float]:
    p = (h + l + c) / 3
    return {
        "pivot": p,
        "r1": 2 * p - l,
        "s1": 2 * p - h,
        "r2": p + (h - l),
        "s2": p - (h - l),
        "r3": h + 2 * (p - l),
    }


def chaikin_money_flow(close: pd.Series, high: pd.Series, low: pd.Series,
                       volume: pd.Series, period: int = 20) -> pd.Series:
    """Order-flow proxy: buying/selling pressure from where each bar's close sits
    within its high-low range, weighted by volume. Not real order flow (that needs
    tick/bid-ask data) — a standard daily-bar approximation (Chaikin Money Flow)."""
    mf_mult = ((close - low) - (high - close)) / (high - low).replace(0, np.nan)
    mf_vol = mf_mult * volume
    return (mf_vol.rolling(period).sum() / volume.rolling(period).sum()).fillna(0.0)


def find_bullish_fvg(high: pd.Series, low: pd.Series,
                     lookback: int = 15) -> tuple[float | None, float | None]:
    """Most recent 3-candle bullish Fair Value Gap (high[i-2] < low[i]) within the
    lookback window. Returns (gap_bottom, gap_top), or (None, None) if none found.

    Simplification: doesn't track whether the gap has since been fully traded
    through ("filled") — the entry gate's own "price is tagging the zone today"
    check is what actually matters for a retest signal.
    """
    n = len(high)
    start = max(2, n - lookback)
    for i in range(n - 1, start - 1, -1):
        gap_bottom, gap_top = float(high.iloc[i - 2]), float(low.iloc[i])
        if gap_bottom < gap_top:
            return gap_bottom, gap_top
    return None, None


def anchored_vwap(df: pd.DataFrame, lookback: int = 60) -> float:
    """VWAP anchored to the lowest-low bar in the lookback window (an objective,
    reproducible anchor point) through today — a daily-bar approximation of
    anchored VWAP, which normally anchors to an intraday event."""
    window = df.iloc[-lookback:] if len(df) >= lookback else df
    anchor_pos = df.index.get_loc(window["Low"].idxmin())
    segment = df.iloc[anchor_pos:]
    typical = (segment["High"] + segment["Low"] + segment["Close"]) / 3
    cum_vol = float(segment["Volume"].sum())
    if cum_vol <= 0:
        return float(segment["Close"].iloc[-1])
    return float((typical * segment["Volume"]).sum() / cum_vol)


def volume_poc(df: pd.DataFrame, lookback: int = 60, bins: int = 20) -> float:
    """Point-of-Control proxy: the price bin with the most volume over the lookback
    window, from a volume-weighted histogram of typical price. Built from daily
    bars (one volume figure per day), not real intraday volume-at-price."""
    window = df.iloc[-lookback:] if len(df) >= lookback else df
    typical = (window["High"] + window["Low"] + window["Close"]) / 3
    vol = window["Volume"].to_numpy()
    lo, hi = float(typical.min()), float(typical.max())
    if hi <= lo:
        return float(typical.iloc[-1])
    edges = np.linspace(lo, hi, bins + 1)
    bin_idx = np.clip(np.digitize(typical.to_numpy(), edges) - 1, 0, bins - 1)
    vol_by_bin = np.zeros(bins)
    np.add.at(vol_by_bin, bin_idx, vol)
    poc_bin = int(np.argmax(vol_by_bin))
    return float((edges[poc_bin] + edges[poc_bin + 1]) / 2)


def compute_snapshot(ticker: str, df: pd.DataFrame, cross_lookback: int = 3) -> Snapshot:
    """Compute the latest-bar snapshot. df must have >= 60 daily bars."""
    close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"]

    rsi = rsi_wilder(close)
    macd, sig, hist = macd_lines(close)

    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean()

    # consecutive closes below SMA20 (counting back from today)
    below = (close < sma20).iloc[::-1]
    consec_below = 0
    for b in below:
        if b:
            consec_below += 1
        else:
            break

    # MACD crossovers
    above = macd > sig
    bull_cross = above & ~above.shift(1, fill_value=False)
    bull_recent = bool(bull_cross.iloc[-cross_lookback:].any())
    bear_cross_today = bool((~above.iloc[-1]) and above.iloc[-2])

    # daily pivots from the PRIOR day's bar
    prev = df.iloc[-2]
    piv = classic_pivots(float(prev["High"]), float(prev["Low"]), float(prev["Close"]))

    # weekly pivots from the prior completed week
    weekly = df.resample("W-FRI").agg({"High": "max", "Low": "min", "Close": "last"}).dropna()
    if len(weekly) >= 2:
        pw = weekly.iloc[-2]
        wpiv = classic_pivots(float(pw["High"]), float(pw["Low"]), float(pw["Close"]))
        weekly_r1, weekly_r2, weekly_r3 = wpiv["r1"], wpiv["r2"], wpiv["r3"]
    else:
        weekly_r1 = weekly_r2 = weekly_r3 = float("inf")

    avg_vol20 = float(vol.rolling(20).mean().iloc[-1])
    vol_ratio = float(vol.iloc[-1]) / avg_vol20 if avg_vol20 > 0 else 0.0
    avg_turnover = float((close * vol).rolling(20).mean().iloc[-1])

    cmf_series = chaikin_money_flow(close, high, low, vol)
    fvg_bottom, fvg_top = find_bullish_fvg(high, low)
    atr_series = atr_wilder(high, low, close)
    atr_val = float(atr_series.iloc[-1])
    if atr_val != atr_val:  # NaN (too little history) -> unknown
        atr_val = 0.0

    return Snapshot(
        ticker=ticker,
        date=df.index[-1].strftime("%Y-%m-%d"),
        close=float(close.iloc[-1]),
        high=float(high.iloc[-1]),
        low=float(low.iloc[-1]),
        rsi=float(rsi.iloc[-1]),
        macd=float(macd.iloc[-1]),
        macd_signal=float(sig.iloc[-1]),
        macd_hist=float(hist.iloc[-1]),
        macd_hist_prev=float(hist.iloc[-2]),
        macd_bullish_cross_recent=bull_recent,
        macd_bearish_cross_today=bear_cross_today,
        sma20=float(sma20.iloc[-1]),
        sma50=float(sma50.iloc[-1]),
        closes_below_sma20=consec_below,
        mom_5d=float((close.iloc[-1] / close.iloc[-6] - 1) * 100),
        mom_20d=float((close.iloc[-1] / close.iloc[-21] - 1) * 100),
        vol_ratio=vol_ratio,
        avg_turnover_20d=avg_turnover,
        swing_low_10d=float(low.iloc[-10:].min()),
        swing_low_10d_prior=float(low.iloc[-11:-1].min()),
        close_prev=float(close.iloc[-2]),
        cmf=float(cmf_series.iloc[-1]),
        cmf_prev=float(cmf_series.iloc[-6]),
        fvg_bull_bottom=fvg_bottom,
        fvg_bull_top=fvg_top,
        anchored_vwap=anchored_vwap(df),
        volume_poc=volume_poc(df),
        high_252d=float(high.rolling(252, min_periods=1).max().iloc[-1]),
        **piv,
        weekly_r1=weekly_r1,
        weekly_r2=weekly_r2,
        weekly_r3=weekly_r3,
        atr=atr_val,
    )


def derive_target_stop(snap: Snapshot, min_upside_pct: float,
                       max_risk_pct: float,
                       min_stop_atr_mult: float = 0.0) -> tuple[float | None, float]:
    """Target = the nearest resistance rung offering at least min upside.

    The ladder combines daily R1/R2/R3 and weekly R1/R2/R3 — so a stock that
    already broke above today's R1/R2 (a breakout) still gets a meaningful
    target from the next rung up. Returns (None, stop) when no rung offers
    enough upside.

    Stop = max(S1, 10d swing low * 0.995), clamped to max risk.
    """
    entry = snap.close
    min_target = entry * (1 + min_upside_pct / 100)
    ladder = sorted(
        lvl for lvl in (snap.r1, snap.r2, snap.r3,
                        snap.weekly_r1, snap.weekly_r2, snap.weekly_r3)
        if lvl != float("inf")
    )
    target = next((lvl for lvl in ladder if lvl >= min_target), None)

    stop = max(snap.s1, snap.swing_low_10d * 0.995)
    if stop >= entry:
        stop = entry * (1 - max_risk_pct / 100)
    risk_pct = (entry - stop) / entry * 100
    if risk_pct > max_risk_pct:
        stop = entry * (1 - max_risk_pct / 100)
        risk_pct = max_risk_pct
    # Floor the stop OUTSIDE routine daily noise: a support pivot right under
    # price otherwise gives a sub-1% stop that gets tagged on day 1. Widen to
    # MIN_STOP_ATR_MULT x ATR, but never past the max-risk ceiling — a stock too
    # volatile to stop within the cap just ends with a worse R:R and is filtered
    # by the reward:risk gate rather than kept on a noise-tight stop.
    atr_pct = (snap.atr / entry * 100) if entry > 0 else 0.0
    if atr_pct > 0 and min_stop_atr_mult > 0:
        min_stop_pct = min(min_stop_atr_mult * atr_pct, max_risk_pct)
        if risk_pct < min_stop_pct:
            stop = entry * (1 - min_stop_pct / 100)
    return (float(target) if target is not None else None), float(stop)
