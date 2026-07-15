"""Strategy-as-data: a portable strategy spec + a SAFE expression evaluator.

The self-evolving fleet historically only tuned numbers inside hardcoded Python
gates (stockbot/signals.py). This module lets a strategy's ENTRY LOGIC itself be
data — a boolean expression over the indicators Snapshot already computes — so
new strategies (discovered from the web, or bred by the mixer) can enter the
fleet without anyone writing or executing new code.

SAFETY MODEL (this is trading-adjacent code — read before touching):
  * Expressions are parsed with `ast` and evaluated by a hand-written interpreter
    (`_eval_node`). There is NO eval()/exec(), NO builtins, NO attribute access,
    NO subscripting, NO comprehensions, NO lambdas.
  * The only names an expression may reference are Snapshot's numeric/bool fields
    (auto-derived, so new indicators become available for free) plus a tiny fixed
    set of safe functions (min/max/abs).
  * `validate_spec`/`validate_expr` reject anything else up front; at runtime any
    error (e.g. comparing a None FVG field) is swallowed and the gate simply does
    NOT fire — a broken strategy can never crash a scan or size a position.

HORIZON: specs are constrained to SWING (multi-day, daily-bar) strategies. The
bot has no intraday feed and exits technically over days, so intraday and
long-term/fundamental strategies are rejected at registration.
"""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field, fields
from typing import Any

from stockbot.indicators import Snapshot, derive_target_stop

# --------------------------------------------------------------------------- #
# Vocabulary — the ONLY names an entry expression may reference.
# Auto-derived from Snapshot so adding an indicator column instantly widens the
# vocabulary (ticker/date are strings, excluded).
# --------------------------------------------------------------------------- #
ALLOWED_FIELDS: frozenset[str] = frozenset(
    f.name for f in fields(Snapshot) if f.name not in ("ticker", "date")
)
SAFE_FUNCS: dict[str, Any] = {"min": min, "max": max, "abs": abs}

# Horizon is enforced: this bot is daily-bar swing only.
ALLOWED_HORIZONS: frozenset[str] = frozenset({"SWING"})

# Human-readable glossary handed to the discoverer LLM so it uses fields
# correctly (every ALLOWED_FIELD is usable; these are the tradeable highlights).
FIELD_GLOSSARY: dict[str, str] = {
    "close": "latest close price", "high": "latest bar high", "low": "latest bar low",
    "close_prev": "prior bar's close",
    "sma20": "20-day simple moving average", "sma50": "50-day SMA",
    "rsi": "14-day RSI (0-100)",
    "macd": "MACD line", "macd_signal": "MACD signal line", "macd_hist": "MACD histogram",
    "macd_bullish_cross_recent": "bool: MACD crossed up within lookback",
    "mom_5d": "5-day % return", "mom_20d": "20-day % return",
    "vol_ratio": "today's volume / 20-day avg volume",
    "avg_turnover_20d": "20-day avg close*volume in INR (liquidity)",
    "cmf": "Chaikin Money Flow(20), order-flow proxy (-1..1)",
    "anchored_vwap": "VWAP anchored to the recent swing low",
    "volume_poc": "60-bar volume point-of-control price",
    "high_252d": "52-week high", "swing_low_10d": "10-bar swing low",
    "closes_below_sma20": "consecutive closes below SMA20",
    "pivot": "daily pivot", "r1": "resistance 1", "r2": "resistance 2",
    "s1": "support 1", "s2": "support 2",
    "weekly_r1": "weekly resistance 1",
}

_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
    ast.USub, ast.UAdd, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div,
    ast.Compare, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq,
    ast.Name, ast.Load, ast.Constant, ast.Call,
)


class SpecError(ValueError):
    """A strategy spec / expression failed validation."""


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_expr(expr: str) -> tuple[bool, str]:
    """Return (ok, reason). ok=True means the expression is structurally safe and
    references only known fields/functions. Does NOT execute anything."""
    if not isinstance(expr, str) or not expr.strip():
        return False, "empty expression"
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        return False, f"syntax error: {exc.msg}"

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            return False, f"forbidden syntax: {type(node).__name__}"
        if isinstance(node, ast.Call):
            # only calls to bare safe-function names, e.g. max(a, b)
            if not isinstance(node.func, ast.Name) or node.func.id not in SAFE_FUNCS:
                return False, "only min/max/abs calls are allowed"
            if node.keywords:
                return False, "keyword arguments are not allowed"
        if isinstance(node, ast.Name) and node.id not in ALLOWED_FIELDS \
                and node.id not in SAFE_FUNCS:
            return False, f"unknown name: {node.id}"
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, bool)):
            return False, "only numeric/bool constants are allowed"
    return True, "ok"


# --------------------------------------------------------------------------- #
# Safe evaluation — a hand-written interpreter (never eval()).
# --------------------------------------------------------------------------- #
def _eval_node(node: ast.AST, ns: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, ns)
    if isinstance(node, ast.BoolOp):
        vals = (_eval_node(v, ns) for v in node.values)
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.UnaryOp):
        v = _eval_node(node.operand, ns)
        if isinstance(node.op, ast.Not):
            return not v
        if isinstance(node.op, ast.USub):
            return -v
        return +v
    if isinstance(node, ast.BinOp):
        a, b = _eval_node(node.left, ns), _eval_node(node.right, ns)
        if isinstance(node.op, ast.Add):
            return a + b
        if isinstance(node.op, ast.Sub):
            return a - b
        if isinstance(node.op, ast.Mult):
            return a * b
        return a / b
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, ns)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval_node(comparator, ns)
            if not _compare(op, left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Call):
        func = SAFE_FUNCS[node.func.id]          # validated to exist
        return func(*[_eval_node(a, ns) for a in node.args])
    if isinstance(node, ast.Name):
        return ns[node.id]                        # KeyError -> caught upstream
    if isinstance(node, ast.Constant):
        return node.value
    raise SpecError(f"forbidden node at eval time: {type(node).__name__}")


def _compare(op: ast.cmpop, a: Any, b: Any) -> bool:
    if isinstance(op, ast.Lt):
        return a < b
    if isinstance(op, ast.LtE):
        return a <= b
    if isinstance(op, ast.Gt):
        return a > b
    if isinstance(op, ast.GtE):
        return a >= b
    if isinstance(op, ast.Eq):
        return a == b
    return a != b


def evaluate_entry(expr: str, snap: Snapshot) -> bool:
    """Evaluate a validated entry expression against a Snapshot. Returns False on
    ANY error (unknown field, None comparison, div-by-zero) so a malformed
    strategy never fires and never crashes the scan. Validate once at
    registration; this is the hot path."""
    try:
        tree = ast.parse(expr, mode="eval")
        ns = {name: getattr(snap, name) for name in ALLOWED_FIELDS}
        return bool(_eval_node(tree, ns))
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# The spec
# --------------------------------------------------------------------------- #
@dataclass
class StrategySpec:
    """A data-driven strategy. `entry_expr` is the novel part; target/stop reuse
    the same pivot-ladder machinery every existing channel uses, so specs slot
    into the fleet with no special-casing."""
    name: str
    entry_expr: str
    source: str = "seed"          # "web_discovered" | "mixer" | "seed" | "manual"
    horizon: str = "SWING"
    min_reward_risk: float = 1.5
    min_upside_pct: float | None = None   # None -> config.MIN_UPSIDE_PCT
    max_risk_pct: float | None = None     # None -> config.MAX_RISK_PCT
    rationale: str = ""
    params: dict[str, Any] = field(default_factory=dict)  # extra metadata

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, blob: str) -> "StrategySpec":
        return cls(**json.loads(blob))


def validate_spec(spec: StrategySpec) -> tuple[bool, str]:
    if spec.horizon not in ALLOWED_HORIZONS:
        return False, (f"horizon {spec.horizon!r} not allowed — this bot is "
                       f"daily-bar swing only ({', '.join(ALLOWED_HORIZONS)})")
    ok, reason = validate_expr(spec.entry_expr)
    if not ok:
        return False, f"entry_expr rejected: {reason}"
    if spec.min_reward_risk <= 0:
        return False, "min_reward_risk must be positive"
    return True, "ok"


# --------------------------------------------------------------------------- #
# Adapter into the scan — a spec becomes a gate + target/stop, exactly the shape
# stockbot/signals._gather_candidates already consumes.
# --------------------------------------------------------------------------- #
def spec_matches(spec: StrategySpec, snap: Snapshot) -> bool:
    """Entry gate for a spec-driven variant (liquidity is applied separately by
    the scan skeleton, as with every channel)."""
    return evaluate_entry(spec.entry_expr, snap)


def spec_target_stop(spec: StrategySpec, snap: Snapshot) -> tuple[float | None, float]:
    import config
    min_up = spec.min_upside_pct if spec.min_upside_pct is not None else config.MIN_UPSIDE_PCT
    max_risk = spec.max_risk_pct if spec.max_risk_pct is not None else config.MAX_RISK_PCT
    return derive_target_stop(snap, min_up, max_risk, config.MIN_STOP_ATR_MULT)
