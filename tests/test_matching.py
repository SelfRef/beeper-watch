"""Tests for the rule matcher — the part where a mistake is silent.

A wrong filter does not crash: it just quietly never fires, or fires on
everything. These cover the cases where that is most likely: the wrapping
time window, sender globs against two different identifiers, and the negative
filters.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.matching import matches
from app.models import Matcher

TZ = ZoneInfo("Europe/Warsaw")
NOON = datetime(2026, 9, 16, 12, 0, tzinfo=TZ)  # a Wednesday
NIGHT = datetime(2026, 9, 16, 23, 30, tzinfo=TZ)
EARLY = datetime(2026, 9, 16, 5, 0, tzinfo=TZ)


def event(**overrides) -> dict:
    base = {
        "bridge": "sh-telegram",
        "network": "telegram",
        "direction": "in",
        "kind": "message",
        "room_id": "!room:beeper.local",
        "event_id": "$one",
        "sender": "@sh-telegram_1127943894:beeper.local",
        "sender_remote_id": "1127943894",
        "is_self": False,
        "msgtype": "m.text",
        "body": "Please send the report",
        "media": None,
    }
    base.update(overrides)
    return base


def test_empty_matcher_matches_everything():
    assert matches(Matcher(), event(), NOON)


def test_network_and_kind():
    assert matches(Matcher(networks=["telegram"], kinds=["message"]), event(), NOON)
    assert not matches(Matcher(networks=["signal"]), event(), NOON)
    assert not matches(Matcher(kinds=["reaction"]), event(), NOON)


def test_network_is_case_insensitive():
    assert matches(Matcher(networks=["Telegram"]), event(), NOON)


def test_sender_by_mxid_remote_id_or_glob():
    assert matches(Matcher(senders=["@sh-telegram_1127943894:beeper.local"]), event(), NOON)
    assert matches(Matcher(senders=["1127943894"]), event(), NOON)
    assert matches(Matcher(senders=["@sh-telegram_*"]), event(), NOON)
    assert not matches(Matcher(senders=["@sh-signal_*"]), event(), NOON)


def test_exclusions_win():
    assert not matches(Matcher(exclude_senders=["1127943894"]), event(), NOON)
    assert not matches(Matcher(exclude_rooms=["!room:*"]), event(), NOON)


def test_text_filters():
    assert matches(Matcher(contains="REPORT"), event(), NOON)
    assert matches(Matcher(regex=r"(?i)\b(report|status)\b"), event(), NOON)
    assert not matches(Matcher(regex=r"invoice"), event(), NOON)
    assert not matches(Matcher(not_regex=r"(?i)report"), event(), NOON)
    assert matches(Matcher(contains_any=["invoice", "report"]), event(), NOON)


def test_text_filter_never_matches_a_reaction():
    # Reactions carry no body, which is the intended reading of "contains".
    assert not matches(Matcher(contains="report"), event(kind="reaction", body=""), NOON)


def test_is_self_and_media():
    assert matches(Matcher(is_self=False), event(), NOON)
    assert not matches(Matcher(is_self=True), event(), NOON)
    assert matches(Matcher(has_media=True), event(media={"mimetype": "image/png"}), NOON)
    assert not matches(Matcher(has_media=True), event(), NOON)


def test_daytime_window():
    day = Matcher(time_from="09:00", time_to="17:00")
    assert matches(day, event(), NOON)
    assert not matches(day, event(), NIGHT)


def test_wrapping_night_window():
    night = Matcher(time_from="22:00", time_to="06:00")
    assert matches(night, event(), NIGHT)
    assert matches(night, event(), EARLY)
    assert not matches(night, event(), NOON)


def test_weekdays():
    assert matches(Matcher(weekdays=[2]), event(), NOON)  # Wednesday
    assert not matches(Matcher(weekdays=[5, 6]), event(), NOON)


def test_reaction_keys():
    assert matches(Matcher(kinds=["reaction"], reaction_keys=["👍"]),
                   event(kind="reaction", body="", reaction_key="👍"), NOON)
    assert not matches(Matcher(reaction_keys=["👍"]),
                       event(kind="reaction", body="", reaction_key="❌"), NOON)


def test_length_bounds():
    assert matches(Matcher(min_length=5), event(), NOON)
    assert not matches(Matcher(max_length=5), event(), NOON)


def test_invalid_regex_is_rejected_at_definition_time():
    with pytest.raises(ValueError):
        Matcher(regex="(unclosed")
