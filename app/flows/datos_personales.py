import logging
import traceback
from dataclasses import dataclass
from enum import StrEnum

from .. import services_client as svc
from ..services_client import ServicesAPIError
from ..state import Session
from ..webhook_parser import IncomingMessage
from .base import BaseFlow, FlowResult

logger = logging.getLogger(__name__)

GENERIC_ERROR = "Uy, tuvimos un problema técnico. Probá de nuevo en unos minutos."

CATEGORIA_LABELS: dict[str, str] = {
    "PreSocio": "Pre-socio",
    "MenorDeEdad": "Menor de edad",
    "SocioComun": "Socio común",
    "Vitalicio": "Vitalicio",
}


class DatosPersonalesStep(StrEnum):
    DONE = "done"


@dataclass
class DatosPersonalesState:
    step: DatosPersonalesStep = DatosPersonalesStep.DONE


class DatosPersonalesFlow(BaseFlow[DatosPersonalesState]):
    def create_state(self) -> DatosPersonalesState:
        return DatosPersonalesState()

    async def enter(self, phone: str, session: Session) -> None:
        session.flow_state = self.create_state()
        socio = session.socio
        assert socio is not None
        socio_id = str(socio["id"])

        try:
            detalle = await svc.services_client.get_socio_detalle(socio_id)
        except ServicesAPIError:
            logger.warning("Error fetching socio detail for %s", socio_id)
            traceback.print_exc()
            session.finish(GENERIC_ERROR, "error")
            return

        if not detalle:
            session.finish("No pudimos obtener tus datos.", "empty")
            return

        nombre = f"{detalle.get('nombre', '')} {detalle.get('apellido', '')}".strip()
        ci = detalle.get("ci", "—")
        email = detalle.get("email") or "—"
        telefono = detalle.get("telefono") or "—"
        whatsapp = detalle.get("whatsapp") or "—"
        categoria = CATEGORIA_LABELS.get(detalle.get("categoria", ""), detalle.get("categoria", "—"))
        fecha_nac = detalle.get("fechaNacimiento") or "—"
        sociedad_medica = detalle.get("sociedadMedica") or "—"

        lines = [
            "*Tus datos personales:*",
            "",
            f"👤 Nombre: {nombre}",
            f"🪪 Cédula: {ci}",
            f"📧 Email: {email}",
            f"📞 Teléfono: {telefono}",
            f"💬 WhatsApp: {whatsapp}",
            f"🏷️ Categoría: {categoria}",
            f"🎂 Fecha de nacimiento: {fecha_nac}",
            f"🏥 Sociedad médica: {sociedad_medica}",
        ]

        session.set_task("view")
        session.finish("\n".join(lines) + "\n\nPara actualizar tus datos, ingresá a https://clubcostaazul.com")

    async def handle(self, phone: str, session: Session, msg: IncomingMessage) -> FlowResult:
        return FlowResult.DONE
