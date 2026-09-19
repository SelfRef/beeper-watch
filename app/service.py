"""Ingest one event: store it, decide which rules it matches, queue what to send.

This is the whole product in one function. Everything around it — REST, MCP,
the delivery worker — exists to feed it or to drain what it produces.
"""

from __future__ import annotations

import logging
from datetime import datetime

from . import config, db
from .logging_setup import log
from .matching import matches
from .models import Matcher

logger = logging.getLogger("beeper_watch.service")

PAYLOAD_SCHEMA = 1


def build_payload(rule: dict, event: dict) -> dict:
    """What n8n receives. One complete object, so a workflow starts at "do the
    thing" instead of working out what happened."""
    action = rule.get("action") or {}
    return {
        "schema": PAYLOAD_SCHEMA,
        "matched_at": db.now_iso(),
        "rule": {
            "id": rule.get("id"),
            "name": rule.get("name"),
            "description": rule.get("description", ""),
            "extra": action.get("extra", {}),
        },
        "event": event,
    }


def evaluate(event: dict, now: datetime | None = None) -> list[dict]:
    """Which enabled rules match, ignoring cooldowns. Used by the dry run too."""
    moment = now or datetime.now(config.TIMEZONE)
    hits = []
    for rule in db.list_rules(enabled_only=True):
        try:
            matcher = Matcher(**rule["match"])
        except Exception as err:  # a rule stored by a newer version
            log(logger, logging.ERROR, "Skipping unreadable rule", rule=rule["name"], error=str(err))
            continue
        if matches(matcher, event, moment):
            hits.append(rule)
    return hits


def ingest(event: dict) -> dict:
    row_id, is_new = db.insert_event(event, config.STORE_BODIES)
    if not is_new:
        # The bridge retries a POST it did not get an answer to, so the same
        # event can arrive twice. Matching it twice would fire the rule twice.
        return {"status": "duplicate", "event_row_id": row_id, "matched": []}

    matched: list[str] = []
    queued = 0
    for rule in evaluate(event):
        matched.append(rule["name"])
        if db.rule_in_cooldown(rule):
            log(logger, logging.INFO, "Rule matched but is in cooldown", rule=rule["name"])
            continue
        url = (rule.get("action") or {}).get("webhook_url") or config.DEFAULT_WEBHOOK
        if not url:
            log(
                logger,
                logging.WARNING,
                "Rule matched but has no webhook and no default is set",
                rule=rule["name"],
            )
            continue
        db.enqueue_delivery(rule, row_id, event.get("event_id", ""), url, build_payload(rule, event))
        db.mark_rule_matched(rule["id"])
        queued += 1

    if matched:
        db.set_event_matches(row_id, matched)
        log(
            logger,
            logging.INFO,
            "Event matched",
            rules=matched,
            queued=queued,
            network=event.get("network"),
            kind=event.get("kind"),
            room_id=event.get("room_id"),
        )
    return {"status": "accepted", "event_row_id": row_id, "matched": matched, "queued": queued}


def dry_run(matcher: Matcher, limit: int = 200) -> dict:
    """Run a matcher over stored events without sending anything.

    The point of keeping recent events at all: an agent can write a filter,
    see exactly which of the last few hundred real messages it would have
    caught, and only then save it.
    """
    now = datetime.now(config.TIMEZONE)
    events = db.recent_events(limit=limit)
    hits = [event for event in events if matches(matcher, event, now)]
    return {
        "checked": len(events),
        "matched": len(hits),
        "samples": [
            {
                "received_at": hit.get("_received_at"),
                "network": hit.get("network"),
                "direction": hit.get("direction"),
                "kind": hit.get("kind"),
                "sender": hit.get("sender"),
                "room_id": hit.get("room_id"),
                "body": (hit.get("body") or "")[:280],
            }
            for hit in hits[:20]
        ],
    }
