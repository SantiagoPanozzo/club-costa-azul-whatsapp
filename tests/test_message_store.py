from app import message_store


async def test_mongodb_index_failure_disables_trace_without_failing_startup(monkeypatch):
    class UnavailableCollection:
        async def create_index(self, *_args, **_kwargs):
            raise ConnectionError("synthetic MongoDB outage")

    monkeypatch.setattr(message_store, "_conversations", UnavailableCollection())
    monkeypatch.setattr(message_store, "_messages", UnavailableCollection())
    monkeypatch.setattr(message_store, "_enabled", True)

    await message_store.ensure_indexes()

    assert not message_store.is_enabled()
