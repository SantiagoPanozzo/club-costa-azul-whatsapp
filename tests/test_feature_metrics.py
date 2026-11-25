from unittest.mock import Mock

import pytest

from app import usage_metrics
from app.flows import activities, eventos
from app.services_client import ServicesAPIError


@pytest.mark.parametrize(
    "flow,lookup,expected",
    [
        ("activities", "get_actividades", "activity.view"),
        ("reservas", "get_espacios", "reservation.view"),
        ("cuotas", "get_cuotas_socio", "fees.view"),
        ("eventos", "get_eventos", "event.view"),
        ("datos_personales", "get_socio_detalle", "profile.view"),
        ("comunicados", "get_noticias", "announcements.view"),
    ],
)
async def test_view_not_recorded_on_lookup_error(transcript, monkeypatch, flow, lookup, expected):
    record = Mock()
    monkeypatch.setattr(usage_metrics, "record_feature", record)
    getattr(transcript.api, lookup).side_effect = ServicesAPIError("synthetic failure")
    await transcript.send(flow)
    assert expected not in [call.args[2] for call in record.call_args_list]


async def test_activity_enrollment_failure_is_not_success(transcript, monkeypatch):
    record = Mock()
    monkeypatch.setattr(usage_metrics, "record_feature", record)
    transcript.api.post_inscripcion.side_effect = ServicesAPIError("synthetic failure")
    await transcript.send("activities")
    await transcript.send("act_a1")
    await transcript.send(activities.CONFIRM_YES)
    assert "activity.view" in [call.args[2] for call in record.call_args_list]
    assert "activity.enroll" not in [call.args[2] for call in record.call_args_list]


async def test_event_enrollment_failure_is_not_success(transcript, monkeypatch):
    record = Mock()
    monkeypatch.setattr(usage_metrics, "record_feature", record)
    transcript.api.post_inscripcion_evento.side_effect = ServicesAPIError("synthetic failure")
    await transcript.send("eventos")
    await transcript.send("evt_e1")
    await transcript.send(eventos.CONFIRM_YES)
    assert "event.view" in [call.args[2] for call in record.call_args_list]
    assert "event.enroll" not in [call.args[2] for call in record.call_args_list]


async def test_receipt_failure_is_not_success(transcript, monkeypatch):
    record = Mock()
    monkeypatch.setattr(usage_metrics, "record_feature", record)
    transcript.api.upload_comprobante.side_effect = ServicesAPIError("synthetic failure")
    await transcript.send("cuotas")
    await transcript.send(type="document", media_id="receipt", media_mime_type="application/pdf")
    assert "fees.view" in [call.args[2] for call in record.call_args_list]
    assert "receipt.submit" not in [call.args[2] for call in record.call_args_list]
