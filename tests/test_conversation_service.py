"""Conversation sessions: token-based access, isolation, expiry, close and delete."""

import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.config import Settings
from app.core.security import generate_access_token, hash_access_token
from app.database.connection import Database, DatabaseError
from app.models.conversation import SessionStatus
from app.repositories.conversation_repository import ConversationRepository
from app.services.answer_decision_service import Decision
from app.services.conversation_service import (
    ConversationNotActiveError,
    ConversationNotFoundError,
    ConversationService,
    CreatedConversation,
)

NOW = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)
TTL = timedelta(minutes=60)
AFTER_EXPIRY = NOW + TTL + timedelta(seconds=1)


@pytest.fixture
def database() -> Iterator[Database]:
    db = Database("sqlite://")
    db.init_schema()
    yield db
    db.dispose()


@pytest.fixture
def service(database: Database) -> ConversationService:
    return ConversationService(ConversationRepository(database), TTL)


@pytest.fixture
def created(service: ConversationService) -> CreatedConversation:
    return service.create_conversation(now=NOW)


def stored_hashes(database: Database) -> list[str]:
    with database.read() as db:
        return list(
            db.execute(text("SELECT access_token_hash FROM conversation_sessions")).scalars()
        )


# --- Creation -------------------------------------------------------------------------


def test_create_returns_session_and_token_once(
    service: ConversationService, database: Database, created: CreatedConversation
) -> None:
    session = created.session

    assert session.status is SessionStatus.ACTIVE
    assert session.session_id.version == 4
    assert session.expires_at == NOW + TTL
    assert created.access_token.startswith("cst_")
    assert stored_hashes(database) == [hash_access_token(created.access_token)]
    assert "access_token" not in session.model_dump()


def test_raw_token_is_never_stored(database: Database, created: CreatedConversation) -> None:
    with database.read() as db:
        dump = "\n".join(db.connection().connection.iterdump())  # type: ignore[union-attr]

    assert created.access_token not in dump
    assert hash_access_token(created.access_token) in dump


def test_token_hidden_from_repr(created: CreatedConversation) -> None:
    assert created.access_token not in repr(created)


def test_ids_and_tokens_are_unique(service: ConversationService) -> None:
    conversations = [service.create_conversation() for _ in range(50)]

    assert len({c.session.session_id for c in conversations}) == 50
    assert len({c.access_token for c in conversations}) == 50


def test_ttl_comes_from_settings(database: Database) -> None:
    service = ConversationService.from_settings(
        Settings(conversation_ttl_minutes=15), ConversationRepository(database)
    )

    assert service.create_conversation(now=NOW).session.expires_at == NOW + timedelta(minutes=15)
    with pytest.raises(ValueError, match="ttl"):
        ConversationService(ConversationRepository(database), timedelta(0))


# --- Authorisation --------------------------------------------------------------------


def test_valid_token_grants_access(
    service: ConversationService, created: CreatedConversation
) -> None:
    sid, token = created.session.session_id, created.access_token

    assert service.get_conversation(sid, token, now=NOW) == created.session
    assert service.get_conversation(str(sid), token, now=NOW).session_id == sid
    assert service.list_messages(sid, token) == []


def every_operation(service: ConversationService, session_id: object, token: str | None) -> list:
    sid = session_id  # type: ignore[assignment]
    return [
        lambda: service.get_conversation(sid, token, now=NOW),
        lambda: service.get_history(sid, token, now=NOW),
        lambda: service.list_messages(sid, token),
        lambda: service.is_expired(sid, token, now=NOW),
        lambda: service.touch(sid, token, now=NOW),
        lambda: service.add_user_message(sid, token, "Hi", now=NOW),
        lambda: service.add_assistant_message(sid, token, "Hello", Decision.ANSWERED, now=NOW),
        lambda: service.close_conversation(sid, token, now=NOW),
        lambda: service.delete_conversation(sid, token),
    ]


@pytest.mark.parametrize(
    "token",
    [None, "", "cst_wrong", "Bearer x", "SESSION_ID"],
    ids=["missing", "empty", "malformed", "garbage", "session-id-as-token"],
)
def test_session_id_alone_grants_nothing(
    service: ConversationService, created: CreatedConversation, token: str | None
) -> None:
    sid = created.session.session_id
    if token == "SESSION_ID":  # noqa: S105 - a placeholder, not a password
        token = str(sid)

    for operation in every_operation(service, sid, token):
        with pytest.raises(ConversationNotFoundError):
            operation()
    # Nothing was changed by the rejected calls.
    assert service.get_conversation(sid, created.access_token, now=NOW) == created.session


def test_wrong_valid_looking_token_is_rejected(
    service: ConversationService, created: CreatedConversation
) -> None:
    for operation in every_operation(service, created.session.session_id, generate_access_token()):
        with pytest.raises(ConversationNotFoundError):
            operation()


def test_unknown_and_unauthorised_sessions_look_identical(
    service: ConversationService, created: CreatedConversation
) -> None:
    errors = []
    for sid, token in [
        (created.session.session_id, generate_access_token()),  # exists, wrong token
        (uuid.uuid4(), created.access_token),  # does not exist
        ("not-a-uuid", created.access_token),  # malformed ID
        (created.session.session_id, None),  # no token
    ]:
        with pytest.raises(ConversationNotFoundError) as exc:
            service.get_conversation(sid, token)
        errors.append((type(exc.value), str(exc.value), exc.value.args))

    assert len(set(errors)) == 1


def test_sessions_are_isolated(service: ConversationService) -> None:
    a = service.create_conversation(now=NOW)
    b = service.create_conversation(now=NOW)
    service.add_user_message(a.session.session_id, a.access_token, "secret from A", now=NOW)

    # B's token does not open A, and A's messages never appear in B.
    with pytest.raises(ConversationNotFoundError):
        service.list_messages(a.session.session_id, b.access_token)
    assert service.list_messages(b.session.session_id, b.access_token) == []
    with pytest.raises(ConversationNotFoundError):
        service.delete_conversation(a.session.session_id, b.access_token)
    assert len(service.list_messages(a.session.session_id, a.access_token)) == 1


def test_tokens_are_never_logged(
    service: ConversationService, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    created = service.create_conversation(now=NOW)
    sid, token = created.session.session_id, created.access_token
    wrong = generate_access_token()

    service.add_user_message(sid, token, "Hi", now=NOW)
    with pytest.raises(ConversationNotFoundError):
        service.get_conversation(sid, wrong)
    service.close_conversation(sid, token, now=NOW)
    service.delete_conversation(sid, token)

    assert str(sid) in caplog.text  # events are logged ...
    assert token not in caplog.text  # ... but never with a token
    assert wrong not in caplog.text
    assert hash_access_token(token) not in caplog.text
    assert "access denied" in caplog.text


# --- Messages -------------------------------------------------------------------------


def test_post_and_read_messages(service: ConversationService, created: CreatedConversation) -> None:
    sid, token = created.session.session_id, created.access_token

    user = service.add_user_message(sid, token, "How do I reset my password?", now=NOW)
    reply = service.add_assistant_message(
        sid, token, "Use Forgot Password.", Decision.ANSWERED, ["faq_027"], now=NOW
    )
    session, messages = service.get_history(sid, token, now=NOW)

    assert messages == [user, reply]
    assert service.list_messages(sid, token) == messages
    assert session.updated_at == NOW


def test_touch_updates_activity(service: ConversationService, created: CreatedConversation) -> None:
    later = NOW + timedelta(minutes=5)

    touched = service.touch(created.session.session_id, created.access_token, now=later)

    assert touched.updated_at == later
    assert (
        service.get_conversation(
            created.session.session_id, created.access_token, now=later
        ).updated_at
        == later
    )


# --- Expiry ---------------------------------------------------------------------------


def test_expiry_is_checked_and_recorded(
    service: ConversationService, created: CreatedConversation
) -> None:
    sid, token = created.session.session_id, created.access_token

    assert not service.is_expired(sid, token, now=NOW)
    assert service.is_expired(sid, token, now=AFTER_EXPIRY)
    # Recorded: later reads report it even with an earlier clock.
    assert service.get_conversation(sid, token, now=NOW).status is SessionStatus.EXPIRED


def test_expired_session_is_readable_but_rejects_customer_messages(
    service: ConversationService, created: CreatedConversation
) -> None:
    sid, token = created.session.session_id, created.access_token
    service.add_user_message(sid, token, "Hi", now=NOW)

    with pytest.raises(ConversationNotActiveError) as exc:
        service.add_user_message(sid, token, "Still there?", now=AFTER_EXPIRY)

    assert exc.value.status is SessionStatus.EXPIRED
    assert len(service.list_messages(sid, token)) == 1


def test_reply_after_expiry_completes_the_exchange(
    service: ConversationService, created: CreatedConversation
) -> None:
    sid, token = created.session.session_id, created.access_token
    service.add_user_message(sid, token, "Hi", now=NOW + TTL - timedelta(seconds=1))

    reply = service.add_assistant_message(sid, token, "Hello", Decision.ANSWERED, now=AFTER_EXPIRY)

    assert reply.sequence == 2


# --- Close and delete -----------------------------------------------------------------


def test_close_conversation(service: ConversationService, created: CreatedConversation) -> None:
    sid, token = created.session.session_id, created.access_token
    later = NOW + timedelta(minutes=1)

    closed = service.close_conversation(sid, token, now=later)

    assert closed.status is SessionStatus.CLOSED
    assert closed.updated_at == later
    assert service.close_conversation(sid, token, now=later + TTL) == closed  # idempotent
    with pytest.raises(ConversationNotActiveError):
        service.add_user_message(sid, token, "Hi", now=later)
    with pytest.raises(ConversationNotActiveError):
        service.add_assistant_message(sid, token, "Hello", Decision.ANSWERED, now=later)
    assert service.list_messages(sid, token) == []  # still readable


def test_closing_an_expired_conversation_keeps_it_expired(
    service: ConversationService, created: CreatedConversation
) -> None:
    session = service.close_conversation(
        created.session.session_id, created.access_token, now=AFTER_EXPIRY
    )

    assert session.status is SessionStatus.EXPIRED


def test_delete_removes_session_and_messages(
    service: ConversationService, database: Database, created: CreatedConversation
) -> None:
    sid, token = created.session.session_id, created.access_token
    service.add_user_message(sid, token, "Hi", now=NOW)
    other = service.create_conversation(now=NOW)
    service.add_user_message(other.session.session_id, other.access_token, "Other", now=NOW)

    service.delete_conversation(sid, token)

    with pytest.raises(ConversationNotFoundError):
        service.get_conversation(sid, token)
    with pytest.raises(ConversationNotFoundError):
        service.delete_conversation(sid, token)
    with database.read() as db:
        remaining = db.execute(text("SELECT content FROM conversation_messages")).scalars()
        assert list(remaining) == ["Other"]


def test_operation_on_session_deleted_concurrently_is_not_found(
    service: ConversationService, database: Database, created: CreatedConversation
) -> None:
    sid, token = created.session.session_id, created.access_token
    repo = ConversationRepository(database)
    original = repo.get_session_and_token_hash

    def delete_after_lookup(session_id: uuid.UUID) -> tuple:
        result = original(session_id)
        repo.delete_session(session_id)  # another request deletes it meanwhile
        return result

    service._repo.get_session_and_token_hash = delete_after_lookup  # type: ignore[method-assign]

    with pytest.raises(ConversationNotFoundError):
        service.add_user_message(sid, token, "Hi", now=NOW)


# --- Schema ---------------------------------------------------------------------------


def test_outdated_database_file_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = Database(f"sqlite:///{path.as_posix()}")
    with old.write() as db:  # the Task 3 table, without access_token_hash
        db.execute(
            text(
                "CREATE TABLE conversation_sessions (session_id CHAR(32) PRIMARY KEY, "
                "created_at DATETIME, updated_at DATETIME, expires_at DATETIME, status TEXT)"
            )
        )
    old.dispose()

    db = Database(f"sqlite:///{path.as_posix()}")
    with pytest.raises(DatabaseError, match="access_token_hash"):
        db.init_schema()
    db.dispose()


def test_expiry_racing_a_close_keeps_the_close(
    service: ConversationService, created: CreatedConversation
) -> None:
    sid, token = created.session.session_id, created.access_token
    repo = service._repo
    original = repo.set_status

    def closed_first(session_id: uuid.UUID, status: SessionStatus, now: datetime | None = None):
        original(session_id, SessionStatus.CLOSED, NOW)  # another request closes it first
        return original(session_id, status, now)

    repo.set_status = closed_first  # type: ignore[method-assign]

    assert service.get_conversation(sid, token, now=AFTER_EXPIRY).status is SessionStatus.CLOSED
