import asyncio
from dataclasses import dataclass


@dataclass
class Session:
    socio: dict[str, object] | None = None
    active_flow: str | None = None
    flow_state: object = None

    def end_flow(self) -> None:
        self.active_flow = None
        self.flow_state = None


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._phone_locks: dict[str, asyncio.Lock] = {}

    def get(self, phone: str) -> Session:
        if phone not in self._sessions:
            self._sessions[phone] = Session()
        return self._sessions[phone]

    def reset(self, phone: str) -> None:
        self._sessions[phone] = Session()

    def lock_for(self, phone: str) -> asyncio.Lock:
        """Return a per-phone asyncio lock for serializing message processing."""
        if phone not in self._phone_locks:
            self._phone_locks[phone] = asyncio.Lock()
        return self._phone_locks[phone]


sessions = SessionStore()
