"""Contrato del cliente de la API de servicios: cada llamada usa la ruta correcta y traduce
éxito, errores HTTP y caídas de red a las excepciones que esperan los flujos."""

import httpx
import pytest

from app.config import settings
from app.services_client import (
    ServicesAPIBadRequest,
    ServicesAPIConflict,
    ServicesAPIError,
    ServicesClient,
)

# (método, argumentos, verbo HTTP, ruta esperada)
LLAMADAS = [
    ("get_socio_by_whatsapp", ("59899123456",), "GET", "/socios/by-whatsapp/59899123456"),
    ("get_actividades", (), "GET", "/actividades"),
    ("get_inscripciones_socio", ("s1",), "GET", "/socios/s1/inscripciones"),
    ("get_cuotas_socio", ("s1",), "GET", "/socios/s1/cuotas"),
    ("get_socio_detalle", ("s1",), "GET", "/socios/s1"),
    ("get_espacios", (), "GET", "/espacios/"),
    ("get_disponibilidad", ("2026-12-01",), "GET", "/espacios/disponibilidad"),
    ("get_reservas_socio", ("s1",), "GET", "/socios/s1/reservas"),
    ("post_reserva", ("s1", "e1", "2026-12-01", "10:00:00", "12:00:00", 10, "Cumpleaños"), "POST", "/reservas"),
    ("delete_reserva", ("r1", "s1"), "DELETE", "/reservas/r1"),
    (
        "upload_comprobante",
        ("s1", "c1", b"%PDF", "pago.pdf", "application/pdf"),
        "POST",
        "/socios/s1/cuotas/c1/comprobantes",
    ),
    ("get_noticias", (), "GET", "/noticias"),
    ("get_eventos", (), "GET", "/eventos/"),
    ("get_inscripciones_evento_socio", ("s1",), "GET", "/socios/s1/evento-inscripciones"),
    ("post_inscripcion_evento", ("ev1", "s1"), "POST", "/eventos/ev1/inscripciones"),
    ("delete_inscripcion_evento", ("ev1", "i1"), "DELETE", "/eventos/ev1/inscripciones/i1"),
]

IDS = [llamada[0] for llamada in LLAMADAS]


def _cliente(handler) -> ServicesClient:
    cliente = ServicesClient()
    cliente._client = httpx.AsyncClient(base_url="https://services.test", transport=httpx.MockTransport(handler))
    return cliente


@pytest.fixture(autouse=True)
def _clave_del_bot(monkeypatch):
    monkeypatch.setattr(settings, "bot_api_key", "bot-secret")


@pytest.mark.asyncio
@pytest.mark.parametrize(("metodo", "args", "verbo", "ruta"), LLAMADAS, ids=IDS)
async def test_exito_devuelve_el_json_y_usa_la_ruta(metodo, args, verbo, ruta):
    pedidos: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        pedidos.append(request)
        return httpx.Response(200, json={"ok": True})

    cliente = _cliente(handler)
    try:
        assert await getattr(cliente, metodo)(*args) == {"ok": True}
    finally:
        await cliente.aclose()

    assert pedidos[0].method == verbo
    assert pedidos[0].url.path == ruta


@pytest.mark.asyncio
@pytest.mark.parametrize(("metodo", "args", "verbo", "ruta"), LLAMADAS, ids=IDS)
async def test_error_del_servidor_es_services_api_error(metodo, args, verbo, ruta):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"mensaje": "boom"})

    cliente = _cliente(handler)
    try:
        with pytest.raises(ServicesAPIError):
            await getattr(cliente, metodo)(*args)
    finally:
        await cliente.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("metodo", "args", "verbo", "ruta"), LLAMADAS, ids=IDS)
async def test_caida_de_red_es_services_api_error(metodo, args, verbo, ruta):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin red", request=request)

    cliente = _cliente(handler)
    try:
        with pytest.raises(ServicesAPIError):
            await getattr(cliente, metodo)(*args)
    finally:
        await cliente.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metodo", "args", "esperado"),
    [
        ("get_socio_by_whatsapp", ("59899123456",), None),
        ("get_socio_detalle", ("s1",), None),
        ("get_reservas_socio", ("s1",), []),
        ("get_inscripciones_evento_socio", ("s1",), []),
    ],
)
async def test_404_es_ausencia_y_no_error(metodo, args, esperado):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    cliente = _cliente(handler)
    try:
        assert await getattr(cliente, metodo)(*args) == esperado
    finally:
        await cliente.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metodo", "args", "codigo", "excepcion", "mensaje"),
    [
        (
            "post_reserva",
            ("s1", "e1", "2026-12-01", "10:00:00", "12:00:00", 10, "x"),
            409,
            ServicesAPIConflict,
            "Ocupado.",
        ),
        ("post_inscripcion_evento", ("ev1", "s1"), 409, ServicesAPIConflict, "Sin cupo."),
        (
            "upload_comprobante",
            ("s1", "c1", b"x", "a.pdf", "application/pdf"),
            400,
            ServicesAPIBadRequest,
            "Archivo inválido.",
        ),
    ],
)
async def test_rechazos_de_negocio_traen_el_mensaje_de_la_api(metodo, args, codigo, excepcion, mensaje):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(codigo, json={"mensaje": mensaje})

    cliente = _cliente(handler)
    try:
        with pytest.raises(excepcion, match=mensaje):
            await getattr(cliente, metodo)(*args)
    finally:
        await cliente.aclose()


@pytest.mark.asyncio
async def test_rechazos_sin_mensaje_usan_un_texto_por_defecto():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409 if request.method == "POST" and "reservas" in request.url.path else 400, json={})

    cliente = _cliente(handler)
    try:
        with pytest.raises(ServicesAPIConflict, match="ya está reservado"):
            await cliente.post_reserva("s1", "e1", "2026-12-01", "10:00:00", "12:00:00", 10, "x")
        with pytest.raises(ServicesAPIBadRequest, match="Error al subir"):
            await cliente.upload_comprobante("s1", "c1", b"x", "a.pdf", "application/pdf")
    finally:
        await cliente.aclose()


@pytest.mark.asyncio
async def test_reserva_manda_el_cuerpo_y_la_clave_del_bot():
    pedidos: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        pedidos.append(request)
        return httpx.Response(201, json={"id": "r1"})

    cliente = _cliente(handler)
    try:
        await cliente.post_reserva("s1", "e1", "2026-12-01", "10:00:00", "12:00:00", 10, "Cumpleaños")
        await cliente.delete_reserva("r1", "s1")
        await cliente.get_disponibilidad("2026-12-01")
    finally:
        await cliente.aclose()

    assert pedidos[0].headers["X-Api-Key"] == "bot-secret"
    assert b'"cantPersonas":10' in pedidos[0].content.replace(b" ", b"")
    assert pedidos[1].url.params["socioId"] == "s1"
    assert pedidos[2].url.params["fecha"] == "2026-12-01"


def test_sin_clave_no_manda_el_encabezado(monkeypatch):
    monkeypatch.setattr(settings, "bot_api_key", "")
    assert ServicesClient()._auth_headers() == {}
