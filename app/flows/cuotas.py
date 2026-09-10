import logging
import traceback
from dataclasses import dataclass, field
from enum import StrEnum

from .. import services_client as svc
from ..services_client import ServicesAPIBadRequest, ServicesAPIError
from ..state import Session
from ..storing_client import storing_client as whatsapp_client
from ..webhook_parser import IncomingMessage
from ..whatsapp_client import whatsapp_client as raw_whatsapp_client
from .base import BaseFlow, FlowResult

logger = logging.getLogger(__name__)

GENERIC_ERROR = "Uy, tuvimos un problema técnico. Probá de nuevo en unos minutos."

ESTADO_ICONS: dict[str, str] = {
    "Pagada": "✓",
    "EnPeriodoDeGracia": "⚠",
    "Atrasada": "✗",
    "Anulada": "—",
}

ESTADO_LABELS: dict[str, str] = {
    "Pagada": "Pagada",
    "EnPeriodoDeGracia": "En periodo de gracia",
    "Atrasada": "Atrasada",
    "Anulada": "Anulada",
}

UPLOAD_BTN = "cuota_upload"
CUOTA_PREFIX = "cuota_"

ALLOWED_MIME_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp"}

MIME_TO_EXT: dict[str, str] = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


class CuotasStep(StrEnum):
    SHOWING_CUOTAS = "showing_cuotas"
    AWAITING_CUOTA_SELECTION = "awaiting_cuota_selection"
    AWAITING_COMPROBANTE = "awaiting_comprobante"


@dataclass
class CuotasState:
    step: CuotasStep = CuotasStep.SHOWING_CUOTAS
    pending_cuotas: dict[str, dict] = field(default_factory=dict)
    selected_cuota_id: str | None = None


class CuotasFlow(BaseFlow[CuotasState]):
    def create_state(self) -> CuotasState:
        return CuotasState()

    async def enter(self, phone: str, session: Session) -> None:
        state = self.create_state()
        session.flow_state = state
        socio = session.socio
        assert socio is not None
        socio_id = str(socio["id"])

        try:
            cuotas = await svc.services_client.get_cuotas_socio(socio_id)
        except ServicesAPIError:
            logger.warning("Error fetching cuotas for socio %s", socio_id)
            traceback.print_exc()
            await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
            session.end_flow()
            return

        if not cuotas:
            await whatsapp_client.send_text(phone, "No tenés cuotas registradas.", session=session)
            session.end_flow()
            return

        lines: list[str] = []
        for c in cuotas:
            estado = c.get("estado", "")
            icon = ESTADO_ICONS.get(estado, "?")
            label = ESTADO_LABELS.get(estado, estado)
            monto = float(str(c.get("monto", 0)))
            periodo = c.get("periodo", "")
            venc = c.get("fechaVencimiento", "")
            lines.append(f"{icon} {periodo}: ${monto:.0f} - {label} (vence {venc})")

        text = "*Tus cuotas:*\n\n" + "\n".join(lines)
        await whatsapp_client.send_text(phone, text, session=session)

        pending = {str(c["id"]): c for c in cuotas if c.get("estado") not in ("Pagada", "Anulada")}

        if not pending:
            session.end_flow()
            return

        state.pending_cuotas = pending
        state.step = CuotasStep.SHOWING_CUOTAS
        await whatsapp_client.send_buttons(
            phone,
            body="¿Querés enviar un comprobante de pago?",
            buttons=[(UPLOAD_BTN, "Subir comprobante")],
            session=session,
        )

    async def handle(self, phone: str, session: Session, msg: IncomingMessage) -> FlowResult:
        state = self.get_state(session)

        if state.step == CuotasStep.SHOWING_CUOTAS:
            return await self._handle_showing_cuotas(phone, session, state, msg)
        if state.step == CuotasStep.AWAITING_CUOTA_SELECTION:
            return await self._handle_cuota_selection(phone, session, state, msg)
        if state.step == CuotasStep.AWAITING_COMPROBANTE:
            return await self._handle_comprobante(phone, session, state, msg)

        return FlowResult.DONE

    async def _handle_showing_cuotas(
        self, phone: str, session: Session, state: CuotasState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.interactive_id != UPLOAD_BTN:
            await whatsapp_client.send_text(
                phone,
                "Tocá *Subir comprobante* o escribí *menu* para volver.",
                session=session,
            )
            return FlowResult.CONTINUE

        rows = [
            {
                "id": f"{CUOTA_PREFIX}{cid}",
                "title": str(c.get("periodo", "")),
                "description": f"${float(str(c.get('monto', 0))):.0f}"
                + f"- {ESTADO_LABELS.get(c.get('estado', ''), c.get('estado', ''))}",
            }
            for cid, c in list(state.pending_cuotas.items())[:10]
        ]
        await whatsapp_client.send_list(
            to=phone,
            body="Elegí la cuota para la que querés enviar un comprobante:",
            button_text="Ver cuotas",
            rows=rows,
            section_title="Cuotas pendientes",
            session=session,
        )
        state.step = CuotasStep.AWAITING_CUOTA_SELECTION
        return FlowResult.CONTINUE

    async def _handle_cuota_selection(
        self, phone: str, session: Session, state: CuotasState, msg: IncomingMessage
    ) -> FlowResult:
        iid = msg.interactive_id or ""
        if not iid.startswith(CUOTA_PREFIX):
            await whatsapp_client.send_text(phone, "Por favor, elegí una cuota de la lista.", session=session)
            return FlowResult.CONTINUE

        cuota_id = iid[len(CUOTA_PREFIX) :]
        cuota = state.pending_cuotas.get(cuota_id)
        if not cuota:
            await whatsapp_client.send_text(phone, "Esa cuota no está disponible.", session=session)
            return FlowResult.DONE

        state.selected_cuota_id = cuota_id
        state.step = CuotasStep.AWAITING_COMPROBANTE
        periodo = cuota.get("periodo", "")
        monto = float(str(cuota.get("monto", 0)))
        await whatsapp_client.send_text(
            phone,
            f"Enviame la foto o PDF del comprobante de pago para la cuota de *{periodo}* (${monto:.0f}).",
            session=session,
        )
        return FlowResult.CONTINUE

    async def _handle_comprobante(
        self, phone: str, session: Session, state: CuotasState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.type not in ("image", "document"):
            await whatsapp_client.send_text(
                phone,
                "Por favor, enviá una imagen o un PDF del comprobante.",
                session=session,
            )
            return FlowResult.CONTINUE

        mime_type = msg.media_mime_type or ""
        if mime_type not in ALLOWED_MIME_TYPES:
            await whatsapp_client.send_text(
                phone,
                "Solo se aceptan archivos PDF, JPEG, PNG o WEBP.",
                session=session,
            )
            return FlowResult.CONTINUE

        if not msg.media_id:
            await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
            return FlowResult.CONTINUE

        result = await raw_whatsapp_client.download_media(msg.media_id)
        if result is None:
            await whatsapp_client.send_text(
                phone,
                "No pude descargar el archivo. Verificá que no supere 5 MB e intentá de nuevo.",
                session=session,
            )
            return FlowResult.CONTINUE

        file_bytes, content_type = result
        filename = msg.media_filename or f"comprobante{MIME_TO_EXT.get(mime_type, '.bin')}"

        socio = session.socio
        assert socio is not None
        socio_id = str(socio["id"])
        cuota_id = state.selected_cuota_id
        assert cuota_id is not None
        cuota = state.pending_cuotas[cuota_id]

        try:
            await svc.services_client.upload_comprobante(socio_id, cuota_id, file_bytes, filename, content_type)
        except ServicesAPIBadRequest as exc:
            await whatsapp_client.send_text(phone, str(exc), session=session)
            return FlowResult.CONTINUE
        except ServicesAPIError:
            logger.warning("Error uploading comprobante (socio=%s, cuota=%s)", socio_id, cuota_id)
            traceback.print_exc()
            await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
            return FlowResult.CONTINUE

        periodo = cuota.get("periodo", "")
        await whatsapp_client.send_text(
            phone,
            f"✓ Comprobante enviado para la cuota de *{periodo}*. Queda pendiente de validación por el club.",
            session=session,
        )
        return FlowResult.DONE
