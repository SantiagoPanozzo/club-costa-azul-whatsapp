"""Unit tests for message_store index/insert behaviour (no live MongoDB needed)."""

import pytest

from app import message_store
from app.state import Session
from app.webhook_parser import IncomingMessage


class FakeCollection:
    def __init__(self, index_info=None):
        self._index_info = index_info or {}
        self.dropped = []
        self.created = []
        self.inserted = []

    async def index_information(self):
        return dict(self._index_info)

    async def drop_index(self, name):
        self.dropped.append(name)
        self._index_info.pop(name, None)

    async def create_index(self, key, **kwargs):
        self.created.append((key, kwargs))

    async def insert_one(self, doc):
        self.inserted.append(doc)


class DuplicateCollection(FakeCollection):
    async def insert_one(self, doc):
        raise Exception("E11000 duplicate key error collection: club_costa_azul.messages index: wamid_unique")


class FakeConversations:
    async def find_one_and_update(self, *args, **kwargs):
        return {"_id": "conv-1"}


async def test_wamid_index_is_partial_and_replaces_legacy_sparse_index(monkeypatch):
    fake = FakeCollection({"wamid_1": {"key": [("wamid", 1)], "unique": True, "sparse": True}})
    monkeypatch.setattr(message_store, "_messages", fake)

    await message_store._ensure_wamid_index()

    assert fake.dropped == ["wamid_1"]
    assert fake.created == [
        (
            "wamid",
            {
                "unique": True,
                "name": "wamid_unique",
                "partialFilterExpression": {"wamid": {"$type": "string"}},
            },
        )
    ]


async def test_wamid_index_created_when_no_legacy_index(monkeypatch):
    fake = FakeCollection()
    monkeypatch.setattr(message_store, "_messages", fake)

    await message_store._ensure_wamid_index()

    assert fake.dropped == []
    assert fake.created[0][1]["partialFilterExpression"] == {"wamid": {"$type": "string"}}


async def test_store_outgoing_omits_wamid_when_send_failed(monkeypatch):
    messages = FakeCollection()
    monkeypatch.setattr(message_store, "_messages", messages)
    monkeypatch.setattr(message_store, "_conversations", FakeConversations())

    await message_store.store_outgoing("59898287145", "text", {"body": "hola"}, None, Session())
    await message_store.store_outgoing("59898287145", "text", {"body": "hola"}, "wamid.ABC", Session())

    assert "wamid" not in messages.inserted[0]
    assert messages.inserted[1]["wamid"] == "wamid.ABC"


async def test_store_incoming_without_wamid_is_stored(monkeypatch):
    messages = FakeCollection()
    monkeypatch.setattr(message_store, "_messages", messages)
    monkeypatch.setattr(message_store, "_conversations", FakeConversations())

    incoming = IncomingMessage(phone="59898287145", type="text", text="hola")

    assert await message_store.store_incoming(incoming, Session()) is True
    assert "wamid" not in messages.inserted[0]


async def test_store_incoming_duplicate_wamid_is_skipped(monkeypatch):
    monkeypatch.setattr(message_store, "_messages", DuplicateCollection())
    monkeypatch.setattr(message_store, "_conversations", FakeConversations())

    incoming = IncomingMessage(phone="59898287145", type="text", text="hola", wamid="wamid.ABC")

    assert await message_store.store_incoming(incoming, Session()) is False


@pytest.mark.parametrize(
    "index_info",
    [
        {},
        {"wamid_1": {"key": [("wamid", 1)]}},
        {"wamid_unique": {"key": [("wamid", 1)]}},
    ],
)
async def test_wamid_index_creation_is_idempotent(monkeypatch, index_info):
    fake = FakeCollection(index_info)
    monkeypatch.setattr(message_store, "_messages", fake)

    await message_store._ensure_wamid_index()

    assert len(fake.created) == 1
