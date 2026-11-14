from copy import deepcopy
from unittest.mock import AsyncMock

import httpx
import pytest

from app import main, message_store
from app.state import Session
from app.webhook_parser import IncomingMessage, extract_statuses
from app.whatsapp_client import whatsapp_client
from tests.test_message_store import DuplicateCollection, FakeCollection, FakeConversations


class Upserts:
    def __init__(self):
        self.docs = {}

    async def update_one(self, filter_, update, **kwargs):
        fresh = filter_["_id"] not in self.docs
        doc = self.docs.setdefault(filter_["_id"], {})
        changes = {**(update.get("$setOnInsert", {}) if fresh else {}), **update.get("$set", {})}
        for path, value in changes.items():
            target = doc
            keys = path.split(".")
            for key in keys[:-1]:
                target = target.setdefault(key, {})
            target[keys[-1]] = deepcopy(value)


async def test_duplicate_does_not_increment_conversation_counter(monkeypatch):
    conversations = FakeConversations()
    monkeypatch.setattr(message_store, "_messages", DuplicateCollection())
    monkeypatch.setattr(message_store, "_conversations", conversations)
    assert not await message_store.store_incoming(IncomingMessage("synthetic", "text", wamid="id"), Session())
    assert conversations.increments == []


async def test_entry_and_completion_keep_run_attribution_despite_flow_reset(monkeypatch):
    messages = FakeCollection()
    conversations = FakeConversations()
    monkeypatch.setattr(message_store, "_messages", messages)
    monkeypatch.setattr(message_store, "_conversations", conversations)
    session = Session(turn_id="turn-1")
    session.begin_run("activities")
    run_id = session.run.id
    assert await message_store.store_incoming(IncomingMessage("synthetic", "interactive", wamid="in-1"), session)
    session.set_task("enroll_class")
    session.finish("Inscripción confirmada")
    session.response_role = "menu"
    await message_store.store_outgoing("synthetic", "list", {"body": "Resultado y menú"}, "out-1", session)
    assert session.active_flow is None
    assert {d["task_run_id"] for d in messages.inserted} == {run_id}
    assert {d["flow"] for d in messages.inserted} == {"activities"}
    assert messages.inserted[1]["response_role"] == "menu"
    assert messages.inserted[1]["task"] == "enroll_class"
    assert len(conversations.increments) == 2


async def test_run_persistence_includes_explicit_outcome_version_and_counters(monkeypatch):
    collection = Upserts()
    monkeypatch.setattr(message_store, "_task_runs", collection)
    monkeypatch.setenv("BOT_RELEASE", "synthetic-release")
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    session = Session()
    session.begin_run("reservas")
    session.set_task("create")
    session.run.outbound_attempts = 7
    session.run.api_accepted = 6
    session.finish("Registrada")
    await message_store.save_run(session.run, "synthetic")
    doc = collection.docs[session.run.id]
    assert doc["outcome"] == "success" and doc["release"] == "synthetic-release"
    assert doc["outbound_attempts"] == 7 and doc["api_accepted"] == 6
    assert doc["finished_at"] >= doc["started_at"] and doc["process_id"]


async def test_storage_failure_does_not_block_run_processing(monkeypatch):
    monkeypatch.setattr(
        message_store, "_task_runs", type("Broken", (), {"update_one": AsyncMock(side_effect=RuntimeError)})()
    )
    session = Session()
    session.begin_run("activities")
    await message_store.save_run(session.run, "synthetic")


async def test_unconfirmed_send_is_not_counted_as_api_accepted(transcript, monkeypatch):
    monkeypatch.setattr(whatsapp_client, "_send", AsyncMock(return_value=None))
    await transcript.send("activities")
    assert transcript.session.run.outbound_attempts == 1
    assert transcript.session.run.api_accepted == 0


async def test_out_of_order_duplicate_receipts_preserve_delivery_and_pricing(monkeypatch):
    collection = Upserts()
    monkeypatch.setattr(message_store, "_message_statuses", collection)
    base = {"id": "wamid-1", "recipient_id": "synthetic", "timestamp": "1800000000"}
    pricing = {"billable": True, "pricing_model": "PMP", "category": "service", "type": "regular"}
    for status in ["delivered", "read", "sent", "delivered", "failed"]:
        await message_store.store_status(
            {**base, "status": status, **({"pricing": pricing} if status == "delivered" else {})}
        )
    assert len(collection.docs) == 1
    doc = collection.docs["wamid-1"]
    assert doc["wamid"] == "wamid-1"
    assert set(doc["statuses"]) == {"delivered", "read", "sent", "failed"}
    assert doc["pricing"] == pricing
    # No outgoing message need exist yet: early receipts are retained for a later join.


async def test_nonbillable_and_missing_pricing_are_not_treated_as_billable(monkeypatch):
    collection = Upserts()
    monkeypatch.setattr(message_store, "_message_statuses", collection)
    await message_store.store_status({"id": "free", "status": "delivered", "pricing": {"billable": False}})
    await message_store.store_status({"id": "unknown", "status": "delivered"})
    assert collection.docs["free"]["pricing"]["billable"] is False
    assert "pricing" not in collection.docs["unknown"]


@pytest.mark.parametrize(
    "status", [{}, {"id": "id", "status": "invalid"}, {"id": {}, "status": "delivered"}, {"id": "id", "status": []}]
)
async def test_malformed_status_ignored(monkeypatch, status):
    collection = Upserts()
    monkeypatch.setattr(message_store, "_message_statuses", collection)
    await message_store.store_status(status)
    assert not collection.docs


def test_extract_statuses_filters_nonobjects_and_handles_missing_statuses():
    payload = {"entry": [{"changes": [{"value": {"statuses": [{"id": "a"}, None]}}, {"value": {"statuses": None}}]}]}
    assert extract_statuses(payload) == [{"id": "a"}]


async def test_status_only_webhook_records_receipts_without_starting_conversation(monkeypatch):
    store = AsyncMock()
    handle = AsyncMock()
    monkeypatch.setattr(message_store, "store_status", store)
    monkeypatch.setattr(main, "handle_message", handle)
    monkeypatch.setattr(main.settings, "router_secret", "")
    status = {"id": "early-id", "status": "delivered", "timestamp": "1800000000"}
    payload = {"entry": [{"changes": [{"value": {"statuses": [status]}}]}]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="https://test") as client:
        response = await client.post("/webhook", json=payload)
    assert response.status_code == 200
    store.assert_awaited_once_with(status)
    handle.assert_not_awaited()


async def test_router_secret_also_protects_status_webhooks(monkeypatch):
    store = AsyncMock()
    monkeypatch.setattr(message_store, "store_status", store)
    monkeypatch.setattr(main.settings, "router_secret", "synthetic-secret")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="https://test") as client:
        response = await client.post("/webhook", json={"entry": []})
    assert response.status_code == 403
    store.assert_not_awaited()
