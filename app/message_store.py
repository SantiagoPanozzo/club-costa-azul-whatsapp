"""Async MongoDB message storage — fire-and-forget writes for conversation traceability."""

import logging
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

from .config import settings
from .privacy import log_reference
from .state import Session
from .webhook_parser import IncomingMessage

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = AsyncIOMotorClient(settings.mongodb_url) if settings.mongodb_url else None
_db = _client.get_default_database("club_costa_azul") if _client is not None else None
_conversations = _db["conversations"] if _db is not None else None
_messages = _db["messages"] if _db is not None else None
_enabled = False


async def ensure_indexes() -> None:
    global _enabled
    if _conversations is None or _messages is None:
        logger.warning("MONGODB_URL is not configured; conversation trace storage is disabled")
        return
    try:
        await _conversations.create_index("phone", unique=True)
        await _conversations.create_index("last_message_at")
        await _messages.create_index([("conversation_id", 1), ("timestamp", 1)])
        await _messages.create_index([("phone", 1), ("timestamp", 1)])
        await _messages.create_index("wamid", unique=True, sparse=True)
        _enabled = True
    except Exception:
        _enabled = False
        logger.exception("MongoDB is unavailable; conversation trace storage is disabled")


def is_enabled() -> bool:
    return _enabled


async def close() -> None:
    if _client is not None:
        _client.close()


def _socio_fields(session: Session) -> dict:
    socio = session.socio
    if socio is None:
        return {"socio_id": None, "socio_nombre": None}
    nombre = socio.get("nombre", "")
    apellido = socio.get("apellido", "")
    full = f"{nombre} {apellido}".strip() or None
    return {"socio_id": str(socio.get("id", "")), "socio_nombre": full}


async def _upsert_conversation(phone: str, contact_name: str | None, session: Session, now: datetime) -> str:
    assert _conversations is not None
    result = await _conversations.find_one_and_update(
        {"phone": phone},
        {
            "$setOnInsert": {"started_at": now},
            "$set": {
                "last_message_at": now,
                "contact_name": contact_name,
                **_socio_fields(session),
            },
            "$inc": {"message_count": 1},
        },
        upsert=True,
        return_document=True,
    )
    return str(result["_id"])


async def store_incoming(incoming: IncomingMessage, session: Session) -> None:
    if not _enabled or _messages is None:
        return
    try:
        now = datetime.now(timezone.utc)
        if incoming.timestamp:
            try:
                now = datetime.fromtimestamp(int(incoming.timestamp), tz=timezone.utc)
            except (ValueError, OSError):
                pass

        conv_id = await _upsert_conversation(incoming.phone, incoming.contact_name, session, now)

        content: dict = {}
        if incoming.text:
            content["body"] = incoming.text
        if incoming.interactive_id:
            content["interactive_id"] = incoming.interactive_id
        if incoming.interactive_title:
            content["interactive_title"] = incoming.interactive_title

        await _messages.insert_one(
            {
                "conversation_id": conv_id,
                "phone": incoming.phone,
                "wamid": incoming.wamid,
                "direction": "incoming",
                "timestamp": now,
                "type": incoming.type,
                "content": content,
                "flow": session.active_flow,
            }
        )
    except Exception:
        logger.exception("Failed to store incoming message for member_ref=%s", log_reference(incoming.phone))


async def store_outgoing(
    phone: str,
    msg_type: str,
    content: dict,
    wamid: str | None,
    session: Session,
    contact_name: str | None = None,
) -> None:
    if not _enabled or _messages is None:
        return
    try:
        now = datetime.now(timezone.utc)
        conv_id = await _upsert_conversation(phone, contact_name, session, now)

        await _messages.insert_one(
            {
                "conversation_id": conv_id,
                "phone": phone,
                "wamid": wamid,
                "direction": "outgoing",
                "timestamp": now,
                "type": msg_type,
                "content": content,
                "flow": session.active_flow,
            }
        )
    except Exception:
        logger.exception("Failed to store outgoing message for member_ref=%s", log_reference(phone))
