from dataclasses import dataclass
from enum import StrEnum

from ..state import Session
from ..storing_client import storing_client as whatsapp_client
from ..webhook_parser import IncomingMessage
from .base import BaseFlow, FlowResult


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
        await whatsapp_client.send_text(
            phone,
            "Para ver los comunicados del club, ingresá a https://clubcostaazul.com",
            session=session,
        )
        session.end_flow()

    async def handle(self, phone: str, session: Session, msg: IncomingMessage) -> FlowResult:
        return FlowResult.DONE
