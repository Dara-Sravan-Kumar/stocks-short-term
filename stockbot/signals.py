"""Entry scan: turn technical snapshots + fundamentals + sentiment into new picks."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import config
from stockbot import db, fundamentals
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


def _passes_technicals(s: Snapshot) -> bool:
    if not _is_liquid(s):
        return False
    if not (s.close > s.sma20 > s.sma50):
        return False
    if not (config.RSI_ENTRY_MIN <= s.rsi <= config.RSI_ENTRY_MAX):
        return False
    macd_bullish = s.macd > s.macd_signal and (
        s.macd_hist > s.macd_hist_prev or s.macd_bullish_cross_recent
    )
    if not macd_bullish:
        return False
    if s.close < s.pivot:
        return False
    return True


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


def scan_new_picks(conn: sqlite3.Connection, snapshots: dict[str, Snapshot],
                   sentiments: dict[str, dict], date: str,
                   warnings: list[str], top_n: int | None = None) -> list[dict]:
    """Evaluate the watchlist and insert up to top_n new ACTIVE picks."""
    top_n = top_n or config.MAX_NEW_PICKS_PER_DAY
    active = {p["ticker"] for p in db.get_active_picks(conn)}

    candidates: list[Candidate] = []
    for ticker in config.WATCHLIST:
        s = snapshots.get(ticker)
        if s is None or ticker in active:
            continue
        if not _passes_technicals(s):
            continue

        target, stop = derive_target_stop(s, config.MIN_UPSIDE_PCT, config.MAX_RISK_PCT)
        if target is None:
            continue  # no resistance rung offers enough upside
        upside = (target - s.close) / s.close * 100
        risk = (s.close - stop) / s.close * 100
        if upside < config.MIN_UPSIDE_PCT or risk <= 0:
            continue
        rr = upside / risk
        if rr < config.MIN_REWARD_RISK:
            continue

        # fundamentals fetched lazily — only for technical survivors
        if not fundamentals.check_fundamentals(conn, ticker, date, warnings):
            continue

        sent = sentiments.get(ticker, {"score": 0.0})["score"] or 0.0
        if sent <= config.SENTIMENT_ENTRY_MIN:
            continue

        score, notes = _rank_score(s, sent)
        tier = config.TIER.get(ticker, "LARGE")
        base = (f"[{tier}] Uptrend (close>SMA20>SMA50), RSI {s.rsi:.0f}, MACD bullish, "
                f"above pivot; R:R {rr:.1f}")
        rationale = base + ("; " + ", ".join(notes) if notes else "")
        candidates.append(Candidate(s, target, stop, rr, sent, score, rationale))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return _insert_candidates(conn, candidates[:top_n], date, "TECHNICAL")


def scan_news_picks(conn: sqlite3.Connection, snapshots: dict[str, Snapshot],
                    sentiments: dict[str, dict], date: str,
                    warnings: list[str], top_n: int | None = None) -> list[dict]:
    """News-first channel: strong bullish catalyst leads, chart confirms.

    Gate: sentiment >= NEWS_SENTIMENT_MIN with confidence >= NEWS_CONFIDENCE_MIN,
    then close > SMA20, RSI <= NEWS_RSI_MAX, liquidity, fundamentals, and a
    pivot-ladder target with R:R >= NEWS_MIN_REWARD_RISK.
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
        if db.insert_pick(conn, pick):
            pick["reward_risk"] = round(c.reward_risk, 2)
            inserted.append(pick)
    return inserted
