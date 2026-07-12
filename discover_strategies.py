"""Daily strategy discovery.

Proposes published SWING strategies (via Claude CLI), translates each into a
safe data-driven spec over the indicators the bot already computes, gates every
one on an out-of-sample historical backtest, and registers the survivors into
the DISCOVERED fleet — where the live scan trades them and the retire/graduate
machinery judges them on real paper outcomes.

Runnable manually (`python discover_strategies.py`) or as a once-daily scheduled
task, mirroring run_stockbot.ps1. Safe to run repeatedly: duplicates and
below-bar proposals are simply rejected.
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

import config
from stockbot import db, market_data, strategy_discovery


def main() -> int:
    load_dotenv(config.PROJECT_ROOT / ".env")
    conn = db.connect()
    warnings: list[str] = []

    from stockbot import universe as universe_mod
    uni = universe_mod.load(conn, warnings)
    universe_mod.apply(uni)
    print(f"Discovery universe: {len(config.WATCHLIST)} tickers "
          f"(existing DISCOVERED variants: "
          f"{len(db.get_active_strategies(conn, channel='DISCOVERED'))})")

    print("Fetching daily history for the backtest gate...")
    histories = market_data.fetch_history(sorted(set(config.WATCHLIST)), warnings)
    if not histories:
        print("FATAL: no market data - aborting discovery.")
        return 1

    print("Asking Claude for candidate published swing strategies...")
    report = strategy_discovery.discover_and_register(conn, histories, warnings)

    print(f"\nProposed: {report['proposed']}")
    print(f"Registered ({len(report['registered'])}):")
    for r in report["registered"]:
        print(f"  + {r['name']}: {r['entry_expr']}")
        if r.get("rationale"):
            print(f"      {r['rationale']}")
    print(f"Rejected ({len(report['rejected'])}):")
    for r in report["rejected"]:
        print(f"  - {r['name']} [{r['stage']}]: {r['reason']}")
    if warnings:
        print("Warnings:")
        for w in warnings:
            print("  ! " + w)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
