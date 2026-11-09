"""Activity budgets and safety through the real router and outbound payload wrapper."""

from copy import deepcopy

import pytest

from app.flow_ui import MENU_ID, list_body_limit
from app.flows import activities as flow
from app.services_client import ServicesAPIError
from tests.test_activities import inscripcion
from tests.transcript import ACTIVITY, CLASS


def rows(payload):
    return payload["interactive"]["action"]["sections"][0]["rows"]


def buttons(payload):
    return {
        button["reply"]["id"].split(":", 1)[0]: button["reply"]["title"]
        for button in payload["interactive"]["action"]["buttons"]
    }


def assert_payload_limits(transcript):
    for payload in transcript.payloads:
        if payload["type"] == "text":
            assert len(payload["text"]["body"]) <= 4096
            continue
        interactive = payload["interactive"]
        assert len(interactive["body"]["text"]) <= list_body_limit()
        if interactive["type"] == "list":
            assert len(rows(payload)) <= 10
            assert all(len(row["title"]) <= 24 and len(row.get("description", "")) <= 72 for row in rows(payload))
        else:
            assert len(buttons(payload)) <= 3


@pytest.mark.parametrize(
    "count,budget,task", [(0, 3, "enroll_general"), (1, 3, "enroll_class"), (2, 4, "enroll_class")]
)
async def test_enrollment_message_budgets(transcript, count, budget, task):
    transcript.api.get_instancias_actividad.return_value = [{**CLASS, "id": f"c{i + 1}"} for i in range(count)]
    catalog = await transcript.send("activities")
    assert flow.ACTION_CANCEL in {row["id"] for row in rows(catalog)}
    await transcript.send("act_a1")
    if count > 1:
        await transcript.send("clase_c1")
    assert buttons(transcript.payloads[-1]) == {
        flow.CONFIRM_YES: "Confirmar",
        flow.CONFIRM_NO: "Volver",
        MENU_ID: "Menú principal",
    }
    assert transcript.session.run.task == task
    await transcript.send(flow.CONFIRM_YES)
    transcript.assert_single_responses(budget)
    assert "¡Listo!" in transcript.bodies[-1]
    assert {row["id"] for row in rows(transcript.payloads[-1])} >= {"activities", "reservas"}
    assert transcript.session.active_flow is None
    expected = {"instancia_id": "c1"} if count else {}
    transcript.api.post_inscripcion.assert_awaited_once_with("s1", "a1", **expected)
    assert_payload_limits(transcript)


@pytest.mark.parametrize("generic", [False, True])
async def test_withdrawal_four_messages_and_correct_endpoint(transcript, generic):
    enrollment = inscripcion()
    if generic:
        enrollment.update(instanciaId=None, clases=[], fechaFin=None)
    transcript.api.get_inscripciones_socio.return_value = [enrollment]
    transcript.api.get_inscripcion.return_value = enrollment
    await transcript.send("activities")
    await transcript.send(flow.ACTION_CANCEL)
    await transcript.send("cancel_general_i1" if generic else "cancel_clase_c1")
    assert MENU_ID in buttons(transcript.payloads[-1])
    assert transcript.session.run.task == ("withdraw_general" if generic else "withdraw_class")
    await transcript.send(flow.CANCEL_CONFIRM_YES)
    transcript.assert_single_responses(4)
    if generic:
        transcript.api.delete_inscripcion.assert_awaited_once_with("i1")
        transcript.api.dar_baja_instancia.assert_not_awaited()
    else:
        transcript.api.dar_baja_instancia.assert_awaited_once_with("c1", "s1")
        transcript.api.delete_inscripcion.assert_not_awaited()
    assert_payload_limits(transcript)


@pytest.mark.parametrize("kind", ["actividades", "clases", "inscripciones"])
async def test_pagination_reaches_every_option_and_keeps_navigation(transcript, kind):
    if kind == "actividades":
        transcript.api.get_actividades.return_value = [{**ACTIVITY, "id": f"a{i}"} for i in range(25)]
        expected = {f"act_a{i}" for i in range(25)}
        navigation = flow.ACTION_CANCEL
    elif kind == "clases":
        transcript.api.get_instancias_actividad.return_value = [{**CLASS, "id": f"c{i}"} for i in range(25)]
        expected = {f"clase_c{i}" for i in range(25)}
        navigation = flow.ACTION_ENROLL
    else:
        transcript.api.get_inscripciones_socio.return_value = [
            inscripcion(f"i{i}", {**CLASS, "id": f"c{i}"}) for i in range(25)
        ]
        expected = {f"cancel_clase_c{i}" for i in range(25)}
        navigation = flow.ACTION_ENROLL
    await transcript.send("activities")
    if kind == "clases":
        await transcript.send("act_a1")
    elif kind == "inscripciones":
        await transcript.send(flow.ACTION_CANCEL)
    found = set()
    for page in range(4):
        if page:
            await transcript.send(f"{flow.PAGINA_PREFIX}{kind}:{page}")
        current = {row["id"] for row in rows(transcript.payloads[-1])}
        assert navigation in current
        found |= current & expected
    assert found == expected
    await transcript.send(f"{flow.PAGINA_PREFIX}{kind}:2")
    assert f"{flow.PAGINA_PREFIX}{kind}:1" in {row["id"] for row in rows(transcript.payloads[-1])}
    transcript.assert_single_responses(len(transcript.turns))
    assert_payload_limits(transcript)


@pytest.mark.parametrize("count", [0, 1, 2])
async def test_enrollment_back_clears_selection_without_writing(transcript, count):
    transcript.api.get_instancias_actividad.return_value = [{**CLASS, "id": f"c{i + 1}"} for i in range(count)]
    await transcript.send("activities")
    await transcript.send("act_a1")
    if count > 1:
        await transcript.send("clase_c1")
    await transcript.send(flow.CONFIRM_NO)
    state = transcript.session.flow_state
    assert state.instancia_elegida is None
    if count > 1:
        assert state.step == flow.ActivitiesStep.ELEGIR_INSTANCIA
        await transcript.send(flow.ACTION_ENROLL)
        state = transcript.session.flow_state
    assert state.selected_activity is None
    assert state.selected_inscription is None
    assert state.instancias_disponibles == {}
    assert state.step == flow.ActivitiesStep.AWAITING_CHOICE
    await transcript.send(flow.CONFIRM_YES)  # stale confirm from before Volver
    transcript.api.post_inscripcion.assert_not_awaited()
    transcript.api.delete_inscripcion.assert_not_awaited()
    transcript.assert_single_responses(len(transcript.turns))


async def test_withdrawal_back_returns_own_list_then_catalog(transcript):
    transcript.api.get_inscripciones_socio.return_value = [inscripcion()]
    await transcript.send("activities")
    await transcript.send(flow.ACTION_CANCEL)
    await transcript.send("cancel_clase_c1")
    await transcript.send(flow.CANCEL_CONFIRM_NO)
    state = transcript.session.flow_state
    assert state.selected_inscription is None
    assert state.step == flow.ActivitiesStep.AWAITING_CANCEL_CHOICE
    assert flow.ACTION_ENROLL in {row["id"] for row in rows(transcript.payloads[-1])}
    await transcript.send(flow.CANCEL_CONFIRM_YES)
    assert "Elegí una inscripción" in transcript.bodies[-1]
    await transcript.send(flow.ACTION_ENROLL)
    assert transcript.session.flow_state.selected_inscription is None
    transcript.api.dar_baja_instancia.assert_not_awaited()
    transcript.api.delete_inscripcion.assert_not_awaited()
    transcript.assert_single_responses(6)


@pytest.mark.parametrize("withdraw", [False, True])
async def test_menu_from_both_confirmations_never_writes(transcript, withdraw):
    transcript.api.get_inscripciones_socio.return_value = [inscripcion()] if withdraw else []
    await transcript.send("activities")
    if withdraw:
        await transcript.send(flow.ACTION_CANCEL)
        await transcript.send("cancel_clase_c1")
    else:
        await transcript.send("act_a1")
    await transcript.send(MENU_ID)
    assert transcript.session.active_flow is None
    assert transcript.session.flow_state is None
    await transcript.send(flow.CANCEL_CONFIRM_YES if withdraw else flow.CONFIRM_YES)
    transcript.api.post_inscripcion.assert_not_awaited()
    transcript.api.dar_baja_instancia.assert_not_awaited()
    transcript.api.delete_inscripcion.assert_not_awaited()
    transcript.assert_single_responses(len(transcript.turns))


@pytest.mark.parametrize("stage", ["catalog", "classes", "enroll_confirm", "own", "withdraw_confirm"])
async def test_invalid_text_repeats_interactive_prompt_once(transcript, stage):
    transcript.api.get_instancias_actividad.return_value = [CLASS, {**CLASS, "id": "c2"}]
    if stage in {"own", "withdraw_confirm"}:
        transcript.api.get_inscripciones_socio.return_value = [inscripcion()]
    await transcript.send("activities")
    if stage in {"classes", "enroll_confirm"}:
        await transcript.send("act_a1")
    if stage == "enroll_confirm":
        await transcript.send("clase_c1")
    if stage in {"own", "withdraw_confirm"}:
        await transcript.send(flow.ACTION_CANCEL)
    if stage == "withdraw_confirm":
        await transcript.send("cancel_clase_c1")
    previous = transcript.payloads[-1]["interactive"]["action"]
    await transcript.send(text="respuesta inválida")
    assert transcript.payloads[-1]["type"] == "interactive"
    assert transcript.payloads[-1]["interactive"]["action"] == previous
    assert transcript.bodies[-1] != transcript.bodies[-2]
    transcript.assert_single_responses(len(transcript.turns))
    transcript.api.post_inscripcion.assert_not_awaited()
    transcript.api.delete_inscripcion.assert_not_awaited()


async def test_full_summary_keeps_baja_and_suspended_details(transcript):
    enrollment = inscripcion()
    enrollment["clases"] = [
        {**enrollment["clases"][0], "estadoClase": "Baja"},
        {**enrollment["clases"][0], "instanciaId": "c2", "estadoClase": "Suspendida"},
    ]
    transcript.api.get_inscripciones_socio.return_value = [enrollment]
    await transcript.send("activities")
    assert "baja registrada" in transcript.bodies[-1]
    assert "suspendida" in transcript.bodies[-1]
    assert "11/05/2099" in transcript.bodies[-1]
    await transcript.send(flow.ACTION_CANCEL)
    for class_id, label in [("c1", "baja registrada"), ("c2", "suspendida")]:
        await transcript.send(f"cancel_clase_{class_id}")
        assert label in transcript.bodies[-1]
        assert flow.CANCEL_CONFIRM_YES not in buttons(transcript.payloads[-1])
        await transcript.send(flow.CANCEL_CONFIRM_YES)
        await transcript.send(flow.CANCEL_CONFIRM_NO)
    transcript.api.dar_baja_instancia.assert_not_awaited()
    transcript.api.delete_inscripcion.assert_not_awaited()
    transcript.assert_single_responses(8)


@pytest.mark.parametrize("huge", [False, True])
async def test_summary_fallback_uses_creation_date_and_all_details_remain_accessible(transcript, huge):
    enrollments = []
    for index in range(4):
        enrollment = inscripcion(f"i{index}", {**CLASS, "id": f"c{index}", "fecha": f"2099-12-{index + 1:02d}"})
        enrollment.update(
            nombreActividad=f"Actividad {index} " + "x" * (1500 if huge else 300),
            fechaInscripcion=f"2026-01-0{index + 1}T09:00:00Z",
        )
        enrollments.append(enrollment)
    # Most recently CREATED, despite being the EARLIEST upcoming class.
    enrollments[-1]["clases"][0]["fecha"] = "2099-05-11"
    transcript.api.get_inscripciones_socio.return_value = enrollments
    await transcript.send("activities")
    if huge:
        assert "Tenés 4 inscripciones" in transcript.bodies[-1]
        assert "Actividad 3" not in transcript.bodies[-1]
    else:
        assert "Actividad 3" in transcript.bodies[-1]
        assert "otras 3 inscripciones" in transcript.bodies[-1]
        assert "Actividad 0" not in transcript.bodies[-1]
    await transcript.send(flow.ACTION_CANCEL)
    assert {f"cancel_clase_c{i}" for i in range(4)} <= {row["id"] for row in rows(transcript.payloads[-1])}
    for index in range(4):
        await transcript.send(f"cancel_clase_c{index}")
        assert enrollments[index]["nombreActividad"] in "".join(
            p["text"]["body"] if p["type"] == "text" else p["interactive"]["body"]["text"] for p in transcript.turns[-1]
        )
        await transcript.send(flow.CANCEL_CONFIRM_NO)
    assert len(transcript.turns[0]) == 1
    assert_payload_limits(transcript)


async def test_missing_creation_date_has_deterministic_fallback(transcript):
    enrollments = [
        {**inscripcion(f"i{i}", {**CLASS, "id": f"c{i}"}), "nombreActividad": f"Actividad {i} " + "x" * 300}
        for i in range(4)
    ]
    transcript.api.get_inscripciones_socio.return_value = enrollments
    await transcript.send("activities")
    first_body = transcript.bodies[-1]
    transcript.api.get_inscripciones_socio.return_value = list(reversed(enrollments))
    await transcript.send(flow.ACTION_ENROLL)
    assert transcript.bodies[-1] == first_body
    assert "Actividad 3" in first_body
    transcript.assert_single_responses(2)


@pytest.mark.parametrize("changes", [{"fecha": "2099-05-12"}, {"horaInicio": "08:00:00"}, {"costo": 200}])
async def test_changed_class_requires_new_consent_in_one_message(transcript, changes):
    await transcript.send("activities")
    await transcript.send("act_a1")
    transcript.api.get_instancias_actividad.return_value = [{**CLASS, **changes}]
    await transcript.send(flow.CONFIRM_YES)
    assert "La clase cambió" in transcript.bodies[-1]
    transcript.api.post_inscripcion.assert_not_awaited()
    await transcript.send(flow.CONFIRM_YES)
    transcript.api.post_inscripcion.assert_awaited_once_with("s1", "a1", instancia_id="c1")
    transcript.assert_single_responses(4)


async def test_changed_withdrawal_requires_new_consent_once(transcript):
    enrollment = inscripcion()
    transcript.api.get_inscripciones_socio.return_value = [enrollment]
    transcript.api.get_inscripcion.return_value = deepcopy(enrollment)
    await transcript.send("activities")
    await transcript.send(flow.ACTION_CANCEL)
    await transcript.send("cancel_clase_c1")
    transcript.api.get_inscripcion.return_value["clases"][0]["horaInicio"] = "08:00:00"
    await transcript.send(flow.CANCEL_CONFIRM_YES)
    assert "La clase cambió" in transcript.bodies[-1] and "08:00" in transcript.bodies[-1]
    transcript.api.dar_baja_instancia.assert_not_awaited()
    await transcript.send(flow.CANCEL_CONFIRM_YES)
    transcript.api.dar_baja_instancia.assert_awaited_once_with("c1", "s1")
    transcript.assert_single_responses(5)


@pytest.mark.parametrize(
    "changes", [{"cupoDisponible": 0}, {"liquidada": True}, {"estado": "Suspendida"}, {"fecha": "2020-01-01"}]
)
async def test_unavailable_after_confirmation_does_not_write(transcript, changes):
    await transcript.send("activities")
    await transcript.send("act_a1")
    transcript.api.get_instancias_actividad.return_value = [{**CLASS, **changes}]
    await transcript.send(flow.CONFIRM_YES)
    transcript.api.post_inscripcion.assert_not_awaited()
    assert transcript.session.active_flow is None
    assert "activities" in {row["id"] for row in rows(transcript.payloads[-1])}
    transcript.assert_single_responses(3)


async def test_generic_revalidation_cannot_bypass_new_full_class(transcript):
    transcript.api.get_instancias_actividad.return_value = []
    await transcript.send("activities")
    await transcript.send("act_a1")
    transcript.api.get_instancias_actividad.return_value = [{**CLASS, "cupoDisponible": 0}]
    await transcript.send(flow.CONFIRM_YES)
    transcript.api.post_inscripcion.assert_not_awaited()
    transcript.assert_single_responses(3)


@pytest.mark.parametrize("changes,detail", [({"costo": 200}, "$200"), ({"horaInicio": "08:00:00"}, "08:00 - 10:00")])
async def test_generic_changed_details_need_renewed_consent(transcript, changes, detail):
    transcript.api.get_instancias_actividad.return_value = []
    await transcript.send("activities")
    await transcript.send("act_a1")
    transcript.api.get_actividades.return_value = [{**ACTIVITY, **changes}]
    await transcript.send(flow.CONFIRM_YES)
    assert "La actividad cambió" in transcript.bodies[-1]
    assert detail in transcript.bodies[-1]
    transcript.api.post_inscripcion.assert_not_awaited()
    await transcript.send(flow.CONFIRM_YES)
    transcript.api.post_inscripcion.assert_awaited_once_with("s1", "a1")
    transcript.assert_single_responses(4)


async def test_all_classes_covered_never_offer_generic_enrollment(transcript):
    transcript.api.get_inscripciones_socio.return_value = [inscripcion()]
    await transcript.send("activities")
    await transcript.send("act_a1")
    assert "ya estás inscripto/a" in transcript.bodies[-1]
    assert transcript.session.active_flow is None
    transcript.api.post_inscripcion.assert_not_awaited()
    transcript.assert_single_responses(2)


async def test_enrollment_revalidation_rejects_new_coverage(transcript):
    await transcript.send("activities")
    await transcript.send("act_a1")
    transcript.api.get_inscripciones_socio.return_value = [inscripcion()]
    await transcript.send(flow.CONFIRM_YES)
    transcript.api.post_inscripcion.assert_not_awaited()
    transcript.assert_single_responses(3)


async def test_withdrawal_revalidates_membership(transcript):
    transcript.api.get_inscripciones_socio.return_value = [inscripcion()]
    transcript.api.get_inscripcion.return_value = {**inscripcion(), "socioId": "other-member"}
    await transcript.send("activities")
    await transcript.send(flow.ACTION_CANCEL)
    await transcript.send("cancel_clase_c1")
    await transcript.send(flow.CANCEL_CONFIRM_YES)
    transcript.api.delete_inscripcion.assert_not_awaited()
    transcript.api.dar_baja_instancia.assert_not_awaited()
    transcript.assert_single_responses(4)


async def test_past_class_in_future_enrollment_is_read_only(transcript):
    enrollment = inscripcion()
    enrollment["clases"].append({**enrollment["clases"][0], "instanciaId": "past", "fecha": "2020-01-01"})
    transcript.api.get_inscripciones_socio.return_value = [enrollment]
    await transcript.send("activities")
    await transcript.send(flow.ACTION_CANCEL)
    await transcript.send("cancel_clase_past")
    assert transcript.session.run.task == "view"
    assert flow.CANCEL_CONFIRM_YES not in buttons(transcript.payloads[-1])
    await transcript.send(flow.CANCEL_CONFIRM_YES)
    transcript.api.get_inscripcion.assert_not_awaited()
    transcript.api.dar_baja_instancia.assert_not_awaited()
    transcript.api.delete_inscripcion.assert_not_awaited()
    transcript.assert_single_responses(4)


async def test_withdrawal_with_parent_cost_does_not_reconfirm_forever(transcript):
    enrollment = {**inscripcion(), "costo": 150}
    transcript.api.get_inscripciones_socio.return_value = [enrollment]
    transcript.api.get_inscripcion.return_value = enrollment
    await transcript.send("activities")
    await transcript.send(flow.ACTION_CANCEL)
    await transcript.send("cancel_clase_c1")
    assert "$150" in transcript.bodies[-1]
    await transcript.send(flow.CANCEL_CONFIRM_YES)
    transcript.api.dar_baja_instancia.assert_awaited_once_with("c1", "s1")
    transcript.assert_single_responses(4)


async def test_generic_withdrawal_changed_range_renews_consent(transcript):
    enrollment = {**inscripcion(), "instanciaId": None, "clases": [], "fechaFin": "2099-12-01"}
    transcript.api.get_inscripciones_socio.return_value = [enrollment]
    transcript.api.get_inscripcion.return_value = {**enrollment, "fechaFin": "2099-12-02"}
    await transcript.send("activities")
    await transcript.send(flow.ACTION_CANCEL)
    await transcript.send("cancel_general_i1")
    await transcript.send(flow.CANCEL_CONFIRM_YES)
    assert "La inscripción cambió" in transcript.bodies[-1] and "02/12/2099" in transcript.bodies[-1]
    transcript.api.delete_inscripcion.assert_not_awaited()
    await transcript.send(flow.CANCEL_CONFIRM_YES)
    transcript.api.delete_inscripcion.assert_awaited_once_with("i1")
    transcript.assert_single_responses(5)


async def test_lost_session_stale_confirmation_cannot_write(transcript):
    await transcript.send("activities")
    await transcript.send("act_a1")
    transcript.session.flow_state = None
    await transcript.send(flow.CONFIRM_YES)
    assert flow.SESION_VENCIDA in transcript.bodies[-1]
    transcript.api.post_inscripcion.assert_not_awaited()
    transcript.assert_single_responses(3)


@pytest.mark.parametrize("empty", [False, True])
async def test_entry_error_or_empty_is_one_result_with_menu(transcript, empty):
    if empty:
        transcript.api.get_actividades.return_value = []
    else:
        transcript.api.get_actividades.side_effect = ServicesAPIError("SQL secret details")
    await transcript.send("activities")
    assert "SQL" not in transcript.bodies[-1]
    assert transcript.session.active_flow is None
    assert "activities" in {row["id"] for row in rows(transcript.payloads[-1])}
    transcript.assert_single_responses(1)


async def test_empty_own_list_retains_back_to_catalog(transcript):
    await transcript.send("activities")
    await transcript.send(flow.ACTION_CANCEL)
    assert flow.ACTION_ENROLL in {row["id"] for row in rows(transcript.payloads[-1])}
    await transcript.send(flow.ACTION_ENROLL)
    assert transcript.session.flow_state.step == flow.ActivitiesStep.AWAITING_CHOICE
    transcript.assert_single_responses(3)


async def test_no_active_catalog_keeps_enrollment_details_accessible(transcript):
    transcript.api.get_actividades.return_value = []
    transcript.api.get_inscripciones_socio.return_value = [inscripcion()]
    await transcript.send("activities")
    assert {row["id"] for row in rows(transcript.payloads[-1])} == {flow.ACTION_CANCEL}
    await transcript.send(flow.ACTION_CANCEL)
    await transcript.send("cancel_clase_c1")
    assert "Yoga inicial" in transcript.bodies[-1]
    transcript.assert_single_responses(3)


@pytest.mark.parametrize("stage", ["choice", "enroll_confirm", "own", "withdraw_confirm"])
async def test_api_error_at_each_stage_is_one_sanitized_completion(transcript, stage):
    if stage == "withdraw_confirm":
        transcript.api.get_inscripciones_socio.return_value = [inscripcion()]
    await transcript.send("activities")
    if stage in {"enroll_confirm"}:
        await transcript.send("act_a1")
        transcript.api.post_inscripcion.side_effect = ServicesAPIError("SQL secret details")
        await transcript.send(flow.CONFIRM_YES)
    elif stage == "withdraw_confirm":
        await transcript.send(flow.ACTION_CANCEL)
        await transcript.send("cancel_clase_c1")
        transcript.api.get_inscripcion.side_effect = ServicesAPIError("SQL secret details")
        await transcript.send(flow.CANCEL_CONFIRM_YES)
    elif stage == "choice":
        transcript.api.get_instancias_actividad.side_effect = ServicesAPIError("SQL secret details")
        await transcript.send("act_a1")
    else:
        transcript.api.get_inscripciones_socio.side_effect = ServicesAPIError("SQL secret details")
        await transcript.send(flow.ACTION_CANCEL)
    assert flow.GENERIC_ERROR in transcript.bodies[-1]
    assert "SQL" not in transcript.bodies[-1]
    assert transcript.session.active_flow is None
    transcript.assert_single_responses(len(transcript.turns))


async def test_confirmation_overflow_is_lossless(transcript):
    name = "Actividad larga " + "nombre completo " * 400
    transcript.api.get_actividades.return_value = [{**ACTIVITY, "nombre": name}]
    await transcript.send("activities")
    await transcript.send("act_a1")
    confirmation = "".join(
        payload["text"]["body"] if payload["type"] == "text" else payload["interactive"]["body"]["text"]
        for payload in transcript.turns[-1]
    )
    assert name in confirmation
    assert "11/05/2099" in confirmation and "Costo base: $100" in confirmation
    assert len(transcript.turns[-1]) > 1
    assert MENU_ID in buttons(transcript.payloads[-1])
    transcript.api.post_inscripcion.assert_not_awaited()
    assert_payload_limits(transcript)
