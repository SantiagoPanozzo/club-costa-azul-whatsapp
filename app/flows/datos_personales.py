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
            logger.exception("Error fetching member detail")
            await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
            session.end_flow()
            return

        if not detalle:
            await whatsapp_client.send_text(phone, "No pudimos obtener tus datos.", session=session)
            session.end_flow()
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

        await whatsapp_client.send_text(phone, "\n".join(lines), session=session)
        await whatsapp_client.send_text(
            phone,
            f"Para actualizar tus datos, ingresá a {settings.frontend_url.rstrip('/')}/#/panel-usuario/perfil",
            session=session,
        )
        session.end_flow()

    async def handle(self, phone: str, session: Session, msg: IncomingMessage) -> FlowResult:
        return FlowResult.DONE
