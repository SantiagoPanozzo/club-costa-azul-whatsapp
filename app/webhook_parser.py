"""
Parses the raw Meta WhatsApp Cloud API webhook payload (forwarded as-is by the
upstream webhook service) into a flat list of IncomingMessage objects.

Reference payload shape (messages event):
{
  "object": "whatsapp_business_account",
  "entry": [{
    "id": "...",
    "changes": [{
      "field": "messages",
      "value": {
        "messaging_product": "whatsapp",
        "metadata": {...},
        "contacts": [{"profile": {"name": "..."}, "wa_id": "59812123123"}],
        "messages": [{
          "from": "59812123123",
          "id": "wamid...",
          "timestamp": "...",
          "type": "text" | "interactive" | ...,
          "text": {"body": "..."},
          "interactive": {
            "type": "list_reply" | "button_reply",
            "list_reply": {"id": "...", "title": "...", "description": "..."},
            "button_reply": {"id": "...", "title": "..."}
          }
        }],
        # delivery/read receipts arrive as "statuses" instead of "messages" -> ignored
      }
    }]
  }]
}
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class IncomingMessage:
    phone: str
    type: str
    text: Optional[str] = None
    interactive_id: Optional[str] = None
    wamid: Optional[str] = None
    timestamp: Optional[str] = None
    contact_name: Optional[str] = None
    interactive_title: Optional[str] = None
    media_id: Optional[str] = None
    media_mime_type: Optional[str] = None
    media_filename: Optional[str] = None


def extract_messages(payload: dict) -> list[IncomingMessage]:
    results: list[IncomingMessage] = []

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages")
            if not messages:
                continue

            contacts = value.get("contacts", [])
            contact_name = None
            if contacts:
                contact_name = contacts[0].get("profile", {}).get("name")

            for msg in messages:
                phone = msg.get("from")
                if not phone:
                    logger.warning("Skipping incoming message with no phone number: %s", msg)
                    continue

                msg_type = msg.get("type", "unknown")
                text = None
                interactive_id = None
                interactive_title = None

                media_id = None
                media_mime_type = None
                media_filename = None

                if msg_type == "text":
                    text = msg.get("text", {}).get("body")
                elif msg_type == "interactive":
                    interactive = msg.get("interactive", {})
                    itype = interactive.get("type")
                    if itype == "list_reply":
                        reply = interactive.get("list_reply", {})
                        interactive_id = reply.get("id")
                        interactive_title = reply.get("title")
                    elif itype == "button_reply":
                        reply = interactive.get("button_reply", {})
                        interactive_id = reply.get("id")
                        interactive_title = reply.get("title")
                elif msg_type == "image":
                    image = msg.get("image", {})
                    media_id = image.get("id")
                    media_mime_type = image.get("mime_type")
                elif msg_type == "document":
                    document = msg.get("document", {})
                    media_id = document.get("id")
                    media_mime_type = document.get("mime_type")
                    media_filename = document.get("filename")

                results.append(
                    IncomingMessage(
                        phone=phone,
                        type=msg_type,
                        text=text,
                        interactive_id=interactive_id,
                        wamid=msg.get("id"),
                        timestamp=msg.get("timestamp"),
                        contact_name=contact_name,
                        interactive_title=interactive_title,
                        media_id=media_id,
                        media_mime_type=media_mime_type,
                        media_filename=media_filename,
                    )
                )

    return results
