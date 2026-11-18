from unittest.mock import AsyncMock

import pytest

from app import usage_metrics


@pytest.mark.asyncio
async def test_initialize_requires_secret_and_all_indexes(monkeypatch):
    collection = AsyncMock()
    monkeypatch.setattr(usage_metrics, "_collection", collection)
    monkeypatch.setattr(usage_metrics.settings, "metrics_hmac_key", "")
    await usage_metrics.initialize()
    assert not usage_metrics._ready
    collection.create_index.assert_not_awaited()

    monkeypatch.setattr(usage_metrics.settings, "metrics_hmac_key", "synthetic-key")
    await usage_metrics.initialize()
    assert usage_metrics._ready
    collection.create_index.assert_any_await("occurred_at", expireAfterSeconds=90 * 24 * 60 * 60)
    await usage_metrics.close()


@pytest.mark.asyncio
async def test_index_failure_disables_writes(monkeypatch):
    collection = AsyncMock()
    collection.create_index.side_effect = RuntimeError("unavailable")
    monkeypatch.setattr(usage_metrics, "_collection", collection)
    monkeypatch.setattr(usage_metrics.settings, "metrics_hmac_key", "synthetic-key")
    await usage_metrics.initialize()
    usage_metrics.record_activity("synthetic-phone", "synthetic-wamid", "identified")
    assert not usage_metrics._ready
    collection.insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_activity_is_pseudonymous_and_retry_id_is_stable(monkeypatch):
    collection = AsyncMock()
    monkeypatch.setattr(usage_metrics, "_collection", collection)
    monkeypatch.setattr(usage_metrics.settings, "metrics_hmac_key", "synthetic-key")
    await usage_metrics.initialize()
    usage_metrics.record_activity("synthetic-phone", "synthetic-wamid", "unidentified")
    usage_metrics.record_activity("synthetic-phone", "synthetic-wamid", "identified")
    import asyncio

    await asyncio.gather(*list(usage_metrics._pending))
    first, second = [call.args[0] for call in collection.insert_one.await_args_list]
    assert first["event_id"] == second["event_id"]
    assert first["actor_id"] == second["actor_id"]
    assert first["identification"] == "unidentified"
    assert "synthetic-phone" not in str(first)
    assert "synthetic-wamid" not in str(first)
    assert first["occurred_at"].tzinfo is not None
    await usage_metrics.close()


@pytest.mark.asyncio
async def test_storage_failure_is_contained(monkeypatch):
    import asyncio

    collection = AsyncMock()
    collection.insert_one.side_effect = RuntimeError("unavailable")
    monkeypatch.setattr(usage_metrics, "_collection", collection)
    monkeypatch.setattr(usage_metrics.settings, "metrics_hmac_key", "synthetic-key")
    await usage_metrics.initialize()
    usage_metrics.record_activity("synthetic-phone", None, "invalid")
    await asyncio.gather(*list(usage_metrics._pending))
    assert collection.insert_one.await_args.args[0]["identification"] == "unknown"
    await usage_metrics.close()


@pytest.mark.asyncio
async def test_feature_ids_are_event_specific_and_duplicate_safe(monkeypatch):
    import asyncio

    collection = AsyncMock()
    monkeypatch.setattr(usage_metrics, "_collection", collection)
    monkeypatch.setattr(usage_metrics.settings, "metrics_hmac_key", "synthetic-key")
    await usage_metrics.initialize()
    usage_metrics.record_activity("phone", "wamid", "identified")
    usage_metrics.record_feature("phone", "wamid", "activity.view")
    usage_metrics.record_feature("phone", "wamid", "activity.view")
    usage_metrics.record_feature("phone", "wamid", "activity.enroll")
    usage_metrics.record_feature("phone", "wamid", "unrecognized")
    await asyncio.gather(*list(usage_metrics._pending))
    docs = [call.args[0] for call in collection.insert_one.await_args_list]
    assert [doc["event"] for doc in docs] == ["bot.activity", "activity.view", "activity.view", "activity.enroll"]
    assert docs[1]["event_id"] == docs[2]["event_id"]
    assert len({docs[0]["event_id"], docs[1]["event_id"], docs[3]["event_id"]}) == 3
    assert docs[0]["event_id"] == usage_metrics._digest("bot:activity:wamid")
    assert all(doc["actor_id"] == docs[0]["actor_id"] for doc in docs)
    await usage_metrics.close()
