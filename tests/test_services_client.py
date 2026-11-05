import json

import httpx
import pytest

from app.config import settings
from app.services_client import (
    ServicesAPIBadRequest,
    ServicesAPIConflict,
    ServicesAPICupoAgotado,
    ServicesAPIError,
    ServicesAPINoEncontrado,
    ServicesClient,
)


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
    client = ServicesClient()
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url="https://services.test", transport=httpx.MockTransport(handler))
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
async def test_contrato_de_clases_detalle_alta_y_baja_puntual(monkeypatch):
    llamadas = []

    async def responder(request):
        llamadas.append(request)
        return httpx.Response(200, json={"mensaje": "OK"})

    monkeypatch.setattr(settings, "bot_api_key", "clave-sintetica")
    cliente = ServicesClient()
    await cliente._client.aclose()
    cliente._client = httpx.AsyncClient(base_url="https://services.test", transport=httpx.MockTransport(responder))
    try:
        await cliente.get_instancias_actividad("a1")
        await cliente.get_inscripcion("i1")
        await cliente.post_inscripcion("s1", "a1", instancia_id="c1")
        await cliente.post_inscripcion("s1", "a1")
        await cliente.dar_baja_instancia("c1", "s1")
    finally:
        await cliente.aclose()
    assert [r.url.path for r in llamadas] == [
        "/actividades/a1/instancias",
        "/inscripciones/i1",
        "/inscripciones",
        "/inscripciones",
        "/instancias/c1/inscripcion",
    ]
    assert json.loads(llamadas[2].content) == {"socioId": "s1", "actividadId": "a1", "instanciaId": "c1"}
    assert json.loads(llamadas[3].content) == {"socioId": "s1", "actividadId": "a1"}
    assert llamadas[4].method == "DELETE"
    assert llamadas[4].url.params["socioId"] == "s1"
    assert all(r.headers["X-Api-Key"] == "clave-sintetica" for r in llamadas)


@pytest.mark.parametrize(
    "estado,cuerpo,error",
    [
        (409, {"mensaje": "La clase no tiene cupo: detalle SQL privado"}, ServicesAPICupoAgotado),
        (409, {"mensaje": "SQL privado"}, ServicesAPIConflict),
        (400, {"mensaje": "SQL privado"}, ServicesAPIBadRequest),
        (404, {"mensaje": "SQL privado"}, ServicesAPINoEncontrado),
        (500, {"mensaje": "SQL privado"}, ServicesAPIError),
        (409, ["SQL privado"], ServicesAPIConflict),
    ],
)
async def test_errores_inscripciones_tipados_sin_detalles_internos(estado, cuerpo, error):
    async def responder(request):
        return httpx.Response(estado, json=cuerpo)

    cliente = ServicesClient()
    await cliente._client.aclose()
    cliente._client = httpx.AsyncClient(base_url="https://services.test", transport=httpx.MockTransport(responder))
    try:
        with pytest.raises(error) as resultado:
            await cliente.post_inscripcion("s1", "a1", "c1")
        assert "SQL privado" not in str(resultado.value)
    finally:
        await cliente.aclose()


@pytest.mark.asyncio
async def test_inscription_404_is_an_error_and_optional_key_is_omitted(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert "X-Api-Key" not in request.headers
        return httpx.Response(404, json={"detail": "not found"})

    monkeypatch.setattr(settings, "bot_api_key", None)
    client = ServicesClient()
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url="https://services.test", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ServicesAPIError):
            await client.get_inscripciones_socio("missing")
    finally:
        await client.aclose()
