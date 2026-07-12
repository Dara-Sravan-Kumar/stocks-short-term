"""Backtest the strategy fleet over real historical daily bars.

Usage:
    python backtest.py [--days 120] [--tickers N] [--channels A,B]
                       [--capital 100000] [--seeds-only]

Replays the last --days trading sessions of the whole scan universe through
the live gate/exit/cost code (see stockbot/backtest.py for fidelity notes:
sentiment is neutral, NEWS channel and fundamentals checks are not replayed).
Prints a per-variant scorecard and writes the full result (including every
trade) to data/backtests/.
"""
from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

import config
from stockbot import backtest


def main() -> int:
    parser = argparse.ArgumentParser(description="stockbot strategy backtest")
    parser.add_argument("--days", type=int, default=120,
                        help="trading sessions to replay (default 120)")
    parser.add_argument("--tickers", type=int, default=None,
                        help="cap universe size for a faster run")
    parser.add_argument("--channels", type=str, default=None,
                        help="comma list, e.g. TECHNICAL,PULLBACK (default all)")
    parser.add_argument("--capital", type=float, default=100_000.0,
                        help="notional per trade in INR (default 100000)")
    parser.add_argument("--seeds-only", action="store_true",
                        help="test channel seed defaults only, skip DB variants")
    args = parser.parse_args()

    load_dotenv(config.PROJECT_ROOT / ".env")
    warnings: list[str] = []

    channels = args.channels.upper().split(",") if args.channels else None
    print(f"Backtest: up to {args.tickers or 'all'} tickers x {args.days} sessions, "
          f"Rs {args.capital:,.0f}/trade")
    payload = backtest.run_and_save(
        days=args.days, tickers_cap=args.tickers, channels=channels,
        capital=args.capital, seeds_only=args.seeds_only, warnings=warnings,
        progress=print)
    if payload.get("error"):
        print("FATAL:", payload["error"])
        return 1
    results = payload["results"]

    # ---------------------------------------------------------------- report
    rows = sorted(results.items(),
                  key=lambda kv: kv[1]["net_inr"], reverse=True)
    hdr = (f"{'variant':28s} {'trades':>6s} {'win%':>6s} {'net Rs':>12s} "
           f"{'avg%':>6s} {'PF':>5s} {'hold':>5s} {'maxDD':>10s} {'grad':>4s}")
    print("\n" + hdr)
    print("-" * len(hdr))
    for key, r in rows:
        print(f"{key:28.28s} {r['trades']:>6d} "
              f"{r['win_rate_pct'] if r['win_rate_pct'] is not None else '-':>6} "
              f"{r['net_inr']:>12,.0f} "
              f"{r['avg_pnl_pct'] if r['avg_pnl_pct'] is not None else '-':>6} "
              f"{r['profit_factor'] if r['profit_factor'] is not None else '-':>5} "
              f"{r['avg_hold_bars'] if r['avg_hold_bars'] is not None else '-':>5} "
              f"{r['max_drawdown_inr']:>10,.0f} "
              f"{'YES' if r['meets_graduation_gate'] else '-':>4s}")

    for w in warnings[:10]:
        print("WARN:", w)
    print(f"\nFull results (incl. every trade) -> {payload['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
