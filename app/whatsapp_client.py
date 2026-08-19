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

    async def _send(self, payload: dict) -> None:
        try:
            resp = await self._client.post(self._url, json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            # Sending failures are logged, not raised: we generally have nothing
            # better to do than retry on the next user message.
            body = getattr(exc, "response", None)
            body_text = body.text if body is not None else ""
            logger.error(
                "Error sending WhatsApp message: %s | response=%s | payload=%s",
                exc,
                body_text,
                payload,
            )

    async def send_text(self, to: str, body: str) -> None:
        await self._send(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": body},
            }
        )

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> None:
        """buttons: list of (id, title). Max 3 buttons, title max 20 chars (WhatsApp limit)."""
        await self._send(
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
    ) -> None:
        """rows: list of {"id": str, "title": str, "description": str}. Max 10 rows (WhatsApp limit)."""
        await self._send(
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


    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Download media from WhatsApp by media ID. Returns (file_bytes, mime_type)."""
        graph_url = f"https://graph.facebook.com/{settings.whatsapp_api_version}/{media_id}"
        try:
            resp = await self._client.get(graph_url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Error fetching media metadata for %s: %s", media_id, exc)
            raise

        data = resp.json()
        url = data["url"]
        mime = data.get("mime_type", "application/octet-stream")

        try:
            file_resp = await self._client.get(url)
            file_resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Error downloading media file for %s: %s", media_id, exc)
            raise

        max_size = 5 * 1024 * 1024
        if len(file_resp.content) > max_size:
            raise ValueError(f"Media file exceeds 5 MB limit ({len(file_resp.content)} bytes)")

        return file_resp.content, mime


whatsapp_client = WhatsAppClient()
