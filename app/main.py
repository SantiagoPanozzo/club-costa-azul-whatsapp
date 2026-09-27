"""FastAPI entrypoint: receives the forwarded Meta webhook payload and routes it."""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import message_store
from .config import settings
from .conversation import handle_message
from .services_client import services_client
from .storing_client import storing_client
from .webhook_parser import extract_messages, extract_metadata
from .whatsapp_client import whatsapp_client

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

app = FastAPI(title="Club Costa Azul WhatsApp Bot")


@app.on_event("startup")
async def startup():
    await message_store.ensure_indexes()
    try:
        await whatsapp_client.check_phone_number()
    except Exception:
        logger.exception("WhatsApp sender self-check failed")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    """
    Receives the raw, unmodified Meta WhatsApp Cloud API payload, forwarded by
    the upstream webhook service. Always returns 200 so the upstream webhook
    doesn't retry/error regardless of how processing goes downstream.
    """
    if settings.router_secret:
        if request.headers.get("X-Router-Secret") != settings.router_secret:
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})

    payload = await request.json()

    for meta in extract_metadata(payload):
        received_id = meta.get("phone_number_id")
        if received_id and str(received_id) != settings.whatsapp_phone_number_id:
            logger.error(
                "WHATSAPP_PHONE_NUMBER_ID mismatch: configured=%s but this message arrived on "
                "phone_number_id=%s (display_phone_number=%s). Outbound replies will fail with "
                "'Unsupported request - method type: post'.",
                settings.whatsapp_phone_number_id,
                received_id,
                meta.get("display_phone_number"),
            )

    try:
        incoming_messages = extract_messages(payload)
    except Exception:
        logger.exception("Failed to parse incoming webhook payload: %s", payload)
        return {"status": "ok"}

    for incoming in incoming_messages:
        try:
            await handle_message(incoming)
        except Exception:
            logger.exception("Unhandled error processing message from %s", incoming.phone)

    return {"status": "ok"}


@app.on_event("shutdown")
async def shutdown():
    await services_client.aclose()
    await storing_client.aclose()
    await message_store.close()
