"""Worker functions for tests/test_sqlite_workload.py.

In a module of their own so that separate processes (spawned, as on Windows) can import
them by name. Each worker opens its own Database (its own engine and connections, as a
separate server process would) and performs the real write sequence of a chat turn.
"""

import hashlib
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.orm import Session

from app.database.connection import Database
from app.models.conversation import ConversationSession, utc_now
from app.repositories.conversation_repository import ConversationRepository
from app.services.answer_decision_service import Decision


@dataclass
class WriteTimings:
    """Durations of write transactions, waiting for SQLite's write lock included."""

    durations: list[float] = field(default_factory=list)

    def wrap(self, database: Database) -> None:
        original = database.write

        @contextmanager
        def timed() -> Iterator[Session]:
            started = time.perf_counter()
            try:
                with original() as session:
                    yield session
            finally:
                self.durations.append(time.perf_counter() - started)

        database.write = timed  # type: ignore[method-assign]


def chat_writes(database_url: str, turns: int) -> dict[str, float | int]:
    """``turns`` conversations, each with the writes of one chat turn: create the session,
    take the processing lease, store the exchange and release the lease."""
    database = Database(database_url)
    timings = WriteTimings()
    timings.wrap(database)
    repository = ConversationRepository(database)
    token_hash = hashlib.sha256(b"worker").hexdigest()
    errors = 0
    started = time.perf_counter()
    for _ in range(turns):
        try:
            now = utc_now()
            session = ConversationSession.new(ttl=timedelta(hours=1), now=now)
            repository.create_session(session, token_hash)
            key = uuid.uuid4().hex
            repository.claim_processing(
                session.session_id, key, now=now, lease=timedelta(minutes=3)
            )
            repository.add_exchange(
                session.session_id,
                "Where is my order?",
                "You can track it from your account.",
                Decision.ANSWERED,
                ["faq_001"],
                received_at=now,
                processing_key=key,
            )
        except Exception:  # noqa: BLE001 - counted and reported; the test asserts none
            errors += 1
    elapsed = time.perf_counter() - started
    database.dispose()
    return {
        "writes": len(timings.durations),
        "errors": errors,
        "max_ms": max(timings.durations) * 1000,
        "mean_ms": sum(timings.durations) / len(timings.durations) * 1000,
        "elapsed_s": elapsed,
    }
