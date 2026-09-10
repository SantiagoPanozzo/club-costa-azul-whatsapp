"""Auto-storing wrapper around WhatsAppClient — records every outgoing message in MongoDB."""

from . import message_store
from .state import Session
from .whatsapp_client import whatsapp_client


class StoringClient:
    """Delegates to whatsapp_client and stores every sent message."""

    async def send_text(self, to: str, body: str, *, session: Session | None = None) -> str:
        wamid = await whatsapp_client.send_text(to, body)
        if session is not None:
            await message_store.store_outgoing(
                phone=to,
                msg_type="text",
                content={"body": body},
                wamid=wamid,
                session=session,
            )
        return wamid

    async def send_buttons(
        self,
        to: str,
        body: str,
        buttons: list[tuple[str, str]],
        *,
        session: Session | None = None,
    ) -> str:
        wamid = await whatsapp_client.send_buttons(to, body, buttons)
        if session is not None:
            await message_store.store_outgoing(
                phone=to,
                msg_type="button",
                content={
                    "body": body,
                    "buttons": [{"id": bid, "title": title} for bid, title in buttons],
                },
                wamid=wamid,
                session=session,
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
    ) -> str:
        wamid = await whatsapp_client.send_list(to, body, button_text, rows, section_title)
        if session is not None:
            await message_store.store_outgoing(
                phone=to,
                msg_type="list",
                content={
                    "body": body,
                    "button_text": button_text,
                    "rows": rows[:10],
                    "section_title": section_title,
                },
                wamid=wamid,
                session=session,
            )
        return wamid

    async def aclose(self) -> None:
        await whatsapp_client.aclose()


storing_client = StoringClient()
