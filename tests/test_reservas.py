from datetime import date, timedelta

import pytest

from app.flow_ui import MENU_ID
from app.flows.reservas import (
    ACTION_LIST,
    ACTION_NEW,
    CANCEL_NO,
    CANCEL_YES,
    CONFIRM_NO,
    CONFIRM_YES,
    RESERVA_PAGE_PREFIX,
    SPACE_PAGE_PREFIX,
    ReservasStep,
    _parse_time_range,
    _reservable,
)
from app.services_client import ServicesAPIConflict, ServicesAPIError
from tests.transcript import SPACE


@pytest.mark.parametrize(
    ("space", "expected"),
    [
        ({"estado": "Disponible"}, True),
        ({"estado": "Disponible", "reservable": True}, True),
        ({"estado": "Disponible", "reservable": False}, False),
        ({"estado": "Mantenimiento", "reservable": True}, False),
        ({"estado": "Mantenimiento"}, False),
    ],
)
def test_reservable_requires_availability_and_no_explicit_opt_out(space, expected):
    assert _reservable(space) is expected


def test_reservable_filter_preserves_order():
    spaces = [
        {"id": "e1", "estado": "Disponible"},
        {"id": "e2", "estado": "Disponible", "reservable": True},
        {"id": "e3", "estado": "Disponible", "reservable": False},
        {"id": "e4", "estado": "Mantenimiento", "reservable": True},
    ]
    assert [space["id"] for space in spaces if _reservable(space)] == ["e1", "e2"]


def rows(payload):
    return [row for section in payload["interactive"]["action"]["sections"] for row in section["rows"]]


def button_ids(payload):
    return [button["reply"]["id"].split(":", 1)[0] for button in payload["interactive"]["action"]["buttons"]]


def body(payload):
    return payload["text"]["body"] if payload["type"] == "text" else payload["interactive"]["body"]["text"]


def assert_no_writes(transcript):
    transcript.api.post_reserva.assert_not_awaited()
    transcript.api.delete_reserva.assert_not_awaited()


def assert_payload_limits(payloads):
    for payload in payloads:
        assert len(body(payload)) <= (4096 if payload["type"] == "text" else 1024)
        if payload["type"] == "interactive":
            interactive = payload["interactive"]
            if interactive["type"] == "list":
                assert 1 <= len(rows(payload)) <= 10
            else:
                assert 1 <= len(button_ids(payload)) <= 3


def future_date(days=2):
    return (date.today() + timedelta(days=days)).strftime("%d/%m/%Y")


def reservation(**overrides):
    return {
        "id": "r1",
        "nombreEspacio": "Quincho",
        "fecha": (date.today() + timedelta(days=2)).isoformat(),
        "estado": "Pendiente",
        "horaInicio": "10:00:00",
        "horaFin": "12:00:00",
        **overrides,
    }


async def reach_confirmation(transcript, *, people="4", reason="Cumpleaños"):
    await transcript.send("reservas")
    await transcript.send("esp_s1")
    await transcript.send(text=future_date())
    await transcript.send(text="10-12")
    await transcript.send(text=people)
    await transcript.send(text=reason)


async def reach_cancellation(transcript):
    transcript.api.get_reservas_socio.return_value = [reservation()]
    await transcript.send("reservas")
    await transcript.send(ACTION_LIST)
    await transcript.send("res_r1")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10-12", ("10:00:00", "12:00:00")),
        ("10:00–12:00", ("10:00:00", "12:00:00")),
        ("de10a12", ("10:00:00", "12:00:00")),
        (" DE 10 A 12 ", ("10:00:00", "12:00:00")),
        ("10.30/12.30", ("10:30:00", "12:30:00")),
        ("10:30,12:45", ("10:30:00", "12:45:00")),
        ("10\n12", ("10:00:00", "12:00:00")),
        ("10 12", ("10:00:00", "12:00:00")),
        ("10:30   12:30", ("10:30:00", "12:30:00")),
        ("10 hasta 12", ("10:00:00", "12:00:00")),
        ("0:00—23:59", ("00:00:00", "23:59:00")),
        ("09.05 - 09:06", ("09:05:00", "09:06:00")),
    ],
)
def test_parse_range_accepts_unambiguous_formats(text, expected):
    assert _parse_time_range(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "10",
        "1012",
        "10-12-14",
        "10 12 14",
        "10:00-12:00 2",
        "2 personas 10-12",
        "10:60-12:00",
        "10:99-12:00",
        "24-25",
        "23:00-24:00",
        "-1-12",
        "10--12",
        "12-10",
        "10-10",
        "23-1",
        "10pm-12am",
        "10 am - 12 pm",
        "10-12 PM",
        "10 a. m. a 12 p. m.",
        "10:3-12:00",
        "10.5-12.5",
        "10:00:00-12:00:00",
        "10,30/12,30",
        "10/12/2026",
        "mañana de 10 a 12",
        "10 a 12 mañana",
        "10h-12h",
        "١٠-١٢",
        "10 + 12",
        "10 a 12 o 14",
    ],
)
def test_parse_range_rejects_invalid_ambiguous_or_extra_data(text):
    assert _parse_time_range(text) is None


async def test_creation_uses_seven_router_turns_and_preserves_api_contract(transcript):
    await reach_confirmation(transcript)
    assert [row["id"] for row in rows(transcript.payloads[0])] == ["esp_s1", ACTION_LIST, MENU_ID]
    assert button_ids(transcript.payloads[-1]) == [CONFIRM_YES, CONFIRM_NO, MENU_ID]
    summary = transcript.bodies[-1]
    for critical in ("Quincho", future_date(), "10:00", "12:00", "Personas: 4", "Cumpleaños", "$100"):
        assert critical in summary
    assert_no_writes(transcript)
    await transcript.send(CONFIRM_YES)
    transcript.api.post_reserva.assert_awaited_once_with(
        "s1", "s1", (date.today() + timedelta(days=2)).isoformat(), "10:00:00", "12:00:00", 4, "Cumpleaños"
    )
    assert "Reserva registrada" in transcript.bodies[-1]
    assert "activities" in {row["id"] for row in rows(transcript.payloads[-1])}
    assert transcript.session.active_flow is None
    assert transcript.session.flow_state is None
    transcript.assert_single_responses(7)
    run = transcript.runs.call_args.args[0]
    assert (run.task, run.outcome, run.incoming_messages, run.outbound_attempts) == ("create", "success", 7, 7)
    assert_payload_limits(transcript.payloads)


async def test_cancellation_uses_four_router_turns_and_disambiguating_times(transcript):
    await reach_cancellation(transcript)
    assert "10:00 - 12:00" in rows(transcript.payloads[1])[0]["description"]
    assert "10:00 - 12:00" in transcript.bodies[-1]
    assert button_ids(transcript.payloads[-1]) == [CANCEL_YES, CANCEL_NO, MENU_ID]
    assert_no_writes(transcript)
    await transcript.send(CANCEL_YES)
    transcript.api.delete_reserva.assert_awaited_once_with("r1", "s1")
    assert "Reserva cancelada." in transcript.bodies[-1]
    assert transcript.session.active_flow is None
    transcript.assert_single_responses(4)
    run = transcript.runs.call_args.args[0]
    assert (run.task, run.outcome, run.incoming_messages, run.outbound_attempts) == ("cancel", "success", 4, 4)


async def test_catalog_preserves_independent_reservable_filter(transcript):
    transcript.api.get_espacios.return_value = [
        SPACE,
        {**SPACE, "id": "yes", "reservable": True},
        {**SPACE, "id": "no", "reservable": False},
        {**SPACE, "id": "busy", "estado": "Mantenimiento", "reservable": True},
        {**SPACE, "id": "busy_missing", "estado": "Mantenimiento"},
    ]
    await transcript.send("reservas")
    assert {row["id"] for row in rows(transcript.payloads[-1])} == {"esp_s1", "esp_yes", ACTION_LIST, MENU_ID}
    assert_no_writes(transcript)


@pytest.mark.parametrize("kind", ["spaces", "reservations"])
async def test_every_choice_is_reachable_with_pagination_and_persistent_navigation(transcript, kind):
    count = 31
    if kind == "spaces":
        transcript.api.get_espacios.return_value = [{**SPACE, "id": str(i)} for i in range(count)]
        prefix, option_prefix, extra = SPACE_PAGE_PREFIX, "esp_", ACTION_LIST
    else:
        transcript.api.get_reservas_socio.return_value = [reservation(id=str(i)) for i in range(count)]
        prefix, option_prefix, extra = RESERVA_PAGE_PREFIX, "res_", ACTION_NEW
    await transcript.send("reservas")
    if kind == "reservations":
        await transcript.send(ACTION_LIST)
    seen = set()
    while True:
        current = rows(transcript.payloads[-1])
        ids = {row["id"] for row in current}
        assert {extra, MENU_ID} <= ids
        assert len(current) <= 10
        seen.update(iid for iid in ids if iid.startswith(option_prefix) and not iid.startswith(prefix))
        next_row = next((row for row in current if row["title"] == "Más opciones"), None)
        if next_row is None:
            break
        await transcript.send(next_row["id"])
    assert seen == {f"{option_prefix}{i}" for i in range(count)}
    await transcript.send(f"{prefix}0")
    assert f"{option_prefix}0" in {row["id"] for row in rows(transcript.payloads[-1])}
    transcript.assert_single_responses(len(transcript.turns))
    assert_no_writes(transcript)
    assert_payload_limits(transcript.payloads)


async def test_creation_back_clears_dependent_state_and_returns_catalog_without_writes(transcript):
    await reach_confirmation(transcript)
    await transcript.send(CONFIRM_NO)
    state = transcript.session.flow_state
    assert state.step == ReservasStep.AWAITING_SPACE
    assert all(
        getattr(state, key) is None
        for key in (
            "selected_space",
            "selected_date",
            "start_time",
            "end_time",
            "cant_personas",
            "motivo",
            "selected_reserva",
        )
    )
    assert ACTION_LIST in {row["id"] for row in rows(transcript.payloads[-1])}
    await transcript.send("esp_s1")
    assert transcript.session.flow_state.step == ReservasStep.AWAITING_DATE
    transcript.assert_single_responses(8)
    assert_no_writes(transcript)


async def test_cancel_back_returns_own_list_and_catalog_back_route_works(transcript):
    await reach_cancellation(transcript)
    await transcript.send(CANCEL_NO)
    assert transcript.session.flow_state.step == ReservasStep.VIEWING_RESERVAS
    assert transcript.session.flow_state.selected_reserva is None
    assert transcript.session.run.task == "view"
    await transcript.send(ACTION_NEW)
    assert transcript.session.flow_state.step == ReservasStep.AWAITING_SPACE
    assert transcript.session.run.task == "create"
    transcript.assert_single_responses(5)
    assert_no_writes(transcript)


@pytest.mark.parametrize("stage", ["catalog", "create_confirmation", "cancel_confirmation", "own_list"])
async def test_explicit_main_menu_abandons_without_writes(transcript, stage):
    if stage == "create_confirmation":
        await reach_confirmation(transcript)
    elif stage == "cancel_confirmation":
        await reach_cancellation(transcript)
    else:
        await transcript.send("reservas")
        if stage == "own_list":
            await transcript.send(ACTION_LIST)
    await transcript.send(MENU_ID)
    assert transcript.session.active_flow is None
    assert transcript.session.flow_state is None
    assert "activities" in {row["id"] for row in rows(transcript.payloads[-1])}
    transcript.assert_single_responses(len(transcript.turns))
    assert_no_writes(transcript)


@pytest.mark.parametrize("stage", ["catalog", "create_confirmation", "cancel_confirmation", "own_list"])
@pytest.mark.parametrize("bad", ["unexpected", "esp_page_bad", "res_page_bad", "text"])
async def test_invalid_choices_redisplay_current_controls_once_with_error(transcript, stage, bad):
    if stage == "create_confirmation":
        await reach_confirmation(transcript)
    elif stage == "cancel_confirmation":
        await reach_cancellation(transcript)
    else:
        await transcript.send("reservas")
        if stage == "own_list":
            await transcript.send(ACTION_LIST)
    previous = transcript.payloads[-1]
    step = transcript.session.flow_state.step
    await transcript.send(text="No sé") if bad == "text" else await transcript.send(bad)
    assert transcript.payloads[-1]["interactive"]["action"] == previous["interactive"]["action"]
    assert "Por favor" in transcript.bodies[-1]
    assert transcript.session.flow_state.step == step
    transcript.assert_single_responses(len(transcript.turns))
    assert_no_writes(transcript)


@pytest.mark.parametrize("change", [{"reservable": False}, {"estado": "Mantenimiento"}])
async def test_space_becoming_unreservable_at_selection_refreshes_in_one_response(transcript, change):
    await transcript.send("reservas")
    transcript.api.get_espacios.return_value = [{**SPACE, **change}, {**SPACE, "id": "s2"}]
    await transcript.send("esp_s1")
    assert "ya no está disponible" in transcript.bodies[-1]
    assert {row["id"] for row in rows(transcript.payloads[-1])} == {"esp_s2", ACTION_LIST, MENU_ID}
    assert transcript.session.flow_state.selected_space is None
    transcript.assert_single_responses(2)
    assert_no_writes(transcript)


@pytest.mark.parametrize("change", [{"reservable": False}, {"estado": "Mantenimiento"}])
async def test_space_becoming_unreservable_at_confirmation_never_writes(transcript, change):
    await reach_confirmation(transcript)
    transcript.api.get_espacios.return_value = [{**SPACE, **change}, {**SPACE, "id": "s2"}]
    await transcript.send(CONFIRM_YES)
    assert "ya no está disponible" in transcript.bodies[-1]
    assert transcript.session.flow_state.step == ReservasStep.AWAITING_SPACE
    assert transcript.session.flow_state.motivo is None
    transcript.assert_single_responses(7)
    assert_no_writes(transcript)


async def test_unknown_space_id_refreshes_without_selecting_newly_added_id(transcript):
    await transcript.send("reservas")
    transcript.api.get_espacios.return_value = [SPACE, {**SPACE, "id": "s2"}]
    await transcript.send("esp_s2")
    assert "lista actualizada" in transcript.bodies[-1]
    assert transcript.session.flow_state.step == ReservasStep.AWAITING_SPACE
    transcript.assert_single_responses(2)
    assert_no_writes(transcript)


@pytest.mark.parametrize("at_confirm", [False, True])
async def test_stale_reservation_refreshes_once_without_cancellation(transcript, at_confirm):
    transcript.api.get_reservas_socio.return_value = [reservation()]
    await transcript.send("reservas")
    await transcript.send(ACTION_LIST)
    if at_confirm:
        await transcript.send("res_r1")
    transcript.api.get_reservas_socio.return_value = [reservation(estado="Cancelada"), reservation(id="r2")]
    await transcript.send(CANCEL_YES if at_confirm else "res_r1")
    assert "ya no está disponible" in transcript.bodies[-1]
    assert {row["id"] for row in rows(transcript.payloads[-1])} == {"res_r2", ACTION_NEW, MENU_ID}
    assert transcript.session.flow_state.selected_reserva is None
    transcript.assert_single_responses(len(transcript.turns))
    assert_no_writes(transcript)


@pytest.mark.parametrize("offset", [-1, 91])
async def test_dates_outside_ninety_day_window_do_not_advance(transcript, offset):
    await transcript.send("reservas")
    await transcript.send("esp_s1")
    await transcript.send(text=future_date(offset))
    assert transcript.session.flow_state.step == ReservasStep.AWAITING_DATE
    assert transcript.session.flow_state.selected_date is None
    transcript.assert_single_responses(3)
    assert_no_writes(transcript)


@pytest.mark.parametrize("offset", [0, 90])
async def test_date_window_boundaries_are_allowed(transcript, offset):
    await transcript.send("reservas")
    await transcript.send("esp_s1")
    await transcript.send(text=future_date(offset))
    assert transcript.session.flow_state.step == ReservasStep.AWAITING_TIME_RANGE
    transcript.assert_single_responses(3)


@pytest.mark.parametrize("text", ["12-10", "10-12-14", "10:70-12:00", "23-1", "10 pm - 12 am"])
async def test_invalid_range_reprompts_once_without_advancing(transcript, text):
    await transcript.send("reservas")
    await transcript.send("esp_s1")
    await transcript.send(text=future_date())
    await transcript.send(text=text)
    assert transcript.session.flow_state.step == ReservasStep.AWAITING_TIME_RANGE
    assert transcript.session.flow_state.start_time is None
    assert "exactamente dos horas" in transcript.bodies[-1]
    transcript.assert_single_responses(4)
    assert_no_writes(transcript)


@pytest.mark.parametrize(
    "capacity,people,valid",
    [
        (3, "4", False),
        (3, "3", True),
        (0, "1", False),
        (None, "4", True),
        (30, "0", False),
        (30, "-1", False),
        (30, "4.5", False),
        (30, "501", False),
    ],
)
async def test_people_respect_actual_known_capacity(transcript, capacity, people, valid):
    transcript.api.get_espacios.return_value = [{**SPACE, "capacidad": capacity}]
    await transcript.send("reservas")
    await transcript.send("esp_s1")
    await transcript.send(text=future_date())
    await transcript.send(text="10-12")
    await transcript.send(text=people)
    expected = ReservasStep.AWAITING_REASON if valid else ReservasStep.AWAITING_PEOPLE
    assert transcript.session.flow_state.step == expected
    transcript.assert_single_responses(5)
    assert_no_writes(transcript)


async def test_capacity_reduction_at_confirmation_reprompts_people_without_writes(transcript):
    await reach_confirmation(transcript)
    transcript.api.get_espacios.return_value = [{**SPACE, "capacidad": 2}]
    await transcript.send(CONFIRM_YES)
    assert "capacidad del espacio cambió" in transcript.bodies[-1]
    assert transcript.session.flow_state.step == ReservasStep.AWAITING_PEOPLE
    assert transcript.session.flow_state.cant_personas is None
    assert transcript.session.flow_state.motivo is None
    transcript.assert_single_responses(7)
    assert_no_writes(transcript)


async def test_changed_price_requires_fresh_explicit_confirmation(transcript):
    await reach_confirmation(transcript)
    transcript.api.get_espacios.return_value = [{**SPACE, "costo": 123.45}]
    await transcript.send(CONFIRM_YES)
    assert "$123.45" in transcript.bodies[-1]
    assert "cambiaron" in transcript.bodies[-1]
    assert_no_writes(transcript)
    await transcript.send(CONFIRM_YES)
    transcript.api.post_reserva.assert_awaited_once()
    transcript.assert_single_responses(8)


async def test_changed_reservation_time_requires_fresh_explicit_cancellation(transcript):
    await reach_cancellation(transcript)
    transcript.api.get_reservas_socio.return_value = [reservation(horaInicio="11:00:00")]
    await transcript.send(CANCEL_YES)
    assert "11:00 - 12:00" in transcript.bodies[-1]
    assert_no_writes(transcript)
    await transcript.send(CANCEL_YES)
    transcript.api.delete_reserva.assert_awaited_once_with("r1", "s1")
    transcript.assert_single_responses(5)


@pytest.mark.parametrize("mode", ["empty", "error"])
@pytest.mark.parametrize("catalog", ["spaces", "reservations"])
async def test_empty_and_api_errors_finish_in_one_menu_response(transcript, mode, catalog):
    api = transcript.api.get_espacios if catalog == "spaces" else transcript.api.get_reservas_socio
    if mode == "empty":
        api.return_value = []
    else:
        api.side_effect = ServicesAPIError("synthetic API error")
    await transcript.send("reservas")
    if catalog == "reservations":
        await transcript.send(ACTION_LIST)
    if catalog == "spaces" and mode == "empty":
        assert transcript.session.active_flow == "reservas"
        assert ACTION_LIST in {row["id"] for row in rows(transcript.payloads[-1])}
    else:
        assert transcript.session.active_flow is None
        assert "activities" in {row["id"] for row in rows(transcript.payloads[-1])}
    expected = (
        "problema técnico" if mode == "error" else ("No hay espacios" if catalog == "spaces" else "No tenés reservas")
    )
    assert expected in transcript.bodies[-1]
    transcript.assert_single_responses(len(transcript.turns))
    assert_no_writes(transcript)


async def test_existing_reservations_remain_accessible_when_no_space_is_available(transcript):
    transcript.api.get_espacios.return_value = []
    await transcript.send("reservas")
    await transcript.send(ACTION_LIST)
    assert "res_r1" in {row["id"] for row in rows(transcript.payloads[-1])}
    transcript.assert_single_responses(2)


@pytest.mark.parametrize("stage", ["space_selection", "create_refresh", "reserve_selection", "cancel_refresh"])
async def test_refresh_api_errors_finish_without_writes(transcript, stage):
    if stage == "create_refresh":
        await reach_confirmation(transcript)
        api, selection = transcript.api.get_espacios, CONFIRM_YES
    elif stage == "cancel_refresh":
        await reach_cancellation(transcript)
        api, selection = transcript.api.get_reservas_socio, CANCEL_YES
    else:
        await transcript.send("reservas")
        if stage == "reserve_selection":
            await transcript.send(ACTION_LIST)
            api, selection = transcript.api.get_reservas_socio, "res_r1"
        else:
            api, selection = transcript.api.get_espacios, "esp_s1"
    api.side_effect = ServicesAPIError("synthetic refresh failure")
    await transcript.send(selection)
    assert "problema técnico" in transcript.bodies[-1]
    assert transcript.session.active_flow is None
    transcript.assert_single_responses(len(transcript.turns))
    assert_no_writes(transcript)


@pytest.mark.parametrize("error", [ServicesAPIConflict("Ya está reservado."), ServicesAPIError("synthetic failure")])
async def test_creation_api_failure_shares_result_with_menu(transcript, error):
    await reach_confirmation(transcript)
    transcript.api.post_reserva.side_effect = error
    await transcript.send(CONFIRM_YES)
    expected = "Ya está reservado." if isinstance(error, ServicesAPIConflict) else "problema técnico"
    assert expected in transcript.bodies[-1]
    assert transcript.session.active_flow is None
    transcript.assert_single_responses(7)
    transcript.api.post_reserva.assert_awaited_once()


async def test_cancel_api_failure_shares_result_with_menu(transcript):
    await reach_cancellation(transcript)
    transcript.api.delete_reserva.side_effect = ServicesAPIError("synthetic failure")
    await transcript.send(CANCEL_YES)
    assert "problema técnico" in transcript.bodies[-1]
    assert transcript.session.active_flow is None
    transcript.assert_single_responses(4)


async def test_long_confirmation_is_lossless_and_keeps_all_critical_fields(transcript):
    name = "Espacio " + "Á" * 1200
    reason = "Celebración " + "ñ" * 5000
    transcript.api.get_espacios.return_value = [{**SPACE, "nombre": name, "costo": 123.45}]
    await reach_confirmation(transcript, reason=reason)
    combined = "".join(body(payload) for payload in transcript.turns[-1])
    for critical in (name, reason, future_date(), "10:00", "12:00", "Personas: 4", "$123.45"):
        assert critical in combined
    assert button_ids(transcript.payloads[-1]) == [CONFIRM_YES, CONFIRM_NO, MENU_ID]
    assert_payload_limits(transcript.payloads)
    assert_no_writes(transcript)


async def test_old_confirmation_after_completion_cannot_replay_creation(transcript):
    await reach_confirmation(transcript)
    await transcript.send(CONFIRM_YES)
    await transcript.send(CONFIRM_YES)
    transcript.api.post_reserva.assert_awaited_once()
    transcript.assert_single_responses(8)
