"""Async MongoDB message storage — fire-and-forget writes for conversation traceability."""

import logging
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

from .config import settings
from .state import Session
from .webhook_parser import IncomingMessage

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient = AsyncIOMotorClient(settings.mongodb_url)
_db = _client.get_default_database("club_costa_azul")
_conversations = _db["conversations"]
_messages = _db["messages"]


async def ensure_indexes() -> None:
    await _conversations.create_index("phone", unique=True)
    await _conversations.create_index("last_message_at")
    await _messages.create_index([("conversation_id", 1), ("timestamp", 1)])
    await _messages.create_index([("phone", 1), ("timestamp", 1)])
    await _ensure_wamid_index()


async def _ensure_wamid_index() -> None:
    """Unique ``wamid`` index, restricted to documents that actually carry one.

    A unique (even sparse) index on a single field still indexes documents whose
    ``wamid`` key exists with a null value, so every outgoing message stored
    after a failed send (``wamid=None``) would collide on ``{wamid: null}``. A
    partial index keeps those documents out of the index entirely.
    """
    info = await _messages.index_information()
    legacy = info.get("wamid_1")
    if legacy is not None and legacy.get("key") == [("wamid", 1)]:
        # Old sparse-unique index; it rejected a second null wamid.
        await _messages.drop_index("wamid_1")
    await _messages.create_index(
        "wamid",
        unique=True,
        name="wamid_unique",
        partialFilterExpression={"wamid": {"$type": "string"}},
    )


async def close() -> None:
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


async def store_incoming(incoming: IncomingMessage, session: Session) -> bool:
    """Store an incoming message. Returns True if this is a new message, False if duplicate."""
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

        doc = {
            "conversation_id": conv_id,
            "phone": incoming.phone,
            "direction": "incoming",
            "timestamp": now,
            "type": incoming.type,
            "content": content,
            "flow": session.active_flow,
        }
        if incoming.wamid:
            doc["wamid"] = incoming.wamid

        await _messages.insert_one(doc)
        return True
    except Exception as exc:
        if "duplicate key" in str(exc).lower() or "E11000" in str(exc):
            logger.info("Duplicate wamid %s from %s, skipping", incoming.wamid, incoming.phone)
            return False
        logger.exception("Failed to store incoming message from %s", incoming.phone)
        return True


async def store_outgoing(
    phone: str,
    msg_type: str,
    content: dict,
    wamid: str | None,
    session: Session,
    contact_name: str | None = None,
) -> None:
    try:
        now = datetime.now(timezone.utc)
        conv_id = await _upsert_conversation(phone, contact_name, session, now)

        doc = {
            "conversation_id": conv_id,
            "phone": phone,
            "direction": "outgoing",
            "timestamp": now,
            "type": msg_type,
            "content": content,
            "flow": session.active_flow,
        }
        if wamid:
            doc["wamid"] = wamid

        await _messages.insert_one(doc)
    except Exception:
        logger.exception("Failed to store outgoing message to %s", phone)
