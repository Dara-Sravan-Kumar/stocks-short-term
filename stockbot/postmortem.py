"""Trade post-mortem — the reflective front-end of the evening R&D run.

Before the discoverer proposes anything, this reads the most-recent CLOSED paper
positions and asks the LLM to diagnose WHAT ACTUALLY WENT WRONG (and right) on
this book: are stops too tight, are winners exited early, is one channel bleeding
in the current regime, do certain entry conditions precede losers? The distilled
lessons are handed to strategy_discovery so new specs answer real failures on
OUR trades rather than generic textbook ideas.

Purely advisory and fully guarded: no LLM, too few trades, or an unparseable
reply all yield empty lessons, and discovery/mixing proceed unchanged.
"""
from __future__ import annotations

from datetime import date

import config
from stockbot import db, strategy_engine


def _holding_days(entry: str | None, exit_: str | None) -> int | None:
    try:
        d0 = date.fromisoformat(str(entry)[:10])
        d1 = date.fromisoformat(str(exit_)[:10])
        return (d1 - d0).days
    except (TypeError, ValueError):
        return None


def _trade_line(r) -> str:
    entry = r["entry_fill_price"]
    exit_ = r["exit_fill_price"]
    pnl_pct = ((exit_ - entry) / entry * 100.0) if entry else 0.0
    held = _holding_days(r["entry_date"], r["exit_date"])
    held_str = f"{held}d" if held is not None else "?"
    # distances the trade was aiming for, as % from entry — surfaces tight stops /
    # far targets without the model needing raw price levels.
    tgt = r["target_price"]
    stop = r["stop_price"]
    tgt_pct = ((tgt - entry) / entry * 100.0) if entry and tgt else None
    stop_pct = ((stop - entry) / entry * 100.0) if entry and stop else None
    tp = f"{tgt_pct:+.1f}%" if tgt_pct is not None else "?"
    sp = f"{stop_pct:+.1f}%" if stop_pct is not None else "?"
    return (f"- {r['ticker']} [{r['strategy']}] pnl {pnl_pct:+.1f}% in {held_str}, "
            f"exit={r['exit_reason'] or '?'} (target {tp} / stop {sp})")


def _build_prompt(rows) -> str:
    wins = sum(1 for r in rows
               if (r["realized_pnl"] or 0) > 0)
    losses = len(rows) - wins
    lines = [
        "You are a trading-desk risk reviewer running a post-mortem on an NSE "
        "(India) short-term SWING paper book (daily bars, holds ~2-20 days). "
        f"Here are the {len(rows)} most-recent CLOSED trades ({wins} winners, "
        f"{losses} losers). Each shows realized P&L %, holding days, the exit "
        "trigger, and the target/stop distances the trade was set for:",
        "",
    ]
    lines += [_trade_line(r) for r in rows]
    lines += [
        "",
        "Diagnose the SYSTEMATIC patterns — not one-off trades. Look for: stops "
        "so tight they cut trades before the thesis played out; winners exited "
        "early vs. targets never reached; a strategy/channel that consistently "
        "bleeds; entry conditions that precede losers; regime mismatch. Turn each "
        "into a concrete, actionable lesson a strategy designer can act on "
        "(e.g. 'PULLBACK stops at -3% get hit on normal noise — widen toward -5%').",
        "",
        "Respond with ONLY this JSON object, no prose, no markdown fences:",
        '{"lessons": ["<=6 short actionable lessons"], '
        '"diagnosis": "<=2 sentence summary"}',
    ]
    return "\n".join(lines)


def analyze_recent_trades(conn, warnings: list[str], use_llm: bool = True) -> dict:
    """Review recent closed paper trades and return distilled lessons.

    Returns {"reviewed": int, "lessons": [str], "diagnosis": str}. Empty lessons
    means: disabled, too few trades, no LLM, or unparseable reply — callers
    proceed unchanged.
    """
    empty = {"reviewed": 0, "lessons": [], "diagnosis": ""}
    if not (use_llm and config.POSTMORTEM_ENABLED):
        return empty

    rows = db.get_recent_closed_paper_positions(conn, config.POSTMORTEM_LOOKBACK_TRADES)
    if len(rows) < config.POSTMORTEM_MIN_TRADES:
        return empty

    parsed = strategy_engine._call_claude_cli(_build_prompt(rows), warnings)
    if not isinstance(parsed, dict):
        warnings.append("trade post-mortem: no usable diagnosis from Claude CLI")
        return {**empty, "reviewed": len(rows)}

    raw = parsed.get("lessons")
    lessons = [str(x).strip() for x in raw if str(x).strip()][:6] if isinstance(raw, list) else []
    return {"reviewed": len(rows), "lessons": lessons,
            "diagnosis": str(parsed.get("diagnosis", "")).strip()[:300]}
