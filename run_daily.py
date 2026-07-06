"""stockbot daily orchestrator.

Usage:
    python run_daily.py [--no-llm] [--no-discord] [--top N]

Pipeline:
    init db/seed -> batched OHLCV download -> indicators + pivots ->
    news + LLM sentiment (cached) -> exit evaluation of ACTIVE picks ->
    new-pick scan -> holdings health check -> rich dashboard ->
    Discord alerts -> run log
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from dotenv import load_dotenv

import config
from stockbot import db, market_data, news, sentiment, signals, exits, portfolio
from stockbot import dashboard, discord_alerts, paper, broker
from stockbot.indicators import compute_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="NSE short-term stockbot daily run")
    parser.add_argument("--no-llm", action="store_true",
                        help="skip Claude CLI sentiment (neutral scores)")
    parser.add_argument("--no-discord", action="store_true",
                        help="skip Discord alerts")
    parser.add_argument("--top", type=int, default=config.MAX_NEW_PICKS_PER_DAY,
                        help="max new picks per day")
    parser.add_argument("--refresh-universe", action="store_true",
                        help="force re-fetch of NSE index constituents")
    args = parser.parse_args()

    load_dotenv(config.PROJECT_ROOT / ".env")

    started_at = datetime.now().isoformat(timespec="seconds")
    run_date = market_data.today_ist()
    run_slot = "AM" if datetime.now(market_data.IST).hour < 12 else "PM"
    # AM and PM runs each score fresh news; cache is per (ticker, date+slot)
    sentiment_key = f"{run_date}:{run_slot}"
    warnings: list[str] = []

    conn = db.connect()
    if db.ensure_paper_book(conn):
        warnings.append(f"Paper book created with Rs {config.PAPER_STARTING_CASH:,.0f} "
                        "virtual cash")
    print("Syncing holdings from broker (OpenAlgo)...")
    holdings_sync = broker.sync_holdings(conn, warnings)
    print(f"Holdings: {holdings_sync['count']} positions "
          f"(source: {holdings_sync['source']}"
          f"{', STALE' if holdings_sync['stale'] else ''})")

    # -------------------------------------------------------------- universe
    from stockbot import universe as universe_mod
    print("Loading scan universe from NSE index constituents...")
    uni = universe_mod.load(conn, warnings, force_refresh=args.refresh_universe)
    universe_mod.apply(uni)
    print(f"Universe: {len(config.WATCHLIST)} tickers "
          f"(source: {uni['source']}; "
          f"LARGE {sum(1 for v in config.TIER.values() if v == 'LARGE')}, "
          f"MID {sum(1 for v in config.TIER.values() if v == 'MID')}, "
          f"SMALL {sum(1 for v in config.TIER.values() if v == 'SMALL')})")

    # ------------------------------------------------------------------ data
    active = [p["ticker"] for p in db.get_active_picks(conn)]
    held = [h["ticker"] for h in db.get_holdings(conn)]
    universe = sorted(set(config.WATCHLIST) | set(active) | set(held))

    print(f"Fetching daily history for {len(universe)} NSE tickers...")
    histories = market_data.fetch_history(universe, warnings)
    if not histories:
        print("FATAL: no market data available - aborting run.")
        return 1
    data_date = market_data.latest_bar_date(histories)

    snapshots = {}
    for ticker, df in histories.items():
        try:
            snapshots[ticker] = compute_snapshot(ticker, df, config.MACD_CROSS_LOOKBACK)
        except Exception as exc:
            warnings.append(f"{ticker}: indicator computation failed ({exc}) - skipped")

    # ------------------------------------------------------- news + sentiment
    # News-first channel sweeps every LIQUID ticker (illiquid names could
    # never become picks, so their news is skipped to bound runtime).
    from stockbot.signals import _is_liquid
    liquid = [t for t, s in snapshots.items() if _is_liquid(s)]
    news_universe = sorted(set(liquid) | set(active) | set(held))
    print(f"Collecting news for {len(news_universe)} liquid tickers "
          f"(of {len(universe)} scanned, parallel)...")
    headlines = news.fetch_headlines_bulk(news_universe, warnings)

    # Score sentiment for: tickers with fresh headlines (news channel),
    # plus technical/pullback-screen survivors, active picks, and holdings.
    from stockbot.signals import _passes_technicals, _passes_pullback  # internal reuse
    screen_survivors = [
        t for t in config.WATCHLIST
        if t in snapshots and t not in active
        and (_passes_technicals(snapshots[t]) or _passes_pullback(snapshots[t]))
    ]
    with_news = [t for t, hl in headlines.items() if hl]
    sentiment_universe = sorted(set(with_news) | set(screen_survivors)
                                | set(active) | set(held))
    headlines = {t: headlines.get(t, []) for t in sentiment_universe}

    use_llm = not args.no_llm
    if use_llm:
        print(f"Scoring sentiment for {len(headlines)} tickers via Claude CLI "
              f"(cached per {run_slot} run)...")
    sentiments = sentiment.score_tickers(conn, sentiment_key, headlines, warnings, use_llm)
    llm_sources = {s["source"] for s in sentiments.values()}
    if not sentiments:
        llm_status = "no tickers"
    elif llm_sources == {"neutral_fallback"} and use_llm:
        llm_status = "FALLBACK (neutral)"
    elif "claude_cli" in llm_sources:
        llm_status = f"Claude CLI ({config.SENTIMENT_MODEL})"
    else:
        llm_status = "neutral (--no-llm)" if not use_llm else "cached"

    # ------------------------------------------------------------- exit logic
    print("Evaluating active picks for exit signals...")
    closed = exits.evaluate_active_picks(conn, snapshots, histories, sentiments,
                                         run_date, warnings)
    # paper exits BEFORE new entries so freed cash is available for sizing
    paper_exits = paper.close_positions_for_exits(conn, closed, run_date,
                                                  run_slot, warnings)

    # ------------------------------------------------------------- new picks
    # Channel B first (news is the primary channel), then A, then C (pullback).
    print("Scanning for news-catalyst picks (Channel B)...")
    news_picks = signals.scan_news_picks(conn, snapshots, sentiments, run_date, warnings)
    print("Scanning for technical picks (Channel A)...")
    tech_picks = signals.scan_new_picks(conn, snapshots, sentiments, run_date,
                                        warnings, args.top)
    print("Scanning for pullback picks (Channel C)...")
    pullback_picks = signals.scan_pullback_picks(conn, snapshots, sentiments,
                                                 run_date, warnings)
    new_picks = news_picks + tech_picks + pullback_picks
    paper_entries = paper.open_positions_for_picks(conn, new_picks, snapshots,
                                                   run_date, run_slot, warnings)
    # mirror paper orders into OpenAlgo's sandbox UI (SELLs first, then BUYs;
    # hard-gated on analyzer mode so nothing can reach the real broker)
    mirrored = broker.mirror_paper_orders(paper_exits + paper_entries, warnings)
    if mirrored:
        print(f"Mirrored {mirrored} paper order(s) to OpenAlgo sandbox")

    # -------------------------------------------------------- active overview
    active_rows = []
    for p in db.get_active_picks(conn):
        snap = snapshots.get(p["ticker"])
        row = dict(p)
        if snap:
            row["ltp"] = round(snap.close, 2)
            row["pct_to_target"] = round((p["target_price"] - snap.close) / snap.close * 100, 2)
            row["pct_to_stop"] = round((p["stop_price"] - snap.close) / snap.close * 100, 2)
        row["sentiment"] = (sentiments.get(p["ticker"]) or {}).get("score")
        active_rows.append(row)

    # ------------------------------------------------------ holdings + stats
    print("Running holdings health check...")
    holdings_report = portfolio.health_check(conn, snapshots, sentiments, warnings)
    stats = db.get_closed_picks_stats(conn)
    paper_book = paper.mark_to_market(conn, snapshots, run_date, run_slot)
    holdings_provenance = db.get_holdings_provenance(conn)

    # ------------------------------------------------- tracking history log
    # Store per-run snapshots: price, return %, sentiment, and news catalyst
    # for every tracked pick and holding (time series in tracking_log).
    for p in active_rows:
        sent_info = sentiments.get(p["ticker"]) or {}
        ret = (round((p["ltp"] - p["entry_price"]) / p["entry_price"] * 100, 2)
               if p.get("ltp") else None)
        db.upsert_tracking(
            conn, run_date, run_slot, "PICK", p["ticker"],
            p.get("channel", "TECHNICAL"), p.get("ltp"), ret,
            sent_info.get("score"), sent_info.get("summary"),
            f"ACTIVE since {p['entry_date']}; target {p['target_price']}, stop {p['stop_price']}",
        )
    for e in closed:
        sent_info = sentiments.get(e["ticker"]) or {}
        db.upsert_tracking(
            conn, run_date, run_slot, "PICK", e["ticker"], None,
            e["exit_price"], e["pnl_pct"], sent_info.get("score"),
            sent_info.get("summary"), f"EXIT {e['status']}: {e['exit_reason']}",
        )
    for h in holdings_report:
        sent_info = sentiments.get(h["ticker"]) or {}
        db.upsert_tracking(
            conn, run_date, run_slot, "HOLDING", h["ticker"], None,
            h.get("ltp"), h.get("pnl_pct"), sent_info.get("score"),
            sent_info.get("summary"), h["signal"],
        )
    for pp in paper_book["open_positions"]:
        sent_info = sentiments.get(pp["ticker"]) or {}
        ret = round((pp["ltp"] - pp["entry_fill_price"])
                    / pp["entry_fill_price"] * 100, 2)
        db.upsert_tracking(
            conn, run_date, run_slot, "PAPER", pp["ticker"], pp["strategy"],
            pp["ltp"], ret, sent_info.get("score"), sent_info.get("summary"),
            f"{pp['qty']} sh @ {pp['entry_fill_price']:.2f} since {pp['entry_date']}; "
            f"unrl Rs {pp['unrealized_pnl']:+,.0f}",
        )

    # --------------------------------------------------------------- discord
    if args.no_discord:
        discord_status = "skipped (--no-discord)"
    else:
        print("Sending Discord alerts...")
        if holdings_sync["source"] == "OPENALGO":
            holdings_note = f"live sync {holdings_sync['synced_at'][:16]}"
        elif holdings_sync["stale"]:
            holdings_note = "STALE SNAPSHOT - refresh IndMoney token"
        elif holdings_sync["source"] == "MOCK":
            holdings_note = "mock data"
        else:
            holdings_note = f"cached {holdings_sync['synced_at'] or 'never'}"
        discord_status = discord_alerts.send_report(
            run_date, closed, new_picks, active_rows, holdings_report, stats, warnings,
            paper_entries=paper_entries, paper_exits=paper_exits, paper_book=paper_book,
            holdings_note=holdings_note)

    # -------------------------------------------------------------- dashboard
    dashboard.render(
        run_date=run_date, data_date=data_date, tickers_scanned=len(histories),
        llm_status=llm_status, exits=closed, active_picks=active_rows,
        new_picks=new_picks, holdings=holdings_report, stats=stats,
        warnings=warnings, discord_status=discord_status,
        paper_entries=paper_entries, paper_exits=paper_exits, paper_book=paper_book,
        holdings_provenance=holdings_provenance,
    )

    db.log_run(conn, run_date, started_at, datetime.now().isoformat(timespec="seconds"),
               len(histories), len(new_picks), len(closed), warnings)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
