# StockBot — NSE Short-Term Trading Suggestion Bot

Twice-daily bot that scans ~500 NSE stocks (NIFTY 100 + Midcap 150 + Smallcap 250,
fetched live from NSE index constituent files), suggests short-term picks with
entry/target/stop levels, tracks every pick with mechanical exit rules, health-checks
personal holdings, and reports to a terminal dashboard + Discord.

## Three discovery channels

- **TECHNICAL** — chart leads: uptrend (close > SMA20 > SMA50), RSI 45-68, MACD
  accelerating, above daily pivot, reward:risk >= 1.5; news sentiment acts as a veto.
- **NEWS** — Claude-scored news catalyst leads (sentiment >= +0.5, confidence >= 0.5);
  chart confirms with looser gates (close > SMA20, RSI <= 75, R:R >= 1.2).
- **PULLBACK** — buy-the-dip: uptrend intact (SMA20 > SMA50, 20d momentum >= +3%)
  but a <= 0% 5-day return, day's low tags SMA20 and the close holds it, RSI reset
  to 35-55, MACD histogram no longer deteriorating; R:R >= 1.5, sentiment veto.

Targets climb the pivot ladder (daily R1/R2/R3 -> weekly R1/R2/R3, first rung with
>= 2% upside); stops at max(S1, 10-day swing low), risk capped at 5%. Exits checked
every run: stop hit, target hit, setup broken (trend/MACD/sentiment), or 10-day expiry.

LLM sentiment runs through the **Claude Code CLI** (`claude -p`, subscription-billed,
no API key). Fundamentals (tier-aware market-cap floor, PE, ROE, D/E) and a
₹25 crore/day liquidity gate filter the universe.

## Paper trading

Every new pick from any channel is also traded on paper from a **single shared
virtual book (₹10,000)**, positions tagged by strategy so per-channel win rates and
realized P&L stay comparable:

- **Risk-based sizing** — each position risks ~1.5% of book equity between entry and
  stop, capped at 40% of equity per position and by available cash (whole shares only;
  unaffordable picks are alerted as SKIP so you see why).
- **Realistic Indian delivery costs** — ₹5/order brokerage (INDmoney), STT 0.1% each
  side, exchange/SEBI/stamp charges, 18% GST, ₹16 DP charge on sells, plus 0.05%
  slippage per side. All rates in `config.py`.
- **Fills are end-of-day approximations** — entry at the signal close, exit at the
  exit engine's price (stop / target / close); a gap through the stop fills at the
  stop in paper but could be worse in reality.
- **Every action alerts on Discord** (`DISCORD_PAPER_CHANNEL_ID`, falls back to the
  picks channel): BUY with qty / invested / target / stop / R:R / reasoning, SELL with
  realized ₹ P&L and exit reason, plus a book summary. Mirror manually in your broker
  if convinced.
- Tables: `paper_book`, `paper_positions`, `paper_trades` (immutable ledger),
  `paper_equity_log` (per-run equity curve).

### Watching paper trades in a real trading UI

INDmoney itself has no paper-trading mode, so the bot mirrors every paper BUY/SELL
into **OpenAlgo's Analyzer (sandbox)** — its web UI at `http://127.0.0.1:5000` shows
the paper orderbook, tradebook, positions, and P&L like a real terminal. Safety:
mirroring is hard-gated on OpenAlgo confirming Analyzer mode is ON
(`PAPER_MIRROR_TO_OPENALGO` in config); if the server is in live mode or
unreachable, nothing is sent and a warning is raised instead.

## Real holdings via OpenAlgo (INDmoney)

The `holdings` table syncs from your broker through a locally-hosted
[OpenAlgo](https://github.com/marketcalls/openalgo) server. One-time setup
(repo already cloned at `C:\Users\srava\openalgo` with `.env` pre-configured
for IndMoney):

1. Install and start it:
   ```powershell
   cd C:\Users\srava\openalgo
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   .venv\Scripts\python.exe app.py
   ```
   then open `http://127.0.0.1:5000`, create the admin account, and toggle
   **Analyzer mode ON** (top bar) for sandbox trading.
2. In the INDmoney API dashboard: generate the bearer token (OTP login) and
   **whitelist your machine's static IP** (mandatory). The token expires every ~24h —
   paste a fresh one into the OpenAlgo UI each trading day you want a live sync.
3. Create an API key in the OpenAlgo web UI and set `OPENALGO_HOST` +
   `OPENALGO_API_KEY` in this repo's `.env`.

Degraded mode is automatic: if OpenAlgo is down or the IndMoney token has expired,
the bot keeps the last-synced snapshot and marks the holdings report **STALE** after
30h instead of failing. With no OpenAlgo config at all, mock holdings are seeded so
the pipeline still runs. Real order placement (`stockbot/broker.py::place_order`) is
hard-disabled via `PLACE_ORDER_ENABLED = False`.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest tests -q
```

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
