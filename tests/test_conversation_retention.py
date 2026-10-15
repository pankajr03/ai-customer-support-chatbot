"""Retention: expiry, cleanup of expired conversations, secure deletion, schema upgrade."""

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from app.core.config import Settings
from app.database.connection import Database
from app.main import create_app
from app.models.conversation import SessionStatus
from app.repositories.conversation_repository import ConversationRepository
from app.services.answer_decision_service import Decision
from app.services.conversation_service import ConversationService
from app.services.faq_service import FAQService
from scripts import cleanup_conversations

NOW = datetime.now(UTC).replace(microsecond=0)
TTL = timedelta(minutes=60)


def file_db(tmp_path: Path, name: str = "conversations.db") -> Database:
    db = Database(f"sqlite:///{(tmp_path / name).as_posix()}")
    db.init_schema()
    return db


@pytest.fixture
def database() -> Iterator[Database]:
    db = Database("sqlite://")
    db.init_schema()
    yield db
    db.dispose()


def service_for(database: Database) -> ConversationService:
    return ConversationService(ConversationRepository(database), TTL)


def add_conversation(
    service: ConversationService, created_at: datetime, text: str = "Hi"
) -> uuid.UUID:
    created = service.create_conversation(now=created_at)
    service.record_exchange(
        created.session.session_id,
        created.access_token,
        text,
        "Hello",
        Decision.ANSWERED,
        received_at=created_at,
        answered_at=created_at,
    )
    return created.session.session_id


def counts(database: Database) -> tuple[int, int]:
    with database.read() as db:
        sessions = db.execute(text("SELECT count(*) FROM conversation_sessions")).scalar()
        messages = db.execute(text("SELECT count(*) FROM conversation_messages")).scalar()
    return sessions, messages


# --- Cleanup --------------------------------------------------------------------------


def test_cleanup_deletes_expired_conversations_and_their_messages(database: Database) -> None:
    service = service_for(database)
    old = add_conversation(service, NOW - 2 * TTL)  # expired an hour ago
    closed_old = add_conversation(service, NOW - 2 * TTL)
    service._repo.set_status(closed_old, SessionStatus.CLOSED, now=NOW - 2 * TTL)
    current = add_conversation(service, NOW)

    assert service.cleanup_expired(now=NOW, dry_run=True) == 2
    assert counts(database) == (3, 6)  # a dry run changes nothing

    assert service.cleanup_expired(now=NOW) == 2

    assert counts(database) == (1, 2)
    remaining = service._repo.list_messages(current)
    assert len(remaining) == 2
    with pytest.raises(Exception, match="not found"):
        service._repo.get_session(old)


def test_cleanup_grace_period(database: Database) -> None:
    service = service_for(database)
    add_conversation(service, NOW - TTL - timedelta(minutes=10))  # expired 10 minutes ago

    assert service.cleanup_expired(now=NOW, grace=timedelta(minutes=30)) == 0
    assert service.cleanup_expired(now=NOW, grace=timedelta(minutes=5)) == 1


def test_cleanup_works_in_batches(database: Database) -> None:
    service = service_for(database)
    for _ in range(5):
        add_conversation(service, NOW - 2 * TTL)

    deleted = service._repo.delete_expired_sessions(before=NOW, batch_size=2)

    assert deleted == 5 and counts(database) == (0, 0)
    with pytest.raises(ValueError, match="batch_size"):
        service._repo.delete_expired_sessions(before=NOW, batch_size=0)


def test_nothing_to_clean(database: Database) -> None:
    assert service_for(database).cleanup_expired(now=NOW) == 0


def test_deleted_text_is_not_left_in_the_database_file(tmp_path: Path) -> None:
    database = file_db(tmp_path)
    service = service_for(database)
    secret = "card-4111-1111-1111-1111-" + uuid.uuid4().hex
    add_conversation(service, NOW - 2 * TTL, text=secret)
    add_conversation(service, NOW, text="keep me")

    with database.read() as db:
        assert db.execute(text("PRAGMA secure_delete")).scalar() == 1
    service.cleanup_expired(now=NOW)
    database.dispose()

    files = [p for p in tmp_path.iterdir() if p.name.startswith("conversations.db")]
    content = b"".join(p.read_bytes() for p in files)
    assert secret.encode() not in content
    assert b"keep me" in content


# --- Startup cleanup ------------------------------------------------------------------


def app_with(
    make_settings: Callable[..., Settings],
    faq_service: FAQService,
    database: Database,
    **kw: object,
) -> TestClient:
    return TestClient(create_app(make_settings(**kw), faq_service=faq_service, database=database))


def test_expired_conversations_are_deleted_at_startup(
    make_settings: Callable[..., Settings], faq_service: FAQService, tmp_path: Path
) -> None:
    seed = file_db(tmp_path)
    add_conversation(service_for(seed), NOW - 2 * TTL)
    add_conversation(service_for(seed), NOW)
    seed.dispose()

    database = Database(f"sqlite:///{(tmp_path / 'conversations.db').as_posix()}")
    with app_with(make_settings, faq_service, database):
        assert counts(database) == (1, 2)


def test_startup_cleanup_can_be_switched_off(
    make_settings: Callable[..., Settings], faq_service: FAQService, tmp_path: Path
) -> None:
    seed = file_db(tmp_path)
    add_conversation(service_for(seed), NOW - 2 * TTL)
    seed.dispose()

    database = Database(f"sqlite:///{(tmp_path / 'conversations.db').as_posix()}")
    with app_with(make_settings, faq_service, database, conversation_cleanup_on_startup=False):
        assert counts(database) == (1, 2)


# --- Cleanup command ------------------------------------------------------------------


def test_cleanup_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed = file_db(tmp_path)
    add_conversation(service_for(seed), NOW - 2 * TTL, text="private text")
    add_conversation(service_for(seed), NOW)
    seed.dispose()
    url = f"sqlite:///{(tmp_path / 'conversations.db').as_posix()}"

    assert cleanup_conversations.main(["--dry-run", "--database-url", url]) == 0
    assert "1 expired conversation(s) would be deleted" in capsys.readouterr().out
    assert cleanup_conversations.main(["--database-url", url]) == 0
    output = capsys.readouterr().out
    assert "Deleted 1 expired conversation(s)" in output
    assert "private text" not in output

    check = Database(url)
    assert counts(check) == (1, 2)
    check.dispose()


def test_cleanup_command_uses_settings_and_grace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url = f"sqlite:///{(tmp_path / 'conversations.db').as_posix()}"
    seed = file_db(tmp_path)
    add_conversation(service_for(seed), NOW - TTL - timedelta(minutes=10))
    seed.dispose()

    settings = Settings(_env_file=None, database_url=url)
    assert cleanup_conversations.main(["--grace-minutes", "60"], settings=settings) == 0
    assert "Deleted 0" in capsys.readouterr().out


def test_cleanup_command_reports_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "taken.db").mkdir()  # a folder where the database file should be
    url = f"sqlite:///{(tmp_path / 'taken.db').as_posix()}"

    assert cleanup_conversations.main(["--database-url", url]) == 1
    assert "Cleanup failed" in capsys.readouterr().err


def test_cleanup_command_rejects_negative_grace() -> None:
    with pytest.raises(SystemExit):
        cleanup_conversations.main(["--grace-minutes", "-1"])


# --- Schema upgrade -------------------------------------------------------------------


def test_older_database_files_gain_new_columns_and_keep_data(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = Database(f"sqlite:///{path.as_posix()}")
    with old.write() as db:  # the Task 9 schema: no lease, idempotency or method columns
        db.execute(
            text(
                "CREATE TABLE conversation_sessions (session_id CHAR(32) PRIMARY KEY, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
                "expires_at DATETIME, status VARCHAR(16) NOT NULL, "
                "access_token_hash VARCHAR(64) NOT NULL)"
            )
        )
        db.execute(
            text(
                "INSERT INTO conversation_sessions VALUES (:sid, '2026-01-01 00:00:00', "
                "'2026-01-01 00:00:00', '2099-01-01 00:00:00', 'active', :hash)"
            ),
            {"sid": uuid.uuid4().hex, "hash": "0" * 64},
        )
    old.dispose()

    upgraded = Database(f"sqlite:///{path.as_posix()}")
    upgraded.init_schema()
    upgraded.init_schema()  # repeatable

    columns = {c["name"] for c in inspect(upgraded.engine).get_columns("conversation_sessions")}
    assert {"processing_key", "processing_until"} <= columns
    indexes = {i["name"] for i in inspect(upgraded.engine).get_indexes("conversation_messages")}
    assert "uq_message_idempotency_key" in indexes
    assert counts(upgraded) == (1, 0)
    upgraded.dispose()
