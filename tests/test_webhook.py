import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from app import main
from app.config import settings


def _signed_headers(body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = hmac.new(
        settings.router_shared_secret.encode(),
        timestamp.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Router-Timestamp": timestamp,
        "X-Router-Signature": f"sha256={signature}",
    }


@pytest.mark.asyncio
async def test_webhook_rejects_requests_not_signed_by_router():
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bot.test") as client:
        response = await client.post("/webhook", json={"entry": []})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_webhook_accepts_current_router_signature(monkeypatch):
    handled = []
    payload = {"entry": []}
    body = json.dumps(payload, separators=(",", ":")).encode()

    monkeypatch.setattr(main, "extract_messages", lambda _: [])
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bot.test") as client:
        response = await client.post("/webhook", content=body, headers=_signed_headers(body))

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert handled == []


@pytest.mark.asyncio
async def test_readiness_fails_when_shared_redis_is_required_but_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "require_redis", True)
    monkeypatch.setattr(main.sessions, "ping", AsyncMock(return_value=False))

    response = await main.health()

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "status": "unavailable",
        "sessionStore": "memory",
        "conversationTrace": "disabled",
    }


def test_router_signature_rejects_expired_timestamp():
    body = b"{}"
    timestamp = str(int(time.time()) - main.MAX_ROUTER_CLOCK_SKEW_SECONDS - 1)
    signature = hmac.new(
        settings.router_shared_secret.encode(),
        timestamp.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()

    assert not main._verify_router_request(body, timestamp, f"sha256={signature}")
