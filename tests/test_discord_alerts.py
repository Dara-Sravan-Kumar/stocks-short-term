"""Unit tests for stockbot.discord_alerts.has_reportable_activity() — the
quiet-mode gate for hourly (--skip-news) runs: skip the Discord post unless
something actually happened.
"""
from stockbot import discord_alerts


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
