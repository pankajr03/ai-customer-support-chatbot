"""Validation rules for the conversation session and message models."""

import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from app.models.conversation import (
    MAX_ASSISTANT_MESSAGE_LENGTH,
    MAX_USER_MESSAGE_LENGTH,
    ConversationCreateResponse,
    ConversationHistoryResponse,
    ConversationMessage,
    ConversationMessageResponse,
    ConversationSession,
    ConversationSessionResponse,
    MessageRole,
    SessionStatus,
    check_message_order,
)
from app.services.answer_decision_service import Decision

NOW = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)
TTL = timedelta(minutes=30)


def make_session() -> ConversationSession:
    return ConversationSession.new(ttl=TTL, now=NOW)


def user_msg(session: ConversationSession, sequence: int = 1, content: str = "Hi") -> Any:
    return ConversationMessage.user_message(session.session_id, sequence, content, now=NOW)


def assistant_msg(session: ConversationSession, sequence: int = 2, **kwargs: Any) -> Any:
    kwargs.setdefault("content", "Hello!")
    kwargs.setdefault("decision", Decision.ANSWERED)
    return ConversationMessage.assistant_message(session.session_id, sequence, now=NOW, **kwargs)


def message_data(**overrides: Any) -> dict[str, Any]:
    return {
        "message_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "sequence": 1,
        "role": "user",
        "content": "Where is my order?",
        "created_at": NOW,
        **overrides,
    }


# --- Identifiers and timestamps -------------------------------------------------------


def test_new_session_defaults() -> None:
    session = ConversationSession.new(ttl=TTL, now=NOW)

    assert session.session_id.version == 4
    assert session.status is SessionStatus.ACTIVE
    assert session.created_at == session.updated_at == NOW
    assert session.expires_at == NOW + TTL


def test_session_without_ttl_does_not_expire() -> None:
    session = ConversationSession.new(now=NOW)

    assert session.expires_at is None
    assert not session.is_expired(NOW + timedelta(days=365))


def test_ids_are_random_and_unique() -> None:
    sessions = [ConversationSession.new() for _ in range(100)]
    session = sessions[0]
    messages = [user_msg(session, sequence=i) for i in range(1, 101)]

    assert len({s.session_id for s in sessions}) == 100
    assert len({m.message_id for m in messages}) == 100
    assert all(m.message_id.version == 4 for m in messages)


def test_non_uuid4_ids_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ConversationMessage.model_validate(message_data(message_id=uuid.uuid1()))
    with pytest.raises(ValidationError):
        ConversationMessage.model_validate(message_data(session_id="session-1"))


def test_default_timestamps_are_utc() -> None:
    session = ConversationSession.new()

    assert session.created_at.tzinfo is UTC


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ConversationMessage.model_validate(message_data(created_at=datetime(2026, 10, 15)))


def test_other_timezones_are_converted_to_utc() -> None:
    plus_two = datetime(2026, 10, 15, 11, 30, tzinfo=timezone(timedelta(hours=2)))

    message = ConversationMessage.model_validate(message_data(created_at=plus_two))

    assert message.created_at == NOW
    assert message.created_at.utcoffset() == timedelta(0)


def test_session_time_invariants() -> None:
    session = make_session()
    data = session.model_dump()

    with pytest.raises(ValidationError, match="updated_at"):
        ConversationSession.model_validate({**data, "updated_at": NOW - timedelta(seconds=1)})
    with pytest.raises(ValidationError, match="expires_at"):
        ConversationSession.model_validate({**data, "expires_at": NOW})
    with pytest.raises(ValueError, match="ttl"):
        ConversationSession.new(ttl=timedelta(0))


# --- Session status -------------------------------------------------------------------


def test_invalid_status_is_rejected() -> None:
    data = make_session().model_dump()

    with pytest.raises(ValidationError):
        ConversationSession.model_validate({**data, "status": "paused"})


def test_expiry_by_time_and_status() -> None:
    session = make_session()

    assert session.accepts_messages(NOW)
    assert not session.is_expired(NOW + TTL - timedelta(seconds=1))
    assert session.is_expired(NOW + TTL)
    assert not session.accepts_messages(NOW + TTL)
    assert session.with_status(SessionStatus.EXPIRED, now=NOW).is_expired(NOW)


def test_closed_session_does_not_accept_messages_and_cannot_reopen() -> None:
    closed = make_session().with_status(SessionStatus.CLOSED, now=NOW + timedelta(minutes=1))

    assert closed.status is SessionStatus.CLOSED
    assert closed.updated_at == NOW + timedelta(minutes=1)
    assert not closed.accepts_messages(NOW)
    assert closed.with_status(SessionStatus.CLOSED) is closed
    with pytest.raises(ValueError, match="cannot change"):
        closed.with_status(SessionStatus.ACTIVE)


def test_touched_never_moves_updated_at_backward() -> None:
    session = make_session()
    later = NOW + timedelta(minutes=5)

    assert session.touched(later).updated_at == later
    assert session.touched(later).touched(NOW).updated_at == later


def test_records_are_immutable_and_reject_extra_fields() -> None:
    session = make_session()

    with pytest.raises(ValidationError):
        session.status = SessionStatus.CLOSED  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ConversationSession.model_validate({**session.model_dump(), "system_prompt": "x"})
    with pytest.raises(ValidationError):
        ConversationMessage.model_validate(message_data(api_key="sk-test"))


# --- Roles and content ----------------------------------------------------------------


@pytest.mark.parametrize("role", ["system", "developer", "tool", "USER ", ""])
def test_only_user_and_assistant_roles(role: str) -> None:
    with pytest.raises(ValidationError):
        ConversationMessage.model_validate(message_data(role=role))


def test_user_message_factory_always_sets_user_role() -> None:
    message = user_msg(make_session(), content="  Where is my order?\n")

    assert message.role is MessageRole.USER
    assert message.content == "Where is my order?"
    assert message.decision is None
    assert message.source_faq_ids == ()


@pytest.mark.parametrize("content", ["", "   ", "\n\t"])
def test_blank_content_is_rejected(content: str) -> None:
    with pytest.raises(ValidationError):
        ConversationMessage.model_validate(message_data(content=content))


def test_user_message_length_limit() -> None:
    ConversationMessage.model_validate(message_data(content="a" * MAX_USER_MESSAGE_LENGTH))

    with pytest.raises(ValidationError, match="at most"):
        ConversationMessage.model_validate(
            message_data(content="a" * (MAX_USER_MESSAGE_LENGTH + 1))
        )


def test_assistant_message_length_limit() -> None:
    session = make_session()

    assert assistant_msg(session, content="a" * MAX_ASSISTANT_MESSAGE_LENGTH)
    with pytest.raises(ValidationError):
        assistant_msg(session, content="a" * (MAX_ASSISTANT_MESSAGE_LENGTH + 1))


def test_assistant_content_keeps_line_breaks() -> None:
    message = assistant_msg(make_session(), content="Steps:\n1. Sign in\n2. Open Orders\n")

    assert message.content == "Steps:\n1. Sign in\n2. Open Orders"


def test_user_messages_cannot_carry_decision_or_sources() -> None:
    with pytest.raises(ValidationError, match="user messages"):
        ConversationMessage.model_validate(message_data(decision="answered"))
    with pytest.raises(ValidationError, match="user messages"):
        ConversationMessage.model_validate(message_data(source_faq_ids=["faq_001"]))


def test_assistant_messages_require_decision() -> None:
    with pytest.raises(ValidationError, match="must have a decision"):
        ConversationMessage.model_validate(message_data(role="assistant"))


def test_assistant_source_faq_ids_are_validated() -> None:
    session = make_session()

    message = assistant_msg(session, source_faq_ids=[" FAQ_001 ", "faq_002"])
    assert message.source_faq_ids == ("faq_001", "faq_002")

    with pytest.raises(ValidationError):
        assistant_msg(session, source_faq_ids=["not-an-id"])
    with pytest.raises(ValidationError, match="duplicates"):
        assistant_msg(session, source_faq_ids=["faq_001", "faq_001"])


def test_sequence_must_be_positive_integer() -> None:
    for sequence in (0, -1, "1", 1.0):
        with pytest.raises(ValidationError):
            ConversationMessage.model_validate(message_data(sequence=sequence))


# --- Ordering -------------------------------------------------------------------------


def test_check_message_order_sorts_by_sequence() -> None:
    session = make_session()
    messages = [user_msg(session, 3), assistant_msg(session, 2), user_msg(session, 1)]

    assert [m.sequence for m in check_message_order(messages)] == [1, 2, 3]
    assert check_message_order([]) == []


def test_check_message_order_rejects_gaps_duplicates_and_mixed_sessions() -> None:
    session, other = make_session(), ConversationSession.new()

    with pytest.raises(ValueError, match="consecutive"):
        check_message_order([user_msg(session, 1), user_msg(session, 3)])
    with pytest.raises(ValueError, match="consecutive"):
        check_message_order([user_msg(session, 1), user_msg(session, 1)])
    with pytest.raises(ValueError, match="different sessions"):
        check_message_order([user_msg(session, 1), user_msg(other, 2)])


# --- API models -----------------------------------------------------------------------


def test_create_response_from_session() -> None:
    session = make_session()

    body = ConversationCreateResponse.from_session(session, "cst_token").model_dump(mode="json")

    assert body == {
        "session_id": str(session.session_id),
        "access_token": "cst_token",
        "token_type": "Bearer",
        "status": "active",
        "created_at": "2026-10-15T09:30:00Z",
        "expires_at": "2026-10-15T10:00:00Z",
    }


def test_session_response_has_metadata_only() -> None:
    session = make_session()

    body = ConversationSessionResponse.model_validate(session).model_dump(mode="json")

    assert set(body) == {"session_id", "status", "created_at", "updated_at", "expires_at"}


def test_history_response_is_ordered_and_hides_internal_fields() -> None:
    session = make_session()
    messages = [assistant_msg(session, 2, source_faq_ids=["faq_027"]), user_msg(session, 1)]

    history = ConversationHistoryResponse.from_records(session, messages, total=2)
    body = history.model_dump(mode="json")

    assert (body["total_messages"], body["offset"], body["has_more"]) == (2, 0, False)

    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][1]["source_faq_ids"] == ["faq_027"]
    assert set(body["messages"][0]) == {
        "message_id",
        "role",
        "content",
        "created_at",
        "decision",
        "source_faq_ids",
        "sources",  # approved questions of source_faq_ids (Stage 9)
        "client_message_id",  # the client's own ID for its message (Stage 9)
        "escalation_offer",  # the offer made with a reply, null otherwise (Stage 10)
    }


def test_history_sources_list_only_approved_faqs_in_stored_order() -> None:
    session = make_session()
    reply = assistant_msg(session, 2, source_faq_ids=["faq_027", "faq_003", "faq_009"])
    questions = {"faq_009": "Q9?", "faq_027": "Q27?"}  # faq_003 was withdrawn

    history = ConversationHistoryResponse.from_records(
        session, [user_msg(session, 1), reply], total=2, questions=questions
    )

    assert history.messages[0].sources == []
    assert [(s.faq_id, s.question) for s in history.messages[1].sources] == [
        ("faq_027", "Q27?"),
        ("faq_009", "Q9?"),
    ]
    assert history.messages[1].source_faq_ids == ["faq_027", "faq_003", "faq_009"]


def test_history_rejects_messages_from_another_session() -> None:
    session = make_session()
    other = ConversationSession.new()

    with pytest.raises(ValueError, match="different session"):
        ConversationHistoryResponse.from_records(session, [user_msg(other, 1)], total=1)


def test_history_page_starts_at_the_offset() -> None:
    session = make_session()
    page = [user_msg(session, 3), assistant_msg(session, 4)]

    history = ConversationHistoryResponse.from_records(session, page, total=6, offset=2, limit=2)

    assert len(history.messages) == 2 and history.has_more
    with pytest.raises(ValueError, match="consecutive"):
        ConversationHistoryResponse.from_records(session, page, total=6, offset=0, limit=2)


def test_message_response_has_no_diagnostic_fields() -> None:
    fields = set(ConversationMessageResponse.model_fields)

    assert fields.isdisjoint({"reason", "prompt", "diagnostics", "sequence", "session_id"})
