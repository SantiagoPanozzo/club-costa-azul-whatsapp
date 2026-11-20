"""Interpretación de fechas escritas a mano y de los mensajes entrantes del webhook de Meta."""

from datetime import date

import pytest

from app.date_parser import parse_flexible_date
from app.webhook_parser import extract_messages


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("15/03/2027", date(2027, 3, 15)),
        ("1-2-27", date(2027, 2, 1)),
        ("el 9.11.2026 a la tarde", date(2026, 11, 9)),
        ("31/02/2027", None),
        ("hola", None),
        ("", None),
    ],
)
def test_fecha_completa_o_invalida(texto, esperado):
    assert parse_flexible_date(texto) == esperado


def test_dia_y_mes_toman_el_anio_actual():
    hoy = date.today()
    assert parse_flexible_date("5/6") == date(hoy.year, 6, 5)
    assert parse_flexible_date("40/6") is None


def test_solo_el_dia_toma_mes_y_anio_actuales():
    hoy = date.today()
    assert parse_flexible_date("el 1") == date(hoy.year, hoy.month, 1)
    assert parse_flexible_date("99") is None


def _payload(*mensajes, contacto="Ana"):
    valor = {"messages": list(mensajes)}
    if contacto:
        valor["contacts"] = [{"profile": {"name": contacto}}]
    return {"entry": [{"changes": [{"value": valor}, {"value": {"statuses": []}}]}]}


def test_extrae_texto_botones_listas_imagenes_y_documentos():
    payload = _payload(
        {"from": "598991", "id": "w1", "timestamp": "1", "type": "text", "text": {"body": "hola"}},
        {
            "from": "598991",
            "type": "interactive",
            "interactive": {"type": "list_reply", "list_reply": {"id": "op1", "title": "Cuotas"}},
        },
        {
            "from": "598991",
            "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": "si", "title": "Sí"}},
        },
        {"from": "598991", "type": "interactive", "interactive": {"type": "nfm_reply"}},
        {"from": "598991", "type": "image", "image": {"id": "m1", "mime_type": "image/jpeg"}},
        {
            "from": "598991",
            "type": "document",
            "document": {"id": "m2", "mime_type": "application/pdf", "filename": "pago.pdf"},
        },
        {"from": "598991"},
        {"type": "text", "text": {"body": "sin número"}},
    )

    mensajes = extract_messages(payload)

    assert [m.type for m in mensajes] == [
        "text",
        "interactive",
        "interactive",
        "interactive",
        "image",
        "document",
        "unknown",
    ]
    texto, lista, boton, otro, imagen, documento, desconocido = mensajes
    assert (texto.text, texto.wamid, texto.timestamp, texto.contact_name) == ("hola", "w1", "1", "Ana")
    assert (lista.interactive_id, lista.interactive_title) == ("op1", "Cuotas")
    assert (boton.interactive_id, boton.interactive_title) == ("si", "Sí")
    assert otro.interactive_id is None
    assert (imagen.media_id, imagen.media_mime_type) == ("m1", "image/jpeg")
    assert (documento.media_id, documento.media_filename) == ("m2", "pago.pdf")
    assert desconocido.text is None


def test_sin_contactos_ni_mensajes():
    assert extract_messages({}) == []
    mensajes = extract_messages(_payload({"from": "1", "type": "text", "text": {"body": "x"}}, contacto=None))
    assert mensajes[0].contact_name is None
