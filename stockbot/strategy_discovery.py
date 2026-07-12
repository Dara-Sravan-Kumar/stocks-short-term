"""Discovery + registration pipeline for data-driven strategy specs.

Phase 2 (this module today):
  * backtest_gate — replay a spec on historical daily bars split into IN-SAMPLE
    and OUT-OF-SAMPLE windows, and decide whether it clears the bar to go live.
    The OOS window is the real gate; requiring in-sample to also be net-positive
    guards against a spec that only looks good on the held-out slice by luck.
    This is the overfitting defense.
  * register_spec — validate -> fleet-cap/dup checks -> backtest_gate -> insert
    into the strategies table as a DISCOVERED variant. From there the live scan
    (signals.scan_discovered_picks) trades it and the fleet's retire/graduate
    machinery judges it on real paper outcomes.

Phase 3 (next): discover_from_web — search for published SWING strategies,
translate them into specs over the existing indicator vocabulary, and feed each
through register_spec.
"""
from __future__ import annotations

import json
import re

import config
from stockbot import backtest, db, strategy_engine
from stockbot.strategy_spec import FIELD_GLOSSARY, StrategySpec, validate_spec


def _variant_from_spec(spec: StrategySpec) -> dict:
    return {
        "variant_key": spec.name,
        "channel": "DISCOVERED",
        "params": {"entry_expr": spec.entry_expr,
                   "min_reward_risk": spec.min_reward_risk,
                   "_toggles": []},
    }


def _split_calendar(histories: dict, eval_days: int, oos_fraction: float):
    """Return (in_sample_days, oos_days, first_oos_date) or None if too little
    history to form both windows."""
    calendar = sorted({d for df in histories.values() for d in df.index})
    window = calendar[-eval_days:] if len(calendar) > eval_days else calendar
    oos_len = max(1, int(len(window) * oos_fraction))
    is_len = len(window) - oos_len
    if is_len < 1:
        return None
    return is_len, oos_len, window[is_len]  # window[is_len] = first OOS date


def _truncate_before(histories: dict, split_date) -> dict:
    out = {}
    for ticker, df in histories.items():
        sub = df.loc[df.index < split_date]
        if len(sub):
            out[ticker] = sub
    return out


def _pf_ok(profit_factor) -> bool:
    if profit_factor == float("inf"):
        return True
    return profit_factor is not None and profit_factor >= config.BACKTEST_GATE_MIN_PROFIT_FACTOR


def backtest_gate(spec: StrategySpec, histories: dict, warnings: list[str]) -> dict:
    """Replay `spec` on in-sample + out-of-sample windows and decide.

    Returns {"passed", "reasons", "in_sample", "out_of_sample"}.
    """
    variant = _variant_from_spec(spec)
    split = _split_calendar(histories, config.BACKTEST_GATE_EVAL_DAYS,
                            config.BACKTEST_GATE_OOS_FRACTION)
    if split is None:
        return {"passed": False, "reasons": ["not enough history to backtest"],
                "in_sample": None, "out_of_sample": None}
    is_len, oos_len, split_date = split

    oos = backtest.run_backtest(histories, [variant], eval_days=oos_len)[spec.name]
    is_hist = _truncate_before(histories, split_date)
    is_rep = (backtest.run_backtest(is_hist, [variant], eval_days=is_len)[spec.name]
              if is_hist else None)

    reasons: list[str] = []
    if oos["trades"] < config.BACKTEST_GATE_MIN_TRADES:
        reasons.append(f"OOS too few trades ({oos['trades']} < "
                       f"{config.BACKTEST_GATE_MIN_TRADES})")
    if (oos["win_rate_pct"] or 0) < config.BACKTEST_GATE_MIN_WIN_RATE:
        reasons.append(f"OOS win rate {oos['win_rate_pct']} < "
                       f"{config.BACKTEST_GATE_MIN_WIN_RATE}")
    if not _pf_ok(oos["profit_factor"]):
        reasons.append(f"OOS profit factor {oos['profit_factor']} < "
                       f"{config.BACKTEST_GATE_MIN_PROFIT_FACTOR}")
    if is_rep is not None and (is_rep["net_inr"] or 0) <= 0:
        reasons.append("in-sample net P&L not positive (inconsistent with OOS)")

    return {"passed": not reasons, "reasons": reasons,
            "in_sample": is_rep, "out_of_sample": oos}


def register_spec(conn, spec: StrategySpec, histories: dict, warnings: list[str],
                  source: str = "web_discovered") -> dict:
    """validate -> fleet-cap/dup -> backtest_gate -> insert as a DISCOVERED
    variant. Returns a result dict; only registered=True means it went live."""
    ok, reason = validate_spec(spec)
    if not ok:
        return {"registered": False, "stage": "validate", "reason": reason}

    active = db.get_active_strategies(conn, channel="DISCOVERED")
    if len(active) >= config.DISCOVERED_FLEET_MAX:
        return {"registered": False, "stage": "fleet_cap",
                "reason": f"DISCOVERED fleet at cap ({config.DISCOVERED_FLEET_MAX})"}

    if conn.execute("SELECT 1 FROM strategies WHERE variant_key=?",
                    (spec.name,)).fetchone():
        return {"registered": False, "stage": "duplicate",
                "reason": f"variant_key {spec.name!r} already exists"}

    gate = backtest_gate(spec, histories, warnings)
    if not gate["passed"]:
        return {"registered": False, "stage": "backtest_gate",
                "reason": "; ".join(gate["reasons"]), "gate": gate}

    db.insert_strategy(conn, {
        "channel": "DISCOVERED",
        "variant_key": spec.name,
        "params_json": json.dumps({"entry_expr": spec.entry_expr,
                                   "min_reward_risk": spec.min_reward_risk}),
        "retirable": 1,
        "origin": source,
        "parent_variant_key": None,
        "generation_rationale": (spec.rationale or "")[:500],
    })
    return {"registered": True, "variant_key": spec.name, "gate": gate}


# --------------------------------------------------------------------------- #
# Phase 3 — the discoverer: propose published SWING strategies as specs, then
# push each through register_spec (validate + backtest gate).
# --------------------------------------------------------------------------- #
def _build_discovery_prompt(existing_exprs: list[str], n: int) -> str:
    glossary = "\n".join(f"  {k}: {v}" for k, v in FIELD_GLOSSARY.items())
    existing = "\n".join(f"  - {e}" for e in existing_exprs) or "  (none yet)"
    return (
        "You are a quantitative researcher curating PUBLISHED short-term SWING "
        "trading strategies (holding ~2-20 trading days on DAILY bars) for an NSE "
        "India paper-trading bot. Draw on well-documented strategies — momentum "
        "breakouts, moving-average pullbacks, mean-reversion, order-flow/volume "
        "setups, 52-week-high leadership, etc.\n\n"
        "Express each strategy's ENTRY as a boolean expression over ONLY these "
        "fields the bot computes per stock per day:\n" + glossary + "\n\n"
        "HARD RULES (violations are discarded):\n"
        "- DAILY-bar SWING only. NO intraday/scalping, NO long-term/fundamental "
        "strategies — the bot cannot evaluate them.\n"
        "- Use ONLY the field names above, numeric constants, comparisons "
        "(< <= > >= == !=), and/or/not, + - * /, and min()/max()/abs(). Nothing else.\n"
        "- Keep it to 2-4 conditions; complex expressions overfit.\n"
        f"- Each must be DISTINCT from these already-registered entries:\n{existing}\n\n"
        f"Return ONLY this JSON object, up to {n} strategies:\n"
        '{"strategies": [{"name": "<short_snake_case>", '
        '"entry_expr": "<expression>", "min_reward_risk": <number 1.2-3.0>, '
        '"rationale": "<one sentence naming the published strategy>"}]}'
    )


def _clamp_rr(value) -> float:
    try:
        return round(max(1.2, min(3.0, float(value))), 2)
    except (TypeError, ValueError):
        return 1.5


def _safe_name(raw, conn, prefix: str = "disc") -> str:
    clean = re.sub(r"[^a-z0-9_]", "", str(raw or "").lower().replace(" ", "_"))[:32]
    base = f"{prefix}_{clean}" if clean else prefix
    name, i = base, 2
    while conn.execute("SELECT 1 FROM strategies WHERE variant_key=?", (name,)).fetchone():
        name = f"{base}_{i}"
        i += 1
    return name


def discover_and_register(conn, histories: dict, warnings: list[str],
                          max_candidates: int = 6, use_llm: bool = True) -> dict:
    """Ask the LLM for published SWING strategies, translate to specs, and push
    each through the backtest gate. Returns {"proposed", "registered", "rejected"}."""
    report = {"proposed": 0, "registered": [], "rejected": []}
    if not use_llm:
        return report

    existing = [json.loads(r["params_json"]).get("entry_expr", "")
                for r in db.get_active_strategies(conn, channel="DISCOVERED")
                if r["params_json"]]
    parsed = strategy_engine._call_claude_cli(
        _build_discovery_prompt(existing, max_candidates), warnings)
    candidates = parsed.get("strategies") if isinstance(parsed, dict) else None
    if not isinstance(candidates, list):
        warnings.append("strategy discovery: no usable proposals from Claude CLI")
        return report

    for cand in candidates[:max_candidates]:
        if not isinstance(cand, dict) or not cand.get("entry_expr"):
            continue
        report["proposed"] += 1
        spec = StrategySpec(
            name=_safe_name(cand.get("name"), conn),
            entry_expr=str(cand["entry_expr"]),
            source="web_discovered",
            min_reward_risk=_clamp_rr(cand.get("min_reward_risk")),
            rationale=str(cand.get("rationale", ""))[:300])
        res = register_spec(conn, spec, histories, warnings, source="web_discovered")
        if res["registered"]:
            report["registered"].append({"name": spec.name, "entry_expr": spec.entry_expr,
                                          "rationale": spec.rationale})
        else:
            report["rejected"].append({"name": spec.name, "stage": res["stage"],
                                       "reason": res["reason"]})
    return report
