"""The outbox: get every match to its webhook, eventually.

The bridges only buffer in memory and give up after a few seconds, so this is
where durability actually lives. A match is a row in SQLite before anything is
sent, and n8n being restarted, redeployed or briefly broken costs nothing but
a later delivery.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

from . import config, db
from .logging_setup import log

logger = logging.getLogger("beeper_watch.delivery")

POLL_SECONDS = 1.0
PRUNE_SECONDS = 3600


async def deliver_one(client: httpx.AsyncClient, row) -> None:
    attempts = row["attempts"] + 1
    headers = {"Content-Type": "application/json"}
    headers.update(json.loads(row["headers_json"] or "{}"))
    try:
        response = await client.request(
            row["method"],
            row["url"],
            content=row["payload_json"].encode(),
            headers=headers,
            timeout=config.DELIVERY_TIMEOUT,
        )
    except Exception as err:
        db.fail_delivery(row["id"], attempts, f"{type(err).__name__}: {err}", None)
        log(
            logger,
            logging.WARNING,
            "Delivery failed",
            delivery=row["id"],
            rule=row["rule_name"],
            attempt=attempts,
            error=str(err),
        )
        return
    if response.status_code < 300:
        db.finish_delivery(row["id"], response.status_code)
        log(
            logger,
            logging.INFO,
            "Delivered",
            delivery=row["id"],
            rule=row["rule_name"],
            status=response.status_code,
        )
        return
    # 4xx will not fix itself by being repeated: a wrong path or a workflow
    # that is not listening is an operator problem, so stop and be visible.
    if response.status_code < 500:
        db.fail_delivery(
            row["id"], config.DELIVERY_MAX_ATTEMPTS, response.text[:500], response.status_code
        )
        log(
            logger,
            logging.ERROR,
            "Webhook rejected the payload, not retrying",
            delivery=row["id"],
            rule=row["rule_name"],
            status=response.status_code,
        )
        return
    db.fail_delivery(row["id"], attempts, response.text[:500], response.status_code)
    log(
        logger,
        logging.WARNING,
        "Webhook returned an error, will retry",
        delivery=row["id"],
        rule=row["rule_name"],
        status=response.status_code,
        attempt=attempts,
    )


async def worker() -> None:
    async with httpx.AsyncClient(follow_redirects=False) as client:
        while True:
            try:
                rows = db.due_deliveries()
                for row in rows:
                    await deliver_one(client, row)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # never let the loop die
                log(logger, logging.ERROR, "Delivery loop error", error=str(err))
            await asyncio.sleep(POLL_SECONDS)


async def pruner() -> None:
    while True:
        await asyncio.sleep(PRUNE_SECONDS)
        try:
            removed = db.prune()
            if removed["events"] or removed["deliveries"]:
                log(logger, logging.INFO, "Pruned old rows", **removed)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            log(logger, logging.ERROR, "Prune failed", error=str(err))
