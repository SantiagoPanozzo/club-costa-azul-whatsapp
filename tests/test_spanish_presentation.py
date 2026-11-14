"""API-shaped data through the router, with Meta and all API writes mocked."""

import json
import re
from copy import deepcopy
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.flows import activities, eventos, reservas
from app.whatsapp_client import whatsapp_client
from tests.transcript import ACTIVITY, CLASS, EVENT, QUOTA


def rows(payload):
    return payload["interactive"]["action"]["sections"][0]["rows"]


def assert_localized(transcript):
    # Inspect every delivered field, including list titles/descriptions, not only bodies.
    rendered = json.dumps(transcript.payloads, ensure_ascii=False)
    assert not re.search(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{2}:\d{2}:\d{2}\b", rendered)
    assert not re.search(r"Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Funday", rendered)


@pytest.mark.parametrize("days", [["Monday", "Wednesday"], [1, 3], ["1", "wed"], ["Monday", "Funday", 3]])
async def test_activity_schedule_and_generic_confirmation_are_spanish(transcript, days):
    activity = {**ACTIVITY, "diasSemana": days}
    original = deepcopy(activity)
    transcript.api.get_actividades.return_value = [activity]
    transcript.api.get_instancias_actividad.return_value = []
    catalog = await transcript.send("activities")
    assert rows(catalog)[0]["description"] == "Lun, Mié 09:00 - 10:00"
    await transcript.send("act_a1")
    assert "Lun, Mié 09:00 - 10:00" in transcript.bodies[-1]
    assert_localized(transcript)
    await transcript.send(activities.CONFIRM_YES)
    transcript.api.post_inscripcion.assert_awaited_once_with("s1", "a1")
    assert activity == original
    transcript.assert_single_responses(3)


async def test_changed_per_day_schedule_requires_new_one_use_consent(transcript):
    activity = {
        **ACTIVITY,
        "diasSemana": ["Monday", "Wednesday"],
        "horarios": [
            {"dia": "Monday", "horaInicio": "09:00:00", "horaFin": "10:00:00"},
            {"dia": "Wednesday", "horaInicio": "18:00:00", "horaFin": "19:00:00"},
        ],
    }
    transcript.api.get_actividades.return_value = [activity]
    transcript.api.get_instancias_actividad.return_value = []
    await transcript.send("activities")
    old = await transcript.send("act_a1")
    assert "Lun 09:00 - 10:00; Mié 18:00 - 19:00" in transcript.bodies[-1]
    changed = deepcopy(activity)
    changed["horarios"][1]["horaInicio"] = "17:00:00"
    transcript.api.get_actividades.return_value = [changed]
    await transcript.send(activities.CONFIRM_YES)
    assert "La actividad cambió" in transcript.bodies[-1]
    assert "Mié 17:00 - 19:00" in transcript.bodies[-1]
    transcript.api.post_inscripcion.assert_not_awaited()
    stale_yes = old["interactive"]["action"]["buttons"][0]["reply"]["id"]
    await transcript.send_raw(stale_yes)
    transcript.api.post_inscripcion.assert_not_awaited()
    await transcript.send(activities.CONFIRM_YES)
    transcript.api.post_inscripcion.assert_awaited_once_with("s1", "a1")
    assert_localized(transcript)
    transcript.assert_single_responses(5)


async def test_class_picker_includes_full_date_and_does_not_rewrite_api_values(transcript):
    transcript.api.get_instancias_actividad.return_value = [CLASS, {**CLASS, "id": "c2"}]
    await transcript.send("activities")
    picker = await transcript.send("act_a1")
    assert rows(picker)[0]["title"] == "Clase 11/05/2099"
    await transcript.send("clase_c1")
    assert "lunes 11/05/2099 09:00 - 10:00" in transcript.bodies[-1]
    assert transcript.session.flow_state.instancia_elegida["fecha"] == "2099-05-11"
    assert transcript.session.flow_state.instancia_elegida["horaInicio"] == "09:00:00"
    assert_localized(transcript)


async def test_reservation_list_and_cancellation_card_format_api_dates(transcript):
    reservation = {
        "id": "r1",
        "nombreEspacio": "Quincho",
        "fecha": "2099-05-11",
        "estado": "Pendiente",
        "horaInicio": "10:00:00",
        "horaFin": "12:00:00",
    }
    transcript.api.get_reservas_socio.return_value = [reservation]
    await transcript.send("reservas")
    listing = await transcript.send(reservas.ACTION_LIST)
    assert "11/05/2099 10:00 - 12:00" in rows(listing)[0]["description"]
    await transcript.send("res_r1")
    assert "Fecha: 11/05/2099" in transcript.bodies[-1]
    assert "10:00 - 12:00" in transcript.bodies[-1]
    transcript.api.delete_reserva.assert_not_awaited()
    assert reservation["fecha"] == "2099-05-11"
    assert_localized(transcript)


async def test_event_catalog_and_enrollment_summary_format_all_dates(transcript):
    transcript.api.get_eventos.return_value = [EVENT, {**EVENT, "id": "e2"}]
    transcript.api.get_inscripciones_evento_socio.return_value = [
        {"id": "i1", "eventoId": "e1", "fechaInscripcion": "2026-11-09", "estado": "Confirmada"}
    ]
    catalog = await transcript.send("eventos")
    assert "11/05/2099" in rows(catalog)[0]["description"]
    assert "Inscripción: 09/11/2026" in transcript.bodies[-1]
    await transcript.send(eventos.ACTION_WITHDRAW)
    await transcript.send("evtw_i1")
    assert "09:00 - 10:00" in transcript.bodies[-1]
    transcript.api.delete_inscripcion_evento.assert_not_awaited()
    assert_localized(transcript)


async def test_real_billing_bounds_and_raw_receipt_binding(transcript):
    cuota = {**QUOTA, "periodo": "2026-07_2026-12", "fechaInicio": "2026-07-01", "fechaFin": "2026-12-31"}
    transcript.api.get_cuotas_socio.return_value = [cuota]
    await transcript.send("cuotas")
    assert "01/07/2026 - 31/12/2026" in transcript.bodies[-1]
    assert "vence 01/11/2026" in transcript.bodies[-1]
    assert transcript.session.flow_state.presented_terms == ("2026-07_2026-12", Decimal("100"))
    assert_localized(transcript)
    # Bounds now form part of the displayed association, even if the legacy label is unchanged.
    transcript.api.get_cuotas_socio.return_value = [{**cuota, "fechaFin": "2026-12-30"}]
    receipt = {"type": "document", "media_id": "synthetic", "media_mime_type": "application/pdf"}
    await transcript.send(**receipt)
    assert "cambió" in transcript.bodies[-1] and "30/12/2026" in transcript.bodies[-1]
    whatsapp_client.download_media.assert_not_awaited()
    transcript.api.upload_comprobante.assert_not_awaited()
    await transcript.send(**receipt)
    assert transcript.api.upload_comprobante.await_args.args[:2] == ("s1", "q1")
    transcript.assert_single_responses(3)


async def test_failed_billing_bounds_presentation_does_not_authorize_upload(transcript, monkeypatch):
    cuota = {**QUOTA, "fechaInicio": "2026-07-01", "fechaFin": "2026-12-31"}
    transcript.api.get_cuotas_socio.return_value = [cuota]
    await transcript.send("cuotas")
    state = transcript.session.flow_state
    transcript.api.get_cuotas_socio.return_value = [{**cuota, "fechaFin": "2026-12-30"}]
    sender = whatsapp_client._send
    monkeypatch.setattr(whatsapp_client, "_send", AsyncMock(return_value=None))
    receipt = {"type": "document", "media_id": "synthetic", "media_mime_type": "application/pdf"}
    await transcript.send(**receipt)
    assert state.presented_bounds == ("2026-07-01", "2026-12-31")
    monkeypatch.setattr(whatsapp_client, "_send", sender)
    await transcript.send(**receipt)
    assert "30/12/2026" in transcript.bodies[-1]
    assert state.presented_bounds == ("2026-07-01", "2026-12-30")
    whatsapp_client.download_media.assert_not_awaited()
    transcript.api.upload_comprobante.assert_not_awaited()
    await transcript.send(**receipt)
    transcript.api.upload_comprobante.assert_awaited_once()


async def test_raw_time_change_is_revalidated_even_if_minute_label_is_unchanged(transcript):
    await transcript.send("eventos")
    await transcript.send("evt_e1")
    transcript.api.get_eventos.return_value = [{**EVENT, "horaInicio": "09:00:30"}]
    await transcript.send(eventos.CONFIRM_YES)
    assert "cambiaron" in transcript.bodies[-1]
    assert "09:00 - 10:00" in transcript.bodies[-1]
    transcript.api.post_inscripcion_evento.assert_not_awaited()
    await transcript.send(eventos.CONFIRM_YES)
    transcript.api.post_inscripcion_evento.assert_awaited_once_with("e1", "s1")
    assert_localized(transcript)


@pytest.mark.parametrize("birth,expected", [("1996-02-29", "29/02/1996"), ("not-a-date", "—"), (None, "—")])
async def test_personal_birth_date_is_formatted_with_safe_fallback(transcript, birth, expected):
    transcript.api.get_socio_detalle.return_value = {"nombre": "Prueba", "fechaNacimiento": birth}
    await transcript.send("datos_personales")
    assert f"Fecha de nacimiento: {expected}" in transcript.bodies[-1]
    assert_localized(transcript)
    transcript.assert_single_responses(1)


@pytest.mark.parametrize("timestamp", ["2026-11-09T00:30:00Z", "2026-11-09T23:30:00-03:00"])
async def test_announcement_date_does_not_shift_timezone(transcript, timestamp):
    transcript.api.get_noticias.return_value = [{"titulo": "Prueba", "descripcion": "Aviso", "fecha": timestamp}]
    await transcript.send("comunicados")
    assert "09/11/2026" in transcript.bodies[-1]
    assert_localized(transcript)
    transcript.assert_single_responses(1)
