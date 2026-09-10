import logging
from dataclasses import dataclass
from enum import StrEnum

from .. import services_client as svc
from ..config import settings
from ..services_client import ServicesAPIError
from ..state import Session
from ..storing_client import storing_client as whatsapp_client
from ..webhook_parser import IncomingMessage
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


class CuotasStep(StrEnum):
    DONE = "done"


@dataclass
class CuotasState:
    step: CuotasStep = CuotasStep.DONE


class CuotasFlow(BaseFlow[CuotasState]):
    def create_state(self) -> CuotasState:
        return CuotasState()

    async def enter(self, phone: str, session: Session) -> None:
        session.flow_state = self.create_state()
        socio = session.socio
        assert socio is not None
        socio_id = str(socio["id"])

        try:
            cuotas = await svc.services_client.get_cuotas_socio(socio_id)
        except ServicesAPIError:
            logger.exception("Error fetching member dues")
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
        await whatsapp_client.send_text(
            phone,
            f"Para registrar un pago, ingresá a {settings.frontend_url.rstrip('/')}/#/panel-usuario/pagos",
            session=session,
        )
        session.end_flow()

    async def handle(self, phone: str, session: Session, msg: IncomingMessage) -> FlowResult:
        return FlowResult.DONE
