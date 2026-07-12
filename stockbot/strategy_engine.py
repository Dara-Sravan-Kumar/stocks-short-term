"""Self-evolving strategy fleet for TECHNICAL & PULLBACK.

Each channel is a small fleet of competing parameter variants, judged on real
paper-trade outcomes (stockbot/db.py's strategies + paper_positions tables).
Retirement is trade-count-based, not calendar-based; a retired variant's slot
is immediately backfilled by an LLM-proposed replacement (same subprocess
pattern as stockbot/sentiment.py's Claude CLI call, reused here for a
different one-shot reasoning task). NEWS keeps a permanent, non-retirable row
so capital weighting spans every live strategy uniformly, but its own gate
logic is untouched.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime

import config
from stockbot import db, market_regime

PROPOSAL_INSTRUCTIONS = """You are designing a parameter variant for an NSE (India) \
short-term trading strategy family in a paper-trading system. You will be shown the \
performance ledger of every variant tried so far in this channel and today's market \
context. Propose ONE new variant likely to perform better, reasoning from what has \
and hasn't worked and from your own knowledge of momentum/pullback trading practice.

Respond with ONLY a JSON object - no prose, no markdown fences."""


def _technical_defaults() -> dict:
    return {
        "rsi_entry_min": config.RSI_ENTRY_MIN,
        "rsi_entry_max": config.RSI_ENTRY_MAX,
        "min_reward_risk": config.MIN_REWARD_RISK,
    }


def _pullback_defaults() -> dict:
    return {
        "pullback_rsi_min": config.PULLBACK_RSI_MIN,
        "pullback_rsi_max": config.PULLBACK_RSI_MAX,
        "pullback_min_mom20": config.PULLBACK_MIN_MOM20,
        "pullback_max_mom5": config.PULLBACK_MAX_MOM5,
        "pullback_min_reward_risk": config.PULLBACK_MIN_REWARD_RISK,
        "pullback_sma20_touch_pct": config.PULLBACK_SMA20_TOUCH_PCT,
    }


def _orderflow_defaults() -> dict:
    return {
        "cmf_min": config.ORDERFLOW_CMF_MIN,
        "min_reward_risk": config.ORDERFLOW_MIN_REWARD_RISK,
    }


def _liquidity_sweep_defaults() -> dict:
    return {
        "reversal_strength_min": config.LIQSWEEP_MIN_REVERSAL_STRENGTH,
        "min_reward_risk": config.LIQSWEEP_MIN_REWARD_RISK,
    }


def _fvg_defaults() -> dict:
    return {"min_reward_risk": config.FVG_MIN_REWARD_RISK}


def _anchored_vwap_defaults() -> dict:
    return {
        "reclaim_tolerance_pct": config.AVWAP_RECLAIM_TOLERANCE_PCT,
        "min_reward_risk": config.AVWAP_MIN_REWARD_RISK,
    }


def _volume_profile_defaults() -> dict:
    return {
        "touch_tolerance_pct": config.VOLPROFILE_TOUCH_TOLERANCE_PCT,
        "min_reward_risk": config.VOLPROFILE_MIN_REWARD_RISK,
    }


def _breakout_52w_defaults() -> dict:
    return {
        "tolerance_pct": config.BREAKOUT52W_TOLERANCE_PCT,
        "min_vol_ratio": config.BREAKOUT52W_MIN_VOL_RATIO,
        "min_reward_risk": config.BREAKOUT52W_MIN_REWARD_RISK,
    }


def _discovered_defaults() -> dict:
    # entry_expr is the spec's whole entry logic; it survives resolve_params
    # only because it's a declared default key (unknown keys are dropped there).
    return {"entry_expr": "", "min_reward_risk": config.MIN_REWARD_RISK}


_CHANNEL_DEFAULTS = {
    "TECHNICAL": _technical_defaults,
    "PULLBACK": _pullback_defaults,
    "ORDERFLOW": _orderflow_defaults,
    "LIQUIDITY_SWEEP": _liquidity_sweep_defaults,
    "FVG": _fvg_defaults,
    "ANCHORED_VWAP": _anchored_vwap_defaults,
    "VOLUME_PROFILE": _volume_profile_defaults,
    "BREAKOUT_52W": _breakout_52w_defaults,
    "DISCOVERED": _discovered_defaults,
}


def resolve_params(channel: str, params_json: str | None) -> dict:
    """Merge a variant's params_json overrides onto the channel's config defaults.

    Always includes a "_toggles" list (possibly empty) so gate functions can
    check it unconditionally. Unknown keys and out-of-bounds values are
    dropped/clamped rather than raising — a malformed or hand-edited row
    degrades to defaults instead of crashing the daily run.
    """
    defaults = _CHANNEL_DEFAULTS[channel]()
    resolved = dict(defaults)
    resolved["_toggles"] = []
    if not params_json:
        return resolved
    try:
        overrides = json.loads(params_json)
    except (json.JSONDecodeError, TypeError):
        return resolved
    if not isinstance(overrides, dict):
        return resolved

    bounds = config.STRATEGY_PARAM_BOUNDS.get(channel, {})
    for key, value in overrides.items():
        if key == "_toggles":
            if isinstance(value, list):
                resolved["_toggles"] = [t for t in value if t in config.STRATEGY_TOGGLE_LIBRARY]
            continue
        if key not in defaults:
            continue
        lo, hi = bounds.get(key, (None, None))
        if lo is not None:
            try:
                value = max(lo, min(hi, float(value)))
            except (TypeError, ValueError):
                continue
        resolved[key] = value
    return resolved


def _extract_json_object(text: str) -> dict | None:
    """Parse a JSON object out of model text, tolerating fences/prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _call_claude_cli(prompt: str, warnings: list[str], attempts: int = 2) -> dict | None:
    """One `claude -p` invocation with retry, mirroring sentiment.py's pattern
    (same CLI, subscription-billed, tolerant JSON parsing) but expecting a
    single JSON object rather than an array."""
    exe = shutil.which("claude")
    if not exe:
        warnings.append("Claude CLI ('claude') not found on PATH - strategy proposal uses fallback")
        return None

    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            proc = subprocess.run(
                [exe, "-p", "--output-format", "json", "--model", config.STRATEGY_LLM_MODEL],
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=config.CLAUDE_CLI_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            last_error = f"timed out after {config.CLAUDE_CLI_TIMEOUT}s"
            continue
        except OSError as exc:
            warnings.append(f"Claude CLI failed to launch ({exc}) - strategy proposal uses fallback")
            return None

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            last_error = f"exited {proc.returncode}: {detail[0][:120] if detail else ''}"
            continue

        result_text = proc.stdout
        try:
            envelope = json.loads(proc.stdout)
            if isinstance(envelope, dict) and "result" in envelope:
                result_text = envelope["result"]
        except json.JSONDecodeError:
            pass

        parsed = _extract_json_object(result_text)
        if parsed is not None:
            return parsed
        last_error = "output was not parseable JSON"

    warnings.append(f"Strategy proposal LLM call failed after {attempts} attempts "
                    f"({last_error}) - using bounds-midpoint fallback")
    return None


def _build_proposal_prompt(channel: str, ledger_stats: dict, market_context: dict,
                           bounds: dict, mode: str) -> str:
    lines = [PROPOSAL_INSTRUCTIONS, "", f"Channel: {channel}", "", "Ledger so far:"]
    channel_stats = {k: v for k, v in ledger_stats.items() if k.startswith(channel)}
    if channel_stats:
        for key, stats in channel_stats.items():
            pf = stats["profit_factor"]
            pf_str = f"{pf:.2f}" if pf is not None else "undefined (no losses yet)"
            lines.append(
                f"- {key}: {stats['closed']} closed trades, win rate "
                f"{stats['win_rate']:.0f}%, realized pnl {stats['realized_pnl']:.0f}, "
                f"profit factor {pf_str}"
            )
    else:
        lines.append("- (no closed trades yet for any variant in this channel)")

    lines.append("")
    lines.append(f"Today's market context: {market_context}")
    lines.append("")
    lines.append("Tunable parameters and their allowed ranges (inclusive):")
    for key, (lo, hi) in bounds.items():
        lines.append(f"- {key}: {lo} to {hi}")

    shape = '{"params": {<param_name>: <value>, ...}'
    if mode == "wildcard":
        lines.append("")
        lines.append(
            "This is a WILDCARD slot: in addition to the parameters above, you may "
            "enable up to 2 of these optional composable conditions by name in a "
            f'"toggles" list: {config.STRATEGY_TOGGLE_LIBRARY}'
        )
        shape += ', "toggles": [<name>, ...]'
    shape += ', "rationale": "<max 2 sentences on why this variant should do better>"}'

    lines.append("")
    lines.append(f"Respond with ONLY a JSON object, no prose, no markdown fences, exactly this shape:\n{shape}")
    return "\n".join(lines)


def _sanitize_proposal(channel: str, parsed: dict, bounds: dict, mode: str) -> dict | None:
    if not isinstance(parsed, dict):
        return None
    params_raw = parsed.get("params")
    if not isinstance(params_raw, dict):
        return None

    defaults = _CHANNEL_DEFAULTS[channel]()
    clamped = {}
    for key, (lo, hi) in bounds.items():
        value = params_raw.get(key, defaults[key])
        try:
            clamped[key] = round(max(lo, min(hi, float(value))), 3)
        except (TypeError, ValueError):
            clamped[key] = defaults[key]

    clamped["_toggles"] = []
    if mode == "wildcard":
        toggles_raw = parsed.get("toggles", [])
        if isinstance(toggles_raw, list):
            clamped["_toggles"] = [t for t in toggles_raw
                                   if t in config.STRATEGY_TOGGLE_LIBRARY][:2]

    rationale = parsed.get("rationale", "")
    return {"params": clamped, "rationale": str(rationale) if rationale else ""}


def _next_variant_key(conn: sqlite3.Connection, channel: str) -> str:
    """Counts ALL rows ever created for this channel (active + retired) so a
    retired variant's key is never reused."""
    n = conn.execute(
        "SELECT COUNT(*) FROM strategies WHERE channel=?", (channel,)
    ).fetchone()[0]
    return f"{channel}_v{n + 1}"


def propose_new_variant(conn: sqlite3.Connection, channel: str, ledger_stats: dict,
                        market_context: dict, warnings: list[str],
                        mode: str = "parameter",
                        parent_variant_key: str | None = None,
                        use_llm: bool = True) -> dict:
    """Returns a strategy dict ready for db.insert_strategy (not yet inserted).

    Never blocks the run: if use_llm is False, the CLI is unavailable, or the
    response can't be sanitized, falls back to a bounds-midpoint variant,
    honestly labeled origin="fallback_*" rather than pretending it was
    LLM-authored.
    """
    bounds = config.STRATEGY_PARAM_BOUNDS.get(channel, {})
    variant_key = _next_variant_key(conn, channel)
    # Two outcomes per mode: the LLM actually produced a usable proposal, or we
    # fell back to a bounds-midpoint variant. Both must still record mode="wildcard"
    # in `origin` (fallback_wildcard, not a generic "seed") so _last_wildcard_date's
    # cadence check isn't fooled into thinking no wildcard was ever attempted —
    # that bug would otherwise spawn a fresh wildcard on every run with no LLM.
    llm_origin = "llm_wildcard" if mode == "wildcard" else "llm_parameter_variant"
    fallback_origin = "fallback_wildcard" if mode == "wildcard" else "fallback_parameter_variant"

    parsed = None
    if use_llm:
        prompt = _build_proposal_prompt(channel, ledger_stats, market_context, bounds, mode)
        parsed = _call_claude_cli(prompt, warnings)
    proposal = _sanitize_proposal(channel, parsed, bounds, mode) if parsed is not None else None

    if proposal is None:
        params = {k: round((lo + hi) / 2, 3) for k, (lo, hi) in bounds.items()}
        params["_toggles"] = []
        rationale = "Claude CLI unavailable or proposal unparsable - bounds-midpoint fallback."
        origin = fallback_origin
    else:
        params = proposal["params"]
        rationale = proposal["rationale"] or "(no rationale given)"
        origin = llm_origin

    return {
        "channel": channel,
        "variant_key": variant_key,
        "params_json": json.dumps(params),
        "retirable": 1,
        "origin": origin,
        "parent_variant_key": parent_variant_key,
        "generation_rationale": rationale[:500],
    }


def _days_since(timestamp: str | None, today: str) -> int:
    if not timestamp:
        return 0
    try:
        created = datetime.strptime(timestamp[:10], "%Y-%m-%d").date()
        today_d = datetime.strptime(today, "%Y-%m-%d").date()
        return (today_d - created).days
    except ValueError:
        return 0


def _last_wildcard_date(conn: sqlite3.Connection, channel: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(created_at) AS d FROM strategies WHERE channel=? "
        "AND origin IN ('llm_wildcard', 'fallback_wildcard')",
        (channel,),
    ).fetchone()
    return row["d"] if row and row["d"] else None


def current_capital_weights(conn: sqlite3.Connection) -> dict[str, float]:
    """Public read-only entry point: today's capital weight per active strategy,
    computed fresh from the DB (no side effects) — for callers like the
    observability dashboard that just want to display them, not evolve the fleet.
    """
    all_active = [s for ch in (*config.EVOLVING_CHANNELS, "NEWS", "DISCOVERED")
                 for s in db.get_active_strategies(conn, channel=ch)]
    return _capital_weights(all_active, db.get_strategy_ledger_stats(conn))


def _capital_weights(active_strategies: list[sqlite3.Row], ledger_stats: dict) -> dict[str, float]:
    """Capital weight per active strategy (incl. NEWS), derived from profit
    factor and bounded to [STRATEGY_MIN_CAPITAL_WEIGHT_PCT, STRATEGY_MAX_CAPITAL_WEIGHT_PCT].
    Untested variants get a neutral baseline score rather than zero, so new
    variants aren't starved of capital before they've had a chance to prove
    themselves.

    Weights are independent per-strategy budget caps, not a strict partition
    of one pool — they are NOT renormalized to sum to 100 after clamping,
    since doing so would push a clamped strategy back over the ceiling it was
    just capped at. In an extreme-skew fleet this means weights can sum to
    less than 100; that only makes sizing more conservative, never unsafe.
    """
    if not active_strategies:
        return {}
    scores: dict[str, float] = {}
    for s in active_strategies:
        key = s["variant_key"]
        stats = ledger_stats.get(key)
        if stats is None or stats["closed"] == 0:
            scores[key] = 1.0
        else:
            pf = stats["profit_factor"]
            pf = 2.0 if pf is None else pf  # no losing trades yet - generous but not infinite
            scores[key] = max(0.05, pf)

    total = sum(scores.values())
    # Floor relaxes as the fleet grows so n * floor never structurally exceeds
    # 100% (with the original flat 5% floor, 8+ evolving channels at 2-3
    # variants each would make the floor guarantee mathematically impossible
    # to honor for every strategy at once).
    floor = min(config.STRATEGY_MIN_CAPITAL_WEIGHT_PCT, 100.0 / len(active_strategies))
    ceiling = config.STRATEGY_MAX_CAPITAL_WEIGHT_PCT
    return {k: max(floor, min(ceiling, v / total * 100)) for k, v in scores.items()}


def evaluate_and_evolve(conn: sqlite3.Connection, date: str, run_slot: str,
                        sentiments: dict[str, dict], warnings: list[str],
                        use_llm: bool = True) -> dict:
    """Daily orchestration: retire underperforming/stalled variants, backfill
    them immediately, add a weekly wildcard slot, flag graduate candidates,
    compute today's capital weights, and record today's market context.

    Returns {"active_by_channel", "capital_weights", "ledger_stats",
             "market_context", "events"} for the scan/sizing/reporting steps.
    """
    ledger_stats = db.get_strategy_ledger_stats(conn)

    market_context = market_regime.fetch_regime(warnings)
    avg_sentiment = None
    if sentiments:
        scores = [v.get("score") for v in sentiments.values() if v.get("score") is not None]
        if scores:
            avg_sentiment = sum(scores) / len(scores)
    market_context["avg_market_sentiment"] = avg_sentiment
    db.upsert_strategy_daily_context(conn, date, run_slot, market_context, avg_sentiment)

    events: list[dict] = []

    for channel in config.EVOLVING_CHANNELS:
        for variant in db.get_active_strategies(conn, channel=channel):
            if not variant["retirable"]:
                continue
            key = variant["variant_key"]
            stats = ledger_stats.get(key, {"closed": 0, "win_rate": 0.0, "realized_pnl": 0.0})
            closed = stats["closed"]
            stalled = (closed < config.STRATEGY_MIN_TRADES_FOR_RETIREMENT
                      and _days_since(variant["created_at"], date) >= config.STRATEGY_STALLED_DAYS)
            eligible = closed >= config.STRATEGY_MIN_TRADES_FOR_RETIREMENT
            underperforming = (stats["realized_pnl"] <= 0
                               or stats["win_rate"] < config.STRATEGY_RETIREMENT_WIN_RATE_FLOOR)

            if stalled or (eligible and underperforming):
                reason = (
                    f"stalled: <{config.STRATEGY_MIN_TRADES_FOR_RETIREMENT} trades after "
                    f"{config.STRATEGY_STALLED_DAYS}+ days"
                ) if stalled else (
                    f"underperforming after {closed} trades: win rate "
                    f"{stats['win_rate']:.0f}%, realized pnl {stats['realized_pnl']:.0f}"
                )
                db.retire_strategy(conn, key, reason, date)
                replacement = propose_new_variant(
                    conn, channel, ledger_stats, market_context, warnings,
                    mode="parameter", parent_variant_key=key, use_llm=use_llm)
                db.insert_strategy(conn, replacement)
                events.append({"type": "retired", "variant_key": key, "reason": reason})
                events.append({
                    "type": "created", "variant_key": replacement["variant_key"],
                    "parent": key, "rationale": replacement["generation_rationale"],
                })
            elif (closed >= config.STRATEGY_GRADUATE_MIN_TRADES
                  and stats["win_rate"] >= config.STRATEGY_GRADUATE_WIN_RATE
                  and stats["realized_pnl"] > 0
                  and not variant["graduate_candidate"]):
                db.set_graduate_candidate(conn, key, True)
                events.append({"type": "graduate_candidate", "variant_key": key})

    # DISCOVERED variants retire on the same performance bar, but are NOT
    # backfilled by parameter mutation — they come from strategy_discovery
    # (web/mixer), so a bad spec simply exits the fleet.
    for variant in db.get_active_strategies(conn, channel="DISCOVERED"):
        if not variant["retirable"]:
            continue
        key = variant["variant_key"]
        stats = ledger_stats.get(key, {"closed": 0, "win_rate": 0.0, "realized_pnl": 0.0})
        closed = stats["closed"]
        stalled = (closed < config.STRATEGY_MIN_TRADES_FOR_RETIREMENT
                   and _days_since(variant["created_at"], date) >= config.STRATEGY_STALLED_DAYS)
        eligible = closed >= config.STRATEGY_MIN_TRADES_FOR_RETIREMENT
        underperforming = (stats["realized_pnl"] <= 0
                           or stats["win_rate"] < config.STRATEGY_RETIREMENT_WIN_RATE_FLOOR)
        if stalled or (eligible and underperforming):
            reason = ("stalled: too few trades after "
                      f"{config.STRATEGY_STALLED_DAYS}+ days" if stalled else
                      f"underperforming after {closed} trades: win rate "
                      f"{stats['win_rate']:.0f}%, realized pnl {stats['realized_pnl']:.0f}")
            db.retire_strategy(conn, key, reason, date)
            events.append({"type": "retired", "variant_key": key, "reason": reason})

    for channel in config.EVOLVING_CHANNELS:
        active_count = len(db.get_active_strategies(conn, channel=channel))
        fleet_max = config.STRATEGY_FLEET_MAX_BY_CHANNEL.get(channel, config.STRATEGY_FLEET_MAX)
        if active_count >= fleet_max:
            continue
        last_wildcard = _last_wildcard_date(conn, channel)
        if last_wildcard is None or _days_since(last_wildcard, date) >= config.STRATEGY_WILDCARD_INTERVAL_DAYS:
            wildcard = propose_new_variant(
                conn, channel, ledger_stats, market_context, warnings,
                mode="wildcard", parent_variant_key=None, use_llm=use_llm)
            db.insert_strategy(conn, wildcard)
            events.append({
                "type": "wildcard_created", "variant_key": wildcard["variant_key"],
                "rationale": wildcard["generation_rationale"],
            })

    active_by_channel = {
        channel: db.get_active_strategies(conn, channel=channel)
        for channel in (*config.EVOLVING_CHANNELS, "NEWS", "DISCOVERED")
    }
    all_active = [s for rows in active_by_channel.values() for s in rows]
    capital_weights = _capital_weights(all_active, ledger_stats)

    return {
        "active_by_channel": active_by_channel,
        "capital_weights": capital_weights,
        "ledger_stats": ledger_stats,
        "market_context": market_context,
        "events": events,
    }
