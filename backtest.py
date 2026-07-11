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
import json
import sys
from datetime import datetime

from dotenv import load_dotenv

import config
from stockbot import backtest, db, market_data


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

    conn = db.connect()
    from stockbot import universe as universe_mod
    uni = universe_mod.load(conn, warnings)
    universe_mod.apply(uni)
    tickers = sorted(config.WATCHLIST)
    if args.tickers:
        tickers = tickers[:args.tickers]

    channels = args.channels.upper().split(",") if args.channels else None
    variants = backtest.build_variant_list(None if args.seeds_only else conn,
                                           channels)
    print(f"Backtest: {len(tickers)} tickers x {args.days} sessions, "
          f"{len(variants)} variants, Rs {args.capital:,.0f}/trade")

    print(f"Fetching history for {len(tickers)} tickers...")
    histories = market_data.fetch_history(tickers, warnings)
    if not histories:
        print("FATAL: no market data")
        return 1
    print(f"Replaying {args.days} sessions over {len(histories)} tickers...")
    results = backtest.run_backtest(histories, variants, args.days,
                                    args.capital, progress=print)

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

    out_dir = config.DATA_DIR / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"backtest_{stamp}.json"
    out_path.write_text(json.dumps(
        {"run_at": stamp, "days": args.days, "tickers": len(histories),
         "capital_per_trade": args.capital, "results": results},
        indent=2, default=str), encoding="utf-8")
    print(f"\nFull results (incl. every trade) -> {out_path}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
