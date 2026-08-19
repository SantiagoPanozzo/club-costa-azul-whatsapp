import logging
import traceback
from dataclasses import dataclass, field
from enum import StrEnum

from .. import services_client as svc
from ..services_client import ConflictError, ServicesAPIError
from ..state import Session
from ..webhook_parser import IncomingMessage
from ..whatsapp_client import whatsapp_client
from .base import BaseFlow, FlowResult

logger = logging.getLogger(__name__)

GENERIC_ERROR = "Uy, tuvimos un problema técnico. Probá de nuevo en unos minutos."
CUOTA_PREFIX = "cuota_"
ACTION_SEND = "pay_send"
ACTION_BACK = "pay_back"

ALLOWED_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
}

ESTADO_LABELS = {
    "Pendiente": "Pendiente",
    "Pagada": "Pagada",
    "Vencida": "Vencida",
}


class PaymentsStep(StrEnum):
    AWAITING_CUOTA_CHOICE = "awaiting_cuota_choice"
    SHOWING_CUOTA_DETAIL = "showing_cuota_detail"
    AWAITING_COMPROBANTE = "awaiting_comprobante"


@dataclass
class PaymentsState:
    step: PaymentsStep = PaymentsStep.AWAITING_CUOTA_CHOICE
    cuotas: dict[str, dict] = field(default_factory=dict)
    selected_cuota_id: str | None = None


class PaymentsFlow(BaseFlow[PaymentsState]):
    def create_state(self) -> PaymentsState:
        return PaymentsState()

    async def enter(self, phone: str, session: Session) -> None:
        state = self.create_state()
        session.flow_state = state
        await self._show_cuotas(phone, session, state)

    async def handle(self, phone: str, session: Session, msg: IncomingMessage) -> FlowResult:
        state = self.get_state(session)

        if state.step == PaymentsStep.AWAITING_CUOTA_CHOICE:
            return await self._handle_cuota_choice(phone, session, state, msg)
        if state.step == PaymentsStep.SHOWING_CUOTA_DETAIL:
            return await self._handle_detail_action(phone, session, state, msg)
        if state.step == PaymentsStep.AWAITING_COMPROBANTE:
            return await self._handle_comprobante(phone, session, state, msg)

        return FlowResult.DONE

    async def _show_cuotas(self, phone: str, session: Session, state: PaymentsState) -> None:
        socio = session.socio
        assert socio is not None
        socio_id = socio["id"]

        try:
            cuotas = await svc.services_client.get_cuotas_socio(str(socio_id))
        except ServicesAPIError:
            logger.warning("Error fetching cuotas for socio %s", socio_id)
            traceback.print_exc()
            await whatsapp_client.send_text(phone, GENERIC_ERROR)
            return

        if not cuotas:
            await whatsapp_client.send_text(phone, "No tenemos cuotas registradas para tu cuenta.")
            session.end_flow()
            return

        pendientes = [c for c in cuotas if c.get("estado") != "Pagada"]
        pagadas = [c for c in cuotas if c.get("estado") == "Pagada"]

        lines: list[str] = []
        if pendientes:
            lines.append("*Pendientes:*")
            for c in pendientes:
                estado = ESTADO_LABELS.get(c.get("estado", ""), c.get("estado", ""))
                lines.append(f"- {c['periodo']}: ${c['monto']:.0f} ({estado})")
        if pagadas:
            lines.append("\n*Pagadas:*")
            for c in pagadas[-5:]:
                lines.append(f"- {c['periodo']}: ${c['monto']:.0f}")

        await whatsapp_client.send_text(phone, "Tus cuotas:\n\n" + "\n".join(lines))

        if not pendientes:
            await whatsapp_client.send_text(phone, "¡Estás al día con todas tus cuotas!")
            session.end_flow()
            return

        state.cuotas = {str(c["id"]): c for c in pendientes}
        state.step = PaymentsStep.AWAITING_CUOTA_CHOICE

        rows = [
            {
                "id": f"{CUOTA_PREFIX}{c['id']}",
                "title": str(c["periodo"])[:24],
                "description": f"${c['monto']:.0f} - Vence {_format_date(c.get('fechaVencimiento', ''))}",
            }
            for c in pendientes[:10]
        ]
        note = " (mostrando las primeras 10)" if len(pendientes) > 10 else ""
        await whatsapp_client.send_list(
            to=phone,
            body=f"Seleccioná una cuota pendiente para ver detalles o enviar comprobante{note}:",
            button_text="Ver cuotas",
            rows=rows,
            section_title="Cuotas pendientes",
        )

    async def _handle_cuota_choice(
        self, phone: str, session: Session, state: PaymentsState, msg: IncomingMessage
    ) -> FlowResult:
        iid = msg.interactive_id or ""
        if not iid.startswith(CUOTA_PREFIX):
            await whatsapp_client.send_text(phone, "Por favor, elegí una cuota de la lista.")
            return FlowResult.CONTINUE

        cuota_id = iid[len(CUOTA_PREFIX):]
        cuota = state.cuotas.get(cuota_id)
        if not cuota:
            await whatsapp_client.send_text(phone, "Esa cuota no está disponible. Probá de nuevo.")
            return FlowResult.CONTINUE

        state.selected_cuota_id = cuota_id

        try:
            comprobantes = await svc.services_client.get_comprobantes_cuota(cuota_id)
        except ServicesAPIError:
            comprobantes = []

        detail = (
            f"*Cuota: {cuota['periodo']}*\n"
            f"Monto: ${cuota['monto']:.0f}\n"
            f"Estado: {cuota.get('estado', 'Pendiente')}\n"
            f"Vencimiento: {_format_date(cuota.get('fechaVencimiento', ''))}"
        )

        if comprobantes:
            detail += "\n\n*Comprobantes enviados:*"
            validacion_labels = {"Pendiente": "Pendiente", "Valido": "Aprobado", "Invalido": "Rechazado"}
            for comp in comprobantes:
                estado_val = validacion_labels.get(comp.get("estadoValidacion", ""), comp.get("estadoValidacion", ""))
                fecha_envio = _format_date(comp.get("fechaEnvio", ""))
                detail += f"\n- {fecha_envio}: {estado_val}"

        await whatsapp_client.send_text(phone, detail)

        state.step = PaymentsStep.SHOWING_CUOTA_DETAIL
        await whatsapp_client.send_buttons(
            phone,
            body="¿Qué querés hacer?",
            buttons=[(ACTION_SEND, "Enviar comprobante"), (ACTION_BACK, "Volver")],
        )
        return FlowResult.CONTINUE

    async def _handle_detail_action(
        self, phone: str, session: Session, state: PaymentsState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.interactive_id == ACTION_SEND:
            state.step = PaymentsStep.AWAITING_COMPROBANTE
            await whatsapp_client.send_text(
                phone, "Enviame una foto o archivo PDF del comprobante de pago."
            )
            return FlowResult.CONTINUE

        if msg.interactive_id == ACTION_BACK:
            await self._show_cuotas(phone, session, state)
            return FlowResult.CONTINUE

        await whatsapp_client.send_text(phone, "Por favor, tocá una de las opciones.")
        return FlowResult.CONTINUE

    async def _handle_comprobante(
        self, phone: str, session: Session, state: PaymentsState, msg: IncomingMessage
    ) -> FlowResult:
        if msg.type not in ("image", "document") or not msg.media_id:
            await whatsapp_client.send_text(
                phone, "Por favor, enviame una imagen (JPG/PNG) o un PDF del comprobante."
            )
            return FlowResult.CONTINUE

        if msg.mime_type and msg.mime_type not in ALLOWED_MIME_TYPES:
            await whatsapp_client.send_text(
                phone, "Formato no soportado. Enviame una imagen JPG/PNG o un PDF."
            )
            return FlowResult.CONTINUE

        socio = session.socio
        assert socio is not None
        cuota_id = state.selected_cuota_id
        assert cuota_id is not None

        await whatsapp_client.send_text(phone, "Recibido. Procesando tu comprobante...")

        try:
            file_bytes, mime = await whatsapp_client.download_media(msg.media_id)
        except ValueError:
            await whatsapp_client.send_text(phone, "El archivo es demasiado grande (máximo 5 MB).")
            return FlowResult.DONE
        except Exception:
            logger.warning("Error downloading media %s", msg.media_id)
            traceback.print_exc()
            await whatsapp_client.send_text(phone, GENERIC_ERROR)
            return FlowResult.DONE

        filename = msg.filename or f"comprobante.{_ext_from_mime(mime)}"

        try:
            await svc.services_client.post_comprobante_cuota(
                str(socio["id"]), cuota_id, file_bytes, mime, filename
            )
        except ConflictError as exc:
            await whatsapp_client.send_text(phone, exc.detail)
            return FlowResult.DONE
        except ServicesAPIError:
            logger.warning("Error uploading comprobante for socio %s, cuota %s", socio["id"], cuota_id)
            traceback.print_exc()
            await whatsapp_client.send_text(phone, GENERIC_ERROR)
            return FlowResult.DONE

        await whatsapp_client.send_text(
            phone, "¡Comprobante enviado! Queda pendiente de validación por administración."
        )
        return FlowResult.DONE


def _format_date(iso_str: str) -> str:
    if not iso_str:
        return "—"
    date_part = iso_str[:10]
    try:
        parts = date_part.split("-")
        return f"{parts[2]}/{parts[1]}/{parts[0]}"
    except (IndexError, ValueError):
        return date_part


def _ext_from_mime(mime: str) -> str:
    mapping = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "application/pdf": "pdf",
    }
    return mapping.get(mime, "bin")
