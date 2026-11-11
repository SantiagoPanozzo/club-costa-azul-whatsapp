from unittest.mock import AsyncMock

import httpx
import pytest

from app.flows import activities as a
from app.flows import eventos as e
from app.flows import reservas as r
from app.storing_client import StoringClient
from app.whatsapp_client import WhatsAppClient, whatsapp_client
from tests.test_activities import inscripcion
from tests.test_reservas import reach_confirmation
from tests.transcript import ACTIVITY, CLASS, EVENT, QUOTA


@pytest.mark.parametrize("flow", ["activities", "eventos", "reservas"])
async def test_original_confirmation_does_not_authorize_changed_terms(transcript, flow):
    if flow == "activities":
        await transcript.send(flow)
        await transcript.send("act_a1")
        action = a.CONFIRM_YES
        api = transcript.api.post_inscripcion
        transcript.api.get_instancias_actividad.return_value = [{**CLASS, "costo": 125.50}]
    elif flow == "eventos":
        await transcript.send(flow)
        await transcript.send("evt_e1")
        action = e.CONFIRM_YES
        api = transcript.api.post_inscripcion_evento
        transcript.api.get_eventos.return_value = [{**EVENT, "costoSocio": 125.50}]
    else:
        await reach_confirmation(transcript)
        action = r.CONFIRM_YES
        api = transcript.api.post_reserva
        from tests.transcript import SPACE

        transcript.api.get_espacios.return_value = [{**SPACE, "costo": 125.50}]
    original = transcript.session.confirmation_id
    await transcript.send_raw(original)
    assert transcript.session.confirmation_id != original
    api.assert_not_awaited()
    await transcript.send_raw(original)
    api.assert_not_awaited()
    await transcript.send(action)
    api.assert_awaited_once()


async def test_old_event_confirmation_does_not_enroll_a_new_selection(transcript):
    transcript.api.get_eventos.return_value = [EVENT, {**EVENT, "id": "e2", "nombre": "Otro evento"}]
    await transcript.send("eventos")
    await transcript.send("evt_e1")
    original = transcript.session.confirmation_id
    await transcript.send(e.CONFIRM_NO)
    await transcript.send("evt_e2")
    await transcript.send_raw(original)
    transcript.api.post_inscripcion_evento.assert_not_awaited()
    await transcript.send(e.CONFIRM_YES)
    transcript.api.post_inscripcion_evento.assert_awaited_once_with("e2", "s1")


async def test_unversioned_confirmation_is_not_a_valid_authorization(transcript):
    await transcript.send("activities")
    await transcript.send("act_a1")
    await transcript.send_raw(a.CONFIRM_YES)
    transcript.api.post_inscripcion.assert_not_awaited()
    await transcript.send(a.CONFIRM_YES)
    transcript.api.post_inscripcion.assert_awaited_once()


async def test_historical_specific_enrollment_cannot_hide_active_generic_class(transcript):
    generic = inscripcion("general")
    generic["instanciaId"] = None
    generic["fechaFin"] = None
    historical = inscripcion("historical")
    historical["estado"] = "Cancelada"
    historical["clases"][0]["estadoClase"] = "Baja"
    transcript.api.get_inscripciones_socio.return_value = [generic, historical]
    transcript.api.get_inscripcion.return_value = generic
    await transcript.send("activities")
    await transcript.send(a.ACTION_CANCEL)
    state = transcript.session.flow_state
    assert state.active_inscriptions["clase_c1"]["id"] == "general"
    assert any(i["id"] == "historical" for i in state.active_inscriptions.values())
    await transcript.send("cancel_clase_c1")
    await transcript.send(a.CANCEL_CONFIRM_YES)
    transcript.api.dar_baja_instancia.assert_awaited_once_with("c1", "s1")
    transcript.api.delete_inscripcion.assert_not_awaited()


async def test_renamed_activity_requires_fresh_consent(transcript):
    await transcript.send("activities")
    await transcript.send("act_a1")
    transcript.api.get_actividades.return_value = [{**ACTIVITY, "nombre": "Otra actividad"}]
    await transcript.send(a.CONFIRM_YES)
    assert "Otra actividad" in transcript.bodies[-1]
    transcript.api.post_inscripcion.assert_not_awaited()
    await transcript.send(a.CONFIRM_YES)
    transcript.api.post_inscripcion.assert_awaited_once()


async def test_failed_required_reservation_overflow_withholds_confirm_controls(transcript, monkeypatch):
    await reach_confirmation(transcript, reason="motivo " * 1000)
    # Re-render an unusually long confirmation, but its required text cannot be confirmed as sent.
    monkeypatch.setattr(whatsapp_client, "_send", AsyncMock(return_value=None))
    transcript.payloads.clear()
    await transcript.send_raw("invalid")
    assert transcript.session.confirmation_id is None
    assert not transcript.payloads
    await transcript.send_raw(r.CONFIRM_YES)
    transcript.api.post_reserva.assert_not_awaited()
    # One failed text attempt on each turn; no actionable card was sent afterward.
    assert whatsapp_client._send.await_count == 2


async def test_failed_event_description_cannot_be_bypassed_by_retry(transcript, monkeypatch):
    transcript.api.get_eventos.return_value = [{**EVENT, "descripcion": "Descripción completa " * 1000}]
    await transcript.send("eventos")
    monkeypatch.setattr(whatsapp_client, "_send", AsyncMock(return_value=None))
    await transcript.send("evt_e1")
    assert transcript.session.confirmation_id is None
    await transcript.send_raw(e.CONFIRM_YES)
    assert transcript.session.confirmation_id is None
    transcript.api.post_inscripcion_evento.assert_not_awaited()
    assert whatsapp_client._send.await_count == 2


async def test_exception_resets_flow_and_run_together(transcript):
    await transcript.send("activities")
    run = transcript.session.run
    transcript.api.get_instancias_actividad.side_effect = RuntimeError("unexpected schema")
    with pytest.raises(RuntimeError):
        await transcript.send("act_a1")
    assert transcript.session.active_flow is None and transcript.session.run is None
    assert run.outcome == "error"
    transcript.api.get_instancias_actividad.side_effect = None
    await transcript.send("activities")
    assert transcript.session.run is not None and transcript.session.run.id != run.id


@pytest.mark.parametrize("change", ["quota", "amount"])
async def test_failed_financial_notice_cannot_silently_authorize_receipt_attribution(transcript, monkeypatch, change):
    await transcript.send("cuotas")
    if change == "quota":
        transcript.api.get_cuotas_socio.return_value = [
            {**QUOTA, "estado": "Pagada"},
            {**QUOTA, "id": "q2", "periodo": "2026-12"},
        ]
        expected_id = "q2"
    else:
        transcript.api.get_cuotas_socio.return_value = [{**QUOTA, "monto": 150}]
        expected_id = "q1"
    media = {"type": "document", "media_id": "synthetic", "media_mime_type": "application/pdf"}
    sender = whatsapp_client._send
    monkeypatch.setattr(whatsapp_client, "_send", AsyncMock(return_value=None))
    await transcript.send(**media)
    await transcript.send(**media)
    transcript.api.upload_comprobante.assert_not_awaited()
    state = transcript.session.flow_state
    assert state.presented_terms == ("2026-11", 100)
    monkeypatch.setattr(whatsapp_client, "_send", sender)
    # This receipt first causes successful presentation of the new association;
    # it is not itself uploaded, even though sending now works again.
    await transcript.send(**media)
    transcript.api.upload_comprobante.assert_not_awaited()
    assert state.presented_cuota_id == expected_id
    await transcript.send(**media)
    assert transcript.api.upload_comprobante.await_args.args[1] == expected_id


async def test_missing_generic_enrollment_name_does_not_reconfirm_forever(transcript):
    enrollment = inscripcion()
    enrollment.pop("nombreActividad")
    enrollment.update(instanciaId=None, clases=[], fechaFin=None)
    transcript.api.get_inscripciones_socio.return_value = [enrollment]
    transcript.api.get_inscripcion.return_value = enrollment
    await transcript.send("activities")
    await transcript.send(a.ACTION_CANCEL)
    await transcript.send("cancel_general_i1")
    await transcript.send(a.CANCEL_CONFIRM_YES)
    transcript.api.delete_inscripcion.assert_awaited_once_with("i1")
    transcript.assert_single_responses(4)


@pytest.mark.parametrize("content", [b"invalid JSON", b'{"messages": []}', b'{"messages": [null]}', b"[]"])
async def test_real_http_post_without_valid_wamid_is_recorded_as_unconfirmed(monkeypatch, content):
    from app import message_store, storing_client
    from app.state import Session

    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, content=content)

    client = WhatsAppClient()
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    record = AsyncMock()
    monkeypatch.setattr(message_store, "store_outgoing", record)
    monkeypatch.setattr(storing_client, "whatsapp_client", client)
    session = Session()
    session.begin_run("test")
    try:
        assert await StoringClient().send_text("synthetic", "Información", session=session) is None
    finally:
        await client.aclose()
    assert len(calls) == session.run.outbound_attempts == record.await_count == 1
    assert session.run.api_accepted == 0
    assert record.await_args.kwargs["wamid"] is None
