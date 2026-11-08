from copy import deepcopy

import pytest

from app.conversation import MENU_OPTIONS
from app.flow_ui import BUTTON_BODY_LIMIT, MENU_ID, list_body_limit
from app.flows import eventos as flow
from app.services_client import ServicesAPIConflict, ServicesAPIError
from tests.transcript import EVENT

pytestmark = pytest.mark.asyncio


def inscription(key="i1", event_id="e1", **kwargs):
    return {
        "id": key,
        "eventoId": event_id,
        "estado": "Confirmada",
        "fechaInscripcion": "2026-10-01T10:00:00Z",
        **kwargs,
    }


def rows(payload):
    return payload["interactive"]["action"]["sections"][0]["rows"]


def buttons(payload):
    return [b["reply"]["id"].split(":", 1)[0] for b in payload["interactive"]["action"]["buttons"]]


def body(payload):
    return payload["interactive"]["body"]["text"]


def assert_menu(payload):
    assert [r["id"] for r in rows(payload)] == [r["id"] for r in MENU_OPTIONS]


def assert_no_writes(transcript):
    transcript.api.post_inscripcion_evento.assert_not_awaited()
    transcript.api.delete_inscripcion_evento.assert_not_awaited()


async def test_enroll_three_turns_three_messages_with_details_and_full_menu(transcript):
    event = {
        **EVENT,
        "descripcion": "Música y encuentro para toda la familia.",
        "costoSocio": 123.45,
        "costoNoSocio": 250.75,
    }
    transcript.api.get_eventos.return_value = [event]
    catalog = await transcript.send("eventos")
    assert {r["id"] for r in rows(catalog)} == {"evt_e1", flow.ACTION_WITHDRAW}
    assert "Todavía no estás inscripto" in body(catalog)
    confirmation = await transcript.send("evt_e1")
    for expected in [
        "Encuentro",
        event["descripcion"],
        event["fecha"],
        "09:00:00",
        "10:00:00",
        "Quincho",
        "$123.45",
        "$250.75",
        "10",
    ]:
        assert expected in body(confirmation)
    assert buttons(confirmation) == [flow.CONFIRM_YES, flow.CONFIRM_NO, MENU_ID]
    assert_no_writes(transcript)
    result = await transcript.send(flow.CONFIRM_YES)
    assert "Te inscribiste en *Encuentro*" in body(result)
    assert_menu(result)
    transcript.api.post_inscripcion_evento.assert_awaited_once_with("e1", "s1")
    assert transcript.session.active_flow is None
    assert transcript.runs.call_args.args[0].task == "enroll"
    transcript.assert_single_responses(3)


async def test_withdraw_four_turns_four_messages_scoped_explicit_consent(transcript):
    transcript.api.get_inscripciones_evento_socio.return_value = [inscription()]
    catalog = await transcript.send("eventos")
    assert "Encuentro" in body(catalog)
    assert {r["id"] for r in rows(catalog)} == {flow.ACTION_WITHDRAW}
    own = await transcript.send(flow.ACTION_WITHDRAW)
    assert {r["id"] for r in rows(own)} == {"evtw_i1", flow.ACTION_BROWSE}
    confirmation = await transcript.send("evtw_i1")
    for expected in ["Encuentro", EVENT["fecha"], "09:00:00", "10:00:00", "Quincho", "$100"]:
        assert expected in body(confirmation)
    assert buttons(confirmation) == [flow.WITHDRAW_YES, flow.WITHDRAW_NO, MENU_ID]
    assert_no_writes(transcript)
    result = await transcript.send(flow.WITHDRAW_YES)
    assert "Inscripción a *Encuentro* cancelada" in body(result)
    assert_menu(result)
    transcript.api.delete_inscripcion_evento.assert_awaited_once_with("e1", "i1")
    assert transcript.runs.call_args.args[0].task == "withdraw"
    transcript.assert_single_responses(4)


@pytest.mark.parametrize("withdraw", [False, True])
async def test_back_returns_previous_catalog_and_menu_never_writes(transcript, withdraw):
    if withdraw:
        transcript.api.get_inscripciones_evento_socio.return_value = [inscription()]
    await transcript.send("eventos")
    if withdraw:
        await transcript.send(flow.ACTION_WITHDRAW)
    await transcript.send("evtw_i1" if withdraw else "evt_e1")
    back = await transcript.send(flow.WITHDRAW_NO if withdraw else flow.CONFIRM_NO)
    expected = {"evtw_i1", flow.ACTION_BROWSE} if withdraw else {"evt_e1", flow.ACTION_WITHDRAW}
    assert {r["id"] for r in rows(back)} == expected
    await transcript.send("evtw_i1" if withdraw else "evt_e1")
    menu = await transcript.send(MENU_ID)
    assert_menu(menu)
    assert transcript.session.active_flow is None
    assert_no_writes(transcript)
    transcript.assert_single_responses(6 if withdraw else 5)


async def test_own_inscriptions_back_to_catalog(transcript):
    await transcript.send("eventos")
    empty = await transcript.send(flow.ACTION_WITHDRAW)
    assert "No tenés inscripciones" in body(empty)
    assert [r["id"] for r in rows(empty)] == [flow.ACTION_BROWSE]
    assert transcript.session.run.task == "view"
    catalog = await transcript.send(flow.ACTION_BROWSE)
    assert "evt_e1" in {r["id"] for r in rows(catalog)}
    assert_no_writes(transcript)
    transcript.assert_single_responses(3)


@pytest.mark.parametrize("stage", ["catalog", "confirmation", "own", "withdrawal"])
async def test_unexpected_reply_repeats_current_interactive_prompt_once(transcript, stage):
    if stage in {"own", "withdrawal"}:
        transcript.api.get_inscripciones_evento_socio.return_value = [inscription()]
    await transcript.send("eventos")
    if stage == "confirmation":
        previous = await transcript.send("evt_e1")
    elif stage in {"own", "withdrawal"}:
        previous = await transcript.send(flow.ACTION_WITHDRAW)
        if stage == "withdrawal":
            previous = await transcript.send("evtw_i1")
    else:
        previous = transcript.payloads[-1]
    current = await transcript.send(text="no entendí")
    assert "Por favor" in body(current)
    assert current["interactive"]["action"] == previous["interactive"]["action"]
    assert_no_writes(transcript)
    transcript.assert_single_responses(len(transcript.turns))


@pytest.mark.parametrize("at_confirmation", [False, True])
@pytest.mark.parametrize("change", [{"estado": "Cancelado"}, {"cupoDisponible": 0}, {"fecha": "2000-01-01"}])
async def test_stale_event_refreshes_one_message_with_notice(transcript, at_confirmation, change):
    other = {**EVENT, "id": "e2", "nombre": "Otro encuentro"}
    transcript.api.get_eventos.return_value = [deepcopy(EVENT), other]
    await transcript.send("eventos")
    if at_confirmation:
        await transcript.send("evt_e1")
    transcript.api.get_eventos.return_value = [{**EVENT, **change}, other]
    refreshed = await transcript.send(flow.CONFIRM_YES if at_confirmation else "evt_e1")
    assert "ya no está disponible" in body(refreshed)
    assert {r["id"] for r in rows(refreshed)} == {"evt_e2", flow.ACTION_WITHDRAW}
    assert_no_writes(transcript)
    transcript.assert_single_responses(3 if at_confirmation else 2)


async def test_changed_price_requires_fresh_consent(transcript):
    await transcript.send("eventos")
    await transcript.send("evt_e1")
    transcript.api.get_eventos.return_value = [{**EVENT, "costoSocio": 200.50}]
    changed = await transcript.send(flow.CONFIRM_YES)
    assert "cambiaron" in body(changed)
    assert "$200.5" in body(changed)
    assert buttons(changed)[0] == flow.CONFIRM_YES
    assert_no_writes(transcript)
    await transcript.send(flow.CONFIRM_YES)
    transcript.api.post_inscripcion_evento.assert_awaited_once_with("e1", "s1")
    transcript.assert_single_responses(4)


async def test_forged_unoffered_event_and_inscription_never_write(transcript):
    await transcript.send("eventos")
    catalog = await transcript.send("evt_not-offered")
    assert "actualizada" in body(catalog)
    await transcript.send(flow.ACTION_WITHDRAW)
    own = await transcript.send("evtw_someone-elses-id")
    assert "actualizada" in body(own)
    await transcript.send(flow.WITHDRAW_YES)
    assert_no_writes(transcript)
    transcript.assert_single_responses(5)


@pytest.mark.parametrize("change", ["missing", "cancelled", "different-event"])
async def test_stale_own_inscription_refreshes_without_delete(transcript, change):
    transcript.api.get_inscripciones_evento_socio.return_value = [inscription()]
    await transcript.send("eventos")
    await transcript.send(flow.ACTION_WITHDRAW)
    await transcript.send("evtw_i1")
    transcript.api.get_inscripciones_evento_socio.return_value = {
        "missing": [],
        "cancelled": [inscription(estado="Cancelada")],
        "different-event": [inscription(event_id="e2")],
    }[change]
    refreshed = await transcript.send(flow.WITHDRAW_YES)
    assert "ya no se puede cancelar" in body(refreshed)
    assert flow.ACTION_BROWSE in {r["id"] for r in rows(refreshed)}
    assert_no_writes(transcript)
    transcript.assert_single_responses(4)


async def test_empty_event_catalog_finishes_with_one_full_menu(transcript):
    transcript.api.get_eventos.return_value = []
    result = await transcript.send("eventos")
    assert "No hay eventos disponibles" in body(result)
    assert_menu(result)
    assert transcript.session.active_flow is None
    transcript.assert_single_responses(1)


@pytest.mark.parametrize("method", ["get_eventos", "get_inscripciones_evento_socio"])
@pytest.mark.parametrize("stage", ["entry", "choice", "confirm", "own", "withdraw_choice", "withdraw_confirm", "back"])
async def test_read_errors_finish_once_with_result_and_menu(transcript, method, stage):
    if stage in {"withdraw_choice", "withdraw_confirm"}:
        transcript.api.get_inscripciones_evento_socio.return_value = [inscription()]
    if stage != "entry":
        await transcript.send("eventos")
    if stage in {"confirm", "back"}:
        await transcript.send("evt_e1")
    if stage in {"withdraw_choice", "withdraw_confirm"}:
        await transcript.send(flow.ACTION_WITHDRAW)
    if stage == "withdraw_confirm":
        await transcript.send("evtw_i1")
    getattr(transcript.api, method).side_effect = ServicesAPIError("synthetic failure")
    selection = {
        "entry": "eventos",
        "choice": "evt_e1",
        "confirm": flow.CONFIRM_YES,
        "own": flow.ACTION_WITHDRAW,
        "withdraw_choice": "evtw_i1",
        "withdraw_confirm": flow.WITHDRAW_YES,
        "back": flow.CONFIRM_NO,
    }[stage]
    result = await transcript.send(selection)
    assert flow.GENERIC_ERROR in body(result)
    assert_menu(result)
    assert transcript.session.active_flow is None
    assert transcript.runs.call_args.args[0].outcome == "error"
    assert_no_writes(transcript)
    transcript.assert_single_responses(len(transcript.turns))


@pytest.mark.parametrize("error", [ServicesAPIError("synthetic"), ServicesAPIConflict("Ya estás inscripto/a.")])
async def test_enrollment_write_error_finishes_once(transcript, error):
    await transcript.send("eventos")
    await transcript.send("evt_e1")
    transcript.api.post_inscripcion_evento.side_effect = error
    result = await transcript.send(flow.CONFIRM_YES)
    assert (str(error) if isinstance(error, ServicesAPIConflict) else flow.GENERIC_ERROR) in body(result)
    assert_menu(result)
    assert transcript.session.active_flow is None
    transcript.assert_single_responses(3)


async def test_withdrawal_write_error_finishes_once(transcript):
    transcript.api.get_inscripciones_evento_socio.return_value = [inscription()]
    await transcript.send("eventos")
    await transcript.send(flow.ACTION_WITHDRAW)
    await transcript.send("evtw_i1")
    transcript.api.delete_inscripcion_evento.side_effect = ServicesAPIError("synthetic")
    result = await transcript.send(flow.WITHDRAW_YES)
    assert flow.GENERIC_ERROR in body(result)
    assert_menu(result)
    assert transcript.session.active_flow is None
    transcript.assert_single_responses(4)


@pytest.mark.parametrize("own", [False, True])
async def test_all_choices_paginated_with_navigation_within_ten_rows(transcript, own):
    events = [{**EVENT, "id": f"e{i}", "nombre": f"Evento {i}"} for i in range(25)]
    transcript.api.get_eventos.return_value = events
    if own:
        transcript.api.get_inscripciones_evento_socio.return_value = [inscription(f"i{i}", f"e{i}") for i in range(25)]
    await transcript.send("eventos")
    current = await transcript.send(flow.ACTION_WITHDRAW) if own else transcript.payloads[-1]
    seen = set()
    nav = flow.ACTION_BROWSE if own else flow.ACTION_WITHDRAW
    prefix = flow.WITHDRAW_PAGE_PREFIX if own else flow.EVENT_PAGE_PREFIX
    choice_prefix = flow.WITHDRAW_PREFIX if own else flow.EVENT_PREFIX
    while True:
        current_rows = rows(current)
        assert 1 <= len(current_rows) <= 10
        assert nav in {r["id"] for r in current_rows}
        assert len(body(current)) <= list_body_limit()
        seen.update(
            r["id"]
            for r in current_rows
            if r["id"].startswith(choice_prefix) and not r["id"].startswith(prefix) and r["id"] != nav
        )
        next_row = next((r for r in current_rows if r["title"] == "Más opciones"), None)
        if next_row is None:
            break
        current = await transcript.send(next_row["id"])
    assert seen == {f"{choice_prefix}{'i' if own else 'e'}{i}" for i in range(25)}
    previous = next(r for r in rows(current) if r["title"] == "Anteriores")
    await transcript.send(previous["id"])
    assert_no_writes(transcript)
    transcript.assert_single_responses(len(transcript.turns))


async def test_catalog_filters_full_past_cancelled_and_already_enrolled(transcript):
    transcript.api.get_eventos.return_value = [
        deepcopy(EVENT),
        {**EVENT, "id": "full", "cupoDisponible": 0},
        {**EVENT, "id": "past", "fecha": "2000-01-01"},
        {**EVENT, "id": "cancelled", "estado": "Cancelado"},
        {**EVENT, "id": "enrolled"},
        {**EVENT, "id": "unlimited", "cupoDisponible": None},
    ]
    transcript.api.get_inscripciones_evento_socio.return_value = [inscription(event_id="enrolled")]
    catalog = await transcript.send("eventos")
    assert {r["id"] for r in rows(catalog)} == {"evt_e1", "evt_unlimited", flow.ACTION_WITHDRAW}
    transcript.assert_single_responses(1)


async def test_full_summary_sorted_by_enrollment_date_not_event_date(transcript):
    transcript.api.get_eventos.return_value = [
        {**EVENT, "id": "old", "nombre": "Evento futuro", "fecha": "2099-12-31"},
        {**EVENT, "id": "new", "nombre": "Evento reciente", "fecha": "2000-01-01"},
        deepcopy(EVENT),
    ]
    transcript.api.get_inscripciones_evento_socio.return_value = [
        inscription("old", "old", fechaInscripcion="2026-01-01T00:00:00Z"),
        inscription("new", "new", fechaInscripcion="2026-11-01T00:00:00Z"),
    ]
    catalog = await transcript.send("eventos")
    text = body(catalog)
    assert text.index("Evento reciente") < text.index("Evento futuro")
    assert "2026-11-01T00:00:00Z" in text
    assert "2026-01-01T00:00:00Z" in text
    transcript.assert_single_responses(1)


@pytest.mark.parametrize("huge_latest", [False, True])
async def test_adaptive_summary_latest_or_count_and_all_details_still_accessible(transcript, huge_latest):
    events = [
        {**EVENT, "id": f"e{i}", "nombre": ("X" * 1200 if huge_latest and i == 0 else f"Inscripción {i}")}
        for i in range(12)
    ]
    transcript.api.get_eventos.return_value = events
    transcript.api.get_inscripciones_evento_socio.return_value = [
        inscription(f"i{i}", f"e{i}", fechaInscripcion=f"2026-10-{25 - i:02d}T00:00:00Z") for i in reversed(range(12))
    ]
    catalog = await transcript.send("eventos")
    assert len(body(catalog)) <= list_body_limit()
    if huge_latest:
        assert "Tenés 12 inscripciones" in body(catalog)
    else:
        assert "Inscripción 0" in body(catalog)
        assert "otras 11 inscripciones" in body(catalog)
        assert "Inscripción 1" not in body(catalog)
    own = await transcript.send(flow.ACTION_WITHDRAW)
    assert rows(own)[0]["id"] == "evtw_i0"
    detail = await transcript.send("evtw_i3")
    assert "Inscripción 3" in body(detail)
    assert "2026-10-22T00:00:00Z" in body(detail)
    assert_no_writes(transcript)
    transcript.assert_single_responses(3)


async def test_historical_cancelled_and_missing_event_details_remain_accessible(transcript):
    transcript.api.get_eventos.return_value = [{**EVENT, "fecha": "2000-01-01"}]
    transcript.api.get_inscripciones_evento_socio.return_value = [
        inscription(estado="Cancelada"),
        inscription("missing", "archived-event"),
    ]
    await transcript.send("eventos")
    own = await transcript.send(flow.ACTION_WITHDRAW)
    assert {r["id"] for r in rows(own)} == {"evtw_i1", "evtw_missing", flow.ACTION_BROWSE}
    cancelled = await transcript.send("evtw_i1")
    assert "2000-01-01" in body(cancelled)
    assert "Cancelada" in body(cancelled)
    assert flow.WITHDRAW_YES not in buttons(cancelled)
    await transcript.send(flow.WITHDRAW_YES)
    await transcript.send(flow.WITHDRAW_NO)
    missing = await transcript.send("evtw_missing")
    assert "archived-event" in body(missing)
    assert "detalle no disponible" in body(missing)
    assert "2026-10-01T10:00:00Z" in body(missing)
    assert_no_writes(transcript)
    transcript.assert_single_responses(6)


@pytest.mark.parametrize("length", [100, 700, 8500])
async def test_description_only_overflows_when_necessary_and_is_lossless(transcript, length):
    description = ("Narrativa completa áéíóú\n" * 400)[:length]
    event = {**EVENT, "descripcion": description, "costoSocio": "123.456", "costoNoSocio": "456.78"}
    transcript.api.get_eventos.return_value = [event]
    await transcript.send("eventos")
    confirmation = await transcript.send("evt_e1")
    expected = flow._details(event) + "\n\n" + description + "\n\n¿Querés inscribirte en este evento?"
    emitted = transcript.turns[-1]
    if len(expected) <= BUTTON_BODY_LIMIT:
        assert len(emitted) == 1
        assert body(confirmation) == expected
    else:
        overflow = [p["text"]["body"] for p in emitted[:-1]]
        assert "".join(overflow) == description
        assert all(len(text) <= 4096 for text in overflow)
        assert description not in body(confirmation)
    assert len(body(confirmation)) <= BUTTON_BODY_LIMIT
    for value in ["Encuentro", EVENT["fecha"], "09:00:00", "10:00:00", "Quincho", "$123.456", "$456.78"]:
        assert value in body(confirmation)
    assert buttons(confirmation) == [flow.CONFIRM_YES, flow.CONFIRM_NO, MENU_ID]
    assert_no_writes(transcript)
    await transcript.send(text="¿cómo?")
    assert len(transcript.turns[-1]) == 1
    assert "Por favor" in body(transcript.payloads[-1])


@pytest.mark.parametrize("price", [0, 125.75, "123.456"])
async def test_member_price_preserves_zero_cents_and_precision(transcript, price):
    transcript.api.get_eventos.return_value = [{**EVENT, "costoSocio": price}]
    catalog = await transcript.send("eventos")
    event_row = next(r for r in rows(catalog) if r["id"] == "evt_e1")
    assert f"${price}" in event_row["description"]
    confirmation = await transcript.send("evt_e1")
    assert f"Costo socio: ${price}" in body(confirmation)
    transcript.assert_single_responses(2)
