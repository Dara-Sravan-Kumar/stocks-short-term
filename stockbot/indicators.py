"""Technical indicators & pivot levels — pure pandas/numpy, no TA-Lib."""
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
        **piv,
        weekly_r1=weekly_r1,
        weekly_r2=weekly_r2,
        weekly_r3=weekly_r3,
    )


def derive_target_stop(snap: Snapshot, min_upside_pct: float,
                       max_risk_pct: float) -> tuple[float | None, float]:
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
    return (float(target) if target is not None else None), float(stop)
