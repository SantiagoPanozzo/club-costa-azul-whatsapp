"""Client for sending outbound messages via Meta's WhatsApp Cloud API (Graph API)."""

import logging

import httpx

from .config import settings

logger = logging.getLogger(__name__)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    logger.warning("Truncating text to %d chars (was %d): %r", limit, len(text), text)
    return text[: limit - 1].rstrip() + "…"


MAX_MEDIA_SIZE = 5 * 1024 * 1024


class WhatsAppClient:
    def __init__(self):
        self._url = (
            f"https://graph.facebook.com/{settings.whatsapp_api_version}/{settings.whatsapp_phone_number_id}/messages"
        )
        self._graph_base = f"https://graph.facebook.com/{settings.whatsapp_api_version}"
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {settings.whatsapp_api_token}",
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )

    async def aclose(self):
        await self._client.aclose()

    async def _send(self, payload: dict) -> str | None:
        """Send a message and return the wamid on success, None on error."""
        try:
            resp = await self._client.post(self._url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("messages", [{}])[0].get("id")
        except httpx.HTTPError as exc:
            body = getattr(exc, "response", None)
            body_text = body.text if body is not None else ""
            logger.error(
                "Error sending WhatsApp message: %s | response=%s | payload=%s",
                exc,
                body_text,
                payload,
            )
            return None

    async def send_text(self, to: str, body: str) -> str | None:
        return await self._send(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": body},
            }
        )

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> str | None:
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

    async def download_media(self, media_id: str) -> tuple[bytes, str] | None:
        """Download a media file sent by a user. Returns (file_bytes, content_type) or None."""
        try:
            meta_resp = await self._client.get(f"{self._graph_base}/{media_id}")
            meta_resp.raise_for_status()
            media_url = meta_resp.json().get("url")
            if not media_url:
                logger.error("No URL in media metadata for %s", media_id)
                return None

            resp = await self._client.get(
                media_url,
                headers={"Authorization": f"Bearer {settings.whatsapp_api_token}"},
            )
            resp.raise_for_status()

            if len(resp.content) > MAX_MEDIA_SIZE:
                logger.warning("Media %s exceeds 5MB (%d bytes), rejecting", media_id, len(resp.content))
                return None

            content_type = resp.headers.get("content-type", "application/octet-stream")
            return resp.content, content_type
        except httpx.HTTPError as exc:
            body = getattr(exc, "response", None)
            body_text = body.text if body is not None else ""
            logger.error("Error downloading media %s: %s | response=%s", media_id, exc, body_text)
            return None

    async def send_list(
        self,
        to: str,
        body: str,
        button_text: str,
        rows: list[dict],
        section_title: str = "Opciones",
    ) -> str | None:
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
