import httpx
import pytest

from app.config import settings
from app.services_client import ServicesAPIError, ServicesClient


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
