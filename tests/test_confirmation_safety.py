from app.flows import activities, cuotas, eventos
from tests.transcript import CLASS, EVENT, QUOTA


async def test_activity_confirmation_does_not_round_away_cents(transcript):
    transcript.api.get_instancias_actividad.return_value = [{**CLASS, "costo": 100.25}]
    await transcript.send("activities")
    await transcript.send("act_a1")
    assert "$100.25" in transcript.bodies[-1]
    transcript.api.post_inscripcion.assert_not_awaited()
    await transcript.send(activities.CONFIRM_YES)
    transcript.assert_single_responses(3)


async def test_receipt_amount_changed_requires_review_and_resubmission(transcript):
    await transcript.send("cuotas")
    transcript.api.get_cuotas_socio.return_value = [{**QUOTA, "monto": 150.25}]
    media = {"type": "document", "media_id": "synthetic-file", "media_mime_type": "application/pdf"}
    await transcript.send(**media)
    assert "$150.25" in transcript.bodies[-1] and "cambió" in transcript.bodies[-1]
    transcript.api.upload_comprobante.assert_not_awaited()
    await transcript.send(**media)
    transcript.api.upload_comprobante.assert_awaited_once()
    transcript.assert_single_responses(3)


async def test_changed_event_details_require_new_withdrawal_consent(transcript):
    transcript.api.get_inscripciones_evento_socio.return_value = [{"id": "ei1", "eventoId": "e1", "estado": "Activa"}]
    await transcript.send("eventos")
    await transcript.send(eventos.ACTION_WITHDRAW)
    await transcript.send("evtw_ei1")
    transcript.api.get_eventos.return_value = [{**EVENT, "horaInicio": "11:00:00", "horaFin": "12:00:00"}]
    await transcript.send(eventos.WITHDRAW_YES)
    transcript.api.delete_inscripcion_evento.assert_not_awaited()
    assert "11:00 - 12:00" in transcript.bodies[-1]
    await transcript.send(eventos.WITHDRAW_YES)
    transcript.api.delete_inscripcion_evento.assert_awaited_once_with("e1", "ei1")
    transcript.assert_single_responses(5)


async def test_empty_event_catalog_is_not_a_successful_enrollment(transcript):
    transcript.api.get_eventos.return_value = []
    await transcript.send("eventos")
    run = transcript.runs.await_args.args[0]
    assert run.task == "view" and run.outcome == "empty"


async def test_statement_view_does_not_submit_or_change_selected_quota(transcript):
    await transcript.send("cuotas")
    await transcript.send(cuotas.STATEMENT_ID)
    await transcript.send(cuotas.BACK_ID)
    assert transcript.session.flow_state.selected_cuota_id == "q1"
    transcript.api.upload_comprobante.assert_not_awaited()
