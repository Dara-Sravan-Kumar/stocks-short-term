"""Discord delivery via bot token + channel IDs (plain REST, no gateway).

Picks report and holdings report are sent as SEPARATE messages (optionally
separate channels). Degrades to a warning if unconfigured or on HTTP errors.
"""
from __future__ import annotations

import time

import requests

import config

RED = 0xE74C3C
GREEN = 0x2ECC71
BLUE = 0x3498DB
PURPLE = 0x9B59B6
GREY = 0x95A5A6
GOLD = 0xF1C40F
ORANGE = 0xE67E22

_LEVEL_EMOJI = {"green": ":green_circle:", "yellow": ":yellow_circle:", "red": ":red_circle:"}

_CHANNEL_ICON = {
    "NEWS": ":newspaper:", "PULLBACK": ":arrow_heading_down:",
    "ORDERFLOW": ":twisted_rightwards_arrows:", "LIQUIDITY_SWEEP": ":shark:",
    "FVG": ":left_right_arrow:", "ANCHORED_VWAP": ":anchor:",
    "VOLUME_PROFILE": ":bar_chart:", "BREAKOUT_52W": ":rocket:",
}  # TECHNICAL (and anything unmatched) falls back to :chart_with_upwards_trend:


def _base_channel(channel: str) -> str:
    """Strip a strategy variant key (e.g. "PULLBACK_v2") back to its base
    channel name so display coloring/icons stay consistent across variants."""
    for prefix in (*config.EVOLVING_CHANNELS, "NEWS"):
        if channel == prefix or channel.startswith(prefix + "_"):
            return prefix
    return channel


def _post(token: str, channel_id: str, payload: dict, warnings: list[str]) -> bool:
    url = f"{config.DISCORD_API_BASE}/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {token}"}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        if resp.status_code == 429:  # rate limited — retry once
            retry_after = float(resp.json().get("retry_after", 1.0))
            time.sleep(min(retry_after, 10.0))
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
        if resp.status_code >= 400:
            warnings.append(f"Discord post failed ({resp.status_code}): {resp.text[:150]}")
            return False
        return True
    except requests.RequestException as exc:
        warnings.append(f"Discord post failed: {exc}")
        return False


_LOGIN_REMINDER_STATE = config.DATA_DIR / ".login_reminder_sent"


def send_login_reminder(warnings: list[str], throttle_minutes: int = 60) -> str:
    """Hourly-throttled "Fyers login missing" nudge, sent when a run finds the
    paper book FROZEN (no fresh Fyers token, so paper.books_on_provider() is
    False and nothing books). Posts to the HOLDINGS channel if set, else PICKS.
    Throttled to one post per `throttle_minutes` (via a small state file in this
    bot's data dir) so the 30-min scans don't spam. Returns a short status
    string (sent / throttled / unconfigured / failed)."""
    from datetime import datetime, timedelta
    now = datetime.now()
    try:
        if _LOGIN_REMINDER_STATE.exists():
            last = datetime.fromisoformat(
                _LOGIN_REMINDER_STATE.read_text(encoding="utf-8").strip())
            if now - last < timedelta(minutes=throttle_minutes):
                return f"throttled (last {last:%H:%M})"
    except (ValueError, OSError):
        pass
    creds = config.discord_settings()
    token = creds["token"]
    channel = creds["alerts_channel"] or creds["holdings_channel"] or creds["picks_channel"]
    if not token or not channel:
        return "unconfigured"
    payload = {"embeds": [{
        "title": ":warning: Fyers login missing — paper book FROZEN",
        "description": (
            "Today's Fyers token is stale, so this run booked **no** trades and "
            "marked nothing (signals were still scanned/alerted).\n\n**Action:** "
            "run the daily Fyers login. One login covers all 3 bots. Deadline: "
            "**before 08:45** (earliest fleet run)."),
        "color": ORANGE,
    }]}
    if not _post(token, channel, payload, warnings):
        return "failed"
    try:
        _LOGIN_REMINDER_STATE.write_text(now.isoformat(timespec="seconds"),
                                         encoding="utf-8")
    except OSError:
        pass
    return "sent"


def send_failure_alert(run_date: str, run_slot: str, failures: list[dict],
                       warnings: list[str]) -> str:
    """Post a red health-alert embed for pipeline failures (news outage, LLM
    fallback, …). `failures` is a list of {"kind", "detail"}. Posts to the
    alerts channel (falls back to holdings → picks). No-op on empty failures or
    when Discord is unconfigured. Returns a short status string.

    News/LLM run only on the scheduled AM/PM anchor runs, so callers pass a
    non-empty list only there — the 30-min scans stay quiet by construction."""
    if not failures:
        return "no failures"
    creds = config.discord_settings()
    token = creds["token"]
    channel = creds["alerts_channel"] or creds["holdings_channel"] or creds["picks_channel"]
    if not token or not channel:
        return "unconfigured"
    fields = [{"name": f":x: {f['kind']}", "value": _clip(str(f["detail"]), 1000),
               "inline": False} for f in failures]
    payload = {"embeds": [{
        "title": f":rotating_light: PIPELINE FAILURE — {run_date} ({run_slot})",
        "description": ("One or more data/LLM steps failed this run. Picks/scoring "
                        "may be degraded — see below."),
        "color": RED,
        "fields": fields[:25],
    }]}
    return "sent" if _post(token, channel, payload, warnings) else "failed"


def _chunk_embeds(embeds: list[dict]) -> list[list[dict]]:
    """Respect Discord limits: <=10 embeds and <=6000 chars per message."""
    chunks, current, size = [], [], 0
    for e in embeds:
        e_size = len(str(e.get("title", ""))) + len(str(e.get("description", "")))
        for f in e.get("fields", []):
            e_size += len(str(f.get("name", ""))) + len(str(f.get("value", "")))
        if current and (len(current) >= 10 or size + e_size > 5500):
            chunks.append(current)
            current, size = [], 0
        current.append(e)
        size += e_size
    if current:
        chunks.append(current)
    return chunks


def _clip(text: str, limit: int = 1000) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def build_picks_embeds(run_date: str, exits: list[dict], new_picks: list[dict],
                       active_picks: list[dict]) -> list[dict]:
    embeds: list[dict] = []

    # Exit signals (red)
    if exits:
        lines = []
        for e in exits:
            lines.append(
                f"**{e['ticker']}** [{e['status']}]  "
                f"{e['entry_price']:.2f} -> {e['exit_price']:.2f}  "
                f"(**{e['pnl_pct']:+.2f}%**)\n> {e['exit_reason']}"
            )
        embeds.append({
            "title": ":rotating_light: EXIT SIGNALS - Suggested to Quit",
            "description": _clip("\n\n".join(lines), 4000),
            "color": RED,
        })
    else:
        embeds.append({
            "title": ":rotating_light: EXIT SIGNALS",
            "description": "No exit signals today - all tracked picks intact.",
            "color": GREY,
        })

    # New picks (green)
    if new_picks:
        fields = []
        for p in new_picks:
            channel = p.get("channel", "TECHNICAL")
            icon = _CHANNEL_ICON.get(_base_channel(channel), ":chart_with_upwards_trend:")
            fields.append({
                "name": f"{icon} {p['ticker']}  [{channel}]",
                "value": _clip(
                    f"Entry: **{p['entry_price']:.2f}**  |  "
                    f"Target: **{p['target_price']:.2f}**  |  "
                    f"Stop: **{p['stop_price']:.2f}**\n"
                    f"R:R {p.get('reward_risk', 0):.1f}  |  RSI {p.get('rsi_at_entry', 0):.0f}  |  "
                    f"Sentiment {p.get('sentiment_at_entry', 0):+.2f}\n"
                    f"_{p.get('rationale', '')}_"
                ),
                "inline": False,
            })
        embeds.append({
            "title": f":sparkles: NEW SHORT-TERM PICKS - {run_date}",
            "color": GREEN,
            "fields": fields[:25],
        })
    else:
        embeds.append({
            "title": f":sparkles: NEW SHORT-TERM PICKS - {run_date}",
            "description": "No new setups met all entry criteria today.",
            "color": GREY,
        })

    # Active picks (blue)
    if active_picks:
        lines = []
        for p in active_picks:
            ltp = p.get("ltp")
            ltp_txt = f"{ltp:.2f}" if isinstance(ltp, (int, float)) else "-"
            tgt = p.get("pct_to_target")
            stp = p.get("pct_to_stop")
            tgt_txt = f"{tgt:+.1f}%" if isinstance(tgt, (int, float)) else "-"
            stp_txt = f"{stp:+.1f}%" if isinstance(stp, (int, float)) else "-"
            lines.append(
                f"**{p['ticker']}**  entry {p['entry_price']:.2f} ({p['entry_date']})  "
                f"LTP {ltp_txt}  |  to target: {tgt_txt}  |  to stop: {stp_txt}"
            )
        embeds.append({
            "title": ":eyes: ACTIVE PICKS - Being Tracked",
            "description": _clip("\n".join(lines), 4000),
            "color": BLUE,
        })
    return embeds


def build_holdings_embeds(run_date: str, holdings: list[dict], stats: dict,
                          holdings_note: str | None = None) -> list[dict]:
    fields = []
    for h in holdings:
        pnl = h.get("pnl_pct")
        ltp = h.get("ltp")
        pnl_txt = f"{pnl:+.2f}%" if isinstance(pnl, (int, float)) else "-"
        ltp_txt = f"{ltp:.2f}" if isinstance(ltp, (int, float)) else "-"
        icon = _LEVEL_EMOJI.get(h["level"], ":white_circle:")
        fields.append({
            "name": f"{icon} {h['ticker']}",
            "value": _clip(
                f"Avg buy {h['avg_buy']:.2f} x {h['qty']}  |  LTP {ltp_txt}  |  "
                f"P&L **{pnl_txt}**\n{h['signal']}"
            ),
            "inline": False,
        })
    summary = (
        f"Closed picks: {stats['closed']}  |  Win rate: {stats['win_rate']:.0f}%  |  "
        f"Avg win: {stats['avg_win']:+.2f}%  |  Avg loss: {stats['avg_loss']:+.2f}%"
    )
    title = f":briefcase: MY HOLDINGS - Health Check ({run_date})"
    if holdings_note:
        title += f"  [{holdings_note}]"
    return [{
        "title": title,
        "color": PURPLE,
        "fields": fields[:25],
        "footer": {"text": summary},
    }]


def build_paper_embeds(run_date: str, paper_entries: list[dict],
                       paper_exits: list[dict], paper_book: dict) -> list[dict]:
    """Every paper action gets an alert — the user mirrors trades manually."""
    embeds: list[dict] = []

    buys = [a for a in paper_entries if a["action"] == "BUY"]
    skips = [a for a in paper_entries if a["action"] == "SKIP"]
    if buys:
        fields = []
        for a in buys:
            rr = a.get("reward_risk")
            rr_txt = f"{rr:.1f}" if isinstance(rr, (int, float)) else "-"
            fields.append({
                "name": f":money_with_wings: BUY {a['ticker']}  [{a['strategy']}]",
                "value": _clip(
                    f"**{a['qty']} shares @ {a['fill_price']:.2f}** = "
                    f"₹{a['invested']:,.0f} (incl. ₹{a['charges']:.0f} charges)\n"
                    f"Target: **{a['target_price']:.2f}**  |  "
                    f"Stop: **{a['stop_price']:.2f}**  |  R:R {rr_txt}\n"
                    f"Cash left: ₹{a['cash_after']:,.0f}  |  {a.get('note', '')}\n"
                    f"_{a.get('rationale', '')}_"
                ),
                "inline": False,
            })
        embeds.append({
            "title": f":page_facing_up: PAPER BUYS - {run_date} (mirror manually if convinced)",
            "color": GREEN,
            "fields": fields[:25],
        })
    if paper_exits:
        fields = []
        for a in paper_exits:
            pnl = a["realized_pnl"]
            arrow = ":chart_with_upwards_trend:" if pnl >= 0 else ":chart_with_downwards_trend:"
            fields.append({
                "name": f"{arrow} SELL {a['ticker']}  [{a['strategy']}]  [{a['status']}]",
                "value": _clip(
                    f"**{a['qty']} shares @ {a['fill_price']:.2f}** -> "
                    f"₹{a['net_proceeds']:,.0f} net (₹{a['charges']:.0f} charges)\n"
                    f"Realized P&L: **₹{pnl:+,.0f} ({a['realized_pct']:+.2f}%)**  |  "
                    f"held since {a['entry_date']}\n"
                    f"_{a['exit_reason']}_"
                ),
                "inline": False,
            })
        embeds.append({
            "title": f":outbox_tray: PAPER SELLS - {run_date}",
            "color": GREEN if all(a["realized_pnl"] >= 0 for a in paper_exits) else RED,
            "fields": fields[:25],
        })

    strat = paper_book.get("strategy_stats", {})
    strat_lines = [
        f"{name}: {st['closed']} closed, {st['win_rate']:.0f}% wins, "
        f"₹{st['realized_pnl']:+,.0f}"
        for name, st in strat.items() if st["closed"]
    ]
    desc = (
        f"Equity: **₹{paper_book['equity']:,.0f}** "
        f"({paper_book['total_return_pct']:+.2f}% on ₹{paper_book['starting_cash']:,.0f})\n"
        f"Cash: ₹{paper_book['cash']:,.0f}  |  "
        f"Positions: ₹{paper_book['positions_value']:,.0f} "
        f"({len(paper_book['open_positions'])} open)\n"
        f"Unrealized: ₹{paper_book['unrealized_pnl']:+,.0f}  |  "
        f"Realized (cum): ₹{paper_book['realized_pnl_cum']:+,.0f}"
    )
    if strat_lines:
        desc += "\n" + " | ".join(strat_lines)
    if skips:
        desc += "\n" + "\n".join(
            f":no_entry_sign: {a['ticker']} [{a['strategy']}] {a['note']}" for a in skips)
    embeds.append({
        "title": f":ledger: PAPER BOOK - {run_date}",
        "description": _clip(desc, 4000),
        "color": PURPLE,
    })

    open_positions = paper_book.get("open_positions") or []
    if open_positions:
        fields = []
        for p in open_positions:
            pnl = p["unrealized_pnl"]
            pnl_pct = pnl / p["cost_basis"] * 100 if p["cost_basis"] else 0.0
            ltp = p["ltp"]
            to_tgt = (p["target_price"] - ltp) / ltp * 100 if ltp else 0.0
            to_stop = (p["stop_price"] - ltp) / ltp * 100 if ltp else 0.0
            arrow = ":small_red_triangle:" if pnl >= 0 else ":small_red_triangle_down:"
            fields.append({
                "name": f"{arrow} {p['ticker']}  [{p['strategy']}]",
                "value": _clip(
                    f"Entry: **{p['entry_fill_price']:.2f}** x {p['qty']} "
                    f"({p['entry_date']})  |  Cost basis: ₹{p['cost_basis']:,.0f} "
                    f"(incl. ₹{p['entry_charges']:.0f} charges)\n"
                    f"LTP: **{ltp:.2f}**  |  Value: ₹{p['value']:,.0f}  |  "
                    f"Unrl P&L: **₹{pnl:+,.0f} ({pnl_pct:+.2f}%)**\n"
                    f"Target: **{p['target_price']:.2f}** ({to_tgt:+.1f}%)  |  "
                    f"Stop: **{p['stop_price']:.2f}** ({to_stop:+.1f}%)\n"
                    f"_{p.get('rationale', '')}_"
                ),
                "inline": False,
            })
        total_invested = sum(p["cost_basis"] for p in open_positions)
        total_value = sum(p["value"] for p in open_positions)
        total_pnl = total_value - total_invested
        total_pnl_pct = total_pnl / total_invested * 100 if total_invested else 0.0
        embeds.append({
            "title": f":bar_chart: OPEN POSITIONS - Full Ledger ({run_date})",
            "color": BLUE,
            "fields": fields[:25],
            "footer": {"text": (
                f"Cash invested: ₹{total_invested:,.0f}  |  "
                f"Current value: ₹{total_value:,.0f}  |  "
                f"Total P/L: ₹{total_pnl:+,.0f} ({total_pnl_pct:+.2f}%)"
            )},
        })
    return embeds


def build_strategy_ledger_embed(run_date: str, strategy_ledger: dict) -> list[dict]:
    """strategy_ledger is stockbot.strategy_engine.evaluate_and_evolve()'s return
    value: active variants per channel, their closed-trade stats, today's capital
    weights, and any retire/create events from this run.
    """
    active_by_channel = strategy_ledger.get("active_by_channel", {})
    ledger_stats = strategy_ledger.get("ledger_stats", {})
    capital_weights = strategy_ledger.get("capital_weights", {})
    events = strategy_ledger.get("events", [])

    fields = []
    for channel in (*config.EVOLVING_CHANNELS, "NEWS"):
        for variant in active_by_channel.get(channel, []):
            key = variant["variant_key"]
            stats = ledger_stats.get(key, {"closed": 0, "win_rate": 0.0, "realized_pnl": 0.0})
            weight = capital_weights.get(key, 0.0)
            flag = "  :trophy:" if variant["graduate_candidate"] else ""
            fields.append({
                "name": f"{key}{flag}",
                "value": _clip(
                    f"Closed: **{stats['closed']}**  |  Win rate: **{stats['win_rate']:.0f}%**  |  "
                    f"Realized P&L: **₹{stats['realized_pnl']:+,.0f}**\n"
                    f"Capital weight: **{weight:.0f}%**  |  Origin: {variant['origin']}"
                ),
                "inline": False,
            })

    embeds = [{
        "title": f":dna: STRATEGY LEDGER - {run_date}",
        "color": GOLD,
        "fields": fields[:25],
    }]

    if events:
        lines = []
        for e in events:
            if e["type"] == "retired":
                lines.append(f":skull: **{e['variant_key']}** retired — {e['reason']}")
            elif e["type"] == "created":
                lines.append(f":seedling: **{e['variant_key']}** created, replacing "
                             f"**{e['parent']}** — {e['rationale']}")
            elif e["type"] == "wildcard_created":
                lines.append(f":game_die: **{e['variant_key']}** wildcard added — {e['rationale']}")
            elif e["type"] == "graduate_candidate":
                lines.append(f":trophy: **{e['variant_key']}** flagged as a graduate candidate "
                             "— paper-proven, your call on going live")
        embeds.append({
            "title": ":arrows_counterclockwise: STRATEGY CHANGES TODAY",
            "description": _clip("\n\n".join(lines), 4000),
            "color": GOLD,
        })
    return embeds


def has_reportable_activity(closed: list[dict], new_picks: list[dict],
                            paper_entries: list[dict] | None,
                            paper_exits: list[dict] | None,
                            strategy_events: list[dict] | None) -> bool:
    """True if this run produced anything worth alerting on: an exit, a new
    pick, an actual paper BUY/SELL (not a SKIP), or a strategy-fleet event
    (retired/created/wildcard/graduate). Used to keep hourly in-between runs
    (--skip-news) quiet on Discord when nothing happened, while the twice-
    daily anchor runs always report regardless of this check.
    """
    paper_buys = [a for a in (paper_entries or []) if a.get("action") == "BUY"]
    return bool(closed or new_picks or paper_buys or paper_exits or strategy_events)


def send_report(run_date: str, exits: list[dict], new_picks: list[dict],
                active_picks: list[dict], holdings: list[dict], stats: dict,
                warnings: list[str], paper_entries: list[dict] | None = None,
                paper_exits: list[dict] | None = None,
                paper_book: dict | None = None,
                holdings_note: str | None = None,
                strategy_ledger: dict | None = None) -> str:
    """Send picks + holdings (+ paper book) Discord messages. Returns status text."""
    cfg = config.discord_settings()
    if not cfg["token"] or not cfg["picks_channel"]:
        warnings.append("Discord not configured (set DISCORD_BOT_TOKEN and "
                        "DISCORD_PICKS_CHANNEL_ID in .env)")
        return "not configured"

    picks_channel = cfg["picks_channel"]
    holdings_channel = cfg["holdings_channel"] or picks_channel
    paper_channel = cfg["paper_channel"] or picks_channel
    strategy_channel = cfg["strategy_channel"] or picks_channel

    ok = True
    for chunk in _chunk_embeds(build_picks_embeds(run_date, exits, new_picks, active_picks)):
        ok &= _post(cfg["token"], picks_channel, {"embeds": chunk}, warnings)
        time.sleep(0.5)
    for chunk in _chunk_embeds(build_holdings_embeds(run_date, holdings, stats,
                                                     holdings_note)):
        ok &= _post(cfg["token"], holdings_channel, {"embeds": chunk}, warnings)
        time.sleep(0.5)
    if paper_book is not None:
        for chunk in _chunk_embeds(build_paper_embeds(
                run_date, paper_entries or [], paper_exits or [], paper_book)):
            ok &= _post(cfg["token"], paper_channel, {"embeds": chunk}, warnings)
            time.sleep(0.5)
    if strategy_ledger is not None:
        for chunk in _chunk_embeds(build_strategy_ledger_embed(run_date, strategy_ledger)):
            ok &= _post(cfg["token"], strategy_channel, {"embeds": chunk}, warnings)
            time.sleep(0.5)
    return "sent" if ok else "partial failure"
