"""SQLite under the application's real workload (Stage 13, SQLite review).

Connection settings as applied, foreign keys actually enforced, no write transaction
held during the slow part of a chat turn (where the LLM call happens), and concurrent
writers in separate processes on one database file. Results are recorded in
docs/sqlite-review.md.
"""

import multiprocessing
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.database.connection import DEFAULT_BUSY_TIMEOUT_SECONDS, Database
from app.main import create_app
from app.services.embedding_service import EmbeddingService
from app.services.faq_service import FAQService
from app.services.semantic_search_service import SemanticSearchService
from tests.fakes import FAKE_MODEL_NAME, FAQS, CountingLoader
from tests.sqlite_workers import WriteTimings, chat_writes

SLOW_TURN_SECONDS = 1.0  # stands in for an LLM call inside the answer pipeline


def file_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database(file_url(tmp_path / "workload.db"))
    db.init_schema()
    yield db
    db.dispose()


def pragma(db: Database, name: str) -> object:
    with db.engine.connect() as connection:
        return connection.exec_driver_sql(f"PRAGMA {name}").scalar()


def test_every_connection_gets_the_documented_settings(database: Database) -> None:
    assert pragma(database, "foreign_keys") == 1
    assert pragma(database, "journal_mode") == "wal"
    assert pragma(database, "busy_timeout") == int(DEFAULT_BUSY_TIMEOUT_SECONDS * 1000)
    assert pragma(database, "synchronous") == 2  # FULL: a commit survives a power loss
    assert pragma(database, "secure_delete") == 1


def test_foreign_keys_are_enforced_not_just_declared(database: Database) -> None:
    with pytest.raises(IntegrityError, match="FOREIGN KEY"), database.write() as session:
        session.execute(
            text(
                "INSERT INTO conversation_messages "
                "(message_id, session_id, sequence, role, content, source_faq_ids, created_at) "
                "VALUES ('orphan', 'no-such-conversation', 1, 'user', 'x', '[]', "
                "'2026-10-19 00:00:00')"
            )
        )


def test_no_write_transaction_is_open_during_the_slow_part_of_a_turn(
    tmp_path: Path, make_settings, faq_service: FAQService
) -> None:
    """While a turn waits for the answer pipeline (an LLM call in production), the write
    lock is free: another customer's write completes at once, and no write transaction
    of the turn lasts anywhere near as long as the turn."""
    database = Database(file_url(tmp_path / "turn.db"))
    semantic = SemanticSearchService(
        faq_service.list_active_faqs, EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader())
    )
    app = create_app(
        make_settings(rate_limit_enabled=False),
        faq_service=faq_service,
        semantic_search_service=semantic,
        database=database,
    )
    timings = WriteTimings()
    timings.wrap(database)
    original = app.state.grounded_answer_service.answer
    in_pipeline = threading.Event()

    def slow_answer(*args: object, **kwargs: object) -> object:
        in_pipeline.set()
        time.sleep(SLOW_TURN_SECONDS)
        return original(*args, **kwargs)

    app.state.grounded_answer_service.answer = slow_answer
    with TestClient(app) as client:
        created = client.post("/api/v1/conversations").json()
        headers = {"Authorization": f"Bearer {created['access_token']}"}
        turn: dict[str, object] = {}

        def run_turn() -> None:
            started = time.perf_counter()
            response = client.post(
                f"/api/v1/conversations/{created['session_id']}/chat",
                json={"message": FAQS["faq_107"].question},
                headers=headers,
            )
            turn.update(status=response.status_code, seconds=time.perf_counter() - started)

        thread = threading.Thread(target=run_turn)
        timings.durations.clear()
        thread.start()
        assert in_pipeline.wait(5)
        started = time.perf_counter()
        other = client.post("/api/v1/conversations")  # a write by someone else, mid-turn
        other_seconds = time.perf_counter() - started
        thread.join(10)

    assert turn["status"] == 200 and other.status_code == 201
    assert turn["seconds"] >= SLOW_TURN_SECONDS
    assert other_seconds < 0.5  # not queued behind the turn
    # Every write transaction of the turn (lease, exchange) and of the other request
    # was short; none spanned the slow pipeline.
    assert len(timings.durations) >= 3
    assert max(timings.durations) < 0.5


def test_concurrent_writer_processes_lose_nothing_and_never_time_out(tmp_path: Path) -> None:
    """Four separate processes (each with its own connections, like separate server
    processes) store chat turns on one file at the same time."""
    url = file_url(tmp_path / "concurrent.db")
    setup = Database(url)
    setup.init_schema()
    setup.dispose()
    workers, turns = 4, 25

    context = multiprocessing.get_context("spawn")
    with context.Pool(workers) as pool:
        results = pool.starmap(chat_writes, [(url, turns)] * workers)

    check = Database(url)
    with check.engine.connect() as connection:
        sessions = connection.exec_driver_sql("SELECT count(*) FROM conversation_sessions").scalar()
        messages = connection.exec_driver_sql("SELECT count(*) FROM conversation_messages").scalar()
        broken = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        integrity = connection.exec_driver_sql("PRAGMA integrity_check").scalar()
        # Message numbers stay gap-free per conversation under concurrent writers.
        gaps = connection.exec_driver_sql(
            "SELECT session_id FROM conversation_messages GROUP BY session_id "
            "HAVING count(*) != max(sequence) - min(sequence) + 1"
        ).fetchall()
    check.dispose()

    assert sum(r["errors"] for r in results) == 0
    assert sessions == workers * turns and messages == 2 * workers * turns
    assert broken == [] and integrity == "ok" and gaps == []
    # The slowest write, waiting for the lock included, stayed far below the busy
    # timeout after which a writer would give up.
    assert max(r["max_ms"] for r in results) < DEFAULT_BUSY_TIMEOUT_SECONDS * 1000 / 2
