"""Comparable wire-level budgets; historical baseline was audited at 0233f2f."""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.flows import activities as a
from app.flows import eventos as e
from app.flows import reservas as r
from tests.test_activities import inscripcion
from tests.transcript import CLASS, QUOTA

BASELINE = json.loads((Path(__file__).parent / "fixtures/message_baseline.json").read_text())
TARGETS = {
    "reservation_create": 7,
    "reservation_cancel": 4,
    "class_single": 3,
    "class_multiple": 4,
    "activity_generic": 3,
    "activity_withdraw": 4,
    "event_enroll": 3,
    "event_withdraw": 4,
    "receipt_single": 2,
    "receipt_multiple": 3,
    "personal_data": 1,
    "announcements": 1,
    "account_summary": 1,
    "welcome": 1,
    "help": 1,
}


@pytest.mark.parametrize("task,target", TARGETS.items())
async def test_completed_task_wire_budget(transcript, task, target):
    t = transcript
    if task == "reservation_create":
        await t.send("reservas")
        await t.send("esp_s1")
        await t.send(text=(date.today() + timedelta(days=2)).strftime("%d/%m/%Y"))
        await t.send(text="10:00–12:00")
        await t.send(text="4")
        await t.send(text="Cumpleaños")
        await t.send(r.CONFIRM_YES)
        t.api.post_reserva.assert_awaited_once()
    elif task == "reservation_cancel":
        await t.send("reservas")
        await t.send(r.ACTION_LIST)
        await t.send("res_r1")
        await t.send(r.CANCEL_YES)
        t.api.delete_reserva.assert_awaited_once()
    elif task in {"class_single", "class_multiple", "activity_generic"}:
        if task == "class_multiple":
            t.api.get_instancias_actividad.return_value = [CLASS, {**CLASS, "id": "c2", "horaInicio": "11:00:00"}]
        elif task == "activity_generic":
            t.api.get_instancias_actividad.return_value = []
        await t.send("activities")
        await t.send("act_a1")
        if task == "class_multiple":
            await t.send("clase_c1")
        await t.send(a.CONFIRM_YES)
        t.api.post_inscripcion.assert_awaited_once()
    elif task == "activity_withdraw":
        t.api.get_inscripciones_socio.return_value = [inscripcion()]
        t.api.get_inscripcion.return_value = inscripcion()
        await t.send("activities")
        await t.send(a.ACTION_CANCEL)
        await t.send("cancel_clase_c1")
        await t.send(a.CANCEL_CONFIRM_YES)
        t.api.dar_baja_instancia.assert_awaited_once()
    elif task == "event_enroll":
        await t.send("eventos")
        await t.send("evt_e1")
        await t.send(e.CONFIRM_YES)
        t.api.post_inscripcion_evento.assert_awaited_once()
    elif task == "event_withdraw":
        t.api.get_inscripciones_evento_socio.return_value = [{"id": "ei1", "eventoId": "e1", "estado": "Activa"}]
        await t.send("eventos")
        await t.send(e.ACTION_WITHDRAW)
        await t.send("evtw_ei1")
        await t.send(e.WITHDRAW_YES)
        t.api.delete_inscripcion_evento.assert_awaited_once()
    elif task in {"receipt_single", "receipt_multiple"}:
        if task == "receipt_multiple":
            t.api.get_cuotas_socio.return_value = [QUOTA, {**QUOTA, "id": "q2", "periodo": "2026-12"}]
        await t.send("cuotas")
        if task == "receipt_multiple":
            await t.send("cuota_q1")
        await t.send(type="document", media_id="synthetic", media_mime_type="application/pdf")
        t.api.upload_comprobante.assert_awaited_once()
    else:
        if task == "account_summary":
            t.api.get_cuotas_socio.return_value = [{**QUOTA, "estado": "Pagada"}]
        if task == "welcome":
            t.session.socio = None
        await t.send(
            {
                "personal_data": "datos_personales",
                "announcements": "comunicados",
                "account_summary": "cuotas",
                "welcome": "menu",
                "help": "ayuda",
            }[task]
        )
    t.assert_single_responses(target)
    assert target <= BASELINE["tasks"][task]
    assert t.session.active_flow is None
