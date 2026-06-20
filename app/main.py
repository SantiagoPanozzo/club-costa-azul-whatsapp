"""FastAPI entrypoint: receives the forwarded Meta webhook payload and routes it."""
import logging

from fastapi import FastAPI, Request

from .config import settings
from .conversation import handle_message
from .services_client import services_client
from .webhook_parser import extract_messages
from .whatsapp_client import whatsapp_client

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

app = FastAPI(title="Club Costa Azul WhatsApp Bot")


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
    payload = await request.json()

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
    await whatsapp_client.aclose()
