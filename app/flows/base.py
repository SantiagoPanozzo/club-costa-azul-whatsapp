from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Generic, TypeVar, cast

from ..state import Session
from ..webhook_parser import IncomingMessage

T = TypeVar("T")


class FlowResult(StrEnum):
    CONTINUE = "continue"
    DONE = "done"


class BaseFlow(ABC, Generic[T]):
    """A self-contained multi-step conversation flow.

    Type parameter T is the flow's state dataclass, which must include
    a `step` field typed as a StrEnum specific to that flow.
    """

    @abstractmethod
    def create_state(self) -> T:
        """Return a fresh instance of this flow's typed state."""

    @abstractmethod
    async def enter(self, phone: str, session: Session) -> None:
        """Set state and send its first prompt, or session.finish() for an immediate result."""

    @abstractmethod
    async def handle(self, phone: str, session: Session, msg: IncomingMessage) -> FlowResult:
        """Handle a turn; finish with session.finish(body, outcome) and return DONE.

        Do not send a terminal text separately: the router composes it with the
        main menu, retaining task-run attribution until that message is recorded.
        """

    def get_state(self, session: Session) -> T:
        return cast(T, session.flow_state)
