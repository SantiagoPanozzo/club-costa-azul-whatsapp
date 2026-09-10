"""FastAPI entrypoint: receives the forwarded Meta webhook payload and routes it."""

import hashlib
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import message_store
from .config import settings
from .conversation import handle_message
from .logging_config import configure_logging
from .privacy import log_reference
from .services_client import services_client
from .state import sessions
from .storing_client import storing_client
from .webhook_parser import extract_messages

configure_logging(settings.log_level)
logger = logging.getLogger(__name__)

MAX_WEBHOOK_BYTES = 1_000_000
MAX_ROUTER_CLOCK_SKEW_SECONDS = 300


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await sessions.initialize()
    await message_store.ensure_indexes()
    try:
        yield
    finally:
        await services_client.aclose()
        await storing_client.aclose()
        await message_store.close()
        await sessions.close()


app = FastAPI(title="Club Costa Azul WhatsApp Bot", lifespan=lifespan)


def _verify_router_request(body: bytes, timestamp_header: str | None, signature_header: str | None) -> bool:
    if not timestamp_header or not signature_header or not signature_header.startswith("sha256="):
        return False
    try:
        timestamp = int(timestamp_header)
    except ValueError:
        return False
    if abs(int(time.time()) - timestamp) > MAX_ROUTER_CLOCK_SKEW_SECONDS:
        return False
    signed = timestamp_header.encode() + b"." + body
    expected = hmac.new(settings.router_shared_secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header[7:])


@app.get("/health")
async def health():
    try:
        redis_ready = await sessions.ping()
    except Exception:
        redis_ready = False
        logger.exception("Redis readiness check failed")
    ready = redis_ready or not settings.require_redis
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ok" if ready else "unavailable",
            "sessionStore": "redis" if redis_ready else "memory",
            "conversationTrace": "mongodb" if message_store.is_enabled() else "disabled",
        },
    )


@app.post("/webhook")
async def webhook(request: Request):
    """
    Receives a Meta WhatsApp Cloud API payload authenticated by the upstream
    router. Processing failures return 503 so its durable queue retries.
    """
    content_length = request.headers.get("Content-Length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")
    if not _verify_router_request(
        body,
        request.headers.get("X-Router-Timestamp"),
        request.headers.get("X-Router-Signature"),
    ):
        raise HTTPException(status_code=401, detail="Invalid router signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON") from None

    try:
        incoming_messages = extract_messages(payload)
    except Exception:
        logger.exception("Failed to parse signed webhook payload")
        raise HTTPException(status_code=400, detail="Invalid webhook payload") from None

    failed = False
    for incoming in incoming_messages:
        try:
            await handle_message(incoming)
        except Exception:
            failed = True
            logger.exception("Unhandled message error for member_ref=%s", log_reference(incoming.phone))

    if failed:
        raise HTTPException(status_code=503, detail="Message processing failed")

    return {"status": "ok"}
