import asyncio
import os

import pytest

from app.config import settings
from app.state import SessionStore


@pytest.mark.asyncio
async def test_redis_shares_sessions_dedupe_mutations_and_phone_lock(monkeypatch):
    redis_url = os.environ.get("REDIS_TEST_URL")
    if not redis_url:
        pytest.skip("Set REDIS_TEST_URL to run the disposable Redis integration test")

    monkeypatch.setattr(settings, "session_redis_url", redis_url)
    first = SessionStore()
    second = SessionStore()
    await first.initialize()
    await second.initialize()
    assert first._redis is not None
    await first._redis.flushdb()

    try:
        async with first.locked("59899000000") as session:
            session.socio = {"id": "member-1", "nombre": "Socio"}
        async with second.locked("59899000000") as session:
            assert session.socio == {"id": "member-1", "nombre": "Socio"}

        assert await first.claim_message("wamid.shared")
        await first.mark_message_processed("wamid.shared")
        assert not await second.claim_message("wamid.shared")

        await first.mark_mutation_applied("wamid.confirm", "create-reservation")
        assert await second.was_mutation_applied("wamid.confirm", "create-reservation")

        entered_first = asyncio.Event()
        release_first = asyncio.Event()
        entered_second = asyncio.Event()

        async def hold_first_lock():
            async with first.locked("59899000001"):
                entered_first.set()
                await release_first.wait()

        async def wait_for_same_phone():
            await entered_first.wait()
            async with second.locked("59899000001"):
                entered_second.set()

        first_task = asyncio.create_task(hold_first_lock())
        second_task = asyncio.create_task(wait_for_same_phone())
        await entered_first.wait()
        await asyncio.sleep(0.05)
        assert not entered_second.is_set()
        release_first.set()
        await asyncio.gather(first_task, second_task)
        assert entered_second.is_set()
    finally:
        await first._redis.flushdb()
        await first.close()
        await second.close()
