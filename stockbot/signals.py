"""Entry scan: turn technical snapshots + fundamentals + sentiment into new picks."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import config
from stockbot import db, fundamentals, strategy_engine
from stockbot.indicators import Snapshot, derive_target_stop


@dataclass
class Candidate:
    snap: Snapshot
    target: float
    stop: float
    reward_risk: float
    sentiment: float
    score: float
    rationale: str


def _is_liquid(s: Snapshot) -> bool:
    return s.avg_turnover_20d >= config.MIN_AVG_TURNOVER


def _toggle_volume_surge(s: Snapshot, context: dict) -> bool:
    return s.vol_ratio > config.VOL_RATIO_BONUS_THRESHOLD


def _toggle_close_above_weekly_r1(s: Snapshot, context: dict) -> bool:
    return s.weekly_r1 != float("inf") and s.close > s.weekly_r1


def _toggle_sector_relative_strength(s: Snapshot, context: dict) -> bool:
    tier_moms = context.get("tier_mom20", {}).get(config.TIER.get(s.ticker, "LARGE"))
    if not tier_moms:
        return True  # no peer data to compare against - don't block
    sorted_moms = sorted(tier_moms)
    median = sorted_moms[len(sorted_moms) // 2]
    return s.mom_20d >= median


def _toggle_positive_order_flow(s: Snapshot, context: dict) -> bool:
    return s.cmf > 0


def _toggle_above_anchored_vwap(s: Snapshot, context: dict) -> bool:
    return s.close > s.anchored_vwap


def _toggle_near_volume_poc_support(s: Snapshot, context: dict) -> bool:
    return s.close >= s.volume_poc


def _toggle_near_52w_high(s: Snapshot, context: dict) -> bool:
    return s.close >= s.high_252d * (1 - config.BREAKOUT52W_TOLERANCE_PCT / 100)


# Composable optional gate conditions a "wildcard" variant can combine
# (config.STRATEGY_TOGGLE_LIBRARY lists the names an LLM proposal may pick from).
# Usable by a wildcard variant of ANY channel, not just the ones the underlying
# signal was originally built for.
TOGGLE_CONDITIONS = {
    "require_volume_surge": _toggle_volume_surge,
    "require_close_above_weekly_r1": _toggle_close_above_weekly_r1,
    "require_sector_relative_strength": _toggle_sector_relative_strength,
    "require_positive_order_flow": _toggle_positive_order_flow,
    "require_above_anchored_vwap": _toggle_above_anchored_vwap,
    "require_near_volume_poc_support": _toggle_near_volume_poc_support,
    "require_near_52w_high": _toggle_near_52w_high,
}


def build_toggle_context(snapshots: dict[str, Snapshot]) -> dict:
    """Cross-sectional data toggles need (e.g. peer momentum), computed once per scan."""
    tier_mom20: dict[str, list[float]] = {}
    for ticker, s in snapshots.items():
        tier_mom20.setdefault(config.TIER.get(ticker, "LARGE"), []).append(s.mom_20d)
    return {"tier_mom20": tier_mom20}


def _passes_toggles(s: Snapshot, params: dict, context: dict) -> bool:
    for name in params.get("_toggles", []):
        fn = TOGGLE_CONDITIONS.get(name)
        if fn is not None and not fn(s, context):
            return False
    return True


# ---------------------------------------------------------------------------
# Entry gates — one per channel. Each returns bool given a Snapshot, that
# channel's resolved params (stockbot/strategy_engine.py), and the shared
# toggle context. Liquidity + the optional toggle check are common to all.
# ---------------------------------------------------------------------------

def _passes_technicals(s: Snapshot, params: dict, context: dict | None = None) -> bool:
    if not _is_liquid(s):
        return False
    if not (s.close > s.sma20 > s.sma50):
        return False
    if not (params["rsi_entry_min"] <= s.rsi <= params["rsi_entry_max"]):
        return False
    macd_bullish = s.macd > s.macd_signal and (
        s.macd_hist > s.macd_hist_prev or s.macd_bullish_cross_recent
    )
    if not macd_bullish:
        return False
    if s.close < s.pivot:
        return False
    return _passes_toggles(s, params, context or {})


def _passes_pullback(s: Snapshot, params: dict, context: dict | None = None) -> bool:
    """Buy-the-dip gate: uptrend intact, price pulled back to SMA20 and held."""
    if not _is_liquid(s):
        return False
    if not (s.sma20 > s.sma50 and s.close > s.sma50):
        return False
    if s.mom_20d < params["pullback_min_mom20"]:
        return False
    if s.mom_5d > params["pullback_max_mom5"]:
        return False
    if s.low > s.sma20 * (1 + params["pullback_sma20_touch_pct"] / 100):
        return False
    if s.close < s.sma20 * (1 - config.PULLBACK_MAX_CLOSE_BELOW_SMA20_PCT / 100):
        return False
    if s.closes_below_sma20 > 1:
        return False
    if not (params["pullback_rsi_min"] <= s.rsi <= params["pullback_rsi_max"]):
        return False
    if s.macd_hist < s.macd_hist_prev:
        return False  # downward force still accelerating
    return _passes_toggles(s, params, context or {})


def _passes_orderflow(s: Snapshot, params: dict, context: dict | None = None) -> bool:
    """Order Flow channel — daily-bar proxy (Chaikin Money Flow), see
    indicators.py module docstring. Requires net buying pressure that's
    building, not just a one-off positive reading."""
    if not _is_liquid(s):
        return False
    if s.cmf < params["cmf_min"]:
        return False
    if s.cmf <= s.cmf_prev:
        return False
    return _passes_toggles(s, params, context or {})


def _passes_liquidity_sweep(s: Snapshot, params: dict, context: dict | None = None) -> bool:
    """Liquidity Sweep channel — daily-bar "stop hunt then reclaim": today's low
    undercuts the prior 10-bar swing low but closes back above it, strongly
    (in the upper half of the day's range), inside an uptrend context."""
    if not _is_liquid(s):
        return False
    if s.close <= s.sma50:
        return False
    if s.low >= s.swing_low_10d_prior:
        return False  # never actually swept below the prior level
    if s.close <= s.swing_low_10d_prior:
        return False  # didn't reclaim
    day_range = s.high - s.low
    if day_range <= 0:
        return False
    reversal_strength = (s.close - s.low) / day_range
    if reversal_strength < params["reversal_strength_min"]:
        return False
    return _passes_toggles(s, params, context or {})


def _passes_fvg(s: Snapshot, params: dict, context: dict | None = None) -> bool:
    """Fair Value Gap channel — retest of a recent unfilled bullish 3-candle
    imbalance: today's low tags into the gap zone and the close reclaims
    above it, inside an uptrend context."""
    if not _is_liquid(s):
        return False
    if s.fvg_bull_bottom is None or s.fvg_bull_top is None:
        return False
    if s.close <= s.sma50:
        return False
    if not (s.fvg_bull_bottom <= s.low <= s.fvg_bull_top):
        return False
    if s.close <= s.fvg_bull_top:
        return False
    return _passes_toggles(s, params, context or {})


def _passes_anchored_vwap(s: Snapshot, params: dict, context: dict | None = None) -> bool:
    """Anchored VWAP channel — a fresh reclaim: yesterday's close was still
    below the anchored VWAP, today's close clears it by at least the
    variant's tolerance."""
    if not _is_liquid(s):
        return False
    if s.anchored_vwap <= 0:
        return False
    if s.close_prev >= s.anchored_vwap:
        return False  # not a FRESH reclaim - already been above it
    reclaim_level = s.anchored_vwap * (1 + params["reclaim_tolerance_pct"] / 100)
    if s.close < reclaim_level:
        return False
    return _passes_toggles(s, params, context or {})


def _passes_volume_profile(s: Snapshot, params: dict, context: dict | None = None) -> bool:
    """Volume Profile channel — price pulls back to the 60-bar Point-of-Control
    proxy from above and holds it as support, inside an uptrend context."""
    if not _is_liquid(s):
        return False
    if s.close <= s.sma50:
        return False
    tolerance = s.volume_poc * (params["touch_tolerance_pct"] / 100)
    if s.low > s.volume_poc + tolerance:
        return False  # never got close enough to the level
    if s.close < s.volume_poc - tolerance:
        return False  # broke down through it rather than holding
    return _passes_toggles(s, params, context or {})


def _passes_breakout_52w(s: Snapshot, params: dict, context: dict | None = None) -> bool:
    """52-Week High Breakout channel — close at/near a fresh 252-day high,
    volume-confirmed."""
    if not _is_liquid(s):
        return False
    threshold = s.high_252d * (1 - params["tolerance_pct"] / 100)
    if s.close < threshold:
        return False
    if s.vol_ratio < params["min_vol_ratio"]:
        return False
    return _passes_toggles(s, params, context or {})


def _rank_score(s: Snapshot, sentiment: float) -> tuple[float, list[str]]:
    score = 2.0 * sentiment
    notes = []
    if sentiment > 0.2:
        notes.append(f"bullish news ({sentiment:+.2f})")
    if s.vol_ratio > config.VOL_RATIO_BONUS_THRESHOLD:
        score += 1.0
        notes.append(f"volume surge ({s.vol_ratio:.1f}x)")
    if s.mom_5d > 0:
        score += 1.0
        notes.append(f"5d momentum {s.mom_5d:+.1f}%")
    lo, hi = config.RSI_SWEETSPOT
    if lo <= s.rsi <= hi:
        score += 0.5
    if 0 <= (s.close - s.pivot) / s.pivot * 100 <= config.PIVOT_PROXIMITY_PCT:
        score += 0.5
        notes.append("fresh breakout above pivot")
    return score, notes


def _pullback_score(s: Snapshot, sentiment: float) -> tuple[float, list[str]]:
    score, notes = _rank_score(s, sentiment)
    # shallower dips are healthier: bonus shrinks as close strays from SMA20
    dip_depth = abs(s.close - s.sma20) / s.sma20 * 100
    score += max(0.0, 1.0 - dip_depth)
    return score, notes


def _technical_rationale(s: Snapshot, rr: float, sent: float, notes: list[str]) -> str:
    tier = config.TIER.get(s.ticker, "LARGE")
    base = (f"[{tier}] Uptrend (close>SMA20>SMA50), RSI {s.rsi:.0f}, MACD bullish, "
            f"above pivot; R:R {rr:.1f}")
    return base + ("; " + ", ".join(notes) if notes else "")


def _pullback_rationale(s: Snapshot, rr: float, sent: float, notes: list[str]) -> str:
    tier = config.TIER.get(s.ticker, "LARGE")
    base = (f"[{tier}] PULLBACK: uptrend intact (SMA20>SMA50, 20d {s.mom_20d:+.1f}%), "
            f"dipped to SMA20 (5d {s.mom_5d:+.1f}%), RSI {s.rsi:.0f} reset, "
            f"MACD hist turning; R:R {rr:.1f}")
    return base + ("; " + ", ".join(notes) if notes else "")


def _orderflow_rationale(s: Snapshot, rr: float, sent: float, notes: list[str]) -> str:
    tier = config.TIER.get(s.ticker, "LARGE")
    base = f"[{tier}] ORDER FLOW (proxy): CMF {s.cmf:+.2f} rising; R:R {rr:.1f}"
    return base + ("; " + ", ".join(notes) if notes else "")


def _liquidity_sweep_rationale(s: Snapshot, rr: float, sent: float, notes: list[str]) -> str:
    tier = config.TIER.get(s.ticker, "LARGE")
    base = (f"[{tier}] LIQUIDITY SWEEP: swept {s.swing_low_10d_prior:.2f}, "
            f"reclaimed strong; R:R {rr:.1f}")
    return base + ("; " + ", ".join(notes) if notes else "")


def _fvg_rationale(s: Snapshot, rr: float, sent: float, notes: list[str]) -> str:
    tier = config.TIER.get(s.ticker, "LARGE")
    base = (f"[{tier}] FAIR VALUE GAP: retest of {s.fvg_bull_bottom:.2f}-{s.fvg_bull_top:.2f}, "
            f"reclaimed; R:R {rr:.1f}")
    return base + ("; " + ", ".join(notes) if notes else "")


def _anchored_vwap_rationale(s: Snapshot, rr: float, sent: float, notes: list[str]) -> str:
    tier = config.TIER.get(s.ticker, "LARGE")
    base = f"[{tier}] ANCHORED VWAP: fresh reclaim of {s.anchored_vwap:.2f}; R:R {rr:.1f}"
    return base + ("; " + ", ".join(notes) if notes else "")


def _volume_profile_rationale(s: Snapshot, rr: float, sent: float, notes: list[str]) -> str:
    tier = config.TIER.get(s.ticker, "LARGE")
    base = (f"[{tier}] VOLUME PROFILE: holding {s.volume_poc:.2f} Point-of-Control "
            f"support; R:R {rr:.1f}")
    return base + ("; " + ", ".join(notes) if notes else "")


def _breakout_52w_rationale(s: Snapshot, rr: float, sent: float, notes: list[str]) -> str:
    tier = config.TIER.get(s.ticker, "LARGE")
    base = (f"[{tier}] 52-WEEK BREAKOUT: near {s.high_252d:.2f} high, "
            f"vol {s.vol_ratio:.1f}x; R:R {rr:.1f}")
    return base + ("; " + ", ".join(notes) if notes else "")


def _gather_candidates(conn: sqlite3.Connection, snapshots: dict[str, Snapshot],
                       sentiments: dict[str, dict], date: str, warnings: list[str],
                       active: set[str], params: dict, context: dict, gate_fn, rr_key: str,
                       rationale_fn, score_fn=None) -> list[Candidate]:
    """Shared skeleton every channel's scan uses: liquidity/entry gate -> pivot-
    ladder target/stop -> reward:risk floor -> fundamentals -> sentiment veto ->
    rank. Only the gate, the R:R params key, the rationale text, and (optionally)
    the ranking score differ per channel.
    """
    score_fn = score_fn or _rank_score
    candidates: list[Candidate] = []
    for ticker in config.WATCHLIST:
        s = snapshots.get(ticker)
        if s is None or ticker in active:
            continue
        if not gate_fn(s, params, context):
            continue

        target, stop = derive_target_stop(s, config.MIN_UPSIDE_PCT, config.MAX_RISK_PCT)
        if target is None:
            continue  # no resistance rung offers enough upside
        upside = (target - s.close) / s.close * 100
        risk = (s.close - stop) / s.close * 100
        if upside < config.MIN_UPSIDE_PCT or risk <= 0:
            continue
        rr = upside / risk
        if rr < params[rr_key]:
            continue

        # fundamentals fetched lazily — only for gate survivors
        if not fundamentals.check_fundamentals(conn, ticker, date, warnings):
            continue

        sent = sentiments.get(ticker, {"score": 0.0})["score"] or 0.0
        if sent <= config.SENTIMENT_ENTRY_MIN:
            continue

        score, notes = score_fn(s, sent)
        rationale = rationale_fn(s, rr, sent, notes)
        candidates.append(Candidate(s, target, stop, rr, sent, score, rationale))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def _scan_channel(conn: sqlite3.Connection, channel: str, snapshots: dict[str, Snapshot],
                  sentiments: dict[str, dict], date: str, warnings: list[str],
                  active: set[str], context: dict, top_n: int | None,
                  max_per_day_default: int | None, gate_fn, rr_key: str,
                  rationale_fn, score_fn=None) -> list[dict]:
    """Loop every active variant of `channel`, gather its candidates, insert up
    to its daily cap. A ticker claimed by an earlier variant this run is
    skipped for the rest (mirrors the pre-existing one-ACTIVE-pick-per-ticker
    rule that already made NEWS/TECHNICAL/PULLBACK compete for the same ticker).
    """
    variants = db.get_active_strategies(conn, channel=channel)
    if not variants:
        variants = [{"variant_key": f"{channel}_seed", "params_json": None}]

    inserted: list[dict] = []
    for variant in variants:
        params = strategy_engine.resolve_params(channel, variant["params_json"])
        candidates = _gather_candidates(conn, snapshots, sentiments, date, warnings, active,
                                        params, context, gate_fn, rr_key, rationale_fn, score_fn)
        cap = top_n if top_n is not None else max_per_day_default
        chosen = candidates[:cap]
        inserted += _insert_candidates(conn, chosen, date, variant["variant_key"])
        active |= {c.snap.ticker for c in chosen}
    return inserted


def scan_new_picks(conn: sqlite3.Connection, snapshots: dict[str, Snapshot],
                   sentiments: dict[str, dict], date: str,
                   warnings: list[str], top_n: int | None = None) -> list[dict]:
    """TECHNICAL channel: evaluate the watchlist against every active variant."""
    active = {p["ticker"] for p in db.get_active_picks(conn)}
    context = build_toggle_context(snapshots)
    return _scan_channel(conn, "TECHNICAL", snapshots, sentiments, date, warnings, active,
                        context, top_n, config.MAX_NEW_PICKS_PER_DAY,
                        _passes_technicals, "min_reward_risk", _technical_rationale)


def scan_news_picks(conn: sqlite3.Connection, snapshots: dict[str, Snapshot],
                    sentiments: dict[str, dict], date: str,
                    warnings: list[str], top_n: int | None = None) -> list[dict]:
    """News-first channel: strong bullish catalyst leads, chart confirms.

    Gate: sentiment >= NEWS_SENTIMENT_MIN with confidence >= NEWS_CONFIDENCE_MIN,
    then close > SMA20, RSI <= NEWS_RSI_MAX, liquidity, fundamentals, and a
    pivot-ladder target with R:R >= NEWS_MIN_REWARD_RISK. NEWS stays a single
    fixed strategy (out of scope for the variant fleet), so this keeps its own
    shape rather than going through _gather_candidates.
    """
    top_n = top_n if top_n is not None else config.MAX_NEWS_PICKS_PER_DAY
    active = {p["ticker"] for p in db.get_active_picks(conn)}

    candidates: list[Candidate] = []
    for ticker, sent_info in sentiments.items():
        if ticker in active or ticker not in config.WATCHLIST:
            continue
        sent = sent_info.get("score") or 0.0
        conf = sent_info.get("confidence") or 0.0
        if sent < config.NEWS_SENTIMENT_MIN or conf < config.NEWS_CONFIDENCE_MIN:
            continue
        s = snapshots.get(ticker)
        if s is None or not _is_liquid(s):
            continue
        # chart confirmation (looser than technical channel)
        if s.close <= s.sma20 or s.rsi > config.NEWS_RSI_MAX:
            continue

        target, stop = derive_target_stop(s, config.MIN_UPSIDE_PCT, config.MAX_RISK_PCT)
        if target is None:
            continue
        upside = (target - s.close) / s.close * 100
        risk = (s.close - stop) / s.close * 100
        if upside < config.MIN_UPSIDE_PCT or risk <= 0:
            continue
        rr = upside / risk
        if rr < config.NEWS_MIN_REWARD_RISK:
            continue

        if not fundamentals.check_fundamentals(conn, ticker, date, warnings):
            continue

        tier = config.TIER.get(ticker, "LARGE")
        summary = (sent_info.get("summary") or "").strip()
        rationale = (f"[{tier}] NEWS CATALYST ({sent:+.2f}, conf {conf:.0%}): {summary} "
                     f"| Chart confirms: close>SMA20, RSI {s.rsi:.0f}, R:R {rr:.1f}")
        candidates.append(Candidate(s, target, stop, rr, sent, sent * conf, rationale))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return _insert_candidates(conn, candidates[:top_n], date, "NEWS")


def scan_pullback_picks(conn: sqlite3.Connection, snapshots: dict[str, Snapshot],
                        sentiments: dict[str, dict], date: str,
                        warnings: list[str], top_n: int | None = None) -> list[dict]:
    """PULLBACK channel: buy the dip to SMA20 inside an uptrend, evaluated
    against every active variant."""
    active = {p["ticker"] for p in db.get_active_picks(conn)}
    context = build_toggle_context(snapshots)
    return _scan_channel(conn, "PULLBACK", snapshots, sentiments, date, warnings, active,
                        context, top_n, config.MAX_PULLBACK_PICKS_PER_DAY,
                        _passes_pullback, "pullback_min_reward_risk", _pullback_rationale,
                        score_fn=_pullback_score)


def scan_orderflow_picks(conn: sqlite3.Connection, snapshots: dict[str, Snapshot],
                        sentiments: dict[str, dict], date: str,
                        warnings: list[str], top_n: int | None = None) -> list[dict]:
    """ORDER FLOW channel (Chaikin Money Flow proxy) — see _passes_orderflow."""
    active = {p["ticker"] for p in db.get_active_picks(conn)}
    context = build_toggle_context(snapshots)
    return _scan_channel(conn, "ORDERFLOW", snapshots, sentiments, date, warnings, active,
                        context, top_n, config.MAX_ORDERFLOW_PICKS_PER_DAY,
                        _passes_orderflow, "min_reward_risk", _orderflow_rationale)


def scan_liquidity_sweep_picks(conn: sqlite3.Connection, snapshots: dict[str, Snapshot],
                               sentiments: dict[str, dict], date: str,
                               warnings: list[str], top_n: int | None = None) -> list[dict]:
    """LIQUIDITY SWEEP channel (stop-hunt-then-reclaim) — see _passes_liquidity_sweep."""
    active = {p["ticker"] for p in db.get_active_picks(conn)}
    context = build_toggle_context(snapshots)
    return _scan_channel(conn, "LIQUIDITY_SWEEP", snapshots, sentiments, date, warnings, active,
                        context, top_n, config.MAX_LIQUIDITY_SWEEP_PICKS_PER_DAY,
                        _passes_liquidity_sweep, "min_reward_risk", _liquidity_sweep_rationale)


def scan_fvg_picks(conn: sqlite3.Connection, snapshots: dict[str, Snapshot],
                   sentiments: dict[str, dict], date: str,
                   warnings: list[str], top_n: int | None = None) -> list[dict]:
    """FAIR VALUE GAP channel (3-candle imbalance retest) — see _passes_fvg."""
    active = {p["ticker"] for p in db.get_active_picks(conn)}
    context = build_toggle_context(snapshots)
    return _scan_channel(conn, "FVG", snapshots, sentiments, date, warnings, active,
                        context, top_n, config.MAX_FVG_PICKS_PER_DAY,
                        _passes_fvg, "min_reward_risk", _fvg_rationale)


def scan_anchored_vwap_picks(conn: sqlite3.Connection, snapshots: dict[str, Snapshot],
                             sentiments: dict[str, dict], date: str,
                             warnings: list[str], top_n: int | None = None) -> list[dict]:
    """ANCHORED VWAP channel (fresh reclaim) — see _passes_anchored_vwap."""
    active = {p["ticker"] for p in db.get_active_picks(conn)}
    context = build_toggle_context(snapshots)
    return _scan_channel(conn, "ANCHORED_VWAP", snapshots, sentiments, date, warnings, active,
                        context, top_n, config.MAX_ANCHORED_VWAP_PICKS_PER_DAY,
                        _passes_anchored_vwap, "min_reward_risk", _anchored_vwap_rationale)


def scan_volume_profile_picks(conn: sqlite3.Connection, snapshots: dict[str, Snapshot],
                              sentiments: dict[str, dict], date: str,
                              warnings: list[str], top_n: int | None = None) -> list[dict]:
    """VOLUME PROFILE channel (Point-of-Control support) — see _passes_volume_profile."""
    active = {p["ticker"] for p in db.get_active_picks(conn)}
    context = build_toggle_context(snapshots)
    return _scan_channel(conn, "VOLUME_PROFILE", snapshots, sentiments, date, warnings, active,
                        context, top_n, config.MAX_VOLUME_PROFILE_PICKS_PER_DAY,
                        _passes_volume_profile, "min_reward_risk", _volume_profile_rationale)


def scan_breakout_52w_picks(conn: sqlite3.Connection, snapshots: dict[str, Snapshot],
                            sentiments: dict[str, dict], date: str,
                            warnings: list[str], top_n: int | None = None) -> list[dict]:
    """52-WEEK HIGH BREAKOUT channel — see _passes_breakout_52w."""
    active = {p["ticker"] for p in db.get_active_picks(conn)}
    context = build_toggle_context(snapshots)
    return _scan_channel(conn, "BREAKOUT_52W", snapshots, sentiments, date, warnings, active,
                        context, top_n, config.MAX_BREAKOUT_52W_PICKS_PER_DAY,
                        _passes_breakout_52w, "min_reward_risk", _breakout_52w_rationale)


def _insert_candidates(conn: sqlite3.Connection, candidates: list[Candidate],
                       date: str, channel: str) -> list[dict]:
    inserted = []
    for c in candidates:
        pick = {
            "ticker": c.snap.ticker,
            "entry_date": date,
            "entry_price": round(c.snap.close, 2),
            "target_price": round(c.target, 2),
            "stop_price": round(c.stop, 2),
            "pivot": round(c.snap.pivot, 2), "r1": round(c.snap.r1, 2),
            "r2": round(c.snap.r2, 2), "s1": round(c.snap.s1, 2), "s2": round(c.snap.s2, 2),
            "rsi_at_entry": round(c.snap.rsi, 1),
            "macd_hist_at_entry": round(c.snap.macd_hist, 4),
            "sentiment_at_entry": round(c.sentiment, 2),
            "rationale": c.rationale,
            "channel": channel,
        }
        pick_id = db.insert_pick(conn, pick)
        if pick_id:
            pick["id"] = pick_id  # paper engine records this as provenance
            pick["reward_risk"] = round(c.reward_risk, 2)
            inserted.append(pick)
    return inserted
