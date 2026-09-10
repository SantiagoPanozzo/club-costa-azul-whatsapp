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


class ComunicadosStep(StrEnum):
    DONE = "done"


@dataclass
class ComunicadosState:
    step: ComunicadosStep = ComunicadosStep.DONE


class ComunicadosFlow(BaseFlow[ComunicadosState]):
    def create_state(self) -> ComunicadosState:
        return ComunicadosState()

    async def enter(self, phone: str, session: Session) -> None:
        session.flow_state = self.create_state()
        try:
            noticias = await svc.services_client.get_noticias_publicadas()
        except ServicesAPIError:
            logger.exception("Error fetching published news")
            await whatsapp_client.send_text(phone, GENERIC_ERROR, session=session)
            session.end_flow()
            return

        if not noticias:
            await whatsapp_client.send_text(phone, "No hay comunicados publicados en este momento.", session=session)
            session.end_flow()
            return

        lines = ["*Últimos comunicados:*", ""]
        for noticia in noticias[:5]:
            description = str(noticia.get("descripcion", "")).strip()
            if len(description) > 240:
                description = description[:239].rstrip() + "…"
            lines.append(f"*{noticia.get('titulo', 'Comunicado')}* — {noticia.get('tag', '')}")
            if description:
                lines.append(description)
            lines.append("")
        await whatsapp_client.send_text(
            phone,
            "\n".join(lines).strip(),
            session=session,
        )
        await whatsapp_client.send_text(
            phone,
            f"Ver todos: {settings.frontend_url.rstrip('/')}/#/novedades",
            session=session,
        )
        session.end_flow()

    async def handle(self, phone: str, session: Session, msg: IncomingMessage) -> FlowResult:
        return FlowResult.DONE
