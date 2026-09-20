"""beeper-watch: turn bridged Beeper events into n8n webhook calls.

Beeper exposes no events at all, so the bridges themselves are patched to
report everything they handle here (see the beeper-watch patch in
beeper-bridge-manager). This service is the other half: it keeps the
rules, decides what matters, and delivers.

Three surfaces:

    POST /v1/events   the bridges, with the ingest token
    /mcp              agents through MCPHub, with the admin token
    /v1/rules …       n8n and humans, with the admin token

Health is open, because a healthcheck that needs a secret is a healthcheck
that silently stops working when the secret changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from . import config, db, delivery, logging_setup, service
from .logging_setup import log
from .mcp_server import http_app, mcp
from .models import Matcher, RuleIn, RulePatch, WatchEvent

logging_setup.setup()
logger = logging.getLogger("beeper_watch")


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    db.connect()
    db.prune()
    if not config.ADMIN_TOKEN:
        log(logger, logging.WARNING, "BEEPER_WATCH_TOKEN is empty: the admin API and MCP are open")
    if not config.INGEST_TOKEN:
        log(logger, logging.WARNING, "BEEPER_WATCH_INGEST_TOKEN is empty: ingest is open")
    tasks = [asyncio.create_task(delivery.worker()), asyncio.create_task(delivery.pruner())]
    log(logger, logging.INFO, "beeper-watch started", **db.stats())
    # The MCP session manager owns its own background state and has to be
    # running for the mounted app to answer anything.
    async with mcp.session_manager.run():
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(
    title="beeper-watch",
    version="1.0",
    description="Filter Beeper bridge events and notify n8n.",
    lifespan=lifespan,
)


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def require_admin(request: Request) -> None:
    if config.ADMIN_TOKEN and _bearer(request) != config.ADMIN_TOKEN:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "admin token required")


def require_ingest(request: Request) -> None:
    # The admin token is accepted here as well, so a human can replay an event
    # by hand without the bridges' token.
    token = _bearer(request)
    if not config.INGEST_TOKEN:
        return
    if token not in (config.INGEST_TOKEN, config.ADMIN_TOKEN):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "ingest token required")


@app.middleware("http")
async def guard_mcp(request: Request, call_next):
    """The MCP app is mounted, so it is outside the dependency system."""
    if request.url.path.startswith("/mcp") and config.ADMIN_TOKEN:
        if _bearer(request) != config.ADMIN_TOKEN:
            return JSONResponse({"detail": "admin token required"}, status_code=401)
    return await call_next(request)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", **db.stats()}


# --- ingest ----------------------------------------------------------------

@app.post("/v1/events", dependencies=[Depends(require_ingest)])
def ingest(event: WatchEvent) -> dict:
    """Called by the patched bridges, once per bridged event."""
    if event.schema_version > config.SUPPORTED_EVENT_SCHEMA:
        # Fail loudly rather than mis-reading a payload whose fields changed
        # meaning: the bridge logs the rejection and drops the event.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"event schema {event.schema_version} is newer than {config.SUPPORTED_EVENT_SCHEMA}",
        )
    payload = event.model_dump(by_alias=True)
    return service.ingest(payload)


# --- rules -----------------------------------------------------------------

@app.get("/v1/rules", dependencies=[Depends(require_admin)])
def get_rules(enabled_only: bool = False) -> list[dict]:
    return db.list_rules(enabled_only=enabled_only)


@app.post("/v1/rules", dependencies=[Depends(require_admin)], status_code=201)
def post_rule(rule: RuleIn) -> dict:
    if db.get_rule(rule.name) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"rule {rule.name!r} already exists")
    return db.insert_rule(rule.model_dump())


@app.get("/v1/rules/{ref}", dependencies=[Depends(require_admin)])
def get_one_rule(ref: str) -> dict:
    rule = db.get_rule(ref)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such rule")
    return rule


@app.patch("/v1/rules/{ref}", dependencies=[Depends(require_admin)])
def patch_rule(ref: str, changes: RulePatch) -> dict:
    rule = db.get_rule(ref)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such rule")
    payload: dict[str, Any] = changes.model_dump(exclude_unset=True)
    return db.update_rule(rule["id"], payload) or rule


@app.delete("/v1/rules/{ref}", dependencies=[Depends(require_admin)])
def remove_rule(ref: str) -> dict:
    rule = db.get_rule(ref)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such rule")
    db.delete_rule(rule["id"])
    return {"deleted": rule["name"]}


@app.post("/v1/rules/test", dependencies=[Depends(require_admin)])
def test_matcher(matcher: Matcher, limit: int = 200) -> dict:
    """Dry run a filter against stored events without saving or sending."""
    return service.dry_run(matcher, limit=limit)


# --- inspection ------------------------------------------------------------

@app.get("/v1/events", dependencies=[Depends(require_admin)])
def get_events(
    limit: int = 30,
    network: str = "",
    kind: str = "",
    direction: str = "",
    room_id: str = "",
    sender: str = "",
    contains: str = "",
) -> list[dict]:
    return db.recent_events(
        limit=limit,
        network=network,
        kind=kind,
        direction=direction,
        room_id=room_id,
        sender=sender,
        contains=contains,
    )


@app.get("/v1/chats", dependencies=[Depends(require_admin)])
def get_chats(days: int = 7) -> list[dict]:
    return db.seen_rooms(days=days)


@app.get("/v1/senders", dependencies=[Depends(require_admin)])
def get_senders(days: int = 7, network: str = "") -> list[dict]:
    return db.seen_senders(days=days, network=network)


@app.get("/v1/deliveries", dependencies=[Depends(require_admin)])
def get_deliveries(limit: int = 20, status_filter: str = "") -> list[dict]:
    return db.list_deliveries(limit=limit, status=status_filter)


@app.post("/v1/deliveries/{delivery_id}/retry", dependencies=[Depends(require_admin)])
def post_retry(delivery_id: int) -> dict:
    return {"retried": db.retry_delivery(delivery_id)}


@app.get("/v1/stats", dependencies=[Depends(require_admin)])
def get_stats() -> dict:
    return db.stats()


# Mounted last, at the root, so /mcp reaches it without a redirect while every
# route declared above still wins. It has to be built at import time, because
# session_manager only exists once streamable_http_app() has been called.
app.mount("/", http_app())
