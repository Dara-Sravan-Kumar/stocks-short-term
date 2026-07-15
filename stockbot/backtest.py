"""Historical replay of the strategy fleet over real daily bars.

Faithfulness first: entries go through the SAME per-channel gate functions,
pivot-ladder target/stop derivation, reward:risk floors and ranking scores as
the live scan (stockbot/signals.py), and exits through the same rule ladder
as stockbot/exits.py (stop -> target -> setup-broken -> expired). Fills use
the paper book's slippage and full Indian delivery cost model, so reported
P&L is net of charges.

Deliberate differences from live (documented, not hidden):
- No news/sentiment history exists, so sentiment is neutral 0.0 everywhere:
  the NEWS channel can't be replayed, sentiment entry vetoes pass, and the
  sentiment-breakdown exit never fires.
- Fundamentals checks are skipped (no as-of-date fundamentals source).
- Each variant trades an independent book (live variants compete for
  tickers), so per-variant stats are uncontaminated by fleet interactions.

Day loop: snapshots for day D are computed from bars up to and including D,
shared across all variants; exits are evaluated before entries (live order);
positions opened on D are exit-eligible from D+1.
"""
from __future__ import annotations

import json
from datetime import datetime

import pandas as pd

import config
from stockbot import paper, strategy_engine
from stockbot.indicators import Snapshot, compute_snapshot, derive_target_stop
from stockbot.signals import (
    _passes_anchored_vwap, _passes_breakout_52w, _passes_fvg,
    _passes_liquidity_sweep, _passes_orderflow, _passes_pullback,
    _passes_spec, _passes_technicals, _passes_volume_profile, _pullback_score,
    _rank_score, build_toggle_context,
)

# (gate_fn, reward:risk params key, ranking score, daily-cap config name) per
# replayable channel — the same wiring each scan_*_picks passes _scan_channel
# live. NEWS is absent by design (needs real news history).
CHANNEL_SPECS = {
    "TECHNICAL": (_passes_technicals, "min_reward_risk", _rank_score,
                  "MAX_NEW_PICKS_PER_DAY"),
    "PULLBACK": (_passes_pullback, "pullback_min_reward_risk", _pullback_score,
                 "MAX_PULLBACK_PICKS_PER_DAY"),
    "ORDERFLOW": (_passes_orderflow, "min_reward_risk", _rank_score,
                  "MAX_ORDERFLOW_PICKS_PER_DAY"),
    "LIQUIDITY_SWEEP": (_passes_liquidity_sweep, "min_reward_risk", _rank_score,
                        "MAX_LIQUIDITY_SWEEP_PICKS_PER_DAY"),
    "FVG": (_passes_fvg, "min_reward_risk", _rank_score,
            "MAX_FVG_PICKS_PER_DAY"),
    "ANCHORED_VWAP": (_passes_anchored_vwap, "min_reward_risk", _rank_score,
                      "MAX_ANCHORED_VWAP_PICKS_PER_DAY"),
    "VOLUME_PROFILE": (_passes_volume_profile, "min_reward_risk", _rank_score,
                       "MAX_VOLUME_PROFILE_PICKS_PER_DAY"),
    "BREAKOUT_52W": (_passes_breakout_52w, "min_reward_risk", _rank_score,
                     "MAX_BREAKOUT_52W_PICKS_PER_DAY"),
    # DISCOVERED variants each carry their own entry_expr in params; one gate
    # (_passes_spec) serves them all. This entry lets run_backtest replay a spec
    # so strategy_discovery can gate it before it goes live.
    "DISCOVERED": (_passes_spec, "min_reward_risk", _rank_score,
                   "MAX_DISCOVERED_PICKS_PER_DAY"),
}


def build_variant_list(conn, channels: list[str] | None = None) -> list[dict]:
    """Active fleet variants plus a seed-default variant per channel, deduped
    by resolved params. Each entry: {variant_key, channel, params}."""
    from stockbot import db
    wanted = [c for c in CHANNEL_SPECS if channels is None or c in channels]
    out, seen = [], set()
    for channel in wanted:
        rows = db.get_active_strategies(conn, channel=channel) if conn else []
        rows = list(rows) + [{"variant_key": f"{channel}_seed", "params_json": None}]
        for row in rows:
            params = strategy_engine.resolve_params(channel, row["params_json"])
            key = (channel, json.dumps(params, sort_keys=True, default=str))
            if key in seen:
                continue
            seen.add(key)
            out.append({"variant_key": row["variant_key"], "channel": channel,
                        "params": params})
    return out


class _Book:
    """One variant's independent positions + realized trades."""

    def __init__(self, variant: dict):
        self.variant = variant
        self.positions: dict[str, dict] = {}
        self.trades: list[dict] = []

    def open(self, ticker: str, date: str, snap: Snapshot, target: float,
             stop: float, capital: float) -> None:
        fill = paper.buy_fill(snap.close)
        qty = int(capital // fill)
        if qty < 1:
            return
        self.positions[ticker] = {
            "ticker": ticker, "entry_date": date, "entry_fill": fill,
            "qty": qty, "target": target, "stop": stop,
            "buy_cost": paper.buy_costs(qty, fill).total, "bars_held": 0,
        }

    def close(self, ticker: str, date: str, ref_exit: float, status: str,
              reason: str) -> None:
        pos = self.positions.pop(ticker)
        fill = paper.sell_fill(ref_exit)
        proceeds = pos["qty"] * fill
        outlay = pos["qty"] * pos["entry_fill"]
        net = proceeds - paper.sell_costs(pos["qty"], fill).total \
            - outlay - pos["buy_cost"]
        self.trades.append({
            "ticker": ticker, "entry_date": pos["entry_date"], "exit_date": date,
            "status": status, "reason": reason, "qty": pos["qty"],
            "entry_fill": round(pos["entry_fill"], 2), "exit_fill": round(fill, 2),
            "bars_held": pos["bars_held"], "net_inr": round(net, 2),
            "pnl_pct": round(net / (outlay + pos["buy_cost"]) * 100, 3),
        })


def _evaluate_exits(book: _Book, date: str,
                    snapshots: dict[str, Snapshot]) -> None:
    """exits.py's rule ladder minus the sentiment-breakdown rule."""
    channel = book.variant["channel"]
    sma_bars = (config.PULLBACK_SETUP_BROKEN_SMA_BARS
                if channel.startswith("PULLBACK")
                else config.SETUP_BROKEN_SMA_BARS)
    for ticker in list(book.positions):
        pos = book.positions[ticker]
        s = snapshots.get(ticker)
        if s is None or pos["entry_date"] == date:
            continue
        pos["bars_held"] += 1
        if s.low <= pos["stop"]:
            book.close(ticker, date, pos["stop"], "STOPPED_OUT", "hit stop")
        elif s.high >= pos["target"]:
            book.close(ticker, date, pos["target"], "TARGET_HIT", "hit target")
        elif pos["bars_held"] < config.MIN_HOLD_BEFORE_SOFT_EXIT:
            continue  # grace period — soft exits can't fire on pre-existing state
        elif s.closes_below_sma20 >= sma_bars:
            book.close(ticker, date, s.close, "SETUP_BROKEN",
                       f"{s.closes_below_sma20} closes below SMA20")
        elif s.macd_bearish_cross_today and s.rsi < config.SETUP_BROKEN_RSI:
            book.close(ticker, date, s.close, "SETUP_BROKEN",
                       f"MACD bearish cross, RSI {s.rsi:.0f}")
        elif pos["bars_held"] > config.MAX_HOLDING_DAYS:
            book.close(ticker, date, s.close, "EXPIRED", "max holding days")


def _evaluate_entries(book: _Book, date: str, snapshots: dict[str, Snapshot],
                      context: dict, capital: float) -> None:
    """_gather_candidates' pipeline: gate -> target/stop -> R:R -> rank.
    (fundamentals + sentiment steps intentionally absent — see module doc)"""
    gate_fn, rr_key, score_fn, cap_name = CHANNEL_SPECS[book.variant["channel"]]
    params = book.variant["params"]
    candidates = []
    for ticker, s in snapshots.items():
        if ticker in book.positions:
            continue
        if not gate_fn(s, params, context):
            continue
        target, stop = derive_target_stop(s, config.MIN_UPSIDE_PCT,
                                          config.MAX_RISK_PCT,
                                          config.MIN_STOP_ATR_MULT)
        if target is None:
            continue
        upside = (target - s.close) / s.close * 100
        risk = (s.close - stop) / s.close * 100
        if upside < config.MIN_UPSIDE_PCT or risk <= 0:
            continue
        rr = upside / risk
        if rr < params[rr_key]:
            continue
        score, _notes = score_fn(s, 0.0)
        candidates.append((score, ticker, s, target, stop))
    candidates.sort(key=lambda c: c[0], reverse=True)
    cap = getattr(config, cap_name)
    for _score, ticker, s, target, stop in candidates[:cap]:
        book.open(ticker, date, s, target, stop, capital)


def run_backtest(histories: dict[str, pd.DataFrame], variants: list[dict],
                 eval_days: int, capital_per_trade: float = 100_000.0,
                 progress=None) -> dict:
    """Replay `eval_days` most recent sessions. Returns {variant_key: report}."""
    calendar = sorted({d for df in histories.values() for d in df.index})
    eval_dates = calendar[-eval_days:]
    books = [_Book(v) for v in variants]

    for i, d in enumerate(eval_dates):
        snapshots: dict[str, Snapshot] = {}
        for ticker, df in histories.items():
            window = df.loc[:d]
            if len(window) < config.MIN_HISTORY_BARS or window.index[-1] != d:
                continue  # ticker not trading this day / not enough history
            try:
                snapshots[ticker] = compute_snapshot(ticker, window,
                                                     config.MACD_CROSS_LOOKBACK)
            except Exception:
                continue
        context = build_toggle_context(snapshots)
        date = d.strftime("%Y-%m-%d")
        for book in books:
            _evaluate_exits(book, date, snapshots)
            _evaluate_entries(book, date, snapshots, context, capital_per_trade)
        if progress and (i + 1) % 20 == 0:
            progress(f"  replayed {i + 1}/{len(eval_dates)} sessions "
                     f"({date}), open positions "
                     f"{sum(len(b.positions) for b in books)}")

    return {b.variant["variant_key"]: _report(b, eval_dates) for b in books}


# period label -> calendar-day floor, so a small `days` with a long `period`
# still pulls the deeper span the caller asked for. Depth is otherwise derived
# from the replay window itself (see _lookback_days).
_PERIOD_DAYS = {"6mo": 190, "1y": 400, "2y": 760, "3y": 1120, "5y": 1850,
                "max": 3800}


def _lookback_days(days: int, period: str | None) -> int:
    """Calendar days of Fyers history to fetch for a `days`-session replay.
    ~1.5x the (replay window + indicator warm-up) covers weekends/holidays, and
    a `period` label raises a floor for deep on-demand runs."""
    from_replay = int((days + config.MIN_HISTORY_BARS) * 1.5) + 10
    return max(from_replay, _PERIOD_DAYS.get(period or "", 0))


def run_and_save(days: int = 120, tickers_cap: int | None = None,
                 channels: list[str] | None = None, capital: float = 100_000.0,
                 seeds_only: bool = False, period: str | None = None,
                 warnings: list | None = None, progress=None) -> dict:
    """Full fleet backtest: load universe, fetch history, replay, and write the
    result JSON to data/backtests/. Shared by the CLI (backtest.py) and the
    dashboard's Run button. `period` raises a floor on the history depth fetched
    (e.g. "3y" for a multi-year replay). Returns the saved payload (plus 'path',
    or 'error').

    History comes from Fyers /history ONLY (free, authorized daily NSE candles),
    chunked to respect the ~366-day per-request cap, so backtest edge is measured
    on the SAME feed that books live trades. If Fyers is unavailable the run
    FAILS LOUD ('error' set) rather than silently backtesting on yfinance."""
    from stockbot import db, fyers_data
    warnings = warnings if warnings is not None else []
    conn = db.connect()
    try:
        from stockbot import universe as universe_mod
        universe_mod.apply(universe_mod.load(conn, warnings))
        tickers = sorted(config.WATCHLIST)
        if tickers_cap:
            tickers = tickers[:tickers_cap]
        variants = build_variant_list(None if seeds_only else conn, channels)
    finally:
        conn.close()

    lookback = _lookback_days(days, period)
    histories = fyers_data.fetch_history_range(tickers, warnings, lookback)
    if not histories:
        return {"error": "Fyers historical data unavailable — backtest aborted "
                "(backtests never fall back to yfinance; run fyers_login.py to "
                "authorize a fresh daily token). Last warnings: "
                + " | ".join(warnings[-3:]), "results": {}}
    results = run_backtest(histories, variants, days, capital, progress=progress)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = config.DATA_DIR / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"backtest_{stamp}.json"
    payload = {"run_at": stamp, "days": days, "tickers": len(histories),
               "capital_per_trade": capital, "results": results}
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    payload["path"] = str(out_path)
    return payload


def _report(book: _Book, eval_dates: list) -> dict:
    trades = book.trades
    wins = [t for t in trades if t["net_inr"] > 0]
    losses = [t for t in trades if t["net_inr"] <= 0]
    gross_win = sum(t["net_inr"] for t in wins)
    gross_loss = -sum(t["net_inr"] for t in losses)
    equity = peak = dd = 0.0
    for t in trades:
        equity += t["net_inr"]
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    win_rate = round(len(wins) / len(trades) * 100, 1) if trades else None
    report = {
        "channel": book.variant["channel"],
        "params": book.variant["params"],
        "sessions": len(eval_dates),
        "trades": len(trades),
        "win_rate_pct": win_rate,
        "net_inr": round(sum(t["net_inr"] for t in trades), 0),
        "avg_pnl_pct": (round(sum(t["pnl_pct"] for t in trades) / len(trades), 2)
                        if trades else None),
        "profit_factor": (round(gross_win / gross_loss, 2) if gross_loss > 0
                          else (None if not wins else float("inf"))),
        "avg_hold_bars": (round(sum(t["bars_held"] for t in trades)
                                / len(trades), 1) if trades else None),
        "max_drawdown_inr": round(dd, 0),
        "exit_breakdown": {s: sum(1 for t in trades if t["status"] == s)
                           for s in ("TARGET_HIT", "STOPPED_OUT",
                                     "SETUP_BROKEN", "EXPIRED")},
        "open_at_end": len(book.positions),
        "meets_graduation_gate": bool(
            trades and len(trades) >= 50 and win_rate >= 55.0
            and sum(t["net_inr"] for t in trades) > 0),
        "trades_detail": trades,
    }
    return report
