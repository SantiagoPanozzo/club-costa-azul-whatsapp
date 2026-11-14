"""Auto-storing wrapper around WhatsAppClient — records every outgoing message in MongoDB."""

from . import message_store
from .flow_ui import BUTTON_BODY_LIMIT, list_body_limit, split_text
from .state import Session
from .whatsapp_client import _truncate, whatsapp_client


class StoringClient:
    """Delegates to whatsapp_client and stores every sent message."""

    async def send_text(self, to: str, body: str, *, session: Session | None = None) -> str | None:
        wamid = None
        for chunk in split_text(body):
            wamid = await whatsapp_client.send_text(to, chunk)
            await self._store(to, "text", {"body": chunk}, wamid, session)
            if not wamid:
                return None
        return wamid

    async def _store(self, to, msg_type, content, wamid, session):
        if session is not None:
            if session.run is not None:
                session.run.outbound_attempts += 1
                session.run.api_accepted += int(bool(wamid))
            await message_store.store_outgoing(
                phone=to,
                msg_type=msg_type,
                content=content,
                wamid=wamid,
                session=session,
            )

    async def _interactive_body(self, to, body, limit, session):
        if len(body) <= limit:
            return body
        # Keep the ending (normally the actionable question) with its controls.
        # Overflow is deliberately separate and every character is preserved.
        if not await self.send_text(to, body[:-limit], session=session):
            if session is not None:
                session.clear_confirmation()
            return None
        return body[-limit:]

    async def send_buttons(
        self,
        to: str,
        body: str,
        buttons: list[tuple[str, str]],
        *,
        session: Session | None = None,
    ) -> str | None:
        if not 1 <= len(buttons) <= 3 or any(len(title) > 20 for _, title in buttons):
            raise ValueError("Invalid reply buttons")
        body = await self._interactive_body(to, body, BUTTON_BODY_LIMIT, session)
        if body is None:
            return None
        wamid = await whatsapp_client.send_buttons(to, body, buttons)
        if not wamid and session is not None:
            session.clear_confirmation()
        await self._store(
            to,
            "button",
            {"body": body, "buttons": [{"id": bid, "title": title} for bid, title in buttons]},
            wamid,
            session,
        )
        return wamid

    async def send_list(
        self,
        to: str,
        body: str,
        button_text: str,
        rows: list[dict],
        section_title: str = "Opciones",
        *,
        session: Session | None = None,
    ) -> str | None:
        if not 1 <= len(rows) <= 10:
            raise ValueError("Lists must contain between one and ten rows")
        rows = [
            {
                "id": str(row["id"]),
                "title": _truncate(str(row["title"]), 24),
                "description": _truncate(str(row.get("description", "")), 72),
            }
            for row in rows
        ]
        button_text = _truncate(button_text, 20)
        section_title = _truncate(section_title, 24)
        body = await self._interactive_body(to, body, list_body_limit(), session)
        if body is None:
            return None
        wamid = await whatsapp_client.send_list(to, body, button_text, rows, section_title)
        await self._store(
            to,
            "list",
            {"body": body, "button_text": button_text, "rows": rows, "section_title": section_title},
            wamid,
            session,
        )
        return wamid

    async def aclose(self) -> None:
        await whatsapp_client.aclose()


storing_client = StoringClient()
