"""Bounded, best-effort usage metrics; never store contact details."""

import asyncio
import hashlib
import hmac
import logging
from datetime import datetime, timezone
from uuid import uuid4

from .config import settings
from .message_store import _db

logger = logging.getLogger(__name__)
_collection = _db["usage_events"]
_ready = False
_pending: set[asyncio.Task] = set()
_max_pending = 100


async def initialize() -> None:
    """Enable writes only when the retention and uniqueness indexes exist."""
    global _ready
    _ready = False
    if not settings.metrics_hmac_key:
        return
    try:
        async with asyncio.timeout(2):
            await _collection.create_index("event_id", unique=True)
            await _collection.create_index("occurred_at", expireAfterSeconds=90 * 24 * 60 * 60)
            await _collection.create_index([("channel", 1), ("event", 1), ("occurred_at", 1), ("actor_id", 1)])
        _ready = True
    except Exception:
        logger.warning("Usage metrics unavailable: index initialization failed")


def _digest(value: str) -> str:
    return hmac.new(settings.metrics_hmac_key.encode(), value.encode(), hashlib.sha256).hexdigest()


async def _insert(document: dict) -> None:
    try:
        async with asyncio.timeout(1):
            await _collection.insert_one(document)
    except Exception:
        # Includes duplicate events, timeouts and unavailable storage. Never log the document.
        logger.debug("Usage metric was not stored")


def record_activity(phone: str, wamid: str | None, identification: str) -> None:
    """Queue an incoming-message event without delaying the conversation."""
    if not _ready or len(_pending) >= _max_pending:
        return
    if identification not in {"identified", "unidentified", "unknown"}:
        identification = "unknown"
    event_id = _digest(f"bot:activity:{wamid}") if wamid else uuid4().hex
    document = {
        "schema_version": 1,
        "event_id": event_id,
        "occurred_at": datetime.now(timezone.utc),
        "channel": "bot",
        "actor_id": _digest(f"bot:actor:{phone}"),
        "identification": identification,
        "event": "bot.activity",
    }
    task = asyncio.create_task(_insert(document))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def close() -> None:
    global _ready
    _ready = False
    tasks = list(_pending)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
