"""Paper trading engine — virtual cash book over the pick/exit signals.

One shared book (config.PAPER_STARTING_CASH) trades every new pick from any
channel; positions are tagged by strategy for P&L attribution. Fills are
end-of-day approximations: entry at the signal close, exit at the exit
engine's price (stop for STOPPED_OUT, target for TARGET_HIT, close
otherwise), both nudged by PAPER_SLIPPAGE_PCT. Stop exits therefore assume a
fill exactly at the stop — real gap-downs can fill worse.

All rupee amounts include the Indian delivery cost model from config
(brokerage, STT, exchange, SEBI, stamp, GST, DP).
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

import config
from stockbot import db
from stockbot.indicators import Snapshot


def _pct(x: float) -> float:
    return x / 100.0


@dataclass
class CostBreakdown:
    brokerage: float
    stt: float
    exch_txn: float
    sebi: float
    stamp: float
    gst: float
    dp: float

    @property
    def total(self) -> float:
        return (self.brokerage + self.stt + self.exch_txn + self.sebi
                + self.stamp + self.gst + self.dp)


def buy_fill(ref_price: float) -> float:
    return ref_price * (1 + _pct(config.PAPER_SLIPPAGE_PCT))


def sell_fill(ref_price: float) -> float:
    return ref_price * (1 - _pct(config.PAPER_SLIPPAGE_PCT))


def buy_costs(qty: int, fill_price: float) -> CostBreakdown:
    turnover = qty * fill_price
    brokerage = config.PAPER_BROKERAGE_PER_ORDER
    stt = turnover * _pct(config.PAPER_STT_PCT)
    exch = turnover * _pct(config.PAPER_EXCH_TXN_PCT)
    sebi = turnover * _pct(config.PAPER_SEBI_PCT)
    stamp = turnover * _pct(config.PAPER_STAMP_PCT_BUY)
    gst = _pct(config.PAPER_GST_PCT) * (brokerage + exch + sebi)
    return CostBreakdown(brokerage, stt, exch, sebi, stamp, gst, 0.0)


def sell_costs(qty: int, fill_price: float) -> CostBreakdown:
    turnover = qty * fill_price
    brokerage = config.PAPER_BROKERAGE_PER_ORDER
    stt = turnover * _pct(config.PAPER_STT_PCT)
    exch = turnover * _pct(config.PAPER_EXCH_TXN_PCT)
    sebi = turnover * _pct(config.PAPER_SEBI_PCT)
    dp = config.PAPER_DP_CHARGE_SELL
    gst = _pct(config.PAPER_GST_PCT) * (brokerage + exch + sebi + dp)
    return CostBreakdown(brokerage, stt, exch, sebi, 0.0, gst, dp)


def size_position(equity: float, cash: float, entry_ref: float,
                  stop: float) -> tuple[int, str]:
    """Risk-based sizing: qty risking PAPER_RISK_PCT_PER_TRADE of equity
    between entry and stop, capped by position-% and affordable cash.

    Returns (qty, note); qty 0 means skip and the note says why.
    """
    per_share_risk = entry_ref - stop
    if per_share_risk <= 0:
        return 0, "invalid stop (at or above entry)"
    fill = buy_fill(entry_ref)

    qty_risk = math.floor(equity * _pct(config.PAPER_RISK_PCT_PER_TRADE) / per_share_risk)
    qty_pos = math.floor(equity * _pct(config.PAPER_MAX_POSITION_PCT) / fill)

    fixed_buy = config.PAPER_BROKERAGE_PER_ORDER * (1 + _pct(config.PAPER_GST_PCT))
    var_rate = (_pct(config.PAPER_STT_PCT) + _pct(config.PAPER_EXCH_TXN_PCT)
                + _pct(config.PAPER_STAMP_PCT_BUY) + _pct(config.PAPER_SEBI_PCT)
                + _pct(config.PAPER_GST_PCT)
                * (_pct(config.PAPER_EXCH_TXN_PCT) + _pct(config.PAPER_SEBI_PCT)))
    available = cash - config.PAPER_MIN_CASH_BUFFER - fixed_buy
    qty_cash = math.floor(available / (fill * (1 + var_rate))) if available > 0 else 0

    qty = min(qty_risk, qty_pos, qty_cash)
    if qty < 1:
        if qty_cash < 1:
            return 0, f"insufficient cash (Rs {cash:,.0f} free)"
        if qty_risk < 1:
            return 0, (f"one share risks more than "
                       f"{config.PAPER_RISK_PCT_PER_TRADE}% of equity")
        return 0, "position cap leaves no room"
    if qty * fill < config.PAPER_MIN_POSITION_VALUE:
        return 0, (f"position too small (Rs {qty * fill:,.0f} < "
                   f"Rs {config.PAPER_MIN_POSITION_VALUE:,.0f} minimum - "
                   "fixed charges would eat returns)")
    binding = ("risk" if qty == qty_risk else
               "position cap" if qty == qty_pos else "cash")
    return qty, f"sized by {binding} (risk {qty_risk} / cap {qty_pos} / cash {qty_cash})"


def _positions_value(positions, snapshots: dict[str, Snapshot]) -> float:
    total = 0.0
    for p in positions:
        snap = snapshots.get(p["ticker"])
        ltp = snap.close if snap else p["entry_fill_price"]
        total += p["qty"] * ltp
    return total


def open_positions_for_picks(conn: sqlite3.Connection, new_picks: list[dict],
                             snapshots: dict[str, Snapshot], run_date: str,
                             run_slot: str, warnings: list[str]) -> list[dict]:
    """Paper-buy every new pick (any channel) from the shared cash pool.

    Returns one action dict per pick: executed BUYs and visible SKIPs.
    """
    actions = []
    for pick in new_picks:
        book = db.get_paper_book(conn)
        open_pos = db.get_open_paper_positions(conn)
        equity = book["cash"] + _positions_value(open_pos, snapshots)

        ticker = pick["ticker"]
        strategy = pick.get("channel", "TECHNICAL")
        entry_ref = pick["entry_price"]
        qty, note = size_position(equity, book["cash"], entry_ref, pick["stop_price"])
        if qty < 1:
            actions.append({
                "action": "SKIP", "ticker": ticker, "strategy": strategy,
                "note": f"not papered: {note}",
            })
            continue

        fill = round(buy_fill(entry_ref), 2)
        costs = buy_costs(qty, fill)
        gross = qty * fill
        cost_basis = gross + costs.total
        cash_after = book["cash"] - cost_basis
        risk_amt = qty * (entry_ref - pick["stop_price"])
        reason = (f"{note}; risking Rs {risk_amt:,.0f} "
                  f"({risk_amt / equity * 100:.1f}% of equity)")

        position_id = db.open_paper_position(
            conn,
            pos={
                "strategy": strategy, "ticker": ticker, "pick_id": pick.get("id"),
                "qty": qty, "entry_date": run_date, "entry_ref_price": entry_ref,
                "entry_fill_price": fill, "entry_charges": round(costs.total, 2),
                "cost_basis": round(cost_basis, 2),
                "target_price": pick["target_price"], "stop_price": pick["stop_price"],
                "rationale": pick.get("rationale"),
            },
            trade={
                "strategy": strategy, "ticker": ticker, "side": "BUY",
                "trade_date": run_date, "run_slot": run_slot, "qty": qty,
                "ref_price": entry_ref, "fill_price": fill,
                "gross_value": round(gross, 2),
                "brokerage": round(costs.brokerage, 2), "stt": round(costs.stt, 2),
                "exch_txn": round(costs.exch_txn, 4), "sebi": round(costs.sebi, 4),
                "stamp": round(costs.stamp, 2), "gst": round(costs.gst, 2),
                "dp": 0.0, "total_charges": round(costs.total, 2),
                "net_amount": round(-cost_basis, 2), "cash_after": round(cash_after, 2),
                "reason": reason,
            },
        )
        if position_id is None:
            warnings.append(f"{ticker}: open paper position already exists — BUY skipped")
            continue
        actions.append({
            "action": "BUY", "ticker": ticker, "strategy": strategy, "qty": qty,
            "fill_price": fill, "invested": round(cost_basis, 2),
            "charges": round(costs.total, 2),
            "target_price": pick["target_price"], "stop_price": pick["stop_price"],
            "reward_risk": pick.get("reward_risk"),
            "cash_after": round(cash_after, 2), "rationale": pick.get("rationale"),
            "note": reason,
        })
    return actions


def close_positions_for_exits(conn: sqlite3.Connection, closed_picks: list[dict],
                              run_date: str, run_slot: str,
                              warnings: list[str]) -> list[dict]:
    """Paper-sell open positions whose underlying pick just closed."""
    actions = []
    for exit_ in closed_picks:
        pos = db.get_open_paper_position(conn, exit_["ticker"])
        if pos is None:
            continue  # pick predates the paper book or was never papered

        exit_ref = exit_["exit_price"]
        fill = round(sell_fill(exit_ref), 2)
        costs = sell_costs(pos["qty"], fill)
        gross = pos["qty"] * fill
        net_proceeds = gross - costs.total
        realized = net_proceeds - pos["cost_basis"]
        book = db.get_paper_book(conn)
        cash_after = book["cash"] + net_proceeds

        db.close_paper_position(
            conn, pos["id"],
            exit_fields={
                "exit_date": run_date, "exit_fill_price": fill,
                "exit_charges": round(costs.total, 2),
                "net_proceeds": round(net_proceeds, 2),
                "realized_pnl": round(realized, 2),
                "exit_reason": f"{exit_['status']}: {exit_['exit_reason']}",
            },
            trade={
                "strategy": pos["strategy"], "ticker": pos["ticker"], "side": "SELL",
                "trade_date": run_date, "run_slot": run_slot, "qty": pos["qty"],
                "ref_price": exit_ref, "fill_price": fill,
                "gross_value": round(gross, 2),
                "brokerage": round(costs.brokerage, 2), "stt": round(costs.stt, 2),
                "exch_txn": round(costs.exch_txn, 4), "sebi": round(costs.sebi, 4),
                "stamp": 0.0, "gst": round(costs.gst, 2), "dp": round(costs.dp, 2),
                "total_charges": round(costs.total, 2),
                "net_amount": round(net_proceeds, 2), "cash_after": round(cash_after, 2),
                "reason": f"{exit_['status']}: {exit_['exit_reason']}",
            },
        )
        actions.append({
            "action": "SELL", "ticker": pos["ticker"], "strategy": pos["strategy"],
            "qty": pos["qty"], "fill_price": fill,
            "net_proceeds": round(net_proceeds, 2),
            "realized_pnl": round(realized, 2),
            "realized_pct": round(realized / pos["cost_basis"] * 100, 2),
            "charges": round(costs.total, 2), "status": exit_["status"],
            "exit_reason": exit_["exit_reason"],
            "entry_fill_price": pos["entry_fill_price"], "entry_date": pos["entry_date"],
            "cash_after": round(cash_after, 2),
        })
    return actions


def mark_to_market(conn: sqlite3.Connection, snapshots: dict[str, Snapshot],
                   run_date: str, run_slot: str) -> dict:
    """Value the book at today's closes and log the equity curve point."""
    book = db.get_paper_book(conn)
    positions = []
    positions_value = 0.0
    unrealized = 0.0
    for p in db.get_open_paper_positions(conn):
        snap = snapshots.get(p["ticker"])
        ltp = snap.close if snap else p["entry_fill_price"]
        value = p["qty"] * ltp
        positions_value += value
        unrealized += value - p["cost_basis"]
        positions.append({
            "ticker": p["ticker"], "strategy": p["strategy"], "qty": p["qty"],
            "entry_fill_price": p["entry_fill_price"], "entry_date": p["entry_date"],
            "ltp": round(ltp, 2), "value": round(value, 2),
            "cost_basis": p["cost_basis"], "entry_charges": p["entry_charges"],
            "unrealized_pnl": round(value - p["cost_basis"], 2),
            "target_price": p["target_price"], "stop_price": p["stop_price"],
            "rationale": p["rationale"],
        })
    realized_cum = db.get_realized_pnl_cum(conn)
    equity = book["cash"] + positions_value
    db.upsert_paper_equity(conn, run_date, run_slot, round(book["cash"], 2),
                           round(positions_value, 2), round(equity, 2),
                           round(unrealized, 2), round(realized_cum, 2),
                           len(positions))
    return {
        "starting_cash": book["starting_cash"], "cash": round(book["cash"], 2),
        "positions_value": round(positions_value, 2), "equity": round(equity, 2),
        "unrealized_pnl": round(unrealized, 2),
        "realized_pnl_cum": round(realized_cum, 2),
        "total_return_pct": round((equity - book["starting_cash"])
                                  / book["starting_cash"] * 100, 2),
        "open_positions": positions,
        "strategy_stats": db.get_paper_stats(conn),
    }
