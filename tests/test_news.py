"""Merge/dedupe behavior of the two-source headline fetch."""
import config
from stockbot import news


def _fake(items):
    return lambda ticker: [dict(i) for i in items]


def test_merges_both_sources(monkeypatch):
    monkeypatch.setattr(news, "_from_yfinance", _fake([
        {"title": "Jio files for IPO", "publisher": "Reuters", "date": "2026-07-05"},
    ]))
    monkeypatch.setattr(news, "_from_google_rss", _fake([
        {"title": "Reliance wins solar order", "publisher": "Moneycontrol",
         "date": "2026-07-06"},
    ]))
    out = news.fetch_headlines("RELIANCE.NS")
    assert len(out) == 2
    # newest first
    assert out[0]["publisher"] == "Moneycontrol"


def test_google_fetched_even_when_yahoo_has_items(monkeypatch):
    called = {"google": False}

    def google(ticker):
        called["google"] = True
        return []

    monkeypatch.setattr(news, "_from_yfinance",
                        _fake([{"title": "Wire item", "publisher": "MT Newswires",
                                "date": "2026-07-06"}]))
    monkeypatch.setattr(news, "_from_google_rss", google)
    news.fetch_headlines("X.NS")
    assert called["google"]  # merge, not fallback


def test_dedupes_same_story_across_sources(monkeypatch):
    monkeypatch.setattr(news, "_from_yfinance", _fake([
        {"title": "Reliance's Jio Platforms to Seek India Listing",
         "publisher": "Bloomberg", "date": "2026-07-05"},
    ]))
    monkeypatch.setattr(news, "_from_google_rss", _fake([
        {"title": "Reliance's Jio Platforms to seek India listing!",
         "publisher": "Moneycontrol", "date": "2026-07-05"},
        {"title": "", "publisher": "x", "date": ""},
    ]))
    out = news.fetch_headlines("RELIANCE.NS")
    assert len(out) == 1  # punctuation/case-insensitive dedupe; empty dropped


def test_caps_at_configured_max(monkeypatch):
    many = [{"title": f"story {i}", "publisher": "p", "date": f"2026-07-0{i%9+1}"}
            for i in range(12)]
    monkeypatch.setattr(news, "_from_yfinance", _fake(many[:6]))
    monkeypatch.setattr(news, "_from_google_rss", _fake(many[6:]))
    out = news.fetch_headlines("X.NS")
    assert len(out) == config.MAX_HEADLINES_PER_TICKER
    dates = [h["date"] for h in out]
    assert dates == sorted(dates, reverse=True)
