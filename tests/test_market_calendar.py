"""NSE run-window gating (weekday 08:30-18:45 IST covers the 08:45 news run,
hourly scans, and the 18:30 post-close run; weekends and nights are blocked)."""
from datetime import datetime

from stockbot.market_calendar import IST, is_run_window


def _ist(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def test_run_window_covers_all_scheduled_weekday_runs():
    assert is_run_window(_ist(2026, 7, 8, 8, 45))    # Wed pre-open news run
    assert is_run_window(_ist(2026, 7, 8, 9, 30))    # first hourly scan
    assert is_run_window(_ist(2026, 7, 8, 15, 30))   # last hourly scan
    assert is_run_window(_ist(2026, 7, 8, 18, 30))   # post-close news run


def test_run_window_blocks_nights():
    assert not is_run_window(_ist(2026, 7, 8, 8, 29))   # before window
    assert not is_run_window(_ist(2026, 7, 8, 18, 46))  # after window
    assert not is_run_window(_ist(2026, 7, 8, 23, 0))   # night
    assert not is_run_window(_ist(2026, 7, 8, 3, 0))    # early morning


def test_run_window_blocks_weekends():
    assert not is_run_window(_ist(2026, 7, 11, 10, 0))  # Sat mid-window time
    assert not is_run_window(_ist(2026, 7, 12, 18, 30)) # Sun at PM run time


def test_window_boundaries_inclusive():
    assert is_run_window(_ist(2026, 7, 8, 8, 30))
    assert is_run_window(_ist(2026, 7, 8, 18, 45))
