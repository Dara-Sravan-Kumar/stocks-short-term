"""stockbot daily orchestrator.

Usage:
    python run_daily.py [--no-llm] [--no-discord] [--skip-news] [--top N]

--skip-news is for hourly in-between runs (see run_stockbot.ps1 -SkipNews):
news fetch and fresh LLM sentiment scoring are skipped entirely (the per-slot
sentiment cache still serves anything scored earlier that AM/PM slot), and
the Discord report is only sent if something actually happened this run.
Exits, strategy-fleet evaluation, all channel scans, and paper trading still
run in full every invocation, hourly or not.

Pipeline:
    init db/seed -> batched OHLCV download -> indicators + pivots ->
    news + LLM sentiment (cached) -> exit evaluation of ACTIVE picks ->
    strategy fleet evaluation (retire/create/capital weights) ->
    new-pick scan -> holdings health check -> rich dashboard ->
    Discord alerts -> run log
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from dotenv import load_dotenv

import config
from stockbot import db, market_calendar, market_data, news, sentiment, signals
from stockbot import exits, portfolio
from stockbot import dashboard, discord_alerts, paper, broker, strategy_engine
from stockbot.indicators import compute_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="NSE short-term stockbot daily run")
    parser.add_argument("--no-llm", action="store_true",
                        help="skip Claude CLI sentiment (neutral scores)")
    parser.add_argument("--no-discord", action="store_true",
                        help="skip Discord alerts")
    parser.add_argument("--skip-news", action="store_true",
                        help="skip news fetch + fresh LLM sentiment (hourly in-between runs); "
                             "Discord only sends if this run had actual activity")
    parser.add_argument("--top", type=int, default=config.MAX_NEW_PICKS_PER_DAY,
                        help="max new picks per day")
    parser.add_argument("--refresh-universe", action="store_true",
                        help="force re-fetch of NSE index constituents")
    parser.add_argument("--force", action="store_true",
                        help="run even outside the weekday run window")
    args = parser.parse_args()

    load_dotenv(config.PROJECT_ROOT / ".env")

    now = market_calendar.now_ist()
    if not market_calendar.is_run_window(now) and not args.force:
        print(f"Outside NSE run window ({now:%a %H:%M} IST, window "
              f"{config.RUN_WINDOW_OPEN}-{config.RUN_WINDOW_CLOSE} Mon-Fri) - "
              "skipping run. Use --force to run anyway.")
        return 0

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
    print("Syncing holdings from broker (Fyers)...")
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
    provider_meta: dict = {}
    histories = market_data.fetch_history(universe, warnings, provider_out=provider_meta)
    if not histories:
        print("FATAL: no market data available - aborting run.")
        return 1
    data_provider = provider_meta.get("provider")
    print(f"Data provider: {data_provider}")
    data_date = market_data.latest_bar_date(histories)

    # Paper positions may only be OPENED / CLOSED / MARKED on a run whose data
    # source is Fyers (real NSE broker data). FYERS and FYERS+YFINANCE (Fyers
    # with a minor yfinance gap-fill) count as real; a pure YFINANCE fallback run
    # FREEZES the book — signals are still scanned and alerted, but no position is
    # opened, closed, or marked, and the equity curve is not written. This mirrors
    # mcxbot's mcx_live gate: the book only ever moves on the real broker feed, so
    # a degraded-data run can never phantom-book or mis-mark it.
    book_live = paper.books_on_provider(data_provider)
    if not book_live:
        warnings.append(
            f"Paper book FROZEN this run: no real Fyers data (provider "
            f"{data_provider}) — run fyers_login.py so real NSE prices book "
            "trades. Signals still scanned/alerted; no positions opened, closed, "
            "or marked, and the equity curve is not written this run.")
        print("Paper book FROZEN — provider is not Fyers; scanning/alerting only.")
        # Nudge the operator to run the daily Fyers login (hourly-throttled so the
        # 30-min scans don't spam the channel). Respects --no-discord.
        if not args.no_discord:
            reminder = discord_alerts.send_login_reminder(warnings)
            print(f"Fyers login reminder: {reminder}")

    snapshots = {}
    for ticker, df in histories.items():
        try:
            snapshots[ticker] = compute_snapshot(ticker, df, config.MACD_CROSS_LOOKBACK)
        except Exception as exc:
            warnings.append(f"{ticker}: indicator computation failed ({exc}) - skipped")

    # ------------------------------------------------------- news + sentiment
    from stockbot.signals import _passes_technicals, _passes_pullback
    # Screened against each channel's default/seed thresholds — a quick net-widener
    # for sentiment scoring, not the actual per-variant gate the picks scan applies.
    _tech_defaults = strategy_engine.resolve_params("TECHNICAL", None)
    _pullback_defaults = strategy_engine.resolve_params("PULLBACK", None)
    screen_survivors = [
        t for t in config.WATCHLIST
        if t in snapshots and t not in active
        and (_passes_technicals(snapshots[t], _tech_defaults)
             or _passes_pullback(snapshots[t], _pullback_defaults))
    ]

    # Health signals for the Discord failure alert (populated only on full runs;
    # the 30-min --skip-news scans neither fetch news nor call the LLM, so they
    # stay quiet by construction — failures ping on the scheduled AM/PM runs).
    news_failure: str | None = None
    llm_failure: str | None = None
    if args.skip_news:
        # Hourly in-between run: no news fetch, no fresh LLM calls. Still look
        # up whatever's cached from this AM/PM slot's real news run for the
        # tickers gates/exits care about right now.
        print("Skipping news fetch (--skip-news) - using cached sentiment only...")
        sentiment_universe = sorted(set(screen_survivors) | set(active) | set(held))
        headlines = {t: [] for t in sentiment_universe}
        use_llm = False
    else:
        # News-first channel sweeps the more-liquid names only (NEWS_MIN_AVG_TURNOVER,
        # not the wider ₹2cr entry gate): news+LLM scoring is the expensive part of a
        # run, and microcaps rarely have the coverage the NEWS channel needs. The 8
        # chart channels still scan the full universe (sentiment is a neutral-pass veto).
        liquid = [t for t, s in snapshots.items()
                  if s.avg_turnover_20d >= config.NEWS_MIN_AVG_TURNOVER]
        news_universe = sorted(set(liquid) | set(active) | set(held))
        print(f"Collecting news for {len(news_universe)} liquid tickers "
              f"(of {len(universe)} scanned, parallel)...")
        headlines = news.fetch_headlines_bulk(news_universe, warnings)

        with_news = [t for t, hl in headlines.items() if hl]
        # A total blackout (asked many liquid names, got zero headlines from BOTH
        # Yahoo Finance and Google News RSS) means the news sources are down —
        # sentiment then scores on empty inputs. Partial gaps are normal and don't
        # alert; only a full outage does.
        if len(news_universe) >= 5 and not with_news:
            news_failure = (f"News blackout: 0 of {len(news_universe)} liquid tickers "
                            "returned any headline (Yahoo Finance + Google News RSS "
                            "both empty). Sentiment ran on empty inputs this run.")
            print(f"  WARNING: {news_failure}")
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
    elif args.skip_news:
        llm_status = "cached (news skipped)"
    elif llm_sources == {"neutral_fallback"} and use_llm:
        llm_status = "FALLBACK (neutral)"
    elif "claude_cli" in llm_sources:
        llm_status = f"Claude CLI ({config.SENTIMENT_MODEL})"
    else:
        llm_status = "neutral (--no-llm)" if not use_llm else "cached"
    # LLM outright failed if every ticker fell back to neutral on a run that
    # meant to score with the CLI (claude missing / all calls errored/timed out).
    if use_llm and llm_status == "FALLBACK (neutral)":
        llm_failure = ("LLM sentiment fell back to NEUTRAL for every ticker — the "
                       "Claude CLI is missing on PATH or every call failed/timed out. "
                       "Picks were scored on neutral sentiment this run.")
        print(f"  WARNING: {llm_failure}")

    # ------------------------------------------------------------- exit logic
    print("Evaluating active picks for exit signals...")
    closed = exits.evaluate_active_picks(conn, snapshots, histories, sentiments,
                                         run_date, warnings)
    # paper exits BEFORE new entries so freed cash is available for sizing —
    # only on a real-Fyers run (see book_live gate above); frozen otherwise.
    paper_exits = (paper.close_positions_for_exits(conn, closed, run_date,
                                                   run_slot, warnings)
                   if book_live else [])

    # ----------------------------------------------------------- strategy fleet
    # Retire underperforming/stalled TECHNICAL & PULLBACK variants (backfilling
    # immediately), add a weekly wildcard slot, flag graduate candidates, and
    # compute today's per-strategy capital weights + market context.
    print("Evaluating strategy fleet...")
    strategy_ledger = strategy_engine.evaluate_and_evolve(
        conn, run_date, run_slot, sentiments, warnings, use_llm=use_llm)
    for event in strategy_ledger["events"]:
        if event["type"] == "retired":
            print(f"  Retired {event['variant_key']}: {event['reason']}")
        elif event["type"] == "created":
            print(f"  Created {event['variant_key']} (replacing {event['parent']})")
        elif event["type"] == "wildcard_created":
            print(f"  Wildcard variant {event['variant_key']} added")
        elif event["type"] == "graduate_candidate":
            print(f"  {event['variant_key']} flagged as a graduate candidate")

    # ------------------------------------------------------------- new picks
    # Channel B first (news is the primary channel), then A, then C (pullback).
    n_tech = len(strategy_ledger["active_by_channel"]["TECHNICAL"])
    n_pullback = len(strategy_ledger["active_by_channel"]["PULLBACK"])
    print("Scanning for news-catalyst picks (Channel B)...")
    news_picks = signals.scan_news_picks(conn, snapshots, sentiments, run_date, warnings)
    print(f"Scanning for technical picks (Channel A, {n_tech} active variant(s))...")
    tech_picks = signals.scan_new_picks(conn, snapshots, sentiments, run_date,
                                        warnings, args.top)
    print(f"Scanning for pullback picks (Channel C, {n_pullback} active variant(s))...")
    pullback_picks = signals.scan_pullback_picks(conn, snapshots, sentiments,
                                                 run_date, warnings)

    # Six SMC/volume-concept channels — all daily-bar proxies (see
    # stockbot/indicators.py), each evaluated against its own active fleet.
    smc_channel_scans = [
        ("ORDERFLOW", "order flow", signals.scan_orderflow_picks),
        ("LIQUIDITY_SWEEP", "liquidity sweep", signals.scan_liquidity_sweep_picks),
        ("FVG", "fair value gap", signals.scan_fvg_picks),
        ("ANCHORED_VWAP", "anchored VWAP", signals.scan_anchored_vwap_picks),
        ("VOLUME_PROFILE", "volume profile", signals.scan_volume_profile_picks),
        ("BREAKOUT_52W", "52-week breakout", signals.scan_breakout_52w_picks),
    ]
    smc_picks: list[dict] = []
    for channel, label, scan_fn in smc_channel_scans:
        n_variants = len(strategy_ledger["active_by_channel"][channel])
        print(f"Scanning for {label} picks ({channel}, {n_variants} active variant(s))...")
        smc_picks += scan_fn(conn, snapshots, sentiments, run_date, warnings)

    # DISCOVERED: data-driven spec variants sourced by strategy_discovery (web
    # discoverer / mixer), each carrying its own entry expression.
    n_discovered = len(strategy_ledger["active_by_channel"].get("DISCOVERED", []))
    print(f"Scanning for discovered-spec picks (DISCOVERED, {n_discovered} active variant(s))...")
    discovered_picks = signals.scan_discovered_picks(conn, snapshots, sentiments,
                                                     run_date, warnings)

    new_picks = news_picks + tech_picks + pullback_picks + smc_picks + discovered_picks
    paper_entries = (paper.open_positions_for_picks(
        conn, new_picks, snapshots, run_date, run_slot, warnings,
        capital_weights=strategy_ledger["capital_weights"])
        if book_live else [])
    # mirror paper orders into OpenAlgo's sandbox UI (SELLs first, then BUYs;
    # hard-gated on analyzer mode so nothing can reach the real broker)
    mirrored = broker.mirror_paper_orders(paper_exits + paper_entries, warnings)
    if mirrored:
        print(f"Mirrored {mirrored} paper order(s) to OpenAlgo sandbox")

    # LIVE mode: mirror paper actions into real Fyers orders. No-op unless
    # TRADING_MODE=LIVE in .env; even then PLACE_ORDER_ENABLED must also be
    # True or every order is merely recorded as BLOCKED in live_trades.
    live = broker.execute_live_orders(conn, paper_exits + paper_entries,
                                      run_date, run_slot, warnings)
    if live["submitted"] or live["failed"]:
        print(f"LIVE orders (Fyers): {live['submitted']} submitted, "
              f"{live['failed']} failed")
    elif live["blocked"]:
        print(f"LIVE mode: {live['blocked']} order(s) blocked - "
              "PLACE_ORDER_ENABLED is False")

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
    # On frozen (non-Fyers) runs, value the book at entry fills (no snapshots) and
    # do NOT write the equity curve — fallback prices must never move the book.
    paper_book = (paper.mark_to_market(conn, snapshots, run_date, run_slot)
                  if book_live
                  else paper.mark_to_market(conn, {}, run_date, run_slot, write=False))
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
    # Health/failure ping first — news outage or LLM fallback on a scheduled full
    # run gets its own red alert (login failures are pinged separately, hourly,
    # up at the book-frozen check). Independent of the picks/activity gate below.
    failures = []
    if news_failure:
        failures.append({"kind": "News fetch", "detail": news_failure})
    if llm_failure:
        failures.append({"kind": "LLM sentiment", "detail": llm_failure})
    if failures and not args.no_discord:
        fstatus = discord_alerts.send_failure_alert(run_date, run_slot, failures, warnings)
        print(f"Failure alert: {fstatus}")

    has_activity = discord_alerts.has_reportable_activity(
        closed, new_picks, paper_entries, paper_exits, strategy_ledger["events"])
    if args.no_discord:
        discord_status = "skipped (--no-discord)"
    elif args.skip_news and not has_activity:
        discord_status = "skipped (quiet - no activity)"
    else:
        print("Sending Discord alerts...")
        if holdings_sync["source"] in ("FYERS", "OPENALGO"):
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
            holdings_note=holdings_note, strategy_ledger=strategy_ledger)

    # -------------------------------------------------------------- dashboard
    dashboard.render(
        run_date=run_date, data_date=data_date, tickers_scanned=len(histories),
        llm_status=llm_status, exits=closed, active_picks=active_rows,
        new_picks=new_picks, holdings=holdings_report, stats=stats,
        warnings=warnings, discord_status=discord_status,
        paper_entries=paper_entries, paper_exits=paper_exits, paper_book=paper_book,
        holdings_provenance=holdings_provenance,
    )

    # ----------------------------------------------------- daily strategy R&D
    # Once a day, on the evening heavy run, discover published strategies and
    # breed genetic-mixer offspring — each gated on an out-of-sample backtest
    # before it can join the DISCOVERED fleet. Runs at the tail (Discord/
    # dashboard already out), reuses the histories already fetched, and is fully
    # guarded so a discovery failure can never break the trading run. New specs
    # are live for the next morning's run.
    if run_slot == "PM" and not args.skip_news and use_llm and book_live:
        try:
            from stockbot import postmortem, strategy_discovery, strategy_mixer
            print("Running daily trade post-mortem + strategy discovery + mixing (Fyers data)...")
            pm = postmortem.analyze_recent_trades(conn, warnings)
            if pm["lessons"]:
                print(f"  Post-mortem ({pm['reviewed']} trades): {pm['diagnosis']}")
                for lesson in pm["lessons"]:
                    print(f"    - {lesson}")
            disc = strategy_discovery.discover_and_register(
                conn, histories, warnings, lessons=pm["lessons"])
            mix = strategy_mixer.mix_and_register(conn, histories, warnings)
            print(f"  Discovery: {len(disc['registered'])}/{disc['proposed']} registered; "
                  f"Mixer: {len(mix['registered'])}/{mix['proposed']} registered")
        except Exception as exc:
            warnings.append(f"strategy discovery/mixing failed: {exc}")

    db.log_run(conn, run_date, started_at, datetime.now().isoformat(timespec="seconds"),
               len(histories), len(new_picks), len(closed), warnings,
               provider=data_provider)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
