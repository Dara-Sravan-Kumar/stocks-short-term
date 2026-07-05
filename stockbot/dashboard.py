"""Rich terminal dashboard — clean, scannable, no emojis (Windows console)."""
from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

console = Console()

_LEVEL_STYLE = {"green": "green", "yellow": "yellow", "red": "bold red"}


def _fmt(v, spec=".2f", dash="-"):
    return format(v, spec) if isinstance(v, (int, float)) else dash


def render(run_date: str, data_date: str, tickers_scanned: int, llm_status: str,
           exits: list[dict], active_picks: list[dict], new_picks: list[dict],
           holdings: list[dict], stats: dict, warnings: list[str],
           discord_status: str) -> None:
    console.print()
    console.print(Panel(
        f"[bold cyan]STOCKBOT[/] - NSE Short-Term Daily Report\n"
        f"Run date: [bold]{run_date}[/]   Data date: [bold]{data_date}[/]   "
        f"Tickers scanned: [bold]{tickers_scanned}[/]   "
        f"Sentiment: [bold]{llm_status}[/]   Discord: [bold]{discord_status}[/]",
        box=box.DOUBLE, style="cyan",
    ))

    # 1. EXIT SIGNALS ------------------------------------------------------
    t = Table(box=box.SIMPLE_HEAVY, expand=True)
    for col in ("Ticker", "Status", "Entry Dt", "Entry", "Exit Px", "P&L %", "Reason"):
        t.add_column(col, overflow="fold")
    if exits:
        for e in exits:
            pnl = e["pnl_pct"]
            pnl_style = "green" if pnl > 0 else "red"
            t.add_row(
                e["ticker"], e["status"], e["entry_date"],
                _fmt(e["entry_price"]), _fmt(e["exit_price"]),
                f"[{pnl_style}]{pnl:+.2f}[/]", e["exit_reason"],
            )
    else:
        t.add_row("-", "-", "-", "-", "-", "-", "No exit signals today")
    console.print(Panel(t, title="[bold red]1. EXIT SIGNALS (Suggested to Quit)[/]",
                        border_style="red"))

    # 2. ACTIVE PICKS ------------------------------------------------------
    t = Table(box=box.SIMPLE_HEAVY, expand=True)
    for col in ("Ticker", "Entry Dt", "Entry", "LTP", "Target", "Stop",
                "% to Tgt", "% to Stop", "Sent"):
        t.add_column(col, overflow="fold")
    if active_picks:
        for p in active_picks:
            t.add_row(
                p["ticker"], p["entry_date"], _fmt(p["entry_price"]), _fmt(p.get("ltp")),
                _fmt(p["target_price"]), _fmt(p["stop_price"]),
                _fmt(p.get("pct_to_target"), "+.1f"), _fmt(p.get("pct_to_stop"), "+.1f"),
                _fmt(p.get("sentiment"), "+.2f"),
            )
    else:
        t.add_row("-", "-", "-", "-", "-", "-", "-", "-", "-")
    console.print(Panel(t, title="[bold blue]2. ACTIVE PICKS (Being Tracked)[/]",
                        border_style="blue"))

    # 3. NEW PICKS TODAY ---------------------------------------------------
    t = Table(box=box.SIMPLE_HEAVY, expand=True)
    for col in ("Ticker", "Ch", "Entry", "Target", "Stop", "R:R", "RSI", "Sent", "Rationale"):
        t.add_column(col, overflow="fold")
    if new_picks:
        for p in new_picks:
            ch = p.get("channel", "TECHNICAL")
            ch_style = "yellow" if ch == "NEWS" else "cyan"
            t.add_row(
                p["ticker"], f"[{ch_style}]{ch}[/]",
                _fmt(p["entry_price"]), _fmt(p["target_price"]),
                _fmt(p["stop_price"]), _fmt(p.get("reward_risk"), ".1f"),
                _fmt(p.get("rsi_at_entry"), ".0f"), _fmt(p.get("sentiment_at_entry"), "+.2f"),
                p.get("rationale", ""),
            )
    else:
        t.add_row("-", "-", "-", "-", "-", "-", "-", "-",
                  "No new setups met all entry criteria today")
    console.print(Panel(t, title="[bold green]3. NEW SHORT-TERM PICKS TODAY[/]",
                        border_style="green"))

    # 4. HOLDINGS HEALTH ---------------------------------------------------
    t = Table(box=box.SIMPLE_HEAVY, expand=True)
    for col in ("Ticker", "Avg Buy", "Qty", "LTP", "Unrl P&L %", "Signal"):
        t.add_column(col, overflow="fold")
    for h in holdings:
        style = _LEVEL_STYLE.get(h["level"], "white")
        pnl = h.get("pnl_pct")
        pnl_txt = f"{pnl:+.2f}" if isinstance(pnl, (int, float)) else "-"
        pnl_style = "green" if isinstance(pnl, (int, float)) and pnl >= 0 else "red"
        t.add_row(
            h["ticker"], _fmt(h["avg_buy"]), str(h["qty"]), _fmt(h.get("ltp")),
            f"[{pnl_style}]{pnl_txt}[/]", f"[{style}]{h['signal']}[/]",
        )
    console.print(Panel(t, title="[bold magenta]4. MY HOLDINGS - HEALTH CHECK[/]",
                        border_style="magenta"))

    # 5. SUMMARY -----------------------------------------------------------
    summary = (
        f"Closed picks (all time): [bold]{stats['closed']}[/]   "
        f"Win rate: [bold]{stats['win_rate']:.0f}%[/]   "
        f"Avg win: [green]{stats['avg_win']:+.2f}%[/]   "
        f"Avg loss: [red]{stats['avg_loss']:+.2f}%[/]"
    )
    console.print(Panel(summary, title="[bold]5. SUMMARY[/]", border_style="white"))

    if warnings:
        console.print(Panel("\n".join(f"- {w}" for w in warnings),
                            title="[bold yellow]WARNINGS[/]", border_style="yellow"))
    console.print()
