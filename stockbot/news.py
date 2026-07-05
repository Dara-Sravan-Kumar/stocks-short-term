"""Daily headline collection: yfinance .news with Google News RSS fallback."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import feedparser
import yfinance as yf

import config


def _from_yfinance(ticker: str) -> list[dict]:
    items = []
    try:
        news = yf.Ticker(ticker).news or []
    except Exception:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.NEWS_MAX_AGE_DAYS)
    for item in news:
        # yfinance >= 0.2.40 nests fields under 'content'
        content = item.get("content", item)
        title = content.get("title")
        if not title:
            continue
        pub = content.get("pubDate") or content.get("providerPublishTime")
        when = None
        if isinstance(pub, str):
            try:
                when = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            except ValueError:
                pass
        elif isinstance(pub, (int, float)):
            when = datetime.fromtimestamp(pub, tz=timezone.utc)
        if when is not None and when < cutoff:
            continue
        provider = content.get("provider") or {}
        items.append({
            "title": title.strip(),
            "publisher": (provider.get("displayName") if isinstance(provider, dict)
                          else content.get("publisher")) or "unknown",
            "date": when.strftime("%Y-%m-%d") if when else "",
        })
    return items


def _from_google_rss(ticker: str) -> list[dict]:
    company = config.COMPANY_NAMES.get(ticker, ticker.replace(".NS", ""))
    url = ("https://news.google.com/rss/search?q="
           + quote_plus(f'"{company}" stock NSE')
           + "&hl=en-IN&gl=IN&ceid=IN:en")
    try:
        feed = feedparser.parse(url)
    except Exception:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.NEWS_MAX_AGE_DAYS)
    items = []
    for entry in feed.entries[:15]:
        title = getattr(entry, "title", "").strip()
        if not title:
            continue
        when = None
        if getattr(entry, "published_parsed", None):
            when = datetime.fromtimestamp(time.mktime(entry.published_parsed), tz=timezone.utc)
        if when is not None and when < cutoff:
            continue
        items.append({
            "title": title,
            "publisher": getattr(entry, "source", {}).get("title", "Google News")
                         if isinstance(getattr(entry, "source", None), dict) else "Google News",
            "date": when.strftime("%Y-%m-%d") if when else "",
        })
    return items


def fetch_headlines(ticker: str) -> list[dict]:
    """Up to MAX_HEADLINES_PER_TICKER recent headlines for a ticker."""
    items = _from_yfinance(ticker)
    if not items:
        items = _from_google_rss(ticker)
    return items[: config.MAX_HEADLINES_PER_TICKER]


def fetch_headlines_bulk(tickers: list[str], warnings: list[str],
                         max_workers: int = 10) -> dict[str, list[dict]]:
    """Fetch headlines for many tickers in parallel (news-first channel)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    out: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_headlines, t): t for t in tickers}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                out[t] = fut.result()
            except Exception as exc:
                warnings.append(f"{t}: news fetch failed ({exc})")
                out[t] = []
    return out
