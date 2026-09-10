import json

import httpx
import pytest

from app.config import settings
from app.services_client import ServicesAPIError, ServicesClient


async def _mock_client(handler) -> ServicesClient:
    client = ServicesClient()
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url="https://services.test", transport=httpx.MockTransport(handler))
    return client


@pytest.mark.asyncio
async def test_protected_member_calls_use_correct_paths_and_api_key(monkeypatch):
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/inscripciones") and request.method == "POST":
            return httpx.Response(201, json={"id": "i1"})
        if request.url.path.endswith("/cuotas"):
            return httpx.Response(200, json=[{"id": "q1"}])
        return httpx.Response(200, json=[{"id": "i1"}])

    monkeypatch.setattr(settings, "bot_api_key", "bot-secret")
    client = await _mock_client(handler)
    try:
        assert await client.get_inscripciones_socio("s1") == [{"id": "i1"}]
        assert await client.get_cuotas_socio("s1") == [{"id": "q1"}]
        assert await client.post_inscripcion("s1", "a1") == {"id": "i1"}
    finally:
        await client.aclose()

    assert [request.url.path for request in calls] == [
        "/socios/s1/inscripciones",
        "/socios/s1/cuotas",
        "/inscripciones",
    ]
    assert all(request.headers["X-Api-Key"] == "bot-secret" for request in calls)


@pytest.mark.asyncio
async def test_every_enabled_member_read_uses_bot_auth_and_expected_route(monkeypatch):
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/socios/by-whatsapp/59899":
            return httpx.Response(200, json={"id": "s1"})
        if request.url.path == "/socios/s1":
            return httpx.Response(200, json={"id": "s1", "nombre": "Socio"})
        return httpx.Response(200, json=[])

    monkeypatch.setattr(settings, "bot_api_key", "bot-secret")
    client = await _mock_client(handler)
    try:
        assert await client.get_socio_by_whatsapp("59899") == {"id": "s1"}
        await client.get_socio_detalle("s1")
        await client.get_inscripciones_socio("s1")
        await client.get_cuotas_socio("s1")
        await client.get_espacios()
        await client.get_disponibilidad("2030-01-02")
        await client.get_reservas_socio("s1")
        await client.get_inscripciones_evento_socio("s1")
    finally:
        await client.aclose()

    assert [(request.method, request.url.path) for request in calls] == [
        ("GET", "/socios/by-whatsapp/59899"),
        ("GET", "/socios/s1"),
        ("GET", "/socios/s1/inscripciones"),
        ("GET", "/socios/s1/cuotas"),
        ("GET", "/espacios/"),
        ("GET", "/espacios/disponibilidad"),
        ("GET", "/socios/s1/reservas"),
        ("GET", "/socios/s1/evento-inscripciones"),
    ]
    assert dict(calls[5].url.params) == {"fecha": "2030-01-02"}
    assert all(request.headers["X-Api-Key"] == "bot-secret" for request in calls)


@pytest.mark.asyncio
async def test_every_bot_mutation_carries_auth_and_member_ownership(monkeypatch):
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201 if request.method == "POST" else 200, json={"mensaje": "ok"})

    monkeypatch.setattr(settings, "bot_api_key", "bot-secret")
    client = await _mock_client(handler)
    try:
        await client.post_inscripcion("s1", "a1")
        await client.delete_inscripcion("i1")
        await client.post_reserva("s1", "space1", "2030-01-02", "18:00", "20:00", 8, "Cumpleaños", None)
        await client.delete_reserva("r1", "s1")
        await client.post_inscripcion_evento("e1", "s1")
        await client.delete_inscripcion_evento("e1", "ei1")
    finally:
        await client.aclose()

    assert [(request.method, request.url.path) for request in calls] == [
        ("POST", "/inscripciones"),
        ("DELETE", "/inscripciones/i1"),
        ("POST", "/reservas/bot"),
        ("DELETE", "/reservas/r1/bot"),
        ("POST", "/eventos/e1/inscripciones"),
        ("DELETE", "/eventos/e1/inscripciones/ei1"),
    ]
    assert json.loads(calls[0].content) == {"socioId": "s1", "actividadId": "a1"}
    assert json.loads(calls[2].content) == {
        "socioId": "s1",
        "espacioId": "space1",
        "fecha": "2030-01-02",
        "horaInicio": "18:00:00",
        "horaFin": "20:00:00",
        "cantPersonas": 8,
        "motivo": "Cumpleaños",
        "notas": None,
    }
    assert dict(calls[3].url.params) == {"socioId": "s1"}
    assert json.loads(calls[4].content) == {"socioId": "s1"}
    assert all(request.headers["X-Api-Key"] == "bot-secret" for request in calls)


@pytest.mark.asyncio
async def test_inscription_404_is_an_error_and_optional_key_is_omitted(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert "X-Api-Key" not in request.headers
        return httpx.Response(404, json={"detail": "not found"})

    monkeypatch.setattr(settings, "bot_api_key", None)
    client = await _mock_client(handler)
    try:
        with pytest.raises(ServicesAPIError):
            await client.get_inscripciones_socio("missing")
    finally:
        await client.aclose()
