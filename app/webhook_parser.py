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


def extract_messages(payload: dict) -> list[IncomingMessage]:
    results: list[IncomingMessage] = []

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages")
            if not messages:
                # e.g. "statuses" (delivery/read receipts) -> nothing to do
                continue

            for msg in messages:
                phone = msg.get("from")
                if not phone:
                    # Defensive case: WhatsApp Cloud API always includes "from" for
                    # genuine user messages, but we can't identify or reply to a
                    # user without a phone number, so we skip and log instead of
                    # raising. (See README for the "consent" design note.)
                    logger.warning("Skipping incoming message with no phone number: %s", msg)
                    continue

                msg_type = msg.get("type", "unknown")
                text = None
                interactive_id = None

                if msg_type == "text":
                    text = msg.get("text", {}).get("body")
                elif msg_type == "interactive":
                    interactive = msg.get("interactive", {})
                    itype = interactive.get("type")
                    if itype == "list_reply":
                        interactive_id = interactive.get("list_reply", {}).get("id")
                    elif itype == "button_reply":
                        interactive_id = interactive.get("button_reply", {}).get("id")

                results.append(
                    IncomingMessage(phone=phone, type=msg_type, text=text, interactive_id=interactive_id)
                )

    return results
