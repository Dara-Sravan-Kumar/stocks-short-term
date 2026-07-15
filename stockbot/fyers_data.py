"""Fyers API v3 adapter — real NSE equity candles (data APIs are free).

Twin of mcx-short-term's mcxbot/data_providers/fyers.py, adapted for cash
equities: no expiry resolution — a yfinance-style ticker "TCS.NS" maps to the
Fyers symbol "NSE:TCS-EQ", validated against Fyers' NSE_CM symbol master
(cached daily in data/) so series exceptions like -BE resolve correctly.

Auth model (shared with mcx-short-term — ONE app, ONE active token; SEBI reality):
- Fyers DISABLED programmatic token refresh: validate-refresh-token now returns
  code -16 ("disabled to comply with SEBI regulations"), so the daily access
  token CANNOT be auto-refreshed by the bot. There is NO auto-refresh fallback —
  a fresh interactive daily login is required: run `python fyers_login.py` in
  either project each trading day.
- Tokens live at FYERS_TOKEN_PATH (shared file); once the cached token is stale
  it cannot be renewed here — the run warns and market_data.py falls back to
  yfinance (equities are scale-safe, so booking continues on fallback).

Scale: ~500 per-symbol history calls per run, so requests go through a
ThreadPoolExecutor throttled to ~170 calls/min (Fyers data-API limit is
200/min). A one-symbol probe validates the token before the fan-out.

Every failure is a per-ticker warning, never an exception — market_data.py
fills gaps (or the whole run) from yfinance.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import threading
import time as time_mod
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

import config

SYMBOL_MASTER_URL = "https://public.fyers.in/sym_details/NSE_CM.csv"
MASTER_CACHE = config.DATA_DIR / "fyers_nse_master.csv"
TOKEN_CACHE = config.DATA_DIR / "fyers_token.json"  # default when env unset
MASTER_MAX_AGE_HOURS = 24

# Equity series in preference order: -EQ (rolling settlement), then -BE/-BZ
# (trade-for-trade), then SME series. Root may contain & and - (M&M, BAJAJ-AUTO).
_SERIES_PREFERENCE = ["EQ", "BE", "BZ", "SM", "ST"]
_TICKER_RE = re.compile(r"^NSE:([A-Z0-9&\-]+)-(" + "|".join(_SERIES_PREFERENCE) + r")$")


# --------------------------------------------------------------------- auth
def app_id_hash(app_id: str, secret_id: str) -> str:
    return hashlib.sha256(f"{app_id}:{secret_id}".encode()).hexdigest()


def token_cache_path() -> Path:
    override = os.getenv("FYERS_TOKEN_PATH", "").strip()
    return Path(override) if override else Path(TOKEN_CACHE)


def load_token_cache() -> dict | None:
    try:
        return json.loads(token_cache_path().read_text(encoding="utf-8"))
    except Exception:
        return None


def save_token_cache(cache: dict) -> None:
    path = token_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def refresh_access_token(creds: dict, cache: dict,
                         warnings: list[str]) -> dict | None:
    """Attempt to mint a fresh daily access token from the refresh token.

    NOTE: Fyers has permanently DISABLED this endpoint to comply with SEBI
    regulations (validate-refresh-token returns HTTP 400, code -16), so in
    practice this always fails and the caller falls back to yfinance. Kept so a
    single, accurate warning is emitted rather than a silent dead code path.
    """
    if not cache.get("refresh_token"):
        warnings.append("Fyers: no refresh token cached - run fyers_login.py")
        return None
    if not creds["pin"]:
        warnings.append("Fyers: FYERS_PIN not set in .env - cannot auto-refresh "
                        "the daily access token")
        return None
    try:
        resp = requests.post(
            f"{config.FYERS_API_BASE}/validate-refresh-token",
            json={"grant_type": "refresh_token",
                  "appIdHash": app_id_hash(creds["app_id"], creds["secret_id"]),
                  "refresh_token": cache["refresh_token"],
                  "pin": creds["pin"]},
            timeout=config.FYERS_TIMEOUT)
        data = resp.json()
        if resp.status_code >= 400 or data.get("s") != "ok" or not data.get("access_token"):
            if data.get("code") == -16 or resp.status_code == 400:
                # SEBI: validate-refresh-token is permanently disabled — no
                # amount of retrying refreshes the token. Only a fresh login can.
                warnings.append("Fyers refresh-token API disabled by SEBI — a full "
                                "daily re-login (fyers_login.py) is required")
            else:
                warnings.append(f"Fyers token refresh failed ({data.get('message', resp.status_code)}) "
                                "- run fyers_login.py to re-authorize")
            return None
    except Exception as exc:
        warnings.append(f"Fyers token refresh failed ({exc})")
        return None
    cache = {**cache, "access_token": data["access_token"],
             "issued": datetime.now().strftime("%Y-%m-%d")}
    save_token_cache(cache)
    return cache


def ensure_token(creds: dict, warnings: list[str],
                 force_refresh: bool = False) -> str | None:
    cache = load_token_cache()
    if cache is None:
        warnings.append("Fyers: no token cache found - run fyers_login.py once "
                        "to authorize")
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    if force_refresh or cache.get("issued") != today or not cache.get("access_token"):
        cache = refresh_access_token(creds, cache, warnings)
        if cache is None:
            return None
    return cache["access_token"]


# -------------------------------------------------------------- rate limiter
class _Throttle:
    """Serialize request starts so N workers stay under the per-minute limit."""

    def __init__(self, min_gap: float):
        self.min_gap = min_gap
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time_mod.monotonic()
            delay = self._next_at - now
            self._next_at = max(now, self._next_at) + self.min_gap
        if delay > 0:
            time_mod.sleep(delay)


# ------------------------------------------------------------------- symbols
def _load_master(warnings: list[str]) -> list[list[str]] | None:
    try:
        if MASTER_CACHE.exists():
            age = datetime.now() - datetime.fromtimestamp(MASTER_CACHE.stat().st_mtime)
            if age < timedelta(hours=MASTER_MAX_AGE_HOURS):
                text = MASTER_CACHE.read_text(encoding="utf-8", errors="replace")
                return list(csv.reader(io.StringIO(text)))
        resp = requests.get(SYMBOL_MASTER_URL, timeout=60)
        resp.raise_for_status()
        MASTER_CACHE.parent.mkdir(parents=True, exist_ok=True)
        MASTER_CACHE.write_text(resp.text, encoding="utf-8")
        return list(csv.reader(io.StringIO(resp.text)))
    except Exception as exc:
        warnings.append(f"Fyers symbol master unavailable ({exc})")
        return None


def resolve_symbols(tickers: list[str],
                    warnings: list[str]) -> dict[str, str]:
    """{'TCS.NS': 'NSE:TCS-EQ', ...} via the NSE_CM master; unknowns warned."""
    rows = _load_master(warnings)
    if rows is None:
        return {}
    by_root: dict[str, dict[str, str]] = {}
    for row in rows:
        for cell in row:
            m = _TICKER_RE.match(cell.strip())
            if m:
                root, series = m.group(1), m.group(2)
                by_root.setdefault(root, {})[series] = cell.strip()
                break
    out: dict[str, str] = {}
    for t in tickers:
        root = t[:-3].upper() if t.upper().endswith(".NS") else t.upper()
        series_map = by_root.get(root)
        if not series_map:
            warnings.append(f"{t}: not in Fyers NSE symbol master - skipped")
            continue
        for series in _SERIES_PREFERENCE:
            if series in series_map:
                out[t] = series_map[series]
                break
    return out


# ------------------------------------------------------------------- candles
def _fetch_candles(symbol: str, app_id: str, token: str,
                   throttle: _Throttle, warnings: list[str],
                   label: str, from_date=None,
                   to_date=None) -> pd.DataFrame | None | str:
    """Returns a DataFrame, None on failure, or "AUTH" on HTTP 401.

    from_date/to_date bound a single request; both default to the trailing
    FYERS_HISTORY_DAYS window (a single sub-366-day request). The backtester
    passes explicit windows via _fetch_candles_chunked to walk multi-year spans.
    """
    to_date = to_date or datetime.now().date()
    from_date = from_date or (to_date - timedelta(days=config.FYERS_HISTORY_DAYS))
    params = {
        "symbol": symbol,
        "resolution": "D",
        "date_format": "1",
        "range_from": from_date.isoformat(),
        "range_to": to_date.isoformat(),
    }
    headers = {"Authorization": f"{app_id}:{token}"}
    throttle.wait()
    try:
        resp = requests.get(f"{config.FYERS_DATA_BASE}/history",
                            params=params, headers=headers,
                            timeout=config.FYERS_TIMEOUT)
        if resp.status_code == 401:
            return "AUTH"
        data = resp.json()
        if resp.status_code >= 400 or data.get("s") != "ok":
            warnings.append(f"{label}: Fyers candles HTTP {resp.status_code}: "
                            f"{str(data.get('message', ''))[:120]}")
            return None
        candles = data.get("candles") or []
        if not candles:
            warnings.append(f"{label}: Fyers returned no candles")
            return None
        frame = pd.DataFrame(candles, columns=["ts", "Open", "High", "Low",
                                               "Close", "Volume"])
        # epoch stamps are IST session dates — convert before .normalize()
        # or bars stamped at 00:00 IST land on the prior UTC day
        idx = (pd.to_datetime(frame.pop("ts"), unit="s", utc=True)
               .dt.tz_convert("Asia/Kolkata").dt.tz_localize(None).dt.normalize())
        frame.index = pd.DatetimeIndex(idx)
        frame = frame[~frame.index.duplicated(keep="last")].sort_index()
        return frame[frame["Close"].notna()]
    except Exception as exc:
        warnings.append(f"{label}: Fyers candle fetch failed ({exc})")
        return None


def fetch_quotes(tickers: list[str], warnings: list[str]) -> dict[str, dict]:
    """Live LTPs: {ticker: {ltp, change_pct, open, high, low, prev_close,
    volume}}. Batched through /data/quotes; any failure is a warning and the
    affected tickers are simply absent from the result."""
    creds = config.fyers_settings()
    if not creds["app_id"] or not creds["secret_id"]:
        return {}
    token = ensure_token(creds, warnings)
    if token is None:
        return {}
    symbols = resolve_symbols(sorted(set(tickers)), warnings)
    if not symbols:
        return {}
    by_symbol = {v: k for k, v in symbols.items()}
    throttle = _Throttle(config.FYERS_MIN_CALL_GAP)

    out: dict[str, dict] = {}
    syms = list(symbols.values())
    for i in range(0, len(syms), config.FYERS_QUOTE_BATCH):
        chunk = ",".join(syms[i:i + config.FYERS_QUOTE_BATCH])
        throttle.wait()
        try:
            resp = requests.get(f"{config.FYERS_DATA_BASE}/quotes",
                                params={"symbols": chunk},
                                headers={"Authorization": f"{creds['app_id']}:{token}"},
                                timeout=config.FYERS_TIMEOUT)
            if resp.status_code == 401:
                token = ensure_token(creds, warnings, force_refresh=True)
                if token is None:
                    return out
                resp = requests.get(f"{config.FYERS_DATA_BASE}/quotes",
                                    params={"symbols": chunk},
                                    headers={"Authorization": f"{creds['app_id']}:{token}"},
                                    timeout=config.FYERS_TIMEOUT)
            data = resp.json()
            if resp.status_code >= 400 or data.get("s") != "ok":
                warnings.append(f"Fyers quotes HTTP {resp.status_code}: "
                                f"{str(data.get('message', ''))[:120]}")
                continue
        except Exception as exc:
            warnings.append(f"Fyers quotes fetch failed ({exc})")
            continue
        for item in data.get("d") or []:
            ticker = by_symbol.get(item.get("n"))
            values = item.get("v") or {}
            if not ticker or not values.get("lp"):
                continue
            out[ticker] = {
                "ltp": float(values["lp"]),
                "change_pct": values.get("chp"),
                "open": values.get("open_price"),
                "high": values.get("high_price"),
                "low": values.get("low_price"),
                "prev_close": values.get("prev_close_price"),
                "volume": values.get("volume"),
            }
    return out


def _fetch_candles_chunked(symbol: str, app_id: str, token: str,
                           throttle: _Throttle, warnings: list[str], label: str,
                           total_days: int) -> pd.DataFrame | None | str:
    """`total_days` calendar days of daily candles, walked in <=FYERS_HISTORY_DAYS
    chunks (Fyers caps a single /history request at ~366 days) and concatenated.
    Returns a DataFrame, None on total failure, or "AUTH" on an auth error in the
    first chunk. Empty intermediate chunks (e.g. before a symbol's listing date)
    are quietly skipped; only a fully empty span surfaces a warning."""
    cap = config.FYERS_HISTORY_DAYS
    to_date = datetime.now().date()
    cursor = to_date - timedelta(days=max(total_days, 1))
    frames: list[pd.DataFrame] = []
    scratch: list[str] = []
    while cursor <= to_date:
        chunk_to = min(cursor + timedelta(days=cap - 1), to_date)
        df = _fetch_candles(symbol, app_id, token, throttle, scratch, label,
                            from_date=cursor, to_date=chunk_to)
        if isinstance(df, str):  # AUTH — surface immediately so the caller refreshes
            return df
        if isinstance(df, pd.DataFrame) and len(df):
            frames.append(df)
        cursor = chunk_to + timedelta(days=1)
    if not frames:
        warnings.extend(scratch[:1])  # one representative failure, not one per chunk
        return None
    combined = pd.concat(frames)
    return combined[~combined.index.duplicated(keep="last")].sort_index()


def _fan_out(symbols: dict[str, str], creds: dict, token: str,
             warnings: list[str], fetch_one) -> dict[str, pd.DataFrame]:
    """Probe one symbol first so a dead token is refreshed ONCE up front (not by
    N workers all hitting 401 in parallel), then fetch the rest concurrently.
    fetch_one(symbol, token, label) -> DataFrame | None | "AUTH". Tickers with
    fewer than MIN_HISTORY_BARS bars are dropped with a warning."""
    probe_ticker, probe_symbol = next(iter(symbols.items()))
    probe = fetch_one(probe_symbol, token, probe_ticker)
    if isinstance(probe, str):  # AUTH
        token = ensure_token(creds, warnings, force_refresh=True)
        if token is None:
            return {}
        probe = fetch_one(probe_symbol, token, probe_ticker)
        if isinstance(probe, str):
            warnings.append("Fyers: auth still failing after refresh - no data")
            return {}

    out: dict[str, pd.DataFrame] = {}
    if isinstance(probe, pd.DataFrame):
        out[probe_ticker] = probe
    rest = [(t, s) for t, s in symbols.items() if t != probe_ticker]

    def worker(item: tuple[str, str]) -> tuple[str, pd.DataFrame | None]:
        t, s = item
        df = fetch_one(s, token, t)
        if isinstance(df, str):  # rare mid-run auth blip: count as a failure
            warnings.append(f"{t}: Fyers auth error mid-run - skipped")
            return t, None
        return t, df

    with ThreadPoolExecutor(max_workers=config.FYERS_MAX_WORKERS) as pool:
        for t, df in pool.map(worker, rest):
            if df is not None:
                out[t] = df

    for t in list(out):
        if len(out[t]) < config.MIN_HISTORY_BARS:
            warnings.append(f"{t}: only {len(out[t])} Fyers bars - skipped")
            del out[t]
    return out


def fetch_history(tickers: list[str],
                  warnings: list[str]) -> dict[str, pd.DataFrame]:
    """{ticker: DataFrame[Open, High, Low, Close, Volume]} of daily NSE bars
    over the trailing FYERS_HISTORY_DAYS window (the daily-run depth)."""
    creds = config.fyers_settings()
    token = ensure_token(creds, warnings)
    if token is None:
        return {}
    symbols = resolve_symbols(sorted(set(tickers)), warnings)
    if not symbols:
        return {}
    throttle = _Throttle(config.FYERS_MIN_CALL_GAP)
    return _fan_out(
        symbols, creds, token, warnings,
        lambda sym, tok, label: _fetch_candles(sym, creds["app_id"], tok,
                                               throttle, warnings, label))


def fetch_history_range(tickers: list[str], warnings: list[str],
                        days: int) -> dict[str, pd.DataFrame]:
    """{ticker: DataFrame} of daily NSE bars going back ~`days` calendar days,
    chunked to respect Fyers' ~366-day per-request cap. This is the backtester's
    history source: the backtest MUST run on the same real Fyers feed that books
    live trades, so it never falls back to yfinance. An empty return means Fyers
    was unavailable — the caller FAILS LOUD rather than silently substituting a
    different data source."""
    creds = config.fyers_settings()
    token = ensure_token(creds, warnings)
    if token is None:
        return {}
    symbols = resolve_symbols(sorted(set(tickers)), warnings)
    if not symbols:
        return {}
    throttle = _Throttle(config.FYERS_MIN_CALL_GAP)
    return _fan_out(
        symbols, creds, token, warnings,
        lambda sym, tok, label: _fetch_candles_chunked(
            sym, creds["app_id"], tok, throttle, warnings, label, days))
