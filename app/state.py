"""
In-memory conversation state, keyed by phone number.

NOTE: This is intentionally simple for the bootstrap version. It is:
- process-local (won't work correctly with >1 instance/replica)
- volatile (lost on restart / redeploy, mid-conversation)
For production, replace with Redis or a DB-backed store, keeping the same
get/reset interface so the rest of the code doesn't need to change.
"""
import threading
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Session:
    step: str = "START"
    socio: Optional[dict] = None
    available_activities: dict = field(default_factory=dict)  # activity_id -> activity dict
    selected_activity: Optional[dict] = None


class SessionStore:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def get(self, phone: str) -> Session:
        with self._lock:
            if phone not in self._sessions:
                self._sessions[phone] = Session()
            return self._sessions[phone]

    def reset(self, phone: str) -> None:
        with self._lock:
            self._sessions[phone] = Session()


sessions = SessionStore()
