"""The shapes on the wire: what a bridge sends, and what a rule is.

A rule is stored as two JSON blobs (`match` and `action`) rather than as
columns. Adding a filter is then a change in one place — this file — instead
of a schema migration, and the MCP tools an agent uses get the new field
automatically because their arguments are these models.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Direction = Literal["in", "out"]
Kind = Literal["message", "edit", "reaction", "unreaction", "deletion", "other"]


class WatchEvent(BaseModel):
    """One bridged event, as the bridge hook POSTs it.

    Unknown fields are kept: a bridge built from a newer patch must not start
    failing against an older watcher, and the extras end up in the stored raw
    payload and in what n8n receives.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: int = Field(1, alias="schema")

    bridge: str = ""
    network: str = ""
    direction: Direction = "in"
    kind: Kind = "other"

    room_id: str = ""
    event_id: str = ""
    sender: str = ""
    sender_remote_id: str = ""
    is_self: bool = False

    timestamp: int = 0
    type: str = ""

    msgtype: str = ""
    body: str = ""
    formatted_body: str = ""
    body_truncated: bool = False

    reaction_key: str = ""
    redacts: str = ""
    reply_to: str = ""
    thread_root: str = ""
    edits: str = ""

    media: dict[str, Any] | None = None
    content: Any = None
    error: str = ""


class Matcher(BaseModel):
    """Everything a rule can filter on. All fields are optional and ANDed;
    a list field is satisfied by any one of its entries.

    Text filters look at the message body only. Reactions and deletions carry
    no text, so a rule with a text filter never matches them — which is the
    intended reading of "message contains X", not a bug.
    """

    model_config = ConfigDict(extra="forbid")

    networks: list[str] = Field(default_factory=list, description="telegram, signal, …")
    bridges: list[str] = Field(default_factory=list, description="sh-telegram, …")
    directions: list[Direction] = Field(default_factory=list)
    kinds: list[Kind] = Field(default_factory=list)

    # MXID, or the network's own user ID, or a glob of either.
    senders: list[str] = Field(default_factory=list)
    exclude_senders: list[str] = Field(default_factory=list)
    rooms: list[str] = Field(default_factory=list, description="Matrix room IDs, globs allowed")
    exclude_rooms: list[str] = Field(default_factory=list)

    is_self: bool | None = Field(None, description="True = only my own, False = only other people's")
    has_media: bool | None = None
    msgtypes: list[str] = Field(default_factory=list, description="m.text, m.image, …")
    reaction_keys: list[str] = Field(default_factory=list, description="only for reaction kinds")

    contains: str = Field("", description="case-insensitive substring of the body")
    contains_any: list[str] = Field(default_factory=list)
    regex: str = Field("", description="Python regex, searched against the body")
    not_regex: str = Field("", description="rejects the event when it matches")
    min_length: int | None = None
    max_length: int | None = None

    # Local time, from the container's TZ. Wrapping is allowed and means what
    # it looks like: 22:00-06:00 is the night.
    time_from: str = Field("", pattern=r"^([01]\d|2[0-3]):[0-5]\d$|^$")
    time_to: str = Field("", pattern=r"^([01]\d|2[0-3]):[0-5]\d$|^$")
    weekdays: list[int] = Field(default_factory=list, description="0 = Monday … 6 = Sunday")

    @field_validator("regex", "not_regex")
    @classmethod
    def _valid_regex(cls, value: str) -> str:
        if value:
            try:
                re.compile(value)
            except re.error as err:
                raise ValueError(f"invalid regex: {err}") from err
        return value

    @field_validator("weekdays")
    @classmethod
    def _valid_weekdays(cls, value: list[int]) -> list[int]:
        for day in value:
            if day < 0 or day > 6:
                raise ValueError("weekdays are 0 (Monday) to 6 (Sunday)")
        return value


class Action(BaseModel):
    """What happens on a match. One webhook, because n8n is the place where
    branching, formatting and anything else belongs — this service decides
    *whether*, not *what*."""

    model_config = ConfigDict(extra="forbid")

    webhook_url: str = Field("", description="empty = BEEPER_WATCH_DEFAULT_WEBHOOK")
    method: Literal["POST", "PUT"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    # Copied verbatim into the payload, so one workflow can serve several
    # rules and still know which is which.
    extra: dict[str, Any] = Field(default_factory=dict)


class RuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    enabled: bool = True
    priority: int = Field(100, description="lower runs first; only affects payload order")
    match: Matcher = Field(default_factory=Matcher)
    action: Action = Field(default_factory=Action)
    cooldown_seconds: int = Field(0, ge=0, description="ignore further matches of this rule for N seconds")


class RulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = None
    enabled: bool | None = None
    priority: int | None = None
    match: Matcher | None = None
    action: Action | None = None
    cooldown_seconds: int | None = Field(None, ge=0)


class Rule(RuleIn):
    id: int
    created_at: str
    updated_at: str
    last_matched_at: str | None = None
    match_count: int = 0
