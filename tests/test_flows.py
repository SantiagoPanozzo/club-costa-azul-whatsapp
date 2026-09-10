from datetime import date, timedelta
from unittest.mock import AsyncMock

from app.flows.activities import CANCEL_YES, ActivitiesFlow, ActivitiesState, ActivitiesStep
from app.flows.eventos import CANCEL_YES as EVENT_CANCEL_YES
from app.flows.eventos import EventosFlow, EventosState, EventosStep
from app.flows.reservas import CONFIRM_YES, ReservasFlow, ReservasState, ReservasStep
from app.flows.utils import send_list_pages
from app.state import Session
from app.webhook_parser import IncomingMessage


async def test_reservation_collects_required_fields_and_creates_pending_request(monkeypatch):
    from app.flows import reservas as module

    send_text = AsyncMock(return_value="out")
    send_buttons = AsyncMock(return_value="out")
    create = AsyncMock(return_value={"reserva": {"id": "r1", "estado": "Pendiente"}})
    monkeypatch.setattr(module.whatsapp_client, "send_text", send_text)
    monkeypatch.setattr(module.whatsapp_client, "send_buttons", send_buttons)
    monkeypatch.setattr(module.svc.services_client, "post_reserva", create)

    flow = ReservasFlow()
    state = ReservasState(
        step=ReservasStep.AWAITING_DATE,
        selected_space={"id": "space-1", "nombre": "Parrillero", "capacidad": 20, "costo": 500},
    )
    session = Session(socio={"id": "member-1"}, active_flow="reservas", flow_state=state)
    future = date.today() + timedelta(days=5)

    await flow.handle("59899", session, IncomingMessage("59899", "text", text=future.strftime("%d/%m/%Y")))
    await flow.handle("59899", session, IncomingMessage("59899", "text", text="18:00-20:00"))
    await flow.handle("59899", session, IncomingMessage("59899", "text", text="12"))
    await flow.handle("59899", session, IncomingMessage("59899", "text", text="Cumpleaños familiar"))
    await flow.handle("59899", session, IncomingMessage("59899", "text", text="omitir"))
    result = await flow.handle("59899", session, IncomingMessage("59899", "interactive", interactive_id=CONFIRM_YES))

    assert result == "done"
    create.assert_awaited_once_with(
        "member-1",
        "space-1",
        future.isoformat(),
        "18:00",
        "20:00",
        12,
        "Cumpleaños familiar",
        None,
    )
    assert "pendiente de aprobación" in send_text.await_args.args[1]


async def test_activity_cancellation_calls_member_safe_endpoint(monkeypatch):
    from app.flows import activities as module

    cancel = AsyncMock(return_value={"mensaje": "ok"})
    monkeypatch.setattr(module.svc.services_client, "delete_inscripcion", cancel)
    monkeypatch.setattr(module.whatsapp_client, "send_text", AsyncMock(return_value="out"))
    flow = ActivitiesFlow()
    state = ActivitiesState(
        step=ActivitiesStep.AWAITING_CANCEL_CONFIRM,
        selected_inscription={"id": "inscription-1", "actividadId": "activity-1"},
    )
    session = Session(socio={"id": "member-1"}, active_flow="activities", flow_state=state)

    result = await flow.handle("59899", session, IncomingMessage("59899", "interactive", interactive_id=CANCEL_YES))

    assert result == "done"
    cancel.assert_awaited_once_with("inscription-1")


async def test_retried_confirmation_does_not_repeat_activity_mutation(monkeypatch):
    from app.flows import activities as module

    create = AsyncMock(return_value={"inscripcion": {"id": "i1"}})
    monkeypatch.setattr(module.svc.services_client, "post_inscripcion", create)
    monkeypatch.setattr(module.whatsapp_client, "send_text", AsyncMock(return_value="out"))
    flow = ActivitiesFlow()
    message = IncomingMessage(
        "59899",
        "interactive",
        interactive_id=module.CONFIRM_YES,
        wamid="wamid.activity-confirm-once",
    )

    for _ in range(2):
        session = Session(
            socio={"id": "member-1"},
            active_flow="activities",
            flow_state=ActivitiesState(
                step=ActivitiesStep.AWAITING_CONFIRM,
                selected_activity={"id": "activity-1", "nombre": "Yoga"},
            ),
        )
        assert await flow.handle("59899", session, message) == "done"

    create.assert_awaited_once_with("member-1", "activity-1")


async def test_event_withdrawal_calls_owner_checked_endpoint(monkeypatch):
    from app.flows import eventos as module

    withdraw = AsyncMock(return_value={"mensaje": "ok"})
    monkeypatch.setattr(module.svc.services_client, "delete_inscripcion_evento", withdraw)
    monkeypatch.setattr(module.whatsapp_client, "send_text", AsyncMock(return_value="out"))
    flow = EventosFlow()
    state = EventosState(
        step=EventosStep.AWAITING_CANCEL_CONFIRM,
        selected_inscription={"id": "event-inscription-1", "eventoId": "event-1"},
    )
    session = Session(socio={"id": "member-1"}, active_flow="eventos", flow_state=state)

    result = await flow.handle(
        "59899", session, IncomingMessage("59899", "interactive", interactive_id=EVENT_CANCEL_YES)
    )

    assert result == "done"
    withdraw.assert_awaited_once_with("event-1", "event-inscription-1")


async def test_long_lists_are_split_without_hiding_rows(monkeypatch):
    from app.flows import utils as module

    send_list = AsyncMock(return_value="out")
    monkeypatch.setattr(module.whatsapp_client, "send_list", send_list)
    rows = [{"id": f"row-{index}", "title": f"Row {index}"} for index in range(23)]

    await send_list_pages(
        "59899",
        body="Elegí una opción",
        button_text="Ver opciones",
        rows=rows,
        section_title="Opciones",
        session=Session(),
    )

    assert send_list.await_count == 3
    sent_rows = [row for call in send_list.await_args_list for row in call.kwargs["rows"]]
    assert sent_rows == rows
    assert [len(call.kwargs["rows"]) for call in send_list.await_args_list] == [10, 10, 3]
    assert [call.kwargs["body"] for call in send_list.await_args_list] == [
        "Elegí una opción (1/3)",
        "Elegí una opción (2/3)",
        "Elegí una opción (3/3)",
    ]
