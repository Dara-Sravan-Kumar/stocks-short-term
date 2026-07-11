"""Central configuration for stockbot.

Every tunable threshold lives here — logic modules must not hardcode values.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "stockbot.db"

# ---------------------------------------------------------------------------
# Universe
#
# The live universe is fetched from NSE's official index constituent files
# (NIFTY 100 + Midcap 150 + Smallcap 250, ~500 stocks) by stockbot/universe.py,
# cached in SQLite, and refreshed weekly. The static lists below are the
# FALLBACK used only when NSE is unreachable and no cache exists.
# universe.apply() overwrites WATCHLIST / TIER / COMPANY_NAMES / FINANCIALS
# at runtime.
# ---------------------------------------------------------------------------
UNIVERSE_REFRESH_DAYS = 7

LARGECAP_WATCHLIST = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "BHARTIARTL.NS", "SBIN.NS", "LT.NS", "ITC.NS", "HINDUNILVR.NS",
    "BAJFINANCE.NS", "AXISBANK.NS", "MARUTI.NS", "KOTAKBANK.NS", "SUNPHARMA.NS",
    "TITAN.NS", "ULTRACEMCO.NS", "NTPC.NS", "TATAPOWER.NS", "POWERGRID.NS",
    "M&M.NS", "WIPRO.NS", "ADANIENT.NS", "ADANIPORTS.NS", "HCLTECH.NS",
    "ASIANPAINT.NS", "COALINDIA.NS", "BAJAJFINSV.NS", "NESTLEIND.NS", "TATASTEEL.NS",
    "GRASIM.NS", "TECHM.NS", "HINDALCO.NS", "JSWSTEEL.NS", "INDUSINDBK.NS",
    "DRREDDY.NS", "CIPLA.NS", "EICHERMOT.NS", "APOLLOHOSP.NS", "DIVISLAB.NS",
    "TATACONSUM.NS", "BRITANNIA.NS", "HEROMOTOCO.NS", "BAJAJ-AUTO.NS", "ONGC.NS",
    "BPCL.NS", "SHRIRAMFIN.NS", "TRENT.NS",
]

MIDCAP_WATCHLIST = [
    "PERSISTENT.NS", "COFORGE.NS", "MPHASIS.NS", "LUPIN.NS", "AUROPHARMA.NS",
    "ALKEM.NS", "BIOCON.NS", "GLENMARK.NS", "LAURUSLABS.NS", "ASHOKLEY.NS",
    "BHARATFORG.NS", "CUMMINSIND.NS", "ASTRAL.NS", "POLYCAB.NS", "DIXON.NS",
    "VOLTAS.NS", "GODREJPROP.NS", "OBEROIRLTY.NS", "JUBLFOOD.NS", "PAGEIND.NS",
    "FEDERALBNK.NS", "IDFCFIRSTB.NS", "INDHOTEL.NS", "IRCTC.NS", "CONCOR.NS",
    "SRF.NS", "DEEPAKNTR.NS", "TATACHEM.NS", "TORNTPOWER.NS", "EXIDEIND.NS",
    "MOTHERSON.NS", "BALKRISIND.NS", "APOLLOTYRE.NS", "JKCEMENT.NS", "RAMCOCEM.NS",
    "DALBHARAT.NS", "MUTHOOTFIN.NS", "LICHSGFIN.NS", "CANBK.NS", "BANKBARODA.NS",
]

SMALLCAP_WATCHLIST = [
    "CDSL.NS", "CAMS.NS", "ANGELONE.NS", "MCX.NS", "IEX.NS",
    "TANLA.NS", "HFCL.NS", "RAILTEL.NS", "HUDCO.NS", "NBCC.NS",
    "NCC.NS", "CESC.NS", "TRIDENT.NS", "PVRINOX.NS", "RBLBANK.NS",
    "GRAPHITE.NS", "HEG.NS", "NATIONALUM.NS", "HINDCOPPER.NS", "MOIL.NS",
    "GESHIP.NS", "SUZLON.NS", "MANAPPURAM.NS", "GRANULES.NS", "COCHINSHIP.NS",
]

# Combined scan universe + tier lookup
WATCHLIST = LARGECAP_WATCHLIST + MIDCAP_WATCHLIST + SMALLCAP_WATCHLIST
TIER = ({t: "LARGE" for t in LARGECAP_WATCHLIST}
        | {t: "MID" for t in MIDCAP_WATCHLIST}
        | {t: "SMALL" for t in SMALLCAP_WATCHLIST})

# Company names for news search (Google News RSS queries)
COMPANY_NAMES = {
    "RELIANCE.NS": "Reliance Industries", "TCS.NS": "Tata Consultancy Services",
    "HDFCBANK.NS": "HDFC Bank", "ICICIBANK.NS": "ICICI Bank", "INFY.NS": "Infosys",
    "BHARTIARTL.NS": "Bharti Airtel", "SBIN.NS": "State Bank of India",
    "LT.NS": "Larsen Toubro", "ITC.NS": "ITC Limited", "HINDUNILVR.NS": "Hindustan Unilever",
    "BAJFINANCE.NS": "Bajaj Finance", "AXISBANK.NS": "Axis Bank", "MARUTI.NS": "Maruti Suzuki",
    "KOTAKBANK.NS": "Kotak Mahindra Bank", "SUNPHARMA.NS": "Sun Pharma",
    "TITAN.NS": "Titan Company", "ULTRACEMCO.NS": "UltraTech Cement", "NTPC.NS": "NTPC",
    "TATAPOWER.NS": "Tata Power", "POWERGRID.NS": "Power Grid Corporation",
    "M&M.NS": "Mahindra Mahindra", "WIPRO.NS": "Wipro", "ADANIENT.NS": "Adani Enterprises",
    "ADANIPORTS.NS": "Adani Ports", "HCLTECH.NS": "HCL Technologies",
    "ASIANPAINT.NS": "Asian Paints", "COALINDIA.NS": "Coal India",
    "BAJAJFINSV.NS": "Bajaj Finserv", "NESTLEIND.NS": "Nestle India",
    "TATASTEEL.NS": "Tata Steel", "GRASIM.NS": "Grasim Industries", "TECHM.NS": "Tech Mahindra",
    "HINDALCO.NS": "Hindalco", "JSWSTEEL.NS": "JSW Steel", "INDUSINDBK.NS": "IndusInd Bank",
    "DRREDDY.NS": "Dr Reddys Laboratories", "CIPLA.NS": "Cipla", "EICHERMOT.NS": "Eicher Motors",
    "APOLLOHOSP.NS": "Apollo Hospitals", "DIVISLAB.NS": "Divis Laboratories",
    "TATACONSUM.NS": "Tata Consumer Products", "BRITANNIA.NS": "Britannia Industries",
    "HEROMOTOCO.NS": "Hero MotoCorp", "BAJAJ-AUTO.NS": "Bajaj Auto", "ONGC.NS": "ONGC",
    "BPCL.NS": "Bharat Petroleum", "SHRIRAMFIN.NS": "Shriram Finance", "TRENT.NS": "Trent Limited",
    # Midcaps
    "PERSISTENT.NS": "Persistent Systems", "COFORGE.NS": "Coforge", "MPHASIS.NS": "Mphasis",
    "LUPIN.NS": "Lupin", "AUROPHARMA.NS": "Aurobindo Pharma", "ALKEM.NS": "Alkem Laboratories",
    "BIOCON.NS": "Biocon", "GLENMARK.NS": "Glenmark Pharma", "LAURUSLABS.NS": "Laurus Labs",
    "ASHOKLEY.NS": "Ashok Leyland", "BHARATFORG.NS": "Bharat Forge", "CUMMINSIND.NS": "Cummins India",
    "ASTRAL.NS": "Astral Limited", "POLYCAB.NS": "Polycab India", "DIXON.NS": "Dixon Technologies",
    "VOLTAS.NS": "Voltas", "GODREJPROP.NS": "Godrej Properties", "OBEROIRLTY.NS": "Oberoi Realty",
    "JUBLFOOD.NS": "Jubilant FoodWorks", "PAGEIND.NS": "Page Industries",
    "FEDERALBNK.NS": "Federal Bank", "IDFCFIRSTB.NS": "IDFC First Bank",
    "INDHOTEL.NS": "Indian Hotels", "IRCTC.NS": "IRCTC", "CONCOR.NS": "Container Corporation",
    "SRF.NS": "SRF Limited", "DEEPAKNTR.NS": "Deepak Nitrite", "TATACHEM.NS": "Tata Chemicals",
    "TORNTPOWER.NS": "Torrent Power", "EXIDEIND.NS": "Exide Industries",
    "MOTHERSON.NS": "Samvardhana Motherson", "BALKRISIND.NS": "Balkrishna Industries",
    "APOLLOTYRE.NS": "Apollo Tyres", "JKCEMENT.NS": "JK Cement", "RAMCOCEM.NS": "Ramco Cements",
    "DALBHARAT.NS": "Dalmia Bharat", "MUTHOOTFIN.NS": "Muthoot Finance",
    "LICHSGFIN.NS": "LIC Housing Finance", "CANBK.NS": "Canara Bank", "BANKBARODA.NS": "Bank of Baroda",
    # Smallcaps
    "CDSL.NS": "CDSL", "CAMS.NS": "CAMS Services", "ANGELONE.NS": "Angel One",
    "MCX.NS": "Multi Commodity Exchange", "IEX.NS": "Indian Energy Exchange",
    "TANLA.NS": "Tanla Platforms", "HFCL.NS": "HFCL Limited", "RAILTEL.NS": "RailTel",
    "HUDCO.NS": "HUDCO", "NBCC.NS": "NBCC India", "NCC.NS": "NCC Limited",
    "CESC.NS": "CESC Limited", "TRIDENT.NS": "Trident Limited", "PVRINOX.NS": "PVR INOX",
    "RBLBANK.NS": "RBL Bank", "GRAPHITE.NS": "Graphite India", "HEG.NS": "HEG Limited",
    "NATIONALUM.NS": "National Aluminium", "HINDCOPPER.NS": "Hindustan Copper",
    "MOIL.NS": "MOIL Limited", "GESHIP.NS": "Great Eastern Shipping", "SUZLON.NS": "Suzlon Energy",
    "MANAPPURAM.NS": "Manappuram Finance", "GRANULES.NS": "Granules India",
    "COCHINSHIP.NS": "Cochin Shipyard",
}

# Financial-sector tickers where debt/equity screening is skipped (leverage is
# part of the business model for banks/NBFCs).
FINANCIALS = {
    "HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS",
    "INDUSINDBK.NS", "BAJFINANCE.NS", "BAJAJFINSV.NS", "SHRIRAMFIN.NS",
    "FEDERALBNK.NS", "IDFCFIRSTB.NS", "MUTHOOTFIN.NS", "LICHSGFIN.NS",
    "CANBK.NS", "BANKBARODA.NS", "RBLBANK.NS", "MANAPPURAM.NS", "HUDCO.NS",
}

# Mock holdings seeded on first run (ticker, avg_buy_price, quantity)
MOCK_HOLDINGS = [
    ("RELIANCE.NS", 1250.0, 10),
    ("TCS.NS", 2050.0, 5),
    ("INFY.NS", 1080.0, 12),
]

# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
HISTORY_PERIOD = "1y"          # daily bars fetched per ticker
MIN_HISTORY_BARS = 60          # skip tickers with less history

# Run window (IST). NSE trades Mon-Fri 09:15-15:30; the window is wider so the
# 08:45 pre-open news run and the 18:30 post-close run still fire. Outside it
# (nights/weekends) run_daily.py exits immediately unless --force is passed.
RUN_WINDOW_OPEN = "08:30"
RUN_WINDOW_CLOSE = "18:45"

# ---------------------------------------------------------------------------
# Entry criteria
# ---------------------------------------------------------------------------
RSI_ENTRY_MIN = 45.0
RSI_ENTRY_MAX = 68.0
MACD_CROSS_LOOKBACK = 3        # bullish crossover within N bars counts
MIN_UPSIDE_PCT = 2.0           # target must be >= 2% above entry
MIN_REWARD_RISK = 1.5
MAX_RISK_PCT = 5.0             # stop clamped to at most 5% below entry
SENTIMENT_ENTRY_MIN = -0.2     # sentiment must be above this to allow entry
MAX_NEW_PICKS_PER_DAY = None   # None = uncapped; capital efficiency (strategy_engine
                               # capital weights + paper.py cash/risk sizing) is the
                               # real limiter now, not an arbitrary per-day pick count

# ---------------------------------------------------------------------------
# News-first channel (Channel B): news is the primary signal
# ---------------------------------------------------------------------------
NEWS_SENTIMENT_MIN = 0.5       # strong bullish catalyst required
NEWS_CONFIDENCE_MIN = 0.5
NEWS_RSI_MAX = 75.0            # don't chase a vertical spike
NEWS_MIN_REWARD_RISK = 1.2     # looser than technical channel
MAX_NEWS_PICKS_PER_DAY = 3

# Ranking bonuses
VOL_RATIO_BONUS_THRESHOLD = 1.2
RSI_SWEETSPOT = (50.0, 65.0)
PIVOT_PROXIMITY_PCT = 1.0      # close within 1% above pivot earns bonus

# ---------------------------------------------------------------------------
# Exit criteria
# ---------------------------------------------------------------------------
SETUP_BROKEN_SMA_BARS = 2      # closes below SMA20 for N consecutive bars
SETUP_BROKEN_RSI = 45.0        # with MACD bearish cross
SENTIMENT_EXIT_MAX = -0.5      # sentiment at/below this breaks the setup
MAX_HOLDING_DAYS = 10          # trading days before EXPIRED

# ---------------------------------------------------------------------------
# Fundamental quality gate (market-cap floor is tier-aware)
# ---------------------------------------------------------------------------
MIN_MARKET_CAP_BY_TIER = {
    "LARGE": 200e9,   # ₹200B
    "MID": 50e9,      # ₹50B
    "SMALL": 5e9,     # ₹5B
}
MAX_PE = 100.0   # Indian premium consumer names routinely trade 60-90x
MIN_ROE = 0.08
MAX_DEBT_TO_EQUITY = 200.0     # yfinance reports as percentage
MIN_EARNINGS_GROWTH = -0.10

# Liquidity gate: 20-day average daily traded value (close * volume), INR
MIN_AVG_TURNOVER = 250e6       # ₹25 crore/day

# ---------------------------------------------------------------------------
# Portfolio health thresholds
# ---------------------------------------------------------------------------
RESISTANCE_PROXIMITY_PCT = 1.5
SUPPORT_PROXIMITY_PCT = 1.5
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 35.0

# ---------------------------------------------------------------------------
# News / sentiment
# ---------------------------------------------------------------------------
MAX_HEADLINES_PER_TICKER = 5
NEWS_MAX_AGE_DAYS = 3
SENTIMENT_BATCH_SIZE = 15      # tickers per claude -p call
SENTIMENT_PARALLEL_CALLS = 1   # concurrent claude -p processes (>1 can exhaust
                               # memory - each CLI process is a full Bun runtime)
SENTIMENT_MODEL = "haiku"      # cheap on subscription quota; e.g. "sonnet"
CLAUDE_CLI_TIMEOUT = 300       # seconds per batched call

# ---------------------------------------------------------------------------
# Paper trading — one shared virtual book, positions tagged by strategy
# ---------------------------------------------------------------------------
PAPER_STRATEGIES = ["TECHNICAL", "NEWS", "PULLBACK"]
PAPER_STARTING_CASH = 100_000.0  # INR, single shared cash pool
PAPER_RISK_PCT_PER_TRADE = 1.5   # % of book equity risked between entry and stop
PAPER_MAX_POSITION_PCT = 40.0    # max position value as % of book equity
PAPER_MIN_CASH_BUFFER = 50.0     # cash kept free for charges (INR)
PAPER_MIN_POSITION_VALUE = 1500.0  # skip positions smaller than this: fixed
                                   # charges (~Rs 23 round trip) would eat them

# Indian delivery cost model (INDmoney/INDstocks rates). All *_PCT are percent.
PAPER_SLIPPAGE_PCT = 0.05        # adverse fill assumption, each side
PAPER_BROKERAGE_PER_ORDER = 5.0  # flat INR per executed order
PAPER_STT_PCT = 0.10             # securities transaction tax, each side
PAPER_EXCH_TXN_PCT = 0.00297     # NSE transaction charge
PAPER_SEBI_PCT = 0.0001          # SEBI turnover fee
PAPER_STAMP_PCT_BUY = 0.015      # stamp duty, buy side only
PAPER_GST_PCT = 18.0             # on brokerage + exchange + SEBI (+ DP on sell)
PAPER_DP_CHARGE_SELL = 16.0      # depository charge per scrip per sell day, INR

# ---------------------------------------------------------------------------
# Pullback channel (Channel C): buy-the-dip to SMA20 inside an uptrend
# ---------------------------------------------------------------------------
PULLBACK_SMA20_TOUCH_PCT = 1.0          # day's low within 1% of SMA20
PULLBACK_MAX_CLOSE_BELOW_SMA20_PCT = 1.0  # close may sit at most 1% under SMA20
PULLBACK_RSI_MIN = 35.0
PULLBACK_RSI_MAX = 55.0                 # below TECHNICAL's 45-68 window
PULLBACK_MIN_MOM20 = 3.0                # 20d momentum proves uptrend (%)
PULLBACK_MAX_MOM5 = 0.0                 # 5d momentum <= 0 proves a dip (%)
PULLBACK_MIN_REWARD_RISK = 1.5
MAX_PULLBACK_PICKS_PER_DAY = None       # None = uncapped, see MAX_NEW_PICKS_PER_DAY note
PULLBACK_SETUP_BROKEN_SMA_BARS = 3      # entry IS at SMA20; default 2 would whipsaw

# ---------------------------------------------------------------------------
# Order Flow channel — Chaikin Money Flow as a daily-bar order-flow proxy.
# Real order flow needs tick/bid-ask data; CMF approximates buying/selling
# pressure from where each day's close sits in its range, weighted by volume.
# ---------------------------------------------------------------------------
ORDERFLOW_CMF_MIN = 0.05          # minimum CMF(20) - mild net buying pressure
ORDERFLOW_MIN_REWARD_RISK = 1.5
MAX_ORDERFLOW_PICKS_PER_DAY = None

# ---------------------------------------------------------------------------
# Liquidity Sweep channel — daily-bar "stop hunt then reclaim" (Wyckoff spring):
# today's low undercuts the prior 10-bar swing low but closes back above it,
# in the upper half of the day's range.
# ---------------------------------------------------------------------------
LIQSWEEP_MIN_REVERSAL_STRENGTH = 0.5   # (close-low)/(high-low) - close in upper half of range
LIQSWEEP_MIN_REWARD_RISK = 1.5
MAX_LIQUIDITY_SWEEP_PICKS_PER_DAY = None

# ---------------------------------------------------------------------------
# Fair Value Gap channel — classic 3-candle imbalance, entered on a retest.
# ---------------------------------------------------------------------------
FVG_LOOKBACK_BARS = 15            # window scanned for an unfilled bullish FVG (global, not per-variant)
FVG_MIN_REWARD_RISK = 1.5
MAX_FVG_PICKS_PER_DAY = None

# ---------------------------------------------------------------------------
# Anchored VWAP channel — VWAP anchored to the most recent swing low (a daily-
# bar approximation; true anchored VWAP normally anchors to an intraday event).
# ---------------------------------------------------------------------------
AVWAP_LOOKBACK_BARS = 60           # window used to pick the anchor swing low (global)
AVWAP_RECLAIM_TOLERANCE_PCT = 1.0  # close must clear anchored VWAP by at least this %
AVWAP_MIN_REWARD_RISK = 1.5
MAX_ANCHORED_VWAP_PICKS_PER_DAY = None

# ---------------------------------------------------------------------------
# Volume Profile channel — pullback to the 60-bar Point-of-Control proxy (a
# daily-bar volume-weighted price histogram; not real intraday volume-at-price).
# ---------------------------------------------------------------------------
VOLPROFILE_LOOKBACK_BARS = 60      # global (see AVWAP note on lookback vs threshold)
VOLPROFILE_BINS = 20               # global
VOLPROFILE_TOUCH_TOLERANCE_PCT = 1.0   # how close to POC counts as "at" it
VOLPROFILE_MIN_REWARD_RISK = 1.5
MAX_VOLUME_PROFILE_PICKS_PER_DAY = None

# ---------------------------------------------------------------------------
# 52-Week High Breakout channel — classic momentum-leadership breakout.
# ---------------------------------------------------------------------------
BREAKOUT52W_TOLERANCE_PCT = 2.0    # close within this % of the 252d high counts as "at" it
BREAKOUT52W_MIN_VOL_RATIO = 1.3    # volume confirmation
BREAKOUT52W_MIN_REWARD_RISK = 1.5
MAX_BREAKOUT_52W_PICKS_PER_DAY = None

# ---------------------------------------------------------------------------
# Market crash detection (feeds strategy_daily_context)
# ---------------------------------------------------------------------------
MARKET_CRASH_THRESHOLD_PCT = -3.0   # single-day Nifty/Bank Nifty return below this = "crash"
GLOBAL_CRASH_THRESHOLD_PCT = -2.0   # US indices are typically less volatile day-to-day

# ---------------------------------------------------------------------------
# Strategy engine — TECHNICAL & PULLBACK become fleets of competing parameter
# variants, judged on paper-trade outcomes (stockbot/strategy_engine.py).
# NEWS is out of scope: it stays a single fixed strategy, but keeps a row in
# the strategies table so capital weighting spans all live strategies evenly.
# ---------------------------------------------------------------------------
STRATEGY_MIN_TRADES_FOR_RETIREMENT = 30   # closed trades before a variant can be judged
STRATEGY_STALLED_DAYS = 120               # force a review if still <30 trades after this many days
STRATEGY_RETIREMENT_WIN_RATE_FLOOR = 35.0 # retire if win rate is below this AND trades >= min
STRATEGY_GRADUATE_MIN_TRADES = 50         # sample size required to flag as graduate-candidate
STRATEGY_GRADUATE_WIN_RATE = 55.0         # win rate required to flag as graduate-candidate
STRATEGY_MIN_CAPITAL_WEIGHT_PCT = 5.0     # floor: every active strategy gets at least this share
STRATEGY_MAX_CAPITAL_WEIGHT_PCT = 40.0    # ceiling: no strategy dominates the shared book
STRATEGY_WILDCARD_INTERVAL_DAYS = 7       # cadence for an extra experimental variant
STRATEGY_FLEET_SIZE = 3                   # fallback target fleet size (see BY_CHANNEL below)
STRATEGY_FLEET_MAX = 6                    # fallback hard ceiling (see BY_CHANNEL below)
STRATEGY_LLM_MODEL = "sonnet"             # reasoning quality matters more than cost here;
                                           # this runs at most a few times a week, not per-ticker

# All channels the self-evolving fleet manages (NEWS excluded — stays a single
# fixed strategy, out of scope for retirement/creation per the original ask).
EVOLVING_CHANNELS = (
    "TECHNICAL", "PULLBACK", "ORDERFLOW", "LIQUIDITY_SWEEP",
    "FVG", "ANCHORED_VWAP", "VOLUME_PROFILE", "BREAKOUT_52W",
)
# TECHNICAL/PULLBACK are proven — keep their existing 3-variant fleets. The six
# new channels start leaner (2) until they've earned a bigger footprint; this
# also keeps total-strategy-count growth (and the capital-weight floor math
# below) manageable as more channels get added.
STRATEGY_FLEET_SIZE_BY_CHANNEL = {
    "TECHNICAL": 3, "PULLBACK": 3,
    "ORDERFLOW": 2, "LIQUIDITY_SWEEP": 2, "FVG": 2,
    "ANCHORED_VWAP": 2, "VOLUME_PROFILE": 2, "BREAKOUT_52W": 2,
}
STRATEGY_FLEET_MAX_BY_CHANNEL = {
    "TECHNICAL": 6, "PULLBACK": 6,
    "ORDERFLOW": 4, "LIQUIDITY_SWEEP": 4, "FVG": 4,
    "ANCHORED_VWAP": 4, "VOLUME_PROFILE": 4, "BREAKOUT_52W": 4,
}

# Guardrail bounds for LLM-proposed parameter variants (min, max) per channel.
# propose_new_variant() clamps any proposal into these ranges before it's ever
# considered — keeps proposals sane and comparable regardless of what the model returns.
STRATEGY_PARAM_BOUNDS = {
    "TECHNICAL": {
        "rsi_entry_min": (30.0, 55.0),
        "rsi_entry_max": (55.0, 80.0),
        "min_reward_risk": (1.2, 3.0),
    },
    "PULLBACK": {
        "pullback_rsi_min": (25.0, 45.0),
        "pullback_rsi_max": (45.0, 65.0),
        "pullback_min_mom20": (1.0, 8.0),
        "pullback_max_mom5": (-3.0, 2.0),
        "pullback_min_reward_risk": (1.2, 3.0),
        "pullback_sma20_touch_pct": (0.5, 2.5),
    },
    "ORDERFLOW": {
        "cmf_min": (-0.05, 0.25),
        "min_reward_risk": (1.2, 3.0),
    },
    "LIQUIDITY_SWEEP": {
        "reversal_strength_min": (0.3, 0.8),
        "min_reward_risk": (1.2, 3.0),
    },
    "FVG": {
        "min_reward_risk": (1.2, 3.0),
    },
    "ANCHORED_VWAP": {
        "reclaim_tolerance_pct": (0.3, 3.0),
        "min_reward_risk": (1.2, 3.0),
    },
    "VOLUME_PROFILE": {
        "touch_tolerance_pct": (0.3, 3.0),
        "min_reward_risk": (1.2, 3.0),
    },
    "BREAKOUT_52W": {
        "tolerance_pct": (0.5, 5.0),
        "min_vol_ratio": (1.0, 2.5),
        "min_reward_risk": (1.2, 3.0),
    },
}

# Composable optional gate toggles the LLM can combine when proposing a
# "wildcard" variant (mode="wildcard") — real novelty without executing
# arbitrary unattended LLM-authored code. Each key maps to a tested predicate
# in stockbot/signals.py's TOGGLE_CONDITIONS registry.
STRATEGY_TOGGLE_LIBRARY = [
    "require_volume_surge",              # 20d avg volume ratio > VOL_RATIO_BONUS_THRESHOLD
    "require_close_above_weekly_r1",     # stronger breakout: close clears last week's R1
    "require_sector_relative_strength",  # ticker's 20d momentum beats its tier's median
    "require_positive_order_flow",       # CMF(20) > 0 - net buying pressure
    "require_above_anchored_vwap",       # close above the anchored VWAP
    "require_near_volume_poc_support",   # close at/above the 60-bar volume POC
    "require_near_52w_high",             # close within BREAKOUT52W_TOLERANCE_PCT of the 252d high
]

# ---------------------------------------------------------------------------
# Fyers API v3 (primary broker since 2026-07-11: market data + holdings; creds
# in .env). The app is SHARED with mcx-short-term — one active access token
# per app, so both projects must point FYERS_TOKEN_PATH at the same file.
# ---------------------------------------------------------------------------
FYERS_API_BASE = "https://api-t1.fyers.in/api/v3"
FYERS_DATA_BASE = "https://api-t1.fyers.in/data"
FYERS_TIMEOUT = 20             # seconds per REST call
FYERS_HISTORY_DAYS = 360       # <= 366-day per-request limit on daily candles
FYERS_MAX_WORKERS = 4          # parallel history fetchers
FYERS_MIN_CALL_GAP = 0.35      # seconds between request starts (~170/min,
                               # under Fyers' 200/min data-API rate limit)


def fyers_settings() -> dict:
    """Read Fyers API credentials from the environment (after load_dotenv)."""
    return {
        "app_id": os.getenv("FYERS_APP_ID", "").strip(),
        "secret_id": os.getenv("FYERS_SECRET_ID", "").strip(),
        "pin": os.getenv("FYERS_PIN", "").strip(),
        "redirect_uri": os.getenv(
            "FYERS_REDIRECT_URI",
            "https://trade.fyers.in/api-login/redirect-uri/index.html").strip(),
    }


# ---------------------------------------------------------------------------
# OpenAlgo broker bridge (RETIRED with INDmoney 2026-07-11 — kept only as a
# holdings-sync fallback while creds remain in .env)
# ---------------------------------------------------------------------------
OPENALGO_TIMEOUT = 15            # seconds per REST call
HOLDINGS_STALE_HOURS = 30        # warn when the last broker sync is older
BROKER_SYMBOL_OVERRIDES = {}     # OpenAlgo symbol -> yfinance ticker exceptions
PLACE_ORDER_ENABLED = False      # hard gate: real order placement is OFF in v1

# Mirror paper BUY/SELL orders into OpenAlgo's Analyzer (sandbox) so they show
# up in its trading UI. Safety: mirroring only happens when OpenAlgo confirms
# analyzer mode is ON — if the server is in live mode, orders are NOT sent.
# OFF until the IndMoney broker connect (OpenAlgo's symbol master needs the
# broker token, so sandbox orders fail without it). Flip to True after.
PAPER_MIRROR_TO_OPENALGO = False


def openalgo_settings() -> dict:
    """Read OpenAlgo connection settings from the environment (after load_dotenv)."""
    return {
        "host": os.getenv("OPENALGO_HOST", "").strip().rstrip("/"),
        "api_key": os.getenv("OPENALGO_API_KEY", "").strip(),
    }


# ---------------------------------------------------------------------------
# Discord (credentials come from .env / environment)
# ---------------------------------------------------------------------------
DISCORD_API_BASE = "https://discord.com/api/v10"


def discord_settings() -> dict:
    """Read Discord credentials from the environment (after load_dotenv)."""
    return {
        "token": os.getenv("DISCORD_BOT_TOKEN", "").strip(),
        "picks_channel": os.getenv("DISCORD_PICKS_CHANNEL_ID", "").strip(),
        "holdings_channel": os.getenv("DISCORD_HOLDINGS_CHANNEL_ID", "").strip(),
        "paper_channel": os.getenv("DISCORD_PAPER_CHANNEL_ID", "").strip(),
        "strategy_channel": os.getenv("DISCORD_STRATEGY_CHANNEL_ID", "").strip(),
    }
