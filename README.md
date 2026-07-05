# StockBot — NSE Short-Term Trading Suggestion Bot

Twice-daily bot that scans ~500 NSE stocks (NIFTY 100 + Midcap 150 + Smallcap 250,
fetched live from NSE index constituent files), suggests short-term picks with
entry/target/stop levels, tracks every pick with mechanical exit rules, health-checks
personal holdings, and reports to a terminal dashboard + Discord.

## Two discovery channels

- **TECHNICAL** — chart leads: uptrend (close > SMA20 > SMA50), RSI 45-68, MACD
  accelerating, above daily pivot, reward:risk >= 1.5; news sentiment acts as a veto.
- **NEWS** — Claude-scored news catalyst leads (sentiment >= +0.5, confidence >= 0.5);
  chart confirms with looser gates (close > SMA20, RSI <= 75, R:R >= 1.2).

Targets climb the pivot ladder (daily R1/R2/R3 -> weekly R1/R2/R3, first rung with
>= 2% upside); stops at max(S1, 10-day swing low), risk capped at 5%. Exits checked
every run: stop hit, target hit, setup broken (trend/MACD/sentiment), or 10-day expiry.

LLM sentiment runs through the **Claude Code CLI** (`claude -p`, subscription-billed,
no API key). Fundamentals (tier-aware market-cap floor, PE, ROE, D/E) and a
₹25 crore/day liquidity gate filter the universe.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # fill in Discord bot token + channel IDs
```

Requires the [Claude Code CLI](https://claude.com/claude-code) on PATH for sentiment.

## Run

```powershell
.venv\Scripts\python.exe run_daily.py                 # full run
.venv\Scripts\python.exe run_daily.py --no-llm        # skip Claude sentiment
.venv\Scripts\python.exe run_daily.py --no-discord    # skip Discord alerts
.venv\Scripts\python.exe run_daily.py --refresh-universe  # force NSE constituent refresh
```

`run_stockbot.ps1` is the Windows Task Scheduler entrypoint (schedule: 08:45 + 18:30
daily with missed-run catch-up; logs to `data/logs/`).

## Storage

SQLite at `data/stockbot.db`: `picks` (tracked suggestions + exit history), `holdings`,
`sentiment_log` (per AM/PM run), `fundamentals_cache`, `universe` (NSE constituents,
weekly refresh), `tracking_log` (per-run price / return % / sentiment / catalyst
time series), `run_log`.

> Suggestions only — not financial advice, no order execution.
