"""SQLite conversation storage: schema, transactions, ordering and error handling."""

import logging
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, StatementError

from app.core.config import Settings
from app.database.connection import Database, DatabaseError, resolve_sqlite_url
from app.database.models import ConversationSessionRow
from app.models.conversation import ConversationSession, MessageRole, SessionStatus
from app.repositories.conversation_repository import (
    ConversationRepository,
    ConversationStoreError,
    SessionNotActiveError,
    SessionNotFoundError,
)
from app.services.answer_decision_service import Decision

NOW = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)
HASH = "0" * 64  # a stand-in token hash; access checks are tested in the service tests


@pytest.fixture
def database() -> Iterator[Database]:
    db = Database("sqlite://")
    db.init_schema()
    yield db
    db.dispose()


@pytest.fixture
def repo(database: Database) -> ConversationRepository:
    return ConversationRepository(database)


@pytest.fixture
def session(repo: ConversationRepository) -> ConversationSession:
    return repo.create_session(ConversationSession.new(ttl=timedelta(minutes=30), now=NOW), HASH)


def file_database(tmp_path: Path) -> Database:
    db = Database(f"sqlite:///{(tmp_path / 'conversations.db').as_posix()}")
    db.init_schema()
    return db


# --- Configuration and initialisation -------------------------------------------------


def test_relative_sqlite_path_resolves_against_root(tmp_path: Path) -> None:
    url = resolve_sqlite_url("sqlite:///data/conversations.db", root=tmp_path)

    assert Path(url.database) == tmp_path / "data" / "conversations.db"
    assert resolve_sqlite_url("sqlite://").database is None


def test_non_sqlite_urls_are_rejected() -> None:
    with pytest.raises(ValueError, match="SQLite"):
        resolve_sqlite_url("postgresql://user:pw@localhost/db")
    with pytest.raises(ValidationError, match="SQLite"):
        Settings(database_url="postgresql://user:pw@localhost/db")
    with pytest.raises(ValidationError, match="valid database URL"):
        Settings(database_url="not a url")


def test_default_database_url_is_a_git_ignored_file() -> None:
    assert Settings().database_url == "sqlite:///data/conversations.db"
    assert "*.db" in (Path(__file__).parents[1] / ".gitignore").read_text().splitlines()


def test_init_schema_creates_tables_and_is_repeatable(tmp_path: Path) -> None:
    db = file_database(tmp_path)
    repo = ConversationRepository(db)
    stored = repo.create_session(ConversationSession.new(now=NOW), HASH)
    repo.add_user_message(stored.session_id, "Where is my order?", now=NOW)

    db.init_schema()  # again, on a database that has data
    db.dispose()
    reopened = file_database(tmp_path)

    assert set(inspect(reopened.engine).get_table_names()) == {
        "conversation_sessions",
        "conversation_messages",
    }
    assert len(ConversationRepository(reopened).list_messages(stored.session_id)) == 1
    reopened.dispose()


def test_file_database_creates_its_folder_and_uses_wal(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{(tmp_path / 'nested' / 'chat.db').as_posix()}")
    db.init_schema()

    with db.read() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
    assert (tmp_path / "nested" / "chat.db").is_file()
    db.dispose()


# --- Integrity ------------------------------------------------------------------------


def test_foreign_keys_are_enforced(database: Database) -> None:
    with database.read() as db:
        assert db.execute(text("PRAGMA foreign_keys")).scalar() == 1

    with pytest.raises(IntegrityError), database.write() as db:
        db.execute(
            text(
                "INSERT INTO conversation_messages (message_id, session_id, sequence, role, "
                "content, created_at, decision, source_faq_ids) VALUES (:mid, :sid, 1, "
                "'user', 'hi', '2026-10-15 09:30:00', NULL, '[]')"
            ),
            {"mid": uuid.uuid4().hex, "sid": uuid.uuid4().hex},
        )


def test_deleting_a_session_deletes_its_messages(
    repo: ConversationRepository, database: Database, session: ConversationSession
) -> None:
    repo.add_user_message(session.session_id, "Hi", now=NOW)

    with database.write() as db:
        db.execute(
            text("DELETE FROM conversation_sessions WHERE session_id = :sid"),
            {"sid": session.session_id.hex},
        )
    with database.read() as db:
        assert db.execute(text("SELECT count(*) FROM conversation_messages")).scalar() == 0


@pytest.mark.parametrize(
    ("column", "value"),
    [("role", "system"), ("decision", "made_up"), ("sequence", 0), ("content", "")],
)
def test_check_constraints_reject_invalid_rows(
    database: Database, session: ConversationSession, column: str, value: object
) -> None:
    row = {
        "mid": uuid.uuid4().hex,
        "sid": session.session_id.hex,
        "sequence": 1,
        "role": "assistant",
        "content": "Hello",
        "decision": "answered",
    } | {column: value}

    with pytest.raises(IntegrityError), database.write() as db:
        db.execute(
            text(
                "INSERT INTO conversation_messages (message_id, session_id, sequence, role, "
                "content, created_at, decision, source_faq_ids) VALUES (:mid, :sid, "
                ":sequence, :role, :content, '2026-10-15 09:30:00', :decision, '[]')"
            ),
            row,
        )


def test_naive_datetimes_cannot_be_stored(database: Database) -> None:
    naive = datetime(2026, 10, 15, 9, 30)
    row = ConversationSessionRow(
        session_id=uuid.uuid4(),
        created_at=naive,
        updated_at=naive,
        status="active",
        access_token_hash=HASH,
    )

    with pytest.raises(StatementError, match="naive"), database.write() as db:
        db.add(row)
        db.flush()


# --- Sessions -------------------------------------------------------------------------


def test_session_round_trip_keeps_utc(repo: ConversationRepository) -> None:
    created = datetime(2026, 10, 15, 11, 30, tzinfo=timezone(timedelta(hours=2)))
    stored = repo.create_session(ConversationSession.new(ttl=timedelta(hours=1), now=created), HASH)

    loaded = repo.get_session(stored.session_id)

    assert loaded == stored
    assert loaded.created_at == NOW
    assert loaded.created_at.tzinfo is UTC
    assert loaded.expires_at == NOW + timedelta(hours=1)


def test_duplicate_session_id_is_a_store_error(
    repo: ConversationRepository, session: ConversationSession
) -> None:
    with pytest.raises(ConversationStoreError):
        repo.create_session(session, HASH)


def test_set_status(repo: ConversationRepository, session: ConversationSession) -> None:
    later = NOW + timedelta(minutes=5)

    closed = repo.set_status(session.session_id, SessionStatus.CLOSED, now=later)

    assert closed.status is SessionStatus.CLOSED
    assert repo.get_session(session.session_id) == closed
    assert closed.updated_at == later
    with pytest.raises(ValueError, match="cannot change"):
        repo.set_status(session.session_id, SessionStatus.ACTIVE)
    assert repo.get_session(session.session_id).status is SessionStatus.CLOSED


# --- Missing sessions -----------------------------------------------------------------


def test_missing_session_is_reported(repo: ConversationRepository) -> None:
    missing = uuid.uuid4()

    for call in (
        lambda: repo.get_session(missing),
        lambda: repo.get_history(missing),
        lambda: repo.list_messages(missing),
        lambda: repo.add_user_message(missing, "Hi"),
        lambda: repo.add_assistant_message(missing, "Hello", Decision.ANSWERED),
        lambda: repo.set_status(missing, SessionStatus.CLOSED),
    ):
        with pytest.raises(SessionNotFoundError) as exc:
            call()
        assert exc.value.session_id == missing


# --- Messages and ordering ------------------------------------------------------------


def test_messages_round_trip(repo: ConversationRepository, session: ConversationSession) -> None:
    user = repo.add_user_message(session.session_id, "How do I reset my password?", now=NOW)
    reply = repo.add_assistant_message(
        session.session_id,
        "Select Sign In, then Forgot Password.\n\nWe'll email you a link.",
        Decision.ANSWERED,
        ["faq_027", "faq_003"],
        now=NOW + timedelta(seconds=1),
    )

    stored_session, messages = repo.get_history(session.session_id)

    assert messages == [user, reply]
    assert [m.sequence for m in messages] == [1, 2]
    assert messages[1].source_faq_ids == ("faq_027", "faq_003")
    assert messages[1].decision is Decision.ANSWERED
    assert stored_session.updated_at == NOW + timedelta(seconds=1)


def test_order_follows_sequence_not_timestamps(
    repo: ConversationRepository, session: ConversationSession
) -> None:
    # Same timestamp for every message, and a clock that goes backwards.
    for i in range(5):
        repo.add_user_message(session.session_id, f"message {i}", now=NOW - timedelta(seconds=i))

    messages = repo.list_messages(session.session_id)

    assert [m.content for m in messages] == [f"message {i}" for i in range(5)]
    assert [m.sequence for m in messages] == [1, 2, 3, 4, 5]
    assert repo.get_session(session.session_id).updated_at == NOW  # never moves backward


def test_sequences_are_per_session(repo: ConversationRepository) -> None:
    a = repo.create_session(ConversationSession.new(now=NOW), HASH)
    b = repo.create_session(ConversationSession.new(now=NOW), HASH)

    repo.add_user_message(a.session_id, "a1", now=NOW)
    repo.add_user_message(b.session_id, "b1", now=NOW)
    repo.add_user_message(a.session_id, "a2", now=NOW)

    assert [(m.content, m.sequence) for m in repo.list_messages(a.session_id)] == [
        ("a1", 1),
        ("a2", 2),
    ]
    assert [m.content for m in repo.list_messages(b.session_id)] == ["b1"]


def test_concurrent_writers_get_unique_consecutive_positions(tmp_path: Path) -> None:
    db = file_database(tmp_path)
    repo = ConversationRepository(db)
    stored = repo.create_session(ConversationSession.new(), HASH)
    errors: list[BaseException] = []

    def write(worker: int) -> None:
        try:
            for i in range(10):
                repo.add_user_message(stored.session_id, f"w{worker}-{i}")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    messages = repo.list_messages(stored.session_id)
    assert errors == []
    assert [m.sequence for m in messages] == list(range(1, 41))
    db.dispose()


def test_content_is_stored_verbatim_not_executed(
    repo: ConversationRepository, session: ConversationSession
) -> None:
    hostile = "'); DROP TABLE conversation_messages; --"

    repo.add_user_message(session.session_id, hostile, now=NOW)

    assert repo.list_messages(session.session_id)[0].content == hostile


def test_roles_are_set_by_the_method(
    repo: ConversationRepository, session: ConversationSession
) -> None:
    user = repo.add_user_message(session.session_id, "Hi", now=NOW)
    reply = repo.add_assistant_message(session.session_id, "Hello", Decision.NO_MATCH, now=NOW)

    assert (user.role, reply.role) == (MessageRole.USER, MessageRole.ASSISTANT)


# --- Inactive sessions ----------------------------------------------------------------


def test_closed_session_rejects_messages(
    repo: ConversationRepository, session: ConversationSession
) -> None:
    repo.set_status(session.session_id, SessionStatus.CLOSED, now=NOW)

    with pytest.raises(SessionNotActiveError) as exc:
        repo.add_user_message(session.session_id, "Hi", now=NOW)
    assert exc.value.status is SessionStatus.CLOSED
    with pytest.raises(SessionNotActiveError):
        repo.add_assistant_message(session.session_id, "Hello", Decision.ANSWERED, now=NOW)


def test_expired_session_rejects_customer_messages_but_completes_a_reply(
    repo: ConversationRepository, session: ConversationSession
) -> None:
    after_expiry = NOW + timedelta(minutes=31)

    with pytest.raises(SessionNotActiveError) as exc:
        repo.add_user_message(session.session_id, "Hi", now=after_expiry)
    assert exc.value.status is SessionStatus.EXPIRED

    reply = repo.add_assistant_message(
        session.session_id, "Hello", Decision.ANSWERED, now=after_expiry
    )
    assert reply.sequence == 1


# --- Transactions and errors ----------------------------------------------------------


def test_failed_write_changes_nothing(
    repo: ConversationRepository, session: ConversationSession
) -> None:
    with pytest.raises(ValidationError):
        repo.add_user_message(session.session_id, "x" * 5000, now=NOW + timedelta(minutes=1))

    assert repo.list_messages(session.session_id) == []
    assert repo.get_session(session.session_id).updated_at == NOW


def test_write_transaction_rolls_back_on_error(database: Database) -> None:
    with pytest.raises(RuntimeError), database.write() as db:
        db.execute(
            text(
                "INSERT INTO conversation_sessions (session_id, created_at, updated_at, "
                "status, access_token_hash) VALUES (:sid, :t, :t, 'active', :hash)"
            ),
            {"sid": uuid.uuid4().hex, "t": "2026-10-15 09:30:00", "hash": HASH},
        )
        raise RuntimeError("boom")

    with database.read() as db:
        assert db.execute(text("SELECT count(*) FROM conversation_sessions")).scalar() == 0


def test_database_errors_become_store_errors_without_leaking_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    uninitialised = Database("sqlite://")  # no tables
    repo = ConversationRepository(uninitialised)
    caplog.set_level(logging.ERROR)

    with pytest.raises(ConversationStoreError) as exc:
        repo.create_session(ConversationSession.new(), HASH)
    with pytest.raises(ConversationStoreError):
        repo.add_user_message(uuid.uuid4(), "my card number is 4111")

    assert "OperationalError" in caplog.text
    assert "4111" not in caplog.text
    assert "4111" not in str(exc.value)
    assert "no such table" not in str(exc.value)
    uninitialised.dispose()


def test_init_schema_failure_is_a_database_error(tmp_path: Path) -> None:
    (tmp_path / "taken.db").mkdir()  # a folder where the database file should be
    db = Database(f"sqlite:///{(tmp_path / 'taken.db').as_posix()}")

    with pytest.raises(DatabaseError, match="initialisation failed"):
        db.init_schema()
    db.dispose()


def test_corrupt_rows_are_reported_as_store_errors(
    repo: ConversationRepository, database: Database, session: ConversationSession
) -> None:
    repo.add_user_message(session.session_id, "Hi", now=NOW)
    with database.write() as db:  # bypasses the model: user messages cannot cite FAQs
        db.execute(text("UPDATE conversation_messages SET source_faq_ids = '[\"faq_001\"]'"))

    with pytest.raises(ConversationStoreError, match="invalid"):
        repo.list_messages(session.session_id)


def test_add_exchange_stores_both_messages_or_refuses(
    repo: ConversationRepository, session: ConversationSession
) -> None:
    user, reply = repo.add_exchange(
        session.session_id, "Hi", "Hello", Decision.ANSWERED, ["faq_001"], received_at=NOW
    )

    assert (user.role, user.sequence, reply.role, reply.sequence) == (
        MessageRole.USER,
        1,
        MessageRole.ASSISTANT,
        2,
    )
    assert reply.source_faq_ids == ("faq_001",)
    repo.set_status(session.session_id, SessionStatus.CLOSED, now=NOW)
    with pytest.raises(SessionNotActiveError):
        repo.add_exchange(session.session_id, "Hi", "Hello", Decision.ANSWERED, received_at=NOW)
    assert len(repo.list_messages(session.session_id)) == 2
