"""Unit tests for stockbot.discord_alerts.has_reportable_activity() — the
quiet-mode gate for hourly (--skip-news) runs: skip the Discord post unless
something actually happened.
"""
from stockbot import discord_alerts
from stockbot import discord_alerts as da


def test_nothing_happened_is_not_reportable():
    assert not discord_alerts.has_reportable_activity(
        closed=[], new_picks=[], paper_entries=[], paper_exits=[], strategy_events=[])


def test_none_arguments_treated_as_empty():
    assert not discord_alerts.has_reportable_activity(
        closed=[], new_picks=[], paper_entries=None, paper_exits=None, strategy_events=None)


def test_a_closed_pick_is_reportable():
    assert discord_alerts.has_reportable_activity(
        closed=[{"ticker": "X.NS"}], new_picks=[], paper_entries=[], paper_exits=[],
        strategy_events=[])


def test_a_new_pick_is_reportable():
    assert discord_alerts.has_reportable_activity(
        closed=[], new_picks=[{"ticker": "X.NS"}], paper_entries=[], paper_exits=[],
        strategy_events=[])


def test_a_paper_buy_is_reportable():
    assert discord_alerts.has_reportable_activity(
        closed=[], new_picks=[], paper_entries=[{"action": "BUY", "ticker": "X.NS"}],
        paper_exits=[], strategy_events=[])


def test_paper_skips_alone_are_not_reportable():
    # SKIPs (budget exhausted, insufficient cash, etc.) aren't worth a post
    assert not discord_alerts.has_reportable_activity(
        closed=[], new_picks=[], paper_entries=[{"action": "SKIP", "ticker": "X.NS"}],
        paper_exits=[], strategy_events=[])


def test_a_paper_sell_is_reportable():
    assert discord_alerts.has_reportable_activity(
        closed=[], new_picks=[], paper_entries=[], paper_exits=[{"ticker": "X.NS"}],
        strategy_events=[])


def test_a_strategy_event_is_reportable():
    assert discord_alerts.has_reportable_activity(
        closed=[], new_picks=[], paper_entries=[], paper_exits=[],
        strategy_events=[{"type": "retired", "variant_key": "TECHNICAL_seed"}])


# ---------------------------------------------------------------------------
# Hourly-throttled "Fyers login missing" reminder — posted when the book is
# FROZEN (no fresh Fyers token). Posts to holdings channel, else picks channel.
# ---------------------------------------------------------------------------

def test_login_reminder_sends_and_records(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setenv("DISCORD_HOLDINGS_CHANNEL_ID", "chan")
    state = tmp_path / ".login_reminder_sent"
    monkeypatch.setattr(da, "_login_reminder_state", lambda: state)
    captured = {}

    def fake_post(token, channel, payload, warnings):
        captured["channel"] = channel
        captured["payload"] = payload
        return True

    monkeypatch.setattr(da, "_post", fake_post)
    assert da.send_login_reminder([]) == "sent"
    assert captured["channel"] == "chan"
    assert "FROZEN" in str(captured["payload"])
    assert state.exists()  # timestamp recorded for throttling


def test_login_reminder_falls_back_to_picks_channel(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.delenv("DISCORD_HOLDINGS_CHANNEL_ID", raising=False)
    monkeypatch.setenv("DISCORD_PICKS_CHANNEL_ID", "picks")
    monkeypatch.setattr(da, "_login_reminder_state",
                        lambda: tmp_path / ".login_reminder_sent")
    captured = {}
    monkeypatch.setattr(da, "_post",
                        lambda t, c, p, w: captured.setdefault("channel", c) or True)
    assert da.send_login_reminder([]) == "sent"
    assert captured["channel"] == "picks"


def test_login_reminder_throttled_within_the_hour(monkeypatch, tmp_path):
    from datetime import datetime
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setenv("DISCORD_HOLDINGS_CHANNEL_ID", "chan")
    state = tmp_path / ".login_reminder_sent"
    state.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    monkeypatch.setattr(da, "_login_reminder_state", lambda: state)

    def must_not_post(*a, **k):
        raise AssertionError("throttled reminder must not post")

    monkeypatch.setattr(da, "_post", must_not_post)
    assert da.send_login_reminder([]).startswith("throttled")


def test_login_reminder_unconfigured(monkeypatch, tmp_path):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_HOLDINGS_CHANNEL_ID", raising=False)
    monkeypatch.delenv("DISCORD_PICKS_CHANNEL_ID", raising=False)
    monkeypatch.setattr(da, "_login_reminder_state", lambda: tmp_path / ".none")
    assert da.send_login_reminder([]) == "unconfigured"
