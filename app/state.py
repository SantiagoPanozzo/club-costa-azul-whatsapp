import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

PROCESS_ID = str(uuid4())


def release_id() -> str:
    return os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("BOT_RELEASE", "local")


@dataclass
class TaskRun:
    id: str
    flow: str
    task: str
    started_at: datetime
    release: str
    process_id: str = PROCESS_ID
    outcome: str = "in_progress"
    finished_at: datetime | None = None
    incoming_messages: int = 0
    outbound_attempts: int = 0
    api_accepted: int = 0
    recovery_turns: int = 0


@dataclass
class Completion:
    body: str
    outcome: str


@dataclass
class Session:
    socio: dict[str, object] | None = None
    active_flow: str | None = None
    flow_state: object = None
    completion: Completion | None = None
    run: TaskRun | None = None
    turn_id: str | None = None
    response_role: str = "content"
    recovery_turn_id: str | None = None
    confirmation_id: str | None = None
    confirmation_action: str | None = None

    def issue_confirmation(self, action: str) -> str:
        # A plain invalid-input retry keeps the same displayed terms. Back/list
        # navigation and consuming consent clear this version before any change.
        if self.confirmation_id and self.confirmation_action == action:
            return self.confirmation_id
        self.confirmation_action = action
        self.confirmation_id = f"{action}:{uuid4().hex}"
        return self.confirmation_id

    def consume_confirmation(self, reply_id: str | None, action: str) -> bool:
        if not self.confirmation_id or reply_id != self.confirmation_id or action != self.confirmation_action:
            return False
        self.clear_confirmation()
        return True

    def clear_confirmation(self) -> None:
        self.confirmation_id = self.confirmation_action = None

    def mark_recovery(self) -> None:
        self.response_role = "recovery"
        if self.run is not None and self.recovery_turn_id != self.turn_id:
            self.run.recovery_turns += 1
            self.recovery_turn_id = self.turn_id

    def begin_run(self, flow: str) -> None:
        self.run = TaskRun(
            id=str(uuid4()),
            flow=flow,
            task="browse",
            started_at=datetime.now(timezone.utc),
            release=release_id(),
        )

    def set_task(self, task: str) -> None:
        if self.run is not None:
            self.run.task = task

    def finish(self, body: str, outcome: str = "success") -> None:
        """Defer the final message to the router so it shares the menu body."""
        self.completion = Completion(body, outcome)
        self.close_run(outcome)
        self.end_flow()

    def close_run(self, outcome: str) -> None:
        if self.run is not None and self.run.outcome == "in_progress":
            self.run.outcome = outcome
            self.run.finished_at = datetime.now(timezone.utc)

    def end_flow(self) -> None:
        self.active_flow = None
        self.flow_state = None
        self.clear_confirmation()


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
