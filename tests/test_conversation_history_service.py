"""Recent conversation history: order, limits, access and isolation."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event

from app.core.config import Settings
from app.database.connection import Database
from app.models.conversation import MessageRole, SessionStatus
from app.repositories.conversation_repository import (
    ConversationRepository,
    ConversationStoreError,
)
from app.services.answer_decision_service import Decision
from app.services.conversation_history_service import (
    ConversationHistoryService,
    HistoryTurn,
    RecentHistory,
)
from app.services.conversation_service import (
    ConversationNotActiveError,
    ConversationNotFoundError,
    ConversationService,
    CreatedConversation,
)

# Relative to the real clock: some calls (like the API) use the current time.
NOW = datetime.now(UTC).replace(microsecond=0)
TTL = timedelta(minutes=60)


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
def conversations(repo: ConversationRepository) -> ConversationService:
    return ConversationService(repo, TTL)


def history_service(
    conversations: ConversationService,
    repo: ConversationRepository,
    max_messages: int = 10,
    max_chars: int = 4000,
) -> ConversationHistoryService:
    return ConversationHistoryService(
        conversations, repo, max_messages=max_messages, max_chars=max_chars
    )


def chat(conversations: ConversationService, created: CreatedConversation, exchanges: int) -> None:
    """Add ``exchanges`` question/answer pairs: q1, a1, q2, a2, ..."""
    sid, token = created.session.session_id, created.access_token
    for i in range(1, exchanges + 1):
        conversations.add_user_message(sid, token, f"q{i}", now=NOW)
        conversations.add_assistant_message(
            sid, token, f"a{i}", Decision.ANSWERED, [f"faq_{i:03d}"], now=NOW
        )


def contents(history: RecentHistory) -> list[str]:
    return [turn.content for turn in history.turns]


@pytest.fixture
def created(conversations: ConversationService) -> CreatedConversation:
    return conversations.create_conversation(now=NOW)


# --- Order, roles and limits ----------------------------------------------------------


def test_recent_history_is_chronological_with_roles(
    conversations: ConversationService, repo: ConversationRepository, created: CreatedConversation
) -> None:
    chat(conversations, created, 2)

    history = history_service(conversations, repo).load_recent(
        created.session.session_id, created.access_token, now=NOW
    )

    assert contents(history) == ["q1", "a1", "q2", "a2"]
    assert [t.role for t in history.turns] == [MessageRole.USER, MessageRole.ASSISTANT] * 2
    assert [t.sequence for t in history.turns] == [1, 2, 3, 4]
    assert history.turns[0] == HistoryTurn(MessageRole.USER, "q1", 1)
    assert history.turns[1].decision is Decision.ANSWERED
    assert history.turns[1].source_faq_ids == ("faq_001",)
    assert history.turns[0].is_from_customer and not history.turns[1].is_from_customer
    assert not history.omitted_older
    assert history.total_chars == 8
    assert len(history) == 4


def test_empty_conversation_has_empty_history(
    conversations: ConversationService, repo: ConversationRepository, created: CreatedConversation
) -> None:
    history = history_service(conversations, repo).load_recent(
        created.session.session_id, created.access_token, now=NOW
    )

    assert history.turns == () and not history.omitted_older


def test_max_messages_keeps_the_most_recent(
    conversations: ConversationService, repo: ConversationRepository, created: CreatedConversation
) -> None:
    chat(conversations, created, 5)

    history = history_service(conversations, repo, max_messages=3).load_recent(
        created.session.session_id, created.access_token, now=NOW
    )

    assert contents(history) == ["a4", "q5", "a5"]
    assert history.omitted_older


def test_exactly_max_messages_omits_nothing(
    conversations: ConversationService, repo: ConversationRepository, created: CreatedConversation
) -> None:
    chat(conversations, created, 2)

    history = history_service(conversations, repo, max_messages=4).load_recent(
        created.session.session_id, created.access_token, now=NOW
    )

    assert len(history) == 4 and not history.omitted_older


def test_character_budget_drops_older_whole_messages(
    conversations: ConversationService, repo: ConversationRepository, created: CreatedConversation
) -> None:
    sid, token = created.session.session_id, created.access_token
    for text in ["a" * 50, "b" * 30, "c" * 40, "d" * 20]:
        conversations.add_user_message(sid, token, text, now=NOW)

    history = history_service(conversations, repo, max_chars=60).load_recent(sid, token, now=NOW)

    assert contents(history) == ["c" * 40, "d" * 20]  # 60 chars; "b" * 30 would exceed it
    assert history.total_chars == 60
    assert history.omitted_older


def test_budget_never_skips_a_message_to_include_an_older_one(
    conversations: ConversationService, repo: ConversationRepository, created: CreatedConversation
) -> None:
    sid, token = created.session.session_id, created.access_token
    for text in ["short", "x" * 100, "tail"]:
        conversations.add_user_message(sid, token, text, now=NOW)

    history = history_service(conversations, repo, max_chars=20).load_recent(sid, token, now=NOW)

    # "short" would fit, but the window stays contiguous: no gap in the conversation.
    assert contents(history) == ["tail"]


def test_message_larger_than_budget_gives_empty_history(
    conversations: ConversationService, repo: ConversationRepository, created: CreatedConversation
) -> None:
    sid, token = created.session.session_id, created.access_token
    conversations.add_user_message(sid, token, "x" * 100, now=NOW)

    history = history_service(conversations, repo, max_chars=50).load_recent(sid, token, now=NOW)

    assert history.turns == () and history.omitted_older


def test_before_sequence_excludes_the_current_question(
    conversations: ConversationService, repo: ConversationRepository, created: CreatedConversation
) -> None:
    chat(conversations, created, 1)
    sid, token = created.session.session_id, created.access_token
    current = conversations.add_user_message(sid, token, "q2", now=NOW)

    history = history_service(conversations, repo).load_recent(
        sid, token, before_sequence=current.sequence, now=NOW
    )

    assert contents(history) == ["q1", "a1"]


def test_only_needed_rows_are_queried(
    conversations: ConversationService,
    repo: ConversationRepository,
    database: Database,
    created: CreatedConversation,
) -> None:
    chat(conversations, created, 20)
    statements: list[tuple[str, Any]] = []

    @event.listens_for(database.engine, "before_cursor_execute")
    def capture(_conn: Any, _cursor: Any, statement: str, params: Any, *_: Any) -> None:
        statements.append((statement, params))

    history_service(conversations, repo, max_messages=4).load_recent(
        created.session.session_id, created.access_token, now=NOW
    )

    selects = [(s, p) for s, p in statements if "FROM conversation_messages" in s]
    assert len(selects) == 1
    sql, params = selects[0]
    assert "ORDER BY conversation_messages.sequence DESC" in sql
    assert "LIMIT" in sql and 5 in params  # max_messages + 1, not all 40 messages


def test_settings_configure_the_limits(
    conversations: ConversationService, repo: ConversationRepository
) -> None:
    service = ConversationHistoryService.from_settings(
        Settings(conversation_history_max_messages=7, conversation_history_max_chars=900),
        conversations,
        repo,
    )

    assert (service.max_messages, service.max_chars) == (7, 900)
    assert (
        Settings().conversation_history_max_messages,
        Settings().conversation_history_max_chars,
    ) == (10, 4000)
    for kwargs in ({"max_messages": 0, "max_chars": 10}, {"max_messages": 1, "max_chars": 0}):
        with pytest.raises(ValueError):
            ConversationHistoryService(conversations, repo, **kwargs)
    with pytest.raises(ValueError, match="limit"):
        repo.list_recent_messages(uuid.uuid4(), 0)


# --- Access and isolation -------------------------------------------------------------


def test_unauthorised_access_gets_no_history(
    conversations: ConversationService, repo: ConversationRepository, created: CreatedConversation
) -> None:
    chat(conversations, created, 1)
    other = conversations.create_conversation(now=NOW)
    service = history_service(conversations, repo)

    for sid, token in [
        (created.session.session_id, None),
        (created.session.session_id, other.access_token),  # another customer's token
        (uuid.uuid4(), created.access_token),
        ("not-a-uuid", created.access_token),
    ]:
        with pytest.raises(ConversationNotFoundError):
            service.load_recent(sid, token, now=NOW)


def test_histories_never_mix_sessions(
    conversations: ConversationService, repo: ConversationRepository
) -> None:
    a = conversations.create_conversation(now=NOW)
    b = conversations.create_conversation(now=NOW)
    conversations.add_user_message(a.session.session_id, a.access_token, "from A", now=NOW)
    conversations.add_user_message(b.session.session_id, b.access_token, "from B", now=NOW)
    conversations.add_user_message(a.session.session_id, a.access_token, "A again", now=NOW)
    service = history_service(conversations, repo)

    history_a = service.load_recent(a.session.session_id, a.access_token, now=NOW)
    history_b = service.load_recent(b.session.session_id, b.access_token, now=NOW)

    assert contents(history_a) == ["from A", "A again"]
    assert contents(history_b) == ["from B"]
    assert history_a.session_id == a.session.session_id


@pytest.mark.parametrize("status", [SessionStatus.CLOSED, SessionStatus.EXPIRED])
def test_inactive_conversations_give_no_history(
    conversations: ConversationService,
    repo: ConversationRepository,
    created: CreatedConversation,
    status: SessionStatus,
) -> None:
    chat(conversations, created, 1)
    sid, token = created.session.session_id, created.access_token
    now = NOW
    if status is SessionStatus.CLOSED:
        conversations.close_conversation(sid, token, now=NOW)
    else:
        now = NOW + TTL  # expires_at reached

    with pytest.raises(ConversationNotActiveError) as exc:
        history_service(conversations, repo).load_recent(sid, token, now=now)
    assert exc.value.status is status


def test_session_deleted_during_load_is_not_found(
    conversations: ConversationService, repo: ConversationRepository, created: CreatedConversation
) -> None:
    sid, token = created.session.session_id, created.access_token
    original = repo.list_recent_messages

    def deleted_first(*args: Any) -> Any:
        repo.delete_session(sid)
        return original(*args)

    repo.list_recent_messages = deleted_first  # type: ignore[method-assign]

    with pytest.raises(ConversationNotFoundError):
        history_service(conversations, repo).load_recent(sid, token, now=NOW)


def test_rows_from_another_session_are_refused(
    conversations: ConversationService, repo: ConversationRepository, created: CreatedConversation
) -> None:
    other = conversations.create_conversation(now=NOW)
    conversations.add_user_message(other.session.session_id, other.access_token, "B", now=NOW)
    leaked = repo.list_messages(other.session.session_id)
    repo.list_recent_messages = lambda *_: leaked  # type: ignore[method-assign]

    with pytest.raises(ConversationStoreError):
        history_service(conversations, repo).load_recent(
            created.session.session_id, created.access_token, now=NOW
        )
