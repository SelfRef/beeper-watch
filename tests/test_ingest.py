"""End-to-end through the ingest path, against a temporary database."""

import importlib
import os
import tempfile

import pytest


@pytest.fixture()
def svc(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    os.environ["BEEPER_WATCH_DB"] = tmp.name
    from app import config, db

    importlib.reload(config)
    importlib.reload(db)
    from app import service

    importlib.reload(service)
    db._conn = None
    db.connect()
    yield service, db
    db._conn = None
    os.unlink(tmp.name)


def sample(**over):
    base = {
        "network": "telegram",
        "direction": "in",
        "kind": "message",
        "room_id": "!r:beeper.local",
        "event_id": "$a",
        "sender": "@sh-telegram_1:beeper.local",
        "body": "the report is ready",
        "timestamp": 1758300000000,
    }
    base.update(over)
    return base


def test_match_queues_one_delivery(svc):
    service, db = svc
    db.insert_rule(
        {
            "name": "reports",
            "match": {"contains": "report"},
            "action": {"webhook_url": "http://example.invalid/hook"},
        }
    )
    result = service.ingest(sample())
    assert result["matched"] == ["reports"]
    assert result["queued"] == 1
    assert len(db.due_deliveries()) == 1


def test_duplicate_event_is_not_matched_twice(svc):
    service, db = svc
    db.insert_rule(
        {"name": "all", "match": {}, "action": {"webhook_url": "http://example.invalid/hook"}}
    )
    service.ingest(sample())
    again = service.ingest(sample())
    assert again["status"] == "duplicate"
    assert len(db.due_deliveries()) == 1


def test_cooldown_suppresses_the_second_delivery(svc):
    service, db = svc
    db.insert_rule(
        {
            "name": "noisy",
            "match": {},
            "cooldown_seconds": 3600,
            "action": {"webhook_url": "http://example.invalid/hook"},
        }
    )
    service.ingest(sample(event_id="$a"))
    second = service.ingest(sample(event_id="$b"))
    assert second["matched"] == ["noisy"]
    assert second["queued"] == 0


def test_disabled_rule_never_matches(svc):
    service, db = svc
    db.insert_rule(
        {
            "name": "off",
            "enabled": False,
            "match": {},
            "action": {"webhook_url": "http://example.invalid/hook"},
        }
    )
    assert service.ingest(sample())["matched"] == []


def test_events_without_an_event_id_still_deduplicate(svc):
    service, db = svc
    payload = sample(event_id="", kind="reaction", reaction_key="👍", body="")
    assert service.ingest(payload)["status"] == "accepted"
    assert service.ingest(payload)["status"] == "duplicate"
