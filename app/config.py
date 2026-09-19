"""Runtime configuration, all from the environment.

Nothing here is required: with an empty environment the service starts, stores
events and matches rules, and only the webhook a rule names decides where a
match goes. That matters because this is the thing that has to be up whenever
the bridges are up — a missing variable must not be a reason to fail to start.
"""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo


def _int(key: str, default: int) -> int:
    try:
        value = int(os.environ.get(key, "").strip() or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


# Where state lives. One SQLite file on a volume: rules, the recent-event
# buffer and the delivery outbox. Postgres would add a dependency to the one
# service that must survive the rest of the stack restarting under it.
DB_PATH = os.environ.get("BEEPER_WATCH_DB", "/data/beeper-watch.db")

# Bearer tokens. Ingest is what the bridges present; admin is what MCPHub and
# n8n present. They are separate so that a bridge token, which is readable in
# ten bridge processes, cannot also rewrite the rules.
INGEST_TOKEN = os.environ.get("BEEPER_WATCH_INGEST_TOKEN", "").strip()
ADMIN_TOKEN = os.environ.get("BEEPER_WATCH_TOKEN", "").strip()

# Used when a rule does not name its own webhook.
DEFAULT_WEBHOOK = os.environ.get("BEEPER_WATCH_DEFAULT_WEBHOOK", "").strip()

# Local time for the time-of-day and weekday filters. A rule that says
# "not at night" means the user's night, not UTC.
TIMEZONE = ZoneInfo(os.environ.get("TZ", "UTC"))

# Recent events are kept so rules can be tested against real traffic before
# being enabled. They are message bodies on disk, so the retention is short
# and STORE_BODIES turns the text off entirely if that trade is unwanted.
EVENT_RETENTION_DAYS = _int("BEEPER_WATCH_EVENT_RETENTION_DAYS", 7)
EVENT_RETENTION_MAX = _int("BEEPER_WATCH_EVENT_RETENTION_MAX", 20000)
STORE_BODIES = _bool("BEEPER_WATCH_STORE_BODIES", True)

# Delivery retries. The bridges only buffer in memory, so durability starts
# here: a match becomes a row in the outbox before anything is sent.
DELIVERY_RETENTION_DAYS = _int("BEEPER_WATCH_DELIVERY_RETENTION_DAYS", 14)
DELIVERY_MAX_ATTEMPTS = _int("BEEPER_WATCH_DELIVERY_MAX_ATTEMPTS", 8)
DELIVERY_TIMEOUT = _int("BEEPER_WATCH_DELIVERY_TIMEOUT", 15)
# Backoff schedule in seconds, index = attempt number - 1; the last value
# repeats. Roughly: retry quickly twice, then settle into every 15 minutes.
DELIVERY_BACKOFF = (5, 15, 60, 300, 900)

# Host header allowlist for the MCP endpoint. Empty turns DNS-rebinding
# protection off entirely, which is right while the port is unpublished.
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("BEEPER_WATCH_ALLOWED_HOSTS", "").split(",") if h.strip()]

LOG_LEVEL = os.environ.get("BEEPER_WATCH_LOG_LEVEL", "INFO").upper()

# The payload version this service accepts from the bridge hook.
SUPPORTED_EVENT_SCHEMA = 1
