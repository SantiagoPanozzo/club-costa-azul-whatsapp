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
            noticias = await svc.services_client.get_noticias()
        except ServicesAPIError:
            logger.warning("Error fetching noticias")
            traceback.print_exc()
            session.finish(GENERIC_ERROR, "error")
            return

        if not noticias:
            session.finish("No hay comunicados publicados en este momento.", "empty")
            return

        lines = ["*Últimos comunicados del club:*", ""]
        for n in noticias[:5]:
            titulo = n.get("titulo", "Sin título")
            fecha = n.get("fecha", "")
            if fecha and len(fecha) >= 10:
                fecha = fecha[:10]
            resumen = n.get("descripcion", "")
            if len(resumen) > 120:
                resumen = resumen[:117] + "..."
            lines.append(f"📌 *{titulo}*")
            if fecha:
                lines.append(f"   {fecha}")
            if resumen:
                lines.append(f"   {resumen}")
            lines.append("")

        session.set_task("view")
        session.finish(
            "\n".join(lines).strip() + "\n\nPara ver todos los comunicados, ingresá a https://clubcostaazul.com"
        )

    async def handle(self, phone: str, session: Session, msg: IncomingMessage) -> FlowResult:
        return FlowResult.DONE
