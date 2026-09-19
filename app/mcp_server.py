"""The agent-facing surface: MCP tools over streamable HTTP at /mcp.

This is the point of the whole service being separate from n8n. Rules are
meant to be written and rewritten in conversation — "tell me when my landlord
writes on WhatsApp", "stop that one, it is too noisy" — so the filter store
needs an interface an agent can drive, with a dry run it can use to check its
own work before saving anything.

Tool names follow the convention MCPHub's read-only filter expects in this
stack: list_*, get_* and test_* read, everything else writes.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from . import config, db, service
from .models import Action, Matcher, RuleIn

INSTRUCTIONS = """Rules that turn Beeper messages into n8n webhook calls.

Every event bridged by a self-hosted Beeper bridge is reported here in
plaintext: messages, edits, reactions, deletions, in both directions. A rule
is a filter plus a webhook; when an event matches, the whole event is POSTed.

Writing a good rule: call list_recent_events or list_senders to find the real
sender MXID or room ID, call test_rule to see what a filter would have caught
over the last few days, then create_rule. Prefer a narrow filter plus a
cooldown over a broad one; a rule that fires on everything is a rule the user
will ask you to delete.
"""

# MCP 2.x: FastMCP became MCPServer, and the transport options moved from the
# constructor to streamable_http_app() — see http_app() below.
mcp = MCPServer("beeper-watch", instructions=INSTRUCTIONS)


def http_app():
    """The Starlette app mounted at /mcp.

    stateless_http because MCPHub reconnects freely and there is no per-session
    state worth keeping.

    It carries its own /mcp path and is mounted at the root rather than at
    /mcp, because a Starlette mount whose inner route is "/" answers a POST to
    /mcp with a 307 to /mcp/ — and a redirected POST is exactly the kind of
    thing an MCP client gets wrong. Mounting last means the REST routes above
    still match first.

    DNS-rebinding protection is off by default: it guards a browser on the same
    machine against a local server, and this port is never published outside
    the compose network. Set BEEPER_WATCH_ALLOWED_HOSTS if that ever changes.
    """
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    if config.ALLOWED_HOSTS:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=config.ALLOWED_HOSTS,
            allowed_origins=config.ALLOWED_HOSTS,
        )
    return mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        transport_security=security,
    )


def _rule_or_error(ref: str) -> dict:
    rule = db.get_rule(ref)
    if rule is None:
        raise ValueError(f"no rule named {ref!r}")
    return rule


@mcp.tool()
def list_rules(enabled_only: bool = False) -> list[dict]:
    """List every rule, with its filter, webhook and match count."""
    return db.list_rules(enabled_only=enabled_only)


@mcp.tool()
def get_rule(name_or_id: str) -> dict:
    """Get one rule by name or id."""
    return _rule_or_error(name_or_id)


@mcp.tool()
def create_rule(
    name: str,
    match: Matcher,
    webhook_url: str = "",
    description: str = "",
    enabled: bool = True,
    cooldown_seconds: int = 0,
    priority: int = 100,
    extra: dict[str, Any] | None = None,
) -> dict:
    """Create a rule.

    match holds the filter; every field is optional and they are ANDed, so an
    empty match fires on everything. webhook_url may be left empty to use the
    configured default. cooldown_seconds suppresses repeat firing of this
    rule. extra is copied into the payload so one workflow can serve several
    rules.
    """
    if db.get_rule(name) is not None:
        raise ValueError(f"a rule named {name!r} already exists")
    rule = RuleIn(
        name=name,
        description=description,
        enabled=enabled,
        priority=priority,
        match=match,
        action=Action(webhook_url=webhook_url, extra=extra or {}),
        cooldown_seconds=cooldown_seconds,
    )
    return db.insert_rule(rule.model_dump())


@mcp.tool()
def update_rule(
    name_or_id: str,
    match: Matcher | None = None,
    webhook_url: str | None = None,
    description: str | None = None,
    enabled: bool | None = None,
    cooldown_seconds: int | None = None,
    priority: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict:
    """Change a rule. Only the arguments given are touched; match replaces the
    whole filter rather than merging into it."""
    rule = _rule_or_error(name_or_id)
    changes: dict[str, Any] = {
        "description": description,
        "enabled": enabled,
        "cooldown_seconds": cooldown_seconds,
        "priority": priority,
    }
    if match is not None:
        changes["match"] = match.model_dump()
    if webhook_url is not None or extra is not None:
        action = Action(**rule["action"])
        if webhook_url is not None:
            action.webhook_url = webhook_url
        if extra is not None:
            action.extra = extra
        changes["action"] = action.model_dump()
    return db.update_rule(rule["id"], changes) or rule


@mcp.tool()
def set_rule_enabled(name_or_id: str, enabled: bool) -> dict:
    """Turn a rule on or off without deleting it."""
    rule = _rule_or_error(name_or_id)
    return db.update_rule(rule["id"], {"enabled": enabled}) or rule


@mcp.tool()
def delete_rule(name_or_id: str) -> dict:
    """Delete a rule permanently."""
    rule = _rule_or_error(name_or_id)
    db.delete_rule(rule["id"])
    return {"deleted": rule["name"]}


@mcp.tool()
def test_rule(match: Matcher, limit: int = 200) -> dict:
    """Dry run: how many of the last `limit` stored events this filter would
    have caught, with up to 20 samples. Nothing is sent and nothing is saved.
    Use this before create_rule."""
    return service.dry_run(match, limit=limit)


@mcp.tool()
def list_recent_events(
    limit: int = 30,
    network: str = "",
    kind: str = "",
    direction: str = "",
    room_id: str = "",
    sender: str = "",
    contains: str = "",
) -> list[dict]:
    """Recent bridged events, newest first. This is the raw material for
    writing a filter: it shows the exact sender MXIDs, room IDs and bodies
    that a rule will be matched against."""
    return db.recent_events(
        limit=limit,
        network=network,
        kind=kind,
        direction=direction,
        room_id=room_id,
        sender=sender,
        contains=contains,
    )


@mcp.tool()
def list_chats(days: int = 7) -> list[dict]:
    """Rooms seen recently, with event counts — for picking a room filter.
    Beeper's own MCP tools (search_chats, get_chat) turn a room ID into a
    human name; this service deliberately does not duplicate that."""
    return db.seen_rooms(days=days)


@mcp.tool()
def list_senders(days: int = 7, network: str = "") -> list[dict]:
    """Senders seen recently, with event counts — for picking a sender filter."""
    return db.seen_senders(days=days, network=network)


@mcp.tool()
def list_deliveries(limit: int = 20, status: str = "") -> list[dict]:
    """Recent webhook deliveries. status is pending, sent or failed."""
    return db.list_deliveries(limit=limit, status=status)


@mcp.tool()
def retry_delivery(delivery_id: int) -> dict:
    """Queue a failed delivery to be attempted again."""
    return {"retried": db.retry_delivery(delivery_id)}


@mcp.tool()
def get_stats() -> dict:
    """Counts: rules, stored events, deliveries by state, and the defaults in
    force (timezone, retention, default webhook)."""
    out = db.stats()
    out["timezone"] = str(config.TIMEZONE)
    out["event_retention_days"] = config.EVENT_RETENTION_DAYS
    out["default_webhook_set"] = bool(config.DEFAULT_WEBHOOK)
    out["store_bodies"] = config.STORE_BODIES
    return out
