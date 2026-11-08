import pytest

from app import conversation, message_store
from app.flow_ui import MENU_ID
from app.services_client import ServicesAPIError


@pytest.mark.parametrize("flow", ["datos_personales", "comunicados"])
async def test_informational_flow_one_result_menu_message(transcript, flow):
    await transcript.send(flow)
    transcript.assert_single_responses(1)
    assert "https://clubcostaazul.com" in transcript.bodies[-1]
    assert "¿Qué querés hacer?" in transcript.bodies[-1]
    run = transcript.runs.await_args.args[0]
    assert run.outcome == "success" and run.task == "view"
    assert run.outbound_attempts == run.api_accepted == 1
    assert transcript.outgoing.await_args.kwargs["session"].run is None


@pytest.mark.parametrize("flow,method", [("datos_personales", "get_socio_detalle"), ("comunicados", "get_noticias")])
async def test_informational_failure_one_error_with_menu(transcript, flow, method):
    getattr(transcript.api, method).side_effect = ServicesAPIError("failure")
    await transcript.send(flow)
    transcript.assert_single_responses(1)
    assert conversation.GENERIC_ERROR in transcript.bodies[-1]
    assert transcript.runs.await_args.args[0].outcome == "error"


async def test_cold_welcome_is_one_menu_and_first_command_is_not_dispatched(transcript):
    transcript.session.socio = None
    await transcript.send(text="reservas")
    transcript.assert_single_responses(1)
    assert "¡Hola, Prueba!" in transcript.bodies[-1]
    assert transcript.session.active_flow is None
    transcript.api.get_espacios.assert_not_awaited()


async def test_help_has_no_added_menu(transcript):
    await transcript.send("ayuda")
    transcript.assert_single_responses(1)
    assert transcript.payloads[0]["type"] == "text"


async def test_explicit_menu_abandons_run_with_one_menu(transcript):
    await transcript.send("activities")
    run = transcript.session.run
    await transcript.send(MENU_ID)
    transcript.assert_single_responses(2)
    assert run.outcome == "abandoned"
    assert run.outbound_attempts == 2 and run.incoming_messages == 2
    assert transcript.session.run is None


async def test_duplicate_incoming_does_not_count_turn_or_send(transcript):
    await transcript.send("activities")
    run = transcript.session.run
    message_store.store_incoming.return_value = False
    await transcript.send("act_a1")
    assert run.incoming_messages == 1 and run.outbound_attempts == 1
    assert len(transcript.payloads) == 1
    transcript.api.post_inscripcion.assert_not_awaited()


async def test_duplicate_entry_does_not_create_persisted_run(transcript):
    message_store.store_incoming.return_value = False
    await transcript.send("activities")
    assert transcript.session.run is None and not transcript.payloads
    transcript.runs.assert_not_awaited()


async def test_recovery_count_and_success_menu_belong_to_same_run(transcript):
    await transcript.send("activities")
    run = transcript.session.run
    await transcript.send("invalid-choice")
    await transcript.send("act_a1")
    await transcript.send("confirm_yes")
    assert transcript.session.run is None
    assert run.outcome == "success" and run.task == "enroll_class"
    assert run.incoming_messages == run.outbound_attempts == run.api_accepted == 4
    assert run.recovery_turns == 1
    assert transcript.session.response_role == "content"


async def test_informational_long_content_is_preserved_with_only_necessary_overflow(transcript):
    name = "Nombre completo " * 700
    transcript.api.get_socio_detalle.return_value = {"nombre": name}
    await transcript.send("datos_personales")
    assert name.strip() in "".join(transcript.bodies)
    assert transcript.payloads[-1]["interactive"]["type"] == "list"
    for payload in transcript.payloads:
        body = payload.get("text", {}).get("body") or payload["interactive"]["body"]["text"]
        assert len(body) <= (4096 if payload["type"] == "text" else 1024)
