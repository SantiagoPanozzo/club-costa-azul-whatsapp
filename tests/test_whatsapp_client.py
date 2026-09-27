"""Tests for the Graph API error hints and the phone-number self-check."""

import httpx

from app.whatsapp_client import WhatsAppClient, _graph_error_hint


async def _client_with(handler) -> WhatsAppClient:
    client = WhatsAppClient()
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def test_hint_for_unsupported_method_points_at_phone_number_id():
    body = '{"error":{"message":"Unsupported request - method type: post","code":100,"type":"GraphMethodException"}}'
    hint = _graph_error_hint(body)
    assert "WHATSAPP_PHONE_NUMBER_ID" in hint
    assert "WABA" in hint


def test_hint_for_authentication_error_points_at_token():
    body = '{"error":{"message":"Authentication Error","code":190,"type":"OAuthException"}}'
    hint = _graph_error_hint(body)
    assert "WHATSAPP_API_TOKEN" in hint


def test_hint_for_waba_id_points_at_phone_number_id():
    body = '{"error":{"message":"(#100) Tried accessing nonexisting field (display_phone_number)"}}'
    hint = _graph_error_hint(body)
    assert "WABA" in hint


def test_no_hint_for_unrelated_errors():
    assert _graph_error_hint('{"error":{"message":"some other failure"}}') == ""


async def test_check_phone_number_resolves_display_number():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/test-phone")
        assert request.url.params["fields"] == "display_phone_number,verified_name"
        return httpx.Response(200, json={"display_phone_number": "+598 99 123 456", "verified_name": "Club"})

    client = await _client_with(handler)
    try:
        data = await client.check_phone_number()
    finally:
        await client.aclose()

    assert data == {"display_phone_number": "+598 99 123 456", "verified_name": "Club"}


async def test_check_phone_number_returns_none_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "Unsupported request - method type: get", "code": 100}})

    client = await _client_with(handler)
    try:
        assert await client.check_phone_number() is None
    finally:
        await client.aclose()
