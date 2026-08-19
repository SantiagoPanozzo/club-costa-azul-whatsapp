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
        """Called when the user enters this flow. Must set session.flow_state."""

    @abstractmethod
    async def handle(self, phone: str, session: Session, msg: IncomingMessage) -> FlowResult:
        """Handle a message while this flow is active."""

    def get_state(self, session: Session) -> T:
        return cast(T, session.flow_state)
