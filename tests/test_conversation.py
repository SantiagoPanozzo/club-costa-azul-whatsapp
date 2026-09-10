import time
from unittest.mock import AsyncMock

from app import conversation
from app.config import settings
from app.state import Session
from app.webhook_parser import IncomingMessage


async def test_cached_member_is_revalidated_and_deactivated_session_is_cleared(monkeypatch):
    session = Session(
        socio={"id": "member-1"},
        active_flow="activities",
        flow_state={"step": "awaiting_choice"},
        member_verified_at=0,
    )
    monkeypatch.setattr(settings, "member_revalidate_seconds", 0)
    monkeypatch.setattr(conversation.message_store, "store_incoming", AsyncMock())
    monkeypatch.setattr(conversation.svc.services_client, "get_socio_by_whatsapp", AsyncMock(return_value=None))
    send_text = AsyncMock(return_value="out")
    monkeypatch.setattr(conversation.whatsapp_client, "send_text", send_text)

    await conversation._handle_locked(IncomingMessage("59899", "text", text="menu"), session)

    assert session.socio is None
    assert session.active_flow is None
    assert session.flow_state is None
    assert send_text.await_args.args[1] == conversation.NOT_REGISTERED


async def test_help_number_alias_and_unsupported_message_return_to_menu(monkeypatch):
    session = Session(socio={"id": "member-1"}, member_verified_at=time.time())
    monkeypatch.setattr(conversation.message_store, "store_incoming", AsyncMock())
    send_help = AsyncMock()
    send_menu = AsyncMock()
    monkeypatch.setattr(conversation, "_send_help", send_help)
    monkeypatch.setattr(conversation, "_send_main_menu", send_menu)

    await conversation._handle_locked(IncomingMessage("59899", "text", text="7"), session)
    await conversation._handle_locked(IncomingMessage("59899", "image"), session)

    send_help.assert_awaited_once_with("59899", session)
    assert send_menu.await_count == 2
