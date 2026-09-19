"""Does this event match this rule?

A pure function over a dict and a Matcher, with no I/O, so it can be run
against stored events to answer "what would this rule have caught?" before the
rule is ever enabled. That dry run is the main reason rules are worth writing
here rather than as nine conditional nodes in an n8n workflow.
"""

from __future__ import annotations

import re
from datetime import datetime, time
from fnmatch import fnmatch
from functools import lru_cache

from .models import Matcher


@lru_cache(maxsize=256)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _any_glob(candidates: list[str], values: list[str]) -> bool:
    """True when any value matches any candidate, literally or as a glob."""
    for value in values:
        if not value:
            continue
        for candidate in candidates:
            if value == candidate or fnmatch(value, candidate):
                return True
    return False


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _in_time_window(now: datetime, start: str, end: str) -> bool:
    if not start and not end:
        return True
    current = now.time()
    begin = _parse_hhmm(start) if start else time(0, 0)
    finish = _parse_hhmm(end) if end else time(23, 59, 59)
    if begin <= finish:
        return begin <= current <= finish
    # Wrapped window, e.g. 22:00-06:00: outside the daytime gap.
    return current >= begin or current <= finish


def matches(matcher: Matcher, event: dict, now: datetime) -> bool:
    if matcher.networks and (event.get("network") or "").lower() not in {
        n.lower() for n in matcher.networks
    }:
        return False
    if matcher.bridges and (event.get("bridge") or "") not in matcher.bridges:
        return False
    if matcher.directions and (event.get("direction") or "") not in matcher.directions:
        return False
    if matcher.kinds and (event.get("kind") or "") not in matcher.kinds:
        return False

    # A sender can be named by MXID or by the network's own ID, because which
    # one is convenient depends on where the agent got it from.
    sender_values = [event.get("sender") or "", event.get("sender_remote_id") or ""]
    if matcher.senders and not _any_glob(matcher.senders, sender_values):
        return False
    if matcher.exclude_senders and _any_glob(matcher.exclude_senders, sender_values):
        return False

    room_values = [event.get("room_id") or ""]
    if matcher.rooms and not _any_glob(matcher.rooms, room_values):
        return False
    if matcher.exclude_rooms and _any_glob(matcher.exclude_rooms, room_values):
        return False

    if matcher.is_self is not None and bool(event.get("is_self")) is not matcher.is_self:
        return False
    if matcher.has_media is not None and bool(event.get("media")) is not matcher.has_media:
        return False
    if matcher.msgtypes and (event.get("msgtype") or "") not in matcher.msgtypes:
        return False
    if matcher.reaction_keys and (event.get("reaction_key") or "") not in matcher.reaction_keys:
        return False

    body = event.get("body") or ""
    if matcher.contains and matcher.contains.lower() not in body.lower():
        return False
    if matcher.contains_any and not any(
        needle.lower() in body.lower() for needle in matcher.contains_any if needle
    ):
        return False
    if matcher.regex and not _compiled(matcher.regex).search(body):
        return False
    if matcher.not_regex and _compiled(matcher.not_regex).search(body):
        return False
    if matcher.min_length is not None and len(body) < matcher.min_length:
        return False
    if matcher.max_length is not None and len(body) > matcher.max_length:
        return False

    if matcher.weekdays and now.weekday() not in matcher.weekdays:
        return False
    if not _in_time_window(now, matcher.time_from, matcher.time_to):
        return False

    return True
