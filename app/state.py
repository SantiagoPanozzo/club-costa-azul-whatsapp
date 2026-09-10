import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, is_dataclass
from typing import AsyncIterator
from uuid import uuid4

import redis.asyncio as redis
from redis.exceptions import LockError

from .config import settings
from .privacy import log_reference

logger = logging.getLogger(__name__)

SESSION_PREFIX = "whatsapp:bot:session:"
MESSAGE_PREFIX = "whatsapp:bot:message:"
MUTATION_PREFIX = "whatsapp:bot:mutation:"
LOCK_PREFIX = "whatsapp:bot:lock:"
MESSAGE_TTL_SECONDS = 7 * 24 * 60 * 60
LOCK_TIMEOUT_SECONDS = 60


@dataclass
class Session:
    socio: dict[str, object] | None = None
    active_flow: str | None = None
    flow_state: object = None
    member_verified_at: float = 0

    def end_flow(self) -> None:
        self.active_flow = None
        self.flow_state = None


class SessionStore:
    def __init__(self) -> None:
        self._redis: redis.Redis | None = None
        self._sessions: dict[str, tuple[Session, float]] = {}
        self._seen_messages: dict[str, float] = {}
        self._applied_mutations: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def initialize(self) -> None:
        if not settings.session_redis_url:
            logger.warning("SESSION_REDIS_URL is not configured; bot state is process-local")
            return
        self._redis = redis.from_url(settings.session_redis_url, decode_responses=True)
        await self._redis.ping()

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()

    async def ping(self) -> bool:
        if self._redis is None:
            return False
        return bool(await self._redis.ping())

    async def claim_message(self, wamid: str | None) -> bool:
        """Return false after a Meta message has completed successfully."""
        if not wamid:
            return True
        if self._redis is not None:
            return not bool(await self._redis.exists(f"{MESSAGE_PREFIX}{wamid}"))

        now = time.monotonic()
        self._seen_messages = {key: expiry for key, expiry in self._seen_messages.items() if expiry > now}
        return wamid not in self._seen_messages

    async def mark_message_processed(self, wamid: str | None) -> None:
        if not wamid:
            return
        if self._redis is not None:
            await self._redis.set(f"{MESSAGE_PREFIX}{wamid}", "1", ex=MESSAGE_TTL_SECONDS)
            return
        self._seen_messages[wamid] = time.monotonic() + MESSAGE_TTL_SECONDS

    async def was_mutation_applied(self, wamid: str | None, operation: str) -> bool:
        """Return whether this inbound message already completed its external mutation."""
        if not wamid:
            return False
        key = f"{wamid}:{operation}"
        if self._redis is not None:
            return bool(await self._redis.exists(f"{MUTATION_PREFIX}{key}"))
        now = time.monotonic()
        self._applied_mutations = {
            stored_key: expiry for stored_key, expiry in self._applied_mutations.items() if expiry > now
        }
        return key in self._applied_mutations

    async def mark_mutation_applied(self, wamid: str | None, operation: str) -> None:
        if not wamid:
            return
        key = f"{wamid}:{operation}"
        if self._redis is not None:
            await self._redis.set(f"{MUTATION_PREFIX}{key}", "1", ex=MESSAGE_TTL_SECONDS)
            return
        self._applied_mutations[key] = time.monotonic() + MESSAGE_TTL_SECONDS

    @asynccontextmanager
    async def locked(self, phone: str) -> AsyncIterator[Session]:
        """Load, exclusively mutate, and persist one phone's conversation session."""
        local_lock = self._locks.setdefault(phone, asyncio.Lock())
        async with local_lock:
            distributed_lock = None
            if self._redis is not None:
                distributed_lock = self._redis.lock(
                    f"{LOCK_PREFIX}{phone}",
                    timeout=LOCK_TIMEOUT_SECONDS,
                    blocking_timeout=LOCK_TIMEOUT_SECONDS,
                    thread_local=False,
                )
                if not await distributed_lock.acquire(token=str(uuid4())):
                    raise TimeoutError("Could not acquire conversation lock")

            try:
                session = await self._load(phone)
                yield session
                await self._save(phone, session)
            finally:
                if distributed_lock is not None:
                    try:
                        await distributed_lock.release()
                    except LockError:
                        logger.exception(
                            "Conversation lock expired before release for member_ref=%s",
                            log_reference(phone),
                        )

    async def _load(self, phone: str) -> Session:
        if self._redis is not None:
            raw = await self._redis.get(f"{SESSION_PREFIX}{phone}")
            return self._decode(raw) if raw else Session()

        stored = self._sessions.get(phone)
        if stored is None or stored[1] <= time.monotonic():
            return Session()
        return stored[0]

    async def _save(self, phone: str, session: Session) -> None:
        if self._redis is not None:
            await self._redis.set(
                f"{SESSION_PREFIX}{phone}",
                self._encode(session),
                ex=settings.session_ttl_seconds,
            )
            return
        self._sessions[phone] = (session, time.monotonic() + settings.session_ttl_seconds)

    @staticmethod
    def _encode(session: Session) -> str:
        flow_state = asdict(session.flow_state) if is_dataclass(session.flow_state) else session.flow_state
        return json.dumps(
            {
                "socio": session.socio,
                "active_flow": session.active_flow,
                "flow_state": flow_state,
                "member_verified_at": session.member_verified_at,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _decode(raw: str) -> Session:
        data = json.loads(raw)
        return Session(
            socio=data.get("socio"),
            active_flow=data.get("active_flow"),
            flow_state=data.get("flow_state"),
            member_verified_at=float(data.get("member_verified_at", 0)),
        )


sessions = SessionStore()
