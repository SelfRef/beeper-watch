# beeper-watch

React to Beeper messages as they arrive.

Beeper has no event API. There are no webhooks, no gateway, and the Beeper
Desktop API — the one the MCP server exposes — is request/response only:
`/v0/events`, `/v0/stream`, `/v0/subscribe` and `/v0/ws` all return 404. So
there is no supported way to be told that a message happened; you can only ask
whether one did.

Everything a self-hosted bridge does *is* a Matrix event on your own
homeserver, but portals are end-to-end encrypted, so an ordinary Matrix client
watching `/sync` sees that something happened, who sent it and where — and not
a word of what it said.

This service takes the other route. The bridges are patched to report every
event they handle, in plaintext, at the one moment it is readable: just before
they encrypt it. beeper-watch keeps the rules, decides which events matter, and
POSTs the ones that do to a webhook — n8n, in the stack this was built for.

```
 network ──▶ mautrix bridge ──▶ [patch] ──▶ beeper-watch ──▶ n8n webhook
                   │                            │
                   └──▶ encrypted Matrix event  └── rules, recent events, outbox
```

## What it can trigger on

Messages, edits, reactions, unreactions and deletions, in both directions —
what arrives from the network and what you send from any Beeper client.

A rule is a filter plus a webhook. Every filter field is optional and they are
ANDed; a list is satisfied by any one entry.

| Field | Matches on |
| --- | --- |
| `networks`, `bridges` | `telegram`, `signal`, … / `sh-telegram`, … |
| `directions` | `in` (from the network), `out` (from you) |
| `kinds` | `message`, `edit`, `reaction`, `unreaction`, `deletion`, `other` |
| `senders`, `exclude_senders` | ghost MXID, the network's own user ID, or a glob of either |
| `rooms`, `exclude_rooms` | Matrix room ID, globs allowed |
| `is_self` | `true` = only mine, `false` = only other people's |
| `has_media`, `msgtypes` | attachments; `m.text`, `m.image`, … |
| `reaction_keys` | the emoji, for reaction kinds |
| `contains`, `contains_any`, `regex`, `not_regex` | the message body |
| `min_length`, `max_length` | body length |
| `time_from`, `time_to`, `weekdays` | local time; a wrapping window like `22:00`–`06:00` means the night |

`cooldown_seconds` on the rule suppresses repeat firing.

Text filters look at the body only, so a rule with `contains` never matches a
reaction or a deletion — those carry no text. That is the intended reading of
"message contains X", not a limitation to work around.

## Three surfaces

| Path | Who | Token |
| --- | --- | --- |
| `POST /v1/events` | the patched bridges | `BEEPER_WATCH_INGEST_TOKEN` |
| `/mcp` | agents, through MCPHub | `BEEPER_WATCH_TOKEN` |
| `/v1/rules`, `/v1/events`, `/v1/deliveries`, … | n8n and humans | `BEEPER_WATCH_TOKEN` |
| `GET /health` | the container healthcheck | none |

The split matters: the ingest token sits in ten bridge processes, so it must
not also be able to rewrite the rules.

Health is deliberately unauthenticated. A healthcheck that needs a secret is a
healthcheck that silently stops working when the secret changes.

### MCP tools

Rules are meant to be written in conversation — "tell me when my landlord
writes on WhatsApp", "that one is too noisy, turn it off" — so the whole store
is an MCP server:

`list_rules`, `get_rule`, `create_rule`, `update_rule`, `set_rule_enabled`,
`delete_rule`, `test_rule`, `list_recent_events`, `list_chats`,
`list_senders`, `list_deliveries`, `retry_delivery`, `get_stats`.

`test_rule` is the important one. It runs a filter against the last few hundred
real events and reports what it *would* have caught, without saving or sending
anything, so an agent can check its own work before committing to a rule. The
names follow the usual read-only convention a gateway can filter on: `list_*`,
`get_*` and `test_*` read, everything else writes.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `BEEPER_WATCH_TOKEN` | — | bearer token for the admin API and MCP. Empty = open, and it says so at startup |
| `BEEPER_WATCH_INGEST_TOKEN` | — | bearer token the bridges present |
| `BEEPER_WATCH_DEFAULT_WEBHOOK` | — | used by rules that name no URL of their own |
| `BEEPER_WATCH_DB` | `/data/beeper-watch.db` | SQLite file |
| `TZ` | `UTC` | the clock the time-of-day filters use |
| `BEEPER_WATCH_EVENT_RETENTION_DAYS` | `7` | how long recent events are kept |
| `BEEPER_WATCH_EVENT_RETENTION_MAX` | `20000` | hard cap on stored events |
| `BEEPER_WATCH_STORE_BODIES` | `true` | `false` keeps the metadata and drops the text |
| `BEEPER_WATCH_DELIVERY_RETENTION_DAYS` | `14` | how long the delivery log is kept |
| `BEEPER_WATCH_DELIVERY_MAX_ATTEMPTS` | `8` | before a delivery is marked failed |
| `BEEPER_WATCH_DELIVERY_TIMEOUT` | `15` | seconds per webhook call |
| `BEEPER_WATCH_ALLOWED_HOSTS` | — | Host allowlist for `/mcp`; empty turns DNS-rebinding protection off, which is right while the port is unpublished |
| `BEEPER_WATCH_LOG_LEVEL` | `INFO` | |

Stored events are message bodies on disk. That is what makes `test_rule`
possible, and it is why the retention is one week rather than forever;
`BEEPER_WATCH_STORE_BODIES=false` trades the dry run away for keeping no text
at all.

## The bridge side

The reporting half is a patch, not part of this repo: see `patches/` in
[beeper-bridge-manager](https://github.com/SelfRef/beeper-bridge-manager).
Set `WATCH_URL` (and `WATCH_TOKEN`) on that container and every bridge with a
patched binary starts reporting.

What it sends, once per event:

```json
{
  "schema": 1,
  "bridge": "sh-telegram", "network": "telegram",
  "direction": "in", "kind": "message",
  "room_id": "!abc:beeper.local", "event_id": "$xyz",
  "sender": "@sh-telegram_1127943894:beeper.local",
  "sender_remote_id": "1127943894", "is_self": false,
  "timestamp": 1758300000000,
  "type": "m.room.message", "msgtype": "m.text",
  "body": "Can you send me the report today?"
}
```

and what a matching rule POSTs to the webhook is that object, whole, wrapped
with the rule that caught it:

```json
{
  "schema": 1,
  "matched_at": "2026-09-19T19:30:51+00:00",
  "rule": { "id": 1, "name": "boss-asks-for-report", "description": "", "extra": {} },
  "event": { "…": "as above" }
}
```

One complete payload, so the workflow starts at "do the thing" rather than at
"work out what happened".

### Why the durability lives here

The bridge holds a bounded in-memory queue and gives up after a few seconds —
it must never block message delivery for this. So a match becomes a row in
SQLite before anything is sent, and the outbox retries with backoff. n8n being
restarted, redeployed or briefly broken costs a later delivery, not a lost
trigger.

Duplicates are dropped on the way in, keyed by Matrix event ID (or, for Discord
reactions, which have none, by room + sender + kind + emoji + timestamp), so a
bridge retrying a POST it never got an answer to cannot fire a rule twice.

## Running it

```sh
docker run -d --name beeper-watch \
  -v beeper-watch:/data \
  -e BEEPER_WATCH_TOKEN=... -e BEEPER_WATCH_INGEST_TOKEN=... \
  -e TZ=Europe/Warsaw \
  ghcr.io/selfref/beeper-watch:latest
```

No port is published on purpose: the bridges, MCPHub and n8n all reach it over
the compose network, and nothing about it belongs on the public internet.

## Development

```sh
pip install -r requirements.txt pytest
python -m pytest tests/
uvicorn app.main:app --reload
```

The tests cover the rule matcher and the ingest path — the places where a
mistake is silent. A wrong filter does not crash; it just never fires, or fires
on everything. CI runs them inside the built image and does not push if they
fail.

## Licence

AGPL-3.0, matching the mautrix bridges this depends on.
