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
MAX_NEW_PICKS_PER_DAY = 3      # per channel (technical)

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
# Discord (credentials come from .env / environment)
# ---------------------------------------------------------------------------
DISCORD_API_BASE = "https://discord.com/api/v10"


def discord_settings() -> dict:
    """Read Discord credentials from the environment (after load_dotenv)."""
    return {
        "token": os.getenv("DISCORD_BOT_TOKEN", "").strip(),
        "picks_channel": os.getenv("DISCORD_PICKS_CHANNEL_ID", "").strip(),
        "holdings_channel": os.getenv("DISCORD_HOLDINGS_CHANNEL_ID", "").strip(),
    }
