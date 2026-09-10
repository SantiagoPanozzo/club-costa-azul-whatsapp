"""Client for sending outbound messages via Meta's WhatsApp Cloud API (Graph API)."""

import logging

import httpx

from .config import settings

logger = logging.getLogger(__name__)


class WhatsAppDeliveryError(Exception):
    """Raised when Meta does not accept an outbound message."""


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    logger.warning("Truncating WhatsApp field to %d chars (was %d)", limit, len(text))
    return text[: limit - 1].rstrip() + "…"


class WhatsAppClient:
    def __init__(self):
        self._url = (
            f"https://graph.facebook.com/{settings.whatsapp_api_version}/{settings.whatsapp_phone_number_id}/messages"
        )
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {settings.whatsapp_api_token}",
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )

    async def aclose(self):
        await self._client.aclose()

    async def _send(self, payload: dict) -> str:
        """Send a message and return its WhatsApp message ID."""
        try:
            resp = await self._client.post(self._url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            wamid = data.get("messages", [{}])[0].get("id")
            if not wamid:
                raise WhatsAppDeliveryError("Meta response did not include a message ID")
            return wamid
        except httpx.HTTPError as exc:
            response = getattr(exc, "response", None)
            status = response.status_code if response is not None else None
            logger.error(
                "Meta rejected outbound WhatsApp message type=%s status=%s error=%s",
                payload.get("type", "unknown"),
                status,
                type(exc).__name__,
            )
            raise WhatsAppDeliveryError(str(exc)) from exc

    async def send_text(self, to: str, body: str) -> str:
        return await self._send(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": body},
            }
        )

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> str:
        """buttons: list of (id, title). Max 3 buttons, title max 20 chars (WhatsApp limit)."""
        return await self._send(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body},
                    "action": {
                        "buttons": [
                            {
                                "type": "reply",
                                "reply": {"id": bid, "title": _truncate(title, 20)},
                            }
                            for bid, title in buttons[:3]
                        ]
                    },
                },
            }
        )

    async def send_list(
        self,
        to: str,
        body: str,
        button_text: str,
        rows: list[dict],
        section_title: str = "Opciones",
    ) -> str:
        """rows: list of {"id": str, "title": str, "description": str}. Max 10 rows (WhatsApp limit)."""
        return await self._send(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "interactive",
                "interactive": {
                    "type": "list",
                    "body": {"text": body},
                    "action": {
                        "button": _truncate(button_text, 20),
                        "sections": [
                            {
                                "title": _truncate(section_title, 24),
                                "rows": [
                                    {
                                        "id": r["id"],
                                        "title": _truncate(r["title"], 24),
                                        "description": _truncate(r.get("description", ""), 72),
                                    }
                                    for r in rows[:10]
                                ],
                            }
                        ],
                    },
                },
            }
        )


whatsapp_client = WhatsAppClient()
