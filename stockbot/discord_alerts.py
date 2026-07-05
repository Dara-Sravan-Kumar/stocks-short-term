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

_LEVEL_EMOJI = {"green": ":green_circle:", "yellow": ":yellow_circle:", "red": ":red_circle:"}


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
            icon = ":newspaper:" if channel == "NEWS" else ":chart_with_upwards_trend:"
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


def build_holdings_embeds(run_date: str, holdings: list[dict], stats: dict) -> list[dict]:
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
    return [{
        "title": f":briefcase: MY HOLDINGS - Health Check ({run_date})",
        "color": PURPLE,
        "fields": fields[:25],
        "footer": {"text": summary},
    }]


def send_report(run_date: str, exits: list[dict], new_picks: list[dict],
                active_picks: list[dict], holdings: list[dict], stats: dict,
                warnings: list[str]) -> str:
    """Send picks + holdings as separate Discord messages. Returns status text."""
    cfg = config.discord_settings()
    if not cfg["token"] or not cfg["picks_channel"]:
        warnings.append("Discord not configured (set DISCORD_BOT_TOKEN and "
                        "DISCORD_PICKS_CHANNEL_ID in .env)")
        return "not configured"

    picks_channel = cfg["picks_channel"]
    holdings_channel = cfg["holdings_channel"] or picks_channel

    ok = True
    for chunk in _chunk_embeds(build_picks_embeds(run_date, exits, new_picks, active_picks)):
        ok &= _post(cfg["token"], picks_channel, {"embeds": chunk}, warnings)
        time.sleep(0.5)
    for chunk in _chunk_embeds(build_holdings_embeds(run_date, holdings, stats)):
        ok &= _post(cfg["token"], holdings_channel, {"embeds": chunk}, warnings)
        time.sleep(0.5)
    return "sent" if ok else "partial failure"
