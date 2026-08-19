import threading
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
        self._lock: threading.Lock = threading.Lock()

    def get(self, phone: str) -> Session:
        with self._lock:
            if phone not in self._sessions:
                self._sessions[phone] = Session()
            return self._sessions[phone]

    def reset(self, phone: str) -> None:
        with self._lock:
            self._sessions[phone] = Session()


sessions = SessionStore()
