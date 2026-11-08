from copy import deepcopy

import pytest

from app.config import settings
from app.flows.cuotas import (
    BACK_ID,
    CUOTA_PREFIX,
    INSTRUCTIONS,
    OTHER_ID,
    PAGE_PREFIX,
    STATEMENT_ID,
    STATEMENT_PAGE_PREFIX,
    CuotasStep,
    _entry,
)
from app.services_client import ServicesAPIBadRequest, ServicesAPIError
from app.whatsapp_client import MAX_MEDIA_SIZE, whatsapp_client
from tests.transcript import QUOTA


def quota(index=1, **changes):
    return {**deepcopy(QUOTA), "id": f"q{index}", "periodo": f"2026-{index:02}", **changes}


def rows(payload):
    return payload["interactive"]["action"]["sections"][0]["rows"]


def button_ids(payload):
    return [b["reply"]["id"] for b in payload["interactive"]["action"]["buttons"]]


def assert_bounded(transcript):
    assert all(len(turn) == 1 for turn in transcript.turns)
    for payload in transcript.payloads:
        if payload["type"] == "text":
            assert len(payload["text"]["body"]) <= 4096
            continue
        interactive = payload["interactive"]
        limit = settings.whatsapp_list_body_limit if interactive["type"] == "list" else 1024
        assert len(interactive["body"]["text"]) <= limit
        if interactive["type"] == "list":
            assert len(rows(payload)) <= 10
        else:
            assert len(interactive["action"]["buttons"]) <= 3


async def receipt(transcript, **changes):
    return await transcript.send(
        **{"type": "document", "media_id": "receipt", "media_mime_type": "application/pdf", **changes}
    )


async def read_statement(transcript):
    payload = await transcript.send(STATEMENT_ID)
    contents = []
    for _ in range(100):
        contents.append(transcript.bodies[-1].split("\n\n", 1)[1])
        next_ids = [row["id"] for row in rows(payload) if row["id"].startswith(STATEMENT_PAGE_PREFIX)]
        current = transcript.session.flow_state.statement_page
        next_id = f"{STATEMENT_PAGE_PREFIX}{current + 1}"
        if next_id not in next_ids:
            return "".join(contents)
        payload = await transcript.send(next_id)
    pytest.fail("Statement did not terminate")


async def test_multiple_normal_budget_three_messages(transcript):
    transcript.api.get_cuotas_socio.return_value = [quota(), quota(2, monto=123.45), quota(3, estado="Pagada")]
    first = await transcript.send("cuotas")
    assert first["interactive"]["type"] == "list"
    assert {r["id"] for r in rows(first)} == {"cuota_q1", "cuota_q2", STATEMENT_ID, "global_menu"}
    assert "Pagada" in transcript.bodies[0]
    assert "Subir comprobante" not in transcript.bodies[0]
    transcript.api.upload_comprobante.assert_not_awaited()

    second = await transcript.send("cuota_q2")
    assert "2026-02" in transcript.bodies[1]
    assert "$123.45" in transcript.bodies[1]
    assert INSTRUCTIONS in transcript.bodies[1]
    assert button_ids(second) == [STATEMENT_ID, OTHER_ID, "global_menu"]
    assert transcript.session.run.task == "upload_receipt"
    await receipt(transcript)

    transcript.assert_single_responses(3)
    transcript.api.upload_comprobante.assert_awaited_once_with(
        "s1", "q2", b"synthetic-receipt", "comprobante.pdf", "application/pdf"
    )
    assert "2026-02" in transcript.bodies[-1]
    assert "$123.45" in transcript.bodies[-1]
    assert "pendiente de validación por el club" in transcript.bodies[-1]
    assert "¿Qué querés hacer?" in transcript.bodies[-1]
    assert transcript.session.active_flow is None
    assert_bounded(transcript)


async def test_singleton_normal_budget_two_messages_no_upload_until_media(transcript):
    first = await transcript.send("cuotas")
    assert transcript.session.flow_state.selected_cuota_id == "q1"
    assert transcript.session.flow_state.step == CuotasStep.AWAITING_COMPROBANTE
    assert "2026-11" in transcript.bodies[0]
    assert "$100.00" in transcript.bodies[0]
    assert "Atrasada" in transcript.bodies[0]
    assert INSTRUCTIONS in transcript.bodies[0]
    assert button_ids(first) == [STATEMENT_ID, "global_menu"]
    whatsapp_client.download_media.assert_not_awaited()
    transcript.api.upload_comprobante.assert_not_awaited()
    await receipt(transcript)
    transcript.assert_single_responses(2)
    assert_bounded(transcript)


@pytest.mark.parametrize("cuotas", [[], [quota(estado="Pagada"), quota(2, estado="Anulada")]])
async def test_no_pending_short_statement_finishes_with_one_result_menu(transcript, cuotas):
    transcript.api.get_cuotas_socio.return_value = cuotas
    payload = await transcript.send("cuotas")
    transcript.assert_single_responses(1)
    assert transcript.session.active_flow is None
    assert "¿Qué querés hacer?" in transcript.bodies[0]
    for cuota_data in cuotas:
        assert _entry(cuota_data) in transcript.bodies[0]
    assert "activities" in {row["id"] for row in rows(payload)}
    transcript.api.upload_comprobante.assert_not_awaited()


async def test_no_pending_long_statement_stays_accessible_including_paid_and_annulled(transcript):
    cuotas = [quota(i, estado="Pagada" if i % 2 else "Anulada") for i in range(1, 80)]
    transcript.api.get_cuotas_socio.return_value = cuotas
    payload = await transcript.send("cuotas")
    assert transcript.session.active_flow == "cuotas"
    assert STATEMENT_ID in button_ids(payload)
    assert "No tenés cuotas pendientes" in transcript.bodies[0]
    assert await read_statement(transcript) == "\n".join(_entry(c) for c in cuotas)
    await transcript.send(BACK_ID)
    assert transcript.session.active_flow == "cuotas"
    await transcript.send("global_menu")
    assert transcript.session.active_flow is None
    assert_bounded(transcript)


@pytest.mark.parametrize("single", [False, True])
async def test_full_statement_preserves_selected_quota_and_returns_to_instructions(transcript, single):
    cuotas = [quota()] if single else [quota(), quota(2)]
    cuotas += [quota(i, estado="Pagada" if i % 2 else "Anulada") for i in range(3, 50)]
    transcript.api.get_cuotas_socio.return_value = cuotas
    await transcript.send("cuotas")
    if not single:
        await transcript.send("cuota_q2")
    selected = "q1" if single else "q2"
    assert await read_statement(transcript) == "\n".join(_entry(c) for c in cuotas)
    assert transcript.session.flow_state.selected_cuota_id == selected
    assert transcript.session.run.task == "view"
    await transcript.send(BACK_ID)
    assert transcript.session.flow_state.selected_cuota_id == selected
    assert INSTRUCTIONS in transcript.bodies[-1]
    assert transcript.session.run.task == "upload_receipt"
    await receipt(transcript)
    assert transcript.api.upload_comprobante.await_args.args[1] == selected
    assert_bounded(transcript)


async def test_other_quota_requires_explicit_selection_without_upload(transcript):
    transcript.api.get_cuotas_socio.return_value = [quota(), quota(2)]
    await transcript.send("cuotas")
    await transcript.send("cuota_q1")
    payload = await transcript.send(OTHER_ID)
    assert transcript.session.flow_state.selected_cuota_id is None
    assert "cuota_q2" in {r["id"] for r in rows(payload)}
    await transcript.send("cuota_q2")
    await receipt(transcript)
    assert transcript.api.upload_comprobante.await_args.args[1] == "q2"
    assert_bounded(transcript)


async def test_pending_selector_paginates_every_quota_over_ten(transcript):
    transcript.api.get_cuotas_socio.return_value = [quota(i) for i in range(1, 30)]
    payload = await transcript.send("cuotas")
    seen = set()
    while True:
        ids = {r["id"] for r in rows(payload)}
        seen |= {iid for iid in ids if iid.startswith(CUOTA_PREFIX)}
        assert STATEMENT_ID in ids and "global_menu" in ids
        page = transcript.session.flow_state.page
        next_id = f"{PAGE_PREFIX}{page + 1}"
        if next_id not in ids:
            break
        payload = await transcript.send(next_id)
    assert seen == {f"cuota_q{i}" for i in range(1, 30)}
    payload = await transcript.send(f"{PAGE_PREFIX}0")
    assert "cuota_q1" in {r["id"] for r in rows(payload)}
    await transcript.send("cuota_q29")
    assert "2026-29" in transcript.bodies[-1]
    assert_bounded(transcript)


@pytest.mark.parametrize("iid", ["cuota_missing", "cuota_q1"])
async def test_stale_or_unknown_selection_refetches_and_renders_current_choices(transcript, iid):
    transcript.api.get_cuotas_socio.return_value = [quota(), quota(2), quota(3)]
    await transcript.send("cuotas")
    transcript.api.get_cuotas_socio.return_value = [quota(estado="Pagada"), quota(2), quota(3)]
    payload = await transcript.send(iid)
    assert "ya no está disponible" in transcript.bodies[-1]
    assert {r["id"] for r in rows(payload)} == {"cuota_q2", "cuota_q3", STATEMENT_ID, "global_menu"}
    assert transcript.session.flow_state.selected_cuota_id is None
    assert transcript.api.get_cuotas_socio.await_count == 2
    whatsapp_client.download_media.assert_not_awaited()
    transcript.api.upload_comprobante.assert_not_awaited()
    transcript.assert_single_responses(2)


async def test_stale_selected_quota_is_never_reattributed_when_receipt_arrives(transcript):
    await transcript.send("cuotas")
    transcript.api.get_cuotas_socio.return_value = [quota(estado="Pagada"), quota(2)]
    await receipt(transcript)
    assert "ya no está pendiente" in transcript.bodies[-1]
    assert "2026-02" in transcript.bodies[-1]
    assert transcript.session.flow_state.selected_cuota_id == "q2"
    whatsapp_client.download_media.assert_not_awaited()
    transcript.api.upload_comprobante.assert_not_awaited()
    await receipt(transcript)
    assert transcript.api.upload_comprobante.await_args.args[1] == "q2"
    transcript.assert_single_responses(3)


@pytest.mark.parametrize(
    "invalid",
    [
        {"text": "hola"},
        {"type": "audio", "media_id": "a", "media_mime_type": "audio/ogg"},
        {"type": "document", "media_id": "a", "media_mime_type": "application/zip"},
        {"type": "image", "media_id": "a", "media_mime_type": "application/pdf"},
        {"type": "document", "media_id": "a"},
        {"type": "document", "media_mime_type": "application/pdf"},
    ],
)
async def test_invalid_receipt_has_no_api_calls_and_one_actionable_retry(transcript, invalid):
    await transcript.send("cuotas")
    transcript.api.get_cuotas_socio.reset_mock()
    await transcript.send(**invalid)
    transcript.api.get_cuotas_socio.assert_not_awaited()
    whatsapp_client.download_media.assert_not_awaited()
    transcript.api.upload_comprobante.assert_not_awaited()
    assert transcript.session.active_flow == "cuotas"
    assert transcript.session.flow_state.selected_cuota_id == "q1"
    assert "2026-11" in transcript.bodies[-1]
    assert "$100.00" in transcript.bodies[-1]
    assert INSTRUCTIONS in transcript.bodies[-1]
    await receipt(transcript)
    transcript.api.upload_comprobante.assert_awaited_once()
    transcript.assert_single_responses(3)
    assert_bounded(transcript)


@pytest.mark.parametrize("failure", ["download", "oversize", "download_mime", "bad_request", "backend", "refresh"])
async def test_receipt_failure_modes_keep_association_and_support_retry(transcript, failure):
    await transcript.send("cuotas")
    if failure == "download":
        whatsapp_client.download_media.return_value = None
    elif failure == "oversize":
        whatsapp_client.download_media.return_value = (b"x" * (MAX_MEDIA_SIZE + 1), "application/pdf")
    elif failure == "download_mime":
        whatsapp_client.download_media.return_value = (b"x", "application/zip")
    elif failure == "bad_request":
        transcript.api.upload_comprobante.side_effect = ServicesAPIBadRequest("No válido " * 200)
    elif failure == "backend":
        transcript.api.upload_comprobante.side_effect = ServicesAPIError("unavailable")
    else:
        transcript.api.get_cuotas_socio.side_effect = ServicesAPIError("unavailable")
    await receipt(transcript)
    assert transcript.session.active_flow == "cuotas"
    assert transcript.session.flow_state.selected_cuota_id == "q1"
    assert INSTRUCTIONS in transcript.bodies[-1]
    assert "2026-11" in transcript.bodies[-1] and "$100.00" in transcript.bodies[-1]
    assert "¿Qué querés hacer?" not in transcript.bodies[-1]
    if failure in ("download", "oversize", "download_mime", "refresh"):
        transcript.api.upload_comprobante.assert_not_awaited()
    whatsapp_client.download_media.return_value = (b"synthetic-receipt", "application/pdf")
    transcript.api.upload_comprobante.side_effect = None
    transcript.api.get_cuotas_socio.side_effect = None
    await receipt(transcript)
    assert transcript.session.active_flow is None
    assert "pendiente de validación" in transcript.bodies[-1]
    transcript.assert_single_responses(3)
    assert_bounded(transcript)


@pytest.mark.parametrize(
    "message_type,mime_type,extension",
    [
        ("document", "application/pdf", "pdf"),
        ("image", "image/jpeg", "jpg"),
        ("image", "image/png", "png"),
        ("image", "image/webp", "webp"),
    ],
)
async def test_allowed_receipt_formats(transcript, message_type, mime_type, extension):
    await transcript.send("cuotas")
    whatsapp_client.download_media.return_value = (b"receipt", mime_type)
    await receipt(transcript, type=message_type, media_mime_type=mime_type)
    transcript.api.upload_comprobante.assert_awaited_once_with(
        "s1", "q1", b"receipt", f"comprobante.{extension}", mime_type
    )
    transcript.assert_single_responses(2)


async def test_receipt_exact_five_mib_is_allowed_and_keeps_filename(transcript):
    await transcript.send("cuotas")
    data = b"x" * MAX_MEDIA_SIZE
    whatsapp_client.download_media.return_value = (data, "application/pdf; charset=binary")
    await receipt(transcript, media_filename="pago.pdf")
    transcript.api.upload_comprobante.assert_awaited_once_with("s1", "q1", data, "pago.pdf", "application/pdf")
    transcript.assert_single_responses(2)


async def test_enter_api_failure_finishes_once_with_menu(transcript):
    transcript.api.get_cuotas_socio.side_effect = ServicesAPIError("offline")
    await transcript.send("cuotas")
    assert transcript.session.active_flow is None
    assert "problema técnico" in transcript.bodies[0]
    assert "¿Qué querés hacer?" in transcript.bodies[0]
    transcript.assert_single_responses(1)


async def test_failed_selection_refresh_stays_active_and_one_retry(transcript):
    transcript.api.get_cuotas_socio.return_value = [quota(), quota(2)]
    await transcript.send("cuotas")
    transcript.api.get_cuotas_socio.side_effect = ServicesAPIError("offline")
    payload = await transcript.send("cuota_q2")
    assert transcript.session.flow_state.selected_cuota_id is None
    assert "No pude verificar" in transcript.bodies[-1]
    assert "cuota_q2" in {r["id"] for r in rows(payload)}
    transcript.api.upload_comprobante.assert_not_awaited()
    transcript.assert_single_responses(2)


async def test_latest_summary_fits_and_omitted_entries_are_available(transcript):
    cuotas = [quota(i, estado="Pagada") for i in range(2, 60)] + [quota()]
    transcript.api.get_cuotas_socio.return_value = cuotas
    await transcript.send("cuotas")
    assert "Última cuota" in transcript.bodies[0]
    assert "2026-59" in transcript.bodies[0]
    assert "Otras cuotas: 58" in transcript.bodies[0]
    assert "2026-01" in transcript.bodies[0]  # Selected association cannot be omitted.
    assert INSTRUCTIONS in transcript.bodies[0]
    assert await read_statement(transcript) == "\n".join(_entry(c) for c in cuotas)
    assert_bounded(transcript)


async def test_compact_summary_and_lossless_statement_for_oversized_entry(transcript):
    cuotas = [quota(), quota(2), quota(99, estado="Anulada", fechaVencimiento="long-date-" * 400)]
    transcript.api.get_cuotas_socio.return_value = cuotas
    await transcript.send("cuotas")
    assert "3 cuotas registradas; 2 pendientes" in transcript.bodies[0]
    assert "Ver estado completo" in transcript.bodies[0]
    assert await read_statement(transcript) == "\n".join(_entry(c) for c in cuotas)
    assert_bounded(transcript)


async def test_singleton_compact_summary_keeps_period_cents_and_upload_instructions(transcript):
    cuotas = [quota(monto=123.45), quota(99, estado="Pagada", fechaVencimiento="date" * 600)]
    transcript.api.get_cuotas_socio.return_value = cuotas
    await transcript.send("cuotas")
    assert "2 cuotas registradas; 1 pendientes" in transcript.bodies[0]
    assert "2026-01" in transcript.bodies[0]
    assert "$123.45" in transcript.bodies[0]
    assert INSTRUCTIONS in transcript.bodies[0]
    assert await read_statement(transcript) == "\n".join(_entry(c) for c in cuotas)
    assert_bounded(transcript)


async def test_long_selected_period_is_not_truncated_by_interactive_body_limit(transcript):
    period = "Periodo detallado " * 80
    transcript.api.get_cuotas_socio.return_value = [quota(periodo=period, monto="123.45")]
    payload = await transcript.send("cuotas")
    assert payload["type"] == "text"
    assert period in transcript.bodies[-1]
    assert "$123.45" in transcript.bodies[-1]
    assert INSTRUCTIONS in transcript.bodies[-1]
    assert "Ver estado completo" in transcript.bodies[-1]
    await transcript.send(text="estado")
    await transcript.send(BACK_ID)
    assert transcript.session.flow_state.selected_cuota_id == "q1"
    assert period in transcript.bodies[-1]
    assert_bounded(transcript)


async def test_statement_invalid_input_keeps_page_boundaries_and_selection(transcript):
    cuotas = [quota(i) for i in range(1, 50)]
    transcript.api.get_cuotas_socio.return_value = cuotas
    await transcript.send("cuotas")
    await transcript.send("cuota_q2")
    await transcript.send(STATEMENT_ID)
    await transcript.send(f"{STATEMENT_PAGE_PREFIX}1")
    original_content = transcript.bodies[-1].split("\n\n", 1)[1]
    await transcript.send(text="hola")
    assert transcript.bodies[-1].endswith(original_content)
    assert transcript.session.flow_state.statement_page == 1
    assert transcript.session.flow_state.selected_cuota_id == "q2"
    await transcript.send(f"{STATEMENT_PAGE_PREFIX}bad")
    assert transcript.session.flow_state.statement_page == 1
    await transcript.send(BACK_ID)
    assert INSTRUCTIONS in transcript.bodies[-1]
    assert_bounded(transcript)


async def test_selected_quota_paid_before_upload_finishes_current_full_state_once(transcript):
    await transcript.send("cuotas")
    transcript.api.get_cuotas_socio.return_value = [quota(estado="Pagada")]
    await receipt(transcript)
    assert transcript.session.active_flow is None
    assert "ya no está pendiente" in transcript.bodies[-1]
    assert "Pagada" in transcript.bodies[-1]
    assert "¿Qué querés hacer?" in transcript.bodies[-1]
    transcript.api.upload_comprobante.assert_not_awaited()
    whatsapp_client.download_media.assert_not_awaited()
    transcript.assert_single_responses(2)


@pytest.mark.parametrize("invalid", ["cuotas_page_bad", "cuotas_page_999", "cuotas_page_-1", "unrecognized"])
async def test_invalid_selector_navigation_is_one_current_actionable_list(transcript, invalid):
    transcript.api.get_cuotas_socio.return_value = [quota(i) for i in range(1, 20)]
    await transcript.send("cuotas")
    await transcript.send(invalid)
    assert transcript.session.active_flow == "cuotas"
    assert transcript.session.flow_state.selected_cuota_id is None
    transcript.api.upload_comprobante.assert_not_awaited()
    transcript.assert_single_responses(2)
    assert_bounded(transcript)
