"""NSE run-window gating and IST helpers (port of mcxbot's market_calendar).

NSE trades Mon-Fri 09:15-15:30 IST, but the bot's day is wider: an 08:45
pre-open news run and an 18:30 post-close run bookend the hourly in-between
scans. is_run_window() therefore checks RUN_WINDOW_OPEN..RUN_WINDOW_CLOSE on
weekdays rather than exchange hours. NSE holidays are not tracked — a holiday
run is a cheap no-op (stale bars, nothing to do), unlike weekend/night runs
which used to fire ~9 times per weekend for nothing.
"""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import config

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist() -> str:
    return now_ist().strftime("%Y-%m-%d")


def _parse_hhmm(hhmm: str) -> time:
    hhmm = hhmm.replace(":", "")
    return time(int(hhmm[:2]), int(hhmm[-2:]))


def is_run_window(now: datetime | None = None) -> bool:
    """True Mon-Fri between RUN_WINDOW_OPEN and RUN_WINDOW_CLOSE IST."""
    now = now or now_ist()
    if now.weekday() >= 5:  # Sat/Sun
        return False
    return (_parse_hhmm(config.RUN_WINDOW_OPEN)
            <= now.time()
            <= _parse_hhmm(config.RUN_WINDOW_CLOSE))
