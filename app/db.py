"""SQLite: rules, the recent-event buffer and the delivery outbox.

One file on a volume. The alternative was the stack's shared Postgres, but
this service exists to be up whenever the bridges are up, and depending on
another container for that is a worse trade than giving up SQL features
nothing here needs.

Everything is synchronous and guarded by one lock. At the measured volume —
about 150 bridged events a day — contention is not a real thing, and a
blocking call of well under a millisecond inside an async handler is cheaper
than the complexity of an async driver.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL UNIQUE,
    description      TEXT    NOT NULL DEFAULT '',
    enabled          INTEGER NOT NULL DEFAULT 1,
    priority         INTEGER NOT NULL DEFAULT 100,
    match_json       TEXT    NOT NULL,
    action_json      TEXT    NOT NULL,
    cooldown_seconds INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    last_matched_at  TEXT,
    match_count      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key        TEXT    NOT NULL UNIQUE,
    received_at      TEXT    NOT NULL,
    ts               INTEGER NOT NULL,
    bridge           TEXT    NOT NULL DEFAULT '',
    network          TEXT    NOT NULL DEFAULT '',
    direction        TEXT    NOT NULL DEFAULT '',
    kind             TEXT    NOT NULL DEFAULT '',
    room_id          TEXT    NOT NULL DEFAULT '',
    event_id         TEXT    NOT NULL DEFAULT '',
    sender           TEXT    NOT NULL DEFAULT '',
    sender_remote_id TEXT    NOT NULL DEFAULT '',
    is_self          INTEGER NOT NULL DEFAULT 0,
    type             TEXT    NOT NULL DEFAULT '',
    msgtype          TEXT    NOT NULL DEFAULT '',
    body             TEXT    NOT NULL DEFAULT '',
    reaction_key     TEXT    NOT NULL DEFAULT '',
    has_media        INTEGER NOT NULL DEFAULT 0,
    raw_json         TEXT    NOT NULL,
    matched_rules    TEXT    NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS events_received ON events (received_at);
CREATE INDEX IF NOT EXISTS events_room     ON events (room_id);
CREATE INDEX IF NOT EXISTS events_sender   ON events (sender);

CREATE TABLE IF NOT EXISTS deliveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id         INTEGER,
    rule_name       TEXT    NOT NULL DEFAULT '',
    event_row_id    INTEGER,
    event_id        TEXT    NOT NULL DEFAULT '',
    url             TEXT    NOT NULL,
    method          TEXT    NOT NULL DEFAULT 'POST',
    headers_json    TEXT    NOT NULL DEFAULT '{}',
    payload_json    TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT    NOT NULL DEFAULT '',
    response_code   INTEGER,
    created_at      TEXT    NOT NULL,
    next_attempt_at TEXT    NOT NULL,
    sent_at         TEXT
);
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries (status, next_attempt_at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            # WAL so the delivery worker's writes never block an ingest, and a
            # busy timeout so a slow write waits instead of raising.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA)
            conn.commit()
            _conn = conn
        return _conn


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with _lock:
        return connect().execute(sql, tuple(params)).fetchall()


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    with _lock:
        conn = connect()
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return cur


# --- rules -----------------------------------------------------------------

def rule_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "enabled": bool(row["enabled"]),
        "priority": row["priority"],
        "match": json.loads(row["match_json"]),
        "action": json.loads(row["action_json"]),
        "cooldown_seconds": row["cooldown_seconds"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_matched_at": row["last_matched_at"],
        "match_count": row["match_count"],
    }


def list_rules(enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM rules"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY priority, id"
    return [rule_row_to_dict(row) for row in query(sql)]


def get_rule(ref: str | int) -> dict | None:
    """Look a rule up by id or by name — agents use names, REST uses ids."""
    rows = query("SELECT * FROM rules WHERE name = ?", (str(ref),))
    if not rows and str(ref).isdigit():
        rows = query("SELECT * FROM rules WHERE id = ?", (int(ref),))
    return rule_row_to_dict(rows[0]) if rows else None


def insert_rule(rule: dict) -> dict:
    stamp = now_iso()
    cur = execute(
        """INSERT INTO rules (name, description, enabled, priority, match_json,
                              action_json, cooldown_seconds, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            rule["name"],
            rule.get("description", ""),
            int(rule.get("enabled", True)),
            rule.get("priority", 100),
            json.dumps(rule.get("match", {})),
            json.dumps(rule.get("action", {})),
            rule.get("cooldown_seconds", 0),
            stamp,
            stamp,
        ),
    )
    return get_rule(int(cur.lastrowid))  # type: ignore[arg-type]


def update_rule(rule_id: int, changes: dict) -> dict | None:
    columns, values = [], []
    for key, value in changes.items():
        if value is None:
            continue
        if key in ("match", "action"):
            columns.append(f"{key}_json = ?")
            values.append(json.dumps(value))
        elif key == "enabled":
            columns.append("enabled = ?")
            values.append(int(value))
        elif key in ("name", "description", "priority", "cooldown_seconds"):
            columns.append(f"{key} = ?")
            values.append(value)
    if columns:
        columns.append("updated_at = ?")
        values.append(now_iso())
        values.append(rule_id)
        execute(f"UPDATE rules SET {', '.join(columns)} WHERE id = ?", values)
    return get_rule(rule_id)


def delete_rule(rule_id: int) -> None:
    execute("DELETE FROM rules WHERE id = ?", (rule_id,))


def mark_rule_matched(rule_id: int) -> None:
    execute(
        "UPDATE rules SET last_matched_at = ?, match_count = match_count + 1 WHERE id = ?",
        (now_iso(), rule_id),
    )


def rule_in_cooldown(rule: dict) -> bool:
    seconds = rule.get("cooldown_seconds") or 0
    last = rule.get("last_matched_at")
    if not seconds or not last:
        return False
    return datetime.fromisoformat(last) + timedelta(seconds=seconds) > datetime.now(timezone.utc)


# --- events ----------------------------------------------------------------

def dedup_key(event: dict) -> str:
    """A stable identity for an event.

    The Matrix event ID when there is one. Discord reactions have none — the
    bridgev1 send path does not return it — so those fall back to the tuple
    that cannot repeat for a distinct event.
    """
    if event.get("event_id"):
        return f"{event['event_id']}|{event.get('direction', '')}|{event.get('kind', '')}"
    return "|".join(
        str(event.get(field, ""))
        for field in ("room_id", "sender", "kind", "reaction_key", "redacts", "timestamp")
    )


def insert_event(event: dict, store_body: bool) -> tuple[int | None, bool]:
    """Store one event. Returns (row id, is_new); a repeat is not stored twice."""
    key = dedup_key(event)
    existing = query("SELECT id FROM events WHERE dedup_key = ?", (key,))
    if existing:
        return existing[0]["id"], False
    cur = execute(
        """INSERT INTO events (dedup_key, received_at, ts, bridge, network, direction,
                               kind, room_id, event_id, sender, sender_remote_id, is_self,
                               type, msgtype, body, reaction_key, has_media, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            key,
            now_iso(),
            event.get("timestamp") or 0,
            event.get("bridge", ""),
            event.get("network", ""),
            event.get("direction", ""),
            event.get("kind", ""),
            event.get("room_id", ""),
            event.get("event_id", ""),
            event.get("sender", ""),
            event.get("sender_remote_id", ""),
            int(bool(event.get("is_self"))),
            event.get("type", ""),
            event.get("msgtype", ""),
            event.get("body", "") if store_body else "",
            event.get("reaction_key", ""),
            int(bool(event.get("media"))),
            json.dumps(event, ensure_ascii=False),
        ),
    )
    return int(cur.lastrowid), True  # type: ignore[arg-type]


def set_event_matches(row_id: int, rule_names: list[str]) -> None:
    execute("UPDATE events SET matched_rules = ? WHERE id = ?", (json.dumps(rule_names), row_id))


def recent_events(limit: int = 50, **filters) -> list[dict]:
    where, params = [], []
    for column in ("network", "kind", "direction", "room_id", "sender", "bridge"):
        value = filters.get(column)
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    if filters.get("contains"):
        where.append("body LIKE ?")
        params.append(f"%{filters['contains']}%")
    sql = "SELECT * FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    return [event_row_to_dict(row) for row in query(sql, params)]


def event_row_to_dict(row: sqlite3.Row) -> dict:
    event = json.loads(row["raw_json"])
    event["_id"] = row["id"]
    event["_received_at"] = row["received_at"]
    event["_matched_rules"] = json.loads(row["matched_rules"])
    return event


def seen_rooms(days: int = 7) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    return [
        dict(row)
        for row in query(
            """SELECT room_id, network, COUNT(*) AS events, MAX(received_at) AS last_seen
               FROM events WHERE received_at >= ? AND room_id != ''
               GROUP BY room_id, network ORDER BY events DESC LIMIT 200""",
            (since,),
        )
    ]


def seen_senders(days: int = 7, network: str = "") -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    sql = """SELECT sender, sender_remote_id, network, COUNT(*) AS events,
                    MAX(received_at) AS last_seen
             FROM events WHERE received_at >= ? AND sender != ''"""
    params: list[Any] = [since]
    if network:
        sql += " AND network = ?"
        params.append(network)
    sql += " GROUP BY sender ORDER BY events DESC LIMIT 200"
    return [dict(row) for row in query(sql, params)]


def prune() -> dict:
    """Drop what is past its retention. Event bodies are message content, so
    keeping them forever would quietly turn this into a second archive."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=config.EVENT_RETENTION_DAYS)).isoformat(
        timespec="seconds"
    )
    events = execute("DELETE FROM events WHERE received_at < ?", (cutoff,)).rowcount
    over = execute(
        """DELETE FROM events WHERE id NOT IN
           (SELECT id FROM events ORDER BY id DESC LIMIT ?)""",
        (config.EVENT_RETENTION_MAX,),
    ).rowcount
    dcutoff = (
        datetime.now(timezone.utc) - timedelta(days=config.DELIVERY_RETENTION_DAYS)
    ).isoformat(timespec="seconds")
    deliveries = execute(
        "DELETE FROM deliveries WHERE status != 'pending' AND created_at < ?", (dcutoff,)
    ).rowcount
    return {"events": events + over, "deliveries": deliveries}


# --- deliveries ------------------------------------------------------------

def enqueue_delivery(
    rule: dict, event_row_id: int | None, event_id: str, url: str, payload: dict
) -> int:
    action = rule.get("action") or {}
    cur = execute(
        """INSERT INTO deliveries (rule_id, rule_name, event_row_id, event_id, url, method,
                                   headers_json, payload_json, created_at, next_attempt_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            rule.get("id"),
            rule.get("name", ""),
            event_row_id,
            event_id,
            url,
            action.get("method", "POST"),
            json.dumps(action.get("headers", {})),
            json.dumps(payload, ensure_ascii=False),
            now_iso(),
            now_iso(),
        ),
    )
    return int(cur.lastrowid)  # type: ignore[arg-type]


def due_deliveries(limit: int = 20) -> list[sqlite3.Row]:
    return query(
        "SELECT * FROM deliveries WHERE status = 'pending' AND next_attempt_at <= ? "
        "ORDER BY id LIMIT ?",
        (now_iso(), limit),
    )


def finish_delivery(delivery_id: int, code: int) -> None:
    execute(
        "UPDATE deliveries SET status='sent', attempts=attempts+1, response_code=?, "
        "sent_at=?, last_error='' WHERE id=?",
        (code, now_iso(), delivery_id),
    )


def fail_delivery(delivery_id: int, attempts: int, error: str, code: int | None) -> None:
    if attempts >= config.DELIVERY_MAX_ATTEMPTS:
        execute(
            "UPDATE deliveries SET status='failed', attempts=?, last_error=?, response_code=? "
            "WHERE id=?",
            (attempts, error[:500], code, delivery_id),
        )
        return
    backoff = config.DELIVERY_BACKOFF[min(attempts - 1, len(config.DELIVERY_BACKOFF) - 1)]
    nxt = (datetime.now(timezone.utc) + timedelta(seconds=backoff)).isoformat(timespec="seconds")
    execute(
        "UPDATE deliveries SET attempts=?, last_error=?, response_code=?, next_attempt_at=? "
        "WHERE id=?",
        (attempts, error[:500], code, nxt, delivery_id),
    )


def list_deliveries(limit: int = 20, status: str = "") -> list[dict]:
    sql = "SELECT * FROM deliveries"
    params: list[Any] = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    out = []
    for row in query(sql, params):
        item = dict(row)
        item.pop("payload_json", None)
        item["headers"] = json.loads(item.pop("headers_json", "{}"))
        out.append(item)
    return out


def retry_delivery(delivery_id: int) -> bool:
    cur = execute(
        "UPDATE deliveries SET status='pending', next_attempt_at=?, attempts=0 "
        "WHERE id=? AND status='failed'",
        (now_iso(), delivery_id),
    )
    return cur.rowcount > 0


def stats() -> dict:
    def count(sql: str, params: Iterable[Any] = ()) -> int:
        return int(query(sql, params)[0][0])

    return {
        "rules": count("SELECT COUNT(*) FROM rules"),
        "rules_enabled": count("SELECT COUNT(*) FROM rules WHERE enabled = 1"),
        "events_stored": count("SELECT COUNT(*) FROM events"),
        "events_last_24h": count(
            "SELECT COUNT(*) FROM events WHERE received_at >= ?",
            ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds"),),
        ),
        "deliveries_pending": count("SELECT COUNT(*) FROM deliveries WHERE status='pending'"),
        "deliveries_failed": count("SELECT COUNT(*) FROM deliveries WHERE status='failed'"),
        "deliveries_sent": count("SELECT COUNT(*) FROM deliveries WHERE status='sent'"),
    }
