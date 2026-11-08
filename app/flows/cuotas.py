import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from .. import services_client as svc
from ..flow_ui import MENU_ID, list_body_limit, paginate_rows, split_text, with_notice
from ..services_client import ServicesAPIBadRequest, ServicesAPIError
from ..state import Session
from ..storing_client import storing_client as whatsapp_client
from ..webhook_parser import IncomingMessage
from ..whatsapp_client import MAX_MEDIA_SIZE
from ..whatsapp_client import whatsapp_client as raw_whatsapp_client
from .base import BaseFlow, FlowResult

logger = logging.getLogger(__name__)

GENERIC_ERROR = "Uy, tuvimos un problema técnico. Probá de nuevo en unos minutos."
ESTADO_ICONS = {"Pagada": "✓", "EnPeriodoDeGracia": "⚠", "Atrasada": "✗", "Anulada": "—"}
ESTADO_LABELS = {
    "Pagada": "Pagada",
    "EnPeriodoDeGracia": "En periodo de gracia",
    "Atrasada": "Atrasada",
    "Anulada": "Anulada",
}
CUOTA_PREFIX = "cuota_"
PAGE_PREFIX = "cuotas_page_"
STATEMENT_ID = "cuotas_statement"
STATEMENT_PAGE_PREFIX = "cuotas_statement_page_"
BACK_ID = "cuotas_back"
OTHER_ID = "cuotas_other"
ALLOWED_MIME_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
MIME_TO_EXT = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
INSTRUCTIONS = "Enviá la foto o PDF del comprobante (JPEG, PNG, WEBP o PDF; máximo 5 MB)."


class CuotasStep(StrEnum):
    SHOWING_CUOTAS = "showing_cuotas"
    AWAITING_CUOTA_SELECTION = "awaiting_cuota_selection"
    AWAITING_COMPROBANTE = "awaiting_comprobante"
    SHOWING_STATEMENT = "showing_statement"


@dataclass
class CuotasState:
    step: CuotasStep = CuotasStep.SHOWING_CUOTAS
    cuotas: list[dict] = field(default_factory=list)
    pending_cuotas: dict[str, dict] = field(default_factory=dict)
    selected_cuota_id: str | None = None
    presented_cuota_id: str | None = None
    presented_terms: tuple[str, Decimal] | None = None
    page: int = 0
    statement_page: int = 0


def _amount(cuota: dict) -> str:
    # Preserve cents: a receipt must reference the actual amount.
    return f"${Decimal(str(cuota.get('monto', 0))):.2f}"


def _terms(cuota: dict) -> tuple[str, Decimal]:
    return str(cuota.get("periodo", "")), Decimal(str(cuota.get("monto", 0)))


def _entry(cuota: dict) -> str:
    estado = cuota.get("estado", "")
    return (
        f"{ESTADO_ICONS.get(estado, '?')} {cuota.get('periodo', '')}: {_amount(cuota)} - "
        f"{ESTADO_LABELS.get(estado, estado)} (vence {cuota.get('fechaVencimiento', '')})"
    )


def _summary(state: CuotasState, prompt: str, limit: int, notice: str = "") -> str:
    """Use the richest account view that fits alongside the actionable prompt."""
    if not state.cuotas:
        return with_notice(f"No tenés cuotas registradas.\n\n{prompt}".strip(), notice)
    entries = [_entry(c) for c in state.cuotas]
    latest = _entry(max(state.cuotas, key=lambda c: str(c.get("periodo", ""))))
    candidates = [
        "*Tus cuotas:*\n\n" + "\n".join(entries),
        f"*Última cuota:*\n{latest}\nOtras cuotas: {len(entries) - 1}. Ver estado completo para el detalle.",
        f"Tenés {len(entries)} cuotas registradas; {len(state.pending_cuotas)} pendientes. "
        "Ver estado completo para el detalle.",
    ]
    for summary in candidates:
        body = with_notice(f"{summary}\n\n{prompt}".strip(), notice)
        if len(body) <= limit:
            return body
    # Never truncate an unusually long selected financial association.
    return with_notice(f"{candidates[-1]}\n\n{prompt}".strip(), notice)


class CuotasFlow(BaseFlow[CuotasState]):
    def create_state(self) -> CuotasState:
        return CuotasState()

    async def _refresh(self, session: Session, state: CuotasState) -> None:
        assert session.socio is not None
        state.cuotas = await svc.services_client.get_cuotas_socio(str(session.socio["id"]))
        state.pending_cuotas = {str(c["id"]): c for c in state.cuotas if c.get("estado") not in ("Pagada", "Anulada")}
        if state.selected_cuota_id not in state.pending_cuotas:
            state.selected_cuota_id = None

    async def enter(self, phone: str, session: Session) -> None:
        state = self.create_state()
        session.flow_state = state
        session.set_task("view")
        try:
            await self._refresh(session, state)
        except ServicesAPIError:
            logger.warning("Error fetching cuotas")
            session.finish(GENERIC_ERROR, outcome="error")
            return
        await self._show_current(phone, session, state)

    async def _show_current(self, phone: str, session: Session, state: CuotasState, notice: str = "") -> None:
        if notice:
            session.mark_recovery()
        if state.selected_cuota_id is not None or len(state.pending_cuotas) == 1:
            if state.selected_cuota_id is None:
                state.selected_cuota_id = next(iter(state.pending_cuotas))
            await self._instructions(phone, session, state, notice, summary=True)
            return
        session.set_task("view")
        if not state.pending_cuotas:
            prompt = "No tenés cuotas pendientes." if state.cuotas else ""
            # Reserve the router's menu prompt; a long statement must remain reachable.
            full = with_notice(
                ("*Tus cuotas:*\n\n" + "\n".join(_entry(c) for c in state.cuotas) + f"\n\n{prompt}")
                if state.cuotas
                else "No tenés cuotas registradas.",
                notice,
            )
            if len(full) <= list_body_limit() - len("\n\n¿Qué querés hacer?"):
                session.finish(full)
                return
            state.step = CuotasStep.SHOWING_CUOTAS
            await whatsapp_client.send_buttons(
                phone,
                _summary(state, prompt, 1024, notice),
                [(STATEMENT_ID, "Ver estado completo"), (MENU_ID, "Menú principal")],
                session=session,
            )
            return
        await self._selector(phone, session, state, notice)

    async def _selector(self, phone: str, session: Session, state: CuotasState, notice: str = "") -> None:
        state.step = CuotasStep.AWAITING_CUOTA_SELECTION
        rows, state.page = paginate_rows(
            [
                {"id": f"{CUOTA_PREFIX}{cid}", "title": str(c.get("periodo", "")), "description": _amount(c)}
                for cid, c in state.pending_cuotas.items()
            ],
            state.page,
            page_prefix=PAGE_PREFIX,
            extra_rows=[
                {"id": STATEMENT_ID, "title": "Ver estado completo"},
                {"id": MENU_ID, "title": "Menú principal"},
            ],
        )
        await whatsapp_client.send_list(
            phone,
            _summary(state, "Elegí la cuota para enviar su comprobante:", list_body_limit(), notice),
            "Ver cuotas",
            rows,
            section_title="Cuotas pendientes",
            session=session,
        )

    async def _instructions(
        self, phone: str, session: Session, state: CuotasState, notice: str = "", *, summary: bool = False
    ) -> None:
        if notice:
            session.mark_recovery()
        state.step = CuotasStep.AWAITING_COMPROBANTE
        session.set_task("upload_receipt")
        cuota = state.pending_cuotas[state.selected_cuota_id]
        prompt = f"Comprobante para la cuota de *{cuota.get('periodo', '')}* ({_amount(cuota)}).\n{INSTRUCTIONS}"
        body = _summary(state, prompt, 1024, notice) if summary else with_notice(prompt, notice)
        buttons = [(STATEMENT_ID, "Ver estado completo")]
        if len(state.pending_cuotas) > 1:
            buttons.append((OTHER_ID, "Elegir otra cuota"))
        buttons.append((MENU_ID, "Menú principal"))
        if len(body) > 1024:
            wamid = await whatsapp_client.send_text(
                phone,
                body + "\nVer estado completo: escribí estado; otra para elegir cuota o menu para salir.",
                session=session,
            )
        else:
            wamid = await whatsapp_client.send_buttons(phone, body, buttons, session=session)
        # Never treat refreshed or auto-selected financial terms as presented
        # until the complete instruction message has been accepted by Meta.
        if wamid:
            state.presented_cuota_id = state.selected_cuota_id
            state.presented_terms = _terms(cuota)

    async def _statement(self, phone: str, session: Session, state: CuotasState, notice: str = "") -> None:
        if notice:
            session.mark_recovery()
        session.set_task("view")
        state.step = CuotasStep.SHOWING_STATEMENT
        # Chunk losslessly, even if one API entry alone exceeds the body limit.
        header = "*Estado completo*"
        # Keep page boundaries stable even when rendering an invalid-input notice.
        budget = list_body_limit() - 160
        pages = split_text("\n".join(_entry(c) for c in state.cuotas) or "No tenés cuotas registradas.", budget)
        state.statement_page = max(0, min(state.statement_page, len(pages) - 1))
        page = state.statement_page
        rows = [{"id": BACK_ID, "title": "Volver"}, {"id": MENU_ID, "title": "Menú principal"}]
        if page:
            rows.append({"id": f"{STATEMENT_PAGE_PREFIX}{page - 1}", "title": "Anteriores"})
        if page + 1 < len(pages):
            rows.append({"id": f"{STATEMENT_PAGE_PREFIX}{page + 1}", "title": "Más cuotas"})
        await whatsapp_client.send_list(
            phone,
            with_notice(f"{header} ({page + 1}/{len(pages)})\n\n{pages[page]}", notice),
            "Ver opciones",
            rows,
            section_title="Estado de cuenta",
            session=session,
        )

    async def handle(self, phone: str, session: Session, msg: IncomingMessage) -> FlowResult:
        state = self.get_state(session)
        iid = msg.interactive_id or ""
        text = (msg.text or "").strip().lower()
        if iid == STATEMENT_ID or text == "estado":
            state.statement_page = 0
            await self._statement(phone, session, state)
        elif state.step == CuotasStep.SHOWING_STATEMENT and iid.startswith(STATEMENT_PAGE_PREFIX):
            try:
                state.statement_page = int(iid[len(STATEMENT_PAGE_PREFIX) :])
            except ValueError:
                pass
            await self._statement(phone, session, state)
        elif iid == BACK_ID:
            await self._show_current(phone, session, state)
        elif iid == OTHER_ID or text == "otra":
            state.selected_cuota_id = None
            await self._show_current(phone, session, state)
        elif state.step == CuotasStep.SHOWING_STATEMENT:
            await self._statement(phone, session, state, "Usá las opciones para recorrer el estado o volver.")
        elif iid.startswith(PAGE_PREFIX):
            try:
                state.page = int(iid[len(PAGE_PREFIX) :])
            except ValueError:
                pass
            await self._show_current(phone, session, state)
        elif iid.startswith(CUOTA_PREFIX):
            await self._select(phone, session, state, iid[len(CUOTA_PREFIX) :])
        elif state.step == CuotasStep.AWAITING_COMPROBANTE:
            await self._receipt(phone, session, state, msg)
        else:
            await self._show_current(phone, session, state, "Elegí una opción de la lista.")
        return FlowResult.DONE if session.active_flow is None else FlowResult.CONTINUE

    async def _select(self, phone: str, session: Session, state: CuotasState, cuota_id: str) -> None:
        known = cuota_id in state.pending_cuotas
        try:
            await self._refresh(session, state)
        except ServicesAPIError:
            await self._show_current(phone, session, state, "No pude verificar las cuotas. Intentá de nuevo.")
            return
        if not known or cuota_id not in state.pending_cuotas:
            await self._show_current(phone, session, state, "Esa cuota ya no está disponible. Revisá el estado actual.")
            return
        state.selected_cuota_id = cuota_id
        await self._instructions(phone, session, state)

    async def _receipt(self, phone: str, session: Session, state: CuotasState, msg: IncomingMessage) -> None:
        if msg.type not in ("image", "document"):
            await self._instructions(phone, session, state, "Necesito una imagen o un PDF del comprobante.")
            return
        mime_type = msg.media_mime_type or ""
        if mime_type not in ALLOWED_MIME_TYPES or (msg.type == "image" and not mime_type.startswith("image/")):
            await self._instructions(phone, session, state, "Formato no admitido. Usá PDF, JPEG, PNG o WEBP.")
            return
        if not msg.media_id:
            await self._instructions(phone, session, state, "No recibí el archivo. Volvé a adjuntarlo.")
            return
        # Revalidate attribution, but never call the API for invalid input.
        selected = state.selected_cuota_id
        try:
            await self._refresh(session, state)
        except ServicesAPIError:
            await self._instructions(phone, session, state, "No pude verificar la cuota. Volvé a enviar el archivo.")
            return
        if selected not in state.pending_cuotas:
            await self._show_current(
                phone, session, state, "La cuota elegida ya no está pendiente. Revisá el estado actual."
            )
            return
        current = state.pending_cuotas[selected]
        if state.presented_cuota_id != selected or state.presented_terms != _terms(current):
            await self._instructions(
                phone,
                session,
                state,
                "El período o importe de la cuota cambió. Revisá los datos y reenviá el comprobante si corresponde.",
            )
            return
        result = await raw_whatsapp_client.download_media(msg.media_id)
        if result is None:
            await self._instructions(phone, session, state, "No pude descargar el archivo. Reenviá uno de hasta 5 MB.")
            return
        file_bytes, content_type = result
        content_type = content_type.split(";", 1)[0].strip().lower()
        if len(file_bytes) > MAX_MEDIA_SIZE:
            await self._instructions(phone, session, state, "El archivo supera 5 MB. Enviá uno más pequeño.")
            return
        if content_type not in ALLOWED_MIME_TYPES:
            await self._instructions(phone, session, state, "El archivo descargado no es PDF, JPEG, PNG o WEBP.")
            return
        assert session.socio is not None
        filename = msg.media_filename or f"comprobante{MIME_TO_EXT[content_type]}"
        try:
            await svc.services_client.upload_comprobante(
                str(session.socio["id"]), selected, file_bytes, filename, content_type
            )
        except ServicesAPIBadRequest:
            await self._instructions(
                phone, session, state, "El club rechazó el comprobante. Revisá el archivo y reenviálo."
            )
            return
        except ServicesAPIError:
            logger.warning("Error uploading comprobante (cuota=%s)", selected)
            await self._instructions(phone, session, state, "No pude guardar el comprobante. Volvé a enviarlo.")
            return
        cuota = state.pending_cuotas[selected]
        session.finish(
            f"✓ Comprobante enviado para la cuota de *{cuota.get('periodo', '')}* ({_amount(cuota)}). "
            "Queda pendiente de validación por el club."
        )
