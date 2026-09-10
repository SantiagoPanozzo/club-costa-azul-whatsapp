import httpx
import pytest

from app.whatsapp_client import WhatsAppClient, WhatsAppDeliveryError


@pytest.mark.asyncio
async def test_delivery_failure_is_propagated_to_webhook_processing():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "temporarily unavailable"}})

    client = WhatsAppClient()
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(WhatsAppDeliveryError):
            await client.send_text("59899000000", "hola")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_success_requires_meta_message_id():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": [{}]})

    client = WhatsAppClient()
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(WhatsAppDeliveryError, match="message ID"):
            await client.send_text("59899000000", "hola")
    finally:
        await client.aclose()
