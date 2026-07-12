"""Phase 4 — the genetic strategy mixer.

Breeds NEW candidate specs by recombining strategies already in the ledger
(crossover of their AND-clauses) plus a small library of seed genes expressing
the proven base channels, then mutating one numeric threshold. Every offspring
goes through the SAME backtest gate as the web discoverer, so only those
clearing out-of-sample performance enter the fleet.

Representation: an entry_expr is (almost always) a conjunction of conditions.
  * crossover = mix the two parents' conjuncts, deduped and bounded
  * mutation  = nudge exactly one numeric literal
Both operate on the validated safe-AST form (field names are ast.Name nodes,
never touched), so offspring stay expressible over the existing vocabulary and
can never smuggle in new syntax. Contradictory offspring (e.g. rsi>60 and rsi<40)
simply never fire and are dropped by the backtest gate.
"""
from __future__ import annotations

import ast
import json
import random

from stockbot import db
from stockbot.strategy_discovery import _safe_name, register_spec
from stockbot.strategy_spec import StrategySpec

# Proven base channels as specs — genetic seed material so the mixer has a
# diverse gene pool from day one (before any DISCOVERED spec exists).
SEED_GENES = [
    "close > sma20 and sma20 > sma50 and rsi > 45 and rsi < 68 and macd_hist > macd_hist_prev",
    "close > sma50 and mom_20d > 3 and mom_5d < 0 and rsi > 35 and rsi < 55",
    "cmf > 0.05 and close > sma20 and vol_ratio > 1.2",
    "close >= high_252d * 0.98 and vol_ratio > 1.3",
    "close > anchored_vwap and mom_20d > 0 and rsi > 45",
    "close >= volume_poc and close > sma20 and cmf > 0",
]


def _clauses(expr: str) -> list[str]:
    """Top-level AND-conjuncts of an expression, as strings."""
    body = ast.parse(expr, mode="eval").body
    if isinstance(body, ast.BoolOp) and isinstance(body.op, ast.And):
        return [ast.unparse(v) for v in body.values]
    return [ast.unparse(body)]


class _NudgeOneConstant(ast.NodeTransformer):
    """Multiply the FIRST numeric literal by a small random factor. Field names
    are ast.Name nodes (e.g. mom_20d, high_252d), so they are never affected."""

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.done = False

    def visit_Constant(self, node):
        if (not self.done and isinstance(node.value, (int, float))
                and not isinstance(node.value, bool)):
            node.value = round(node.value * self.rng.uniform(0.85, 1.15), 3)
            self.done = True
        return node


def _mutate(expr: str, rng: random.Random) -> str:
    tree = ast.parse(expr, mode="eval")
    _NudgeOneConstant(rng).visit(tree)
    return ast.unparse(tree.body)


def _crossover(a: str, b: str, rng: random.Random, max_clauses: int = 4) -> str:
    pool = list(dict.fromkeys(_clauses(a) + _clauses(b)))  # dedup, keep order
    rng.shuffle(pool)
    k = min(len(pool), rng.randint(2, max_clauses))
    return " and ".join(pool[:k])


def _gene_pool(conn) -> list[dict]:
    stats = db.get_strategy_ledger_stats(conn)
    genes: list[dict] = []
    for row in db.get_active_strategies(conn, channel="DISCOVERED"):
        params = json.loads(row["params_json"] or "{}")
        expr = params.get("entry_expr")
        if not expr:
            continue
        wr = (stats.get(row["variant_key"], {}) or {}).get("win_rate", 0.0) or 0.0
        genes.append({"expr": expr, "fitness": max(0.3, 0.5 + wr / 200.0),
                      "min_rr": params.get("min_reward_risk", 1.5)})
    for expr in SEED_GENES:
        genes.append({"expr": expr, "fitness": 0.5, "min_rr": 1.5})
    return genes


def _select_parents(genes: list[dict], rng: random.Random) -> tuple[dict, dict]:
    a = rng.choices(genes, weights=[g["fitness"] for g in genes], k=1)[0]
    rest = [g for g in genes if g is not a]
    b = rng.choices(rest, weights=[g["fitness"] for g in rest], k=1)[0]
    return a, b


def mix_and_register(conn, histories: dict, warnings: list[str],
                     max_offspring: int = 5,
                     rng: random.Random | None = None) -> dict:
    """Breed offspring from the ledger's best + seed genes, gate each, register
    survivors. Returns {"proposed", "registered", "rejected"}."""
    rng = rng or random.Random()
    report = {"proposed": 0, "registered": [], "rejected": []}
    genes = _gene_pool(conn)
    if len(genes) < 2:
        return report

    seen: set[str] = set()
    attempts = 0
    while report["proposed"] < max_offspring and attempts < max_offspring * 5:
        attempts += 1
        a, b = _select_parents(genes, rng)
        child = _crossover(a["expr"], b["expr"], rng)
        if rng.random() < 0.6:
            child = _mutate(child, rng)
        if not child or child in seen:
            continue
        seen.add(child)
        report["proposed"] += 1
        spec = StrategySpec(
            name=_safe_name("", conn, prefix="mix"), entry_expr=child,
            source="mixer",
            min_reward_risk=round((a["min_rr"] + b["min_rr"]) / 2, 2),
            rationale="genetic crossover of ledger + seed strategies")
        res = register_spec(conn, spec, histories, warnings, source="mixer")
        if res["registered"]:
            report["registered"].append({"name": spec.name, "entry_expr": child})
        else:
            report["rejected"].append({"name": spec.name, "stage": res["stage"],
                                       "reason": res["reason"]})
    return report
