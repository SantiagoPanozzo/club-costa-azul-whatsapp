from unittest.mock import AsyncMock

import pytest

from app import message_store
from app.flow_ui import adaptive_summary, paginate_rows, split_text
from app.state import Session
from app.storing_client import StoringClient
from app.whatsapp_client import whatsapp_client


@pytest.mark.parametrize("limit", [1, 2, 100, 1024, 4096])
@pytest.mark.parametrize("body", ["", "x" * 10000, "\n" * 5000, "á🙂\n" * 3000])
def test_text_splitting_is_lossless_and_bounded(body, limit):
    chunks = split_text(body, limit)
    assert "".join(chunks) == body
    assert all(len(chunk) <= limit for chunk in chunks)
    assert all(chunks) or body == ""


def test_nonpositive_split_limit_is_rejected():
    with pytest.raises(ValueError):
        split_text("abc", 0)


def test_paginated_rows_reach_every_item_and_keep_navigation():
    choices = [{"id": str(i), "title": str(i)} for i in range(30)]
    seen = []
    for page in range(5):
        rows, actual = paginate_rows(choices, page, page_prefix="page_", extra_rows=[{"id": "mine"}, {"id": "menu"}])
        assert actual == page
        assert len(rows) <= 10
        assert {"mine", "menu"}.issubset({r["id"] for r in rows})
        seen.extend(r["id"] for r in rows if r["id"].isdigit())
    assert seen == [str(i) for i in range(30)]


def test_adaptive_summary_uses_richest_fitting_option():
    args = {"empty": "Sin inscripciones", "limit": 150}
    assert "new\nold" in adaptive_summary("Resumen", ["new", "old"], "Elegí", **args)
    reduced = adaptive_summary("Resumen", ["new", "old" * 100], "Elegí", **args)
    assert "new" in reduced and "otras 1" in reduced and "old" not in reduced
    smallest = adaptive_summary("Resumen", ["new" * 100, "old"], "Elegí", **args)
    assert "Tenés 2 inscripciones" in smallest and len(smallest) <= 150


@pytest.mark.parametrize("kind,limit", [("text", 4096), ("buttons", 1024), ("list", 1024)])
async def test_wrapper_overflow_preserves_every_character_and_tracks_each_send(monkeypatch, kind, limit):
    sent = []

    async def capture(payload):
        sent.append(payload)
        return f"id-{len(sent)}"

    monkeypatch.setattr(whatsapp_client, "_send", AsyncMock(side_effect=capture))
    record = AsyncMock()
    monkeypatch.setattr(message_store, "store_outgoing", record)
    session = Session()
    session.begin_run("test")
    body = ("Información completa🙂\n" * 550) + "¿Confirmás?"
    client = StoringClient()
    if kind == "text":
        await client.send_text("synthetic", body, session=session)
    elif kind == "buttons":
        await client.send_buttons("synthetic", body, [("yes", "Confirmar")], session=session)
    else:
        await client.send_list("synthetic", body, "Opciones", [{"id": "one", "title": "Uno"}], session=session)
    parts = [p["text"]["body"] if p["type"] == "text" else p["interactive"]["body"]["text"] for p in sent]
    assert "".join(parts) == body
    assert all(len(part) <= 4096 for part in parts)
    assert len(parts[-1]) <= limit
    assert session.run.outbound_attempts == session.run.api_accepted == len(sent) == record.await_count


async def test_invalid_buttons_fail_before_any_overflow_is_sent(monkeypatch):
    sender = AsyncMock()
    monkeypatch.setattr(whatsapp_client, "_send", sender)
    with pytest.raises(ValueError):
        await StoringClient().send_buttons("synthetic", "x" * 10000, [(str(i), str(i)) for i in range(4)])
    sender.assert_not_awaited()


async def test_stored_rows_match_normalized_wire_payload(transcript):
    from app.storing_client import storing_client

    await storing_client.send_list(
        "synthetic",
        "Elegí",
        "x" * 40,
        [{"id": "one", "title": "á" * 50, "description": "ñ" * 100}],
        "s" * 50,
        session=transcript.session,
    )
    content = transcript.outgoing.await_args.kwargs["content"]
    interactive = transcript.payloads[-1]["interactive"]
    assert content["rows"] == interactive["action"]["sections"][0]["rows"]
    assert content["button_text"] == interactive["action"]["button"]
    assert content["section_title"] == interactive["action"]["sections"][0]["title"]
