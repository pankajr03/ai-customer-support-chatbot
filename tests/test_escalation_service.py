"""Escalation service (Stage 10, Tasks 7 and 8): from a confirmed request to a ticket."""

import logging
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.config import Settings
from app.database.connection import Database
from app.models.conversation import SessionStatus
from app.models.escalation import EscalationReason
from app.models.support import (
    SupportRequestCreate,
    TicketStatus,
    TicketSubmissionOutcome,
    TicketType,
)
from app.repositories.conversation_repository import (
    ConversationRepository,
    ConversationStoreError,
)
from app.repositories.ticket_repository import (
    TicketNotFoundError,
    TicketRepository,
    TicketStoreError,
)
from app.services.answer_decision_service import Decision
from app.services.conversation_service import (
    ConversationExpiredError,
    ConversationNotFoundError,
    ConversationService,
)
from app.services.escalation_service import (
    ConfirmationRequiredError,
    ConsentRequiredError,
    EscalationService,
    IdempotencyKeyConflictError,
    SupportStoreUnavailableError,
)
from tests.ticket_test_data import form, form_body

NOW = datetime(2026, 10, 16, 9, 30, tzinfo=UTC)


@dataclass
class Setup:
    service: EscalationService
    conversations: ConversationService
    messages: ConversationRepository
    tickets: TicketRepository
    database: Database
    session_id: uuid.UUID
    token: str

    def submit(self, request: SupportRequestCreate | None = None, **options: Any) -> Any:
        return self.service.submit(
            self.session_id, self.token, request or form(), now=options.pop("now", NOW), **options
        )

    def offer(self, reason: EscalationReason | None, sequence_hint: str = "Help") -> None:
        """Store a chat exchange whose reply carried ``reason`` as its offer."""
        self.messages.add_exchange(
            self.session_id,
            sequence_hint,
            "Reply",
            Decision.NO_MATCH,
            received_at=NOW,
            escalation_reason=reason,
        )

    def ticket_count(self) -> int:
        return self.tickets.list_tickets(limit=100).total


def build(database: Database, **options: Any) -> Setup:
    messages = ConversationRepository(database)
    conversations = ConversationService(messages, timedelta(hours=1))
    created = conversations.create_conversation(now=NOW)
    tickets = TicketRepository(database)
    service = EscalationService(conversations, messages, tickets, **options)
    return Setup(
        service,
        conversations,
        messages,
        tickets,
        database,
        created.session.session_id,
        created.access_token,
    )


@pytest.fixture
def database() -> Iterator[Database]:
    db = Database("sqlite://")
    db.init_schema()
    yield db
    db.dispose()


@pytest.fixture
def setup(database: Database) -> Setup:
    return build(database)


# --- Offering ---------------------------------------------------------------------------


def test_offers_use_the_escalation_rules() -> None:
    offer = EscalationService.offer_for("I want to speak with a human.", Decision.ANSWERED)

    assert offer is not None and offer.reason is EscalationReason.CUSTOMER_REQUESTED
    assert EscalationService.offer_for("How do I reset my password?", Decision.ANSWERED) is None


# --- Submitting -----------------------------------------------------------------------


def test_a_confirmed_request_becomes_a_ticket(setup: Setup) -> None:
    submission = setup.submit()
    response = submission.response

    assert response.outcome is TicketSubmissionOutcome.CREATED
    assert response.status is TicketStatus.OPEN
    assert response.reference_number == submission.ticket.reference_number
    assert response.reference_number.startswith("SUP-")
    assert response.reference_number in response.message
    assert response.expected_response is None
    assert submission.ticket.session_id == setup.session_id
    assert not submission.replayed


def test_the_confirmation_promises_nothing_it_cannot_keep(setup: Setup) -> None:
    for ticket_type in TicketType:
        message = setup.submit(form(ticket_type=ticket_type.value)).response.message.lower()

        for claim in ("notified", "agent", "assigned", "within", "hours", "soon", "contact you"):
            assert claim not in message, claim


def test_a_configured_response_time_is_shown_as_written(database: Database) -> None:
    text = "We usually reply within one working day."
    setup = build(database, response_time_text=text)

    assert setup.submit().response.expected_response == text


def test_sales_inquiries_get_a_sales_reference(setup: Setup) -> None:
    response = setup.submit(form(ticket_type="sales")).response

    assert response.ticket_type is TicketType.SALES
    assert response.reference_number.startswith("SAL-")


@pytest.mark.parametrize(
    ("field", "error"),
    [("confirmed", ConfirmationRequiredError), ("consent_given", ConsentRequiredError)],
)
def test_no_ticket_without_confirmation_and_consent(
    setup: Setup, field: str, error: type[Exception]
) -> None:
    # The model refuses these already; a request built in code is still checked.
    request = SupportRequestCreate.model_construct(**{**form().model_dump(), field: False})

    with pytest.raises(error):
        setup.submit(request)
    assert setup.ticket_count() == 0


def test_the_conversation_must_be_authorised(setup: Setup) -> None:
    for session_id, token in (
        (setup.session_id, None),
        (setup.session_id, "cst_" + "a" * 43),
        (uuid.uuid4(), setup.token),
        ("not-a-uuid", setup.token),
    ):
        with pytest.raises(ConversationNotFoundError):
            setup.service.submit(session_id, token, form(), now=NOW)
    assert setup.ticket_count() == 0


def test_an_expired_conversation_cannot_send_a_request(setup: Setup) -> None:
    with pytest.raises(ConversationExpiredError):
        setup.submit(now=NOW + timedelta(hours=2))
    assert setup.ticket_count() == 0


def test_a_closed_conversation_can_still_ask_for_help(setup: Setup) -> None:
    setup.conversations.close_conversation(setup.session_id, setup.token, now=NOW)

    assert setup.submit().response.outcome is TicketSubmissionOutcome.CREATED


def test_the_conversation_is_kept_and_stays_open(setup: Setup) -> None:
    setup.offer(EscalationReason.CUSTOMER_REQUESTED)
    before = setup.messages.list_messages(setup.session_id)

    setup.submit()
    setup.messages.add_exchange(  # the customer keeps chatting
        setup.session_id, "One more question", "Reply", Decision.ANSWERED, ["faq_001"],
        received_at=NOW,
    )  # fmt: skip

    session = setup.conversations.get_conversation(setup.session_id, setup.token, now=NOW)
    after = setup.messages.list_messages(setup.session_id)
    assert session.status is SessionStatus.ACTIVE
    assert after[: len(before)] == before and len(after) == len(before) + 2


# --- The offer that led to the request --------------------------------------------------


def test_the_latest_matching_offer_is_recorded(setup: Setup) -> None:
    setup.offer(EscalationReason.REPEATED_NO_MATCH)
    setup.offer(None)
    setup.offer(EscalationReason.CUSTOMER_REQUESTED)

    ticket = setup.submit().ticket

    assert ticket.escalation_reason is EscalationReason.CUSTOMER_REQUESTED


def test_an_offer_of_the_other_type_is_not_recorded(setup: Setup) -> None:
    setup.offer(EscalationReason.SALES_INQUIRY)

    support = setup.submit(form()).ticket
    sales = setup.submit(form(ticket_type="sales", subject="Bulk order")).ticket

    assert support.escalation_reason is None  # unprompted support request
    assert sales.escalation_reason is EscalationReason.SALES_INQUIRY


def test_an_unprompted_request_is_accepted(setup: Setup) -> None:
    assert setup.submit().ticket.escalation_reason is None


def test_an_offer_older_than_the_lookback_is_not_used(database: Database) -> None:
    setup = build(database, offer_lookback_messages=2)
    setup.offer(EscalationReason.CUSTOMER_REQUESTED)
    setup.offer(None)  # two newer messages push it out

    assert setup.submit().ticket.escalation_reason is None


def test_failing_to_read_offers_does_not_block_the_request(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise ConversationStoreError("down")

    monkeypatch.setattr(setup.messages, "list_recent_messages", broken)

    submission = setup.submit()

    assert submission.response.outcome is TicketSubmissionOutcome.CREATED
    assert submission.ticket.escalation_reason is None


# --- Idempotency (Task 8) ---------------------------------------------------------------


def test_a_retry_returns_the_original_ticket(setup: Setup) -> None:
    body = form_body()

    first = setup.submit(SupportRequestCreate.model_validate(body))
    retry = setup.submit(SupportRequestCreate.model_validate(body), now=NOW + timedelta(seconds=30))

    assert retry.response.outcome is TicketSubmissionOutcome.REPLAYED
    assert retry.replayed
    assert retry.response.reference_number == first.response.reference_number
    assert retry.response.message == first.response.message
    assert retry.response.created_at == first.response.created_at
    assert setup.ticket_count() == 1


def test_a_retry_after_an_uncertain_failure_is_safe(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = form_body()
    real = setup.tickets.create_ticket

    def stored_but_reply_lost(*args: Any, **kwargs: Any) -> Any:
        real(*args, **kwargs)  # the ticket is committed ...
        raise TicketStoreError("connection lost")  # ... but the caller never hears

    monkeypatch.setattr(setup.tickets, "create_ticket", stored_but_reply_lost)
    with pytest.raises(SupportStoreUnavailableError):
        setup.submit(SupportRequestCreate.model_validate(body))
    monkeypatch.setattr(setup.tickets, "create_ticket", real)

    retry = setup.submit(SupportRequestCreate.model_validate(body))

    assert retry.response.outcome is TicketSubmissionOutcome.REPLAYED
    assert setup.ticket_count() == 1


def test_the_same_id_with_different_details_is_refused(setup: Setup) -> None:
    key = str(uuid.uuid4())
    setup.submit(form(client_request_id=key))

    with pytest.raises(IdempotencyKeyConflictError):
        setup.submit(form(client_request_id=key, subject="Something else"))
    assert setup.ticket_count() == 1


def test_sending_the_same_request_twice_returns_the_open_ticket(setup: Setup) -> None:
    first = setup.submit(form())
    second = setup.submit(form(), now=NOW + timedelta(minutes=2))  # a new client_request_id

    assert second.response.outcome is TicketSubmissionOutcome.DUPLICATE
    assert second.response.reference_number == first.response.reference_number
    assert "already sent" in second.response.message
    assert setup.ticket_count() == 1


def test_the_duplicate_window_comes_from_settings(
    database: Database, make_settings: Callable[..., Settings]
) -> None:
    settings = make_settings(ticket_duplicate_window_minutes=5)
    messages = ConversationRepository(database)
    conversations = ConversationService(messages, timedelta(hours=1))
    created = conversations.create_conversation(now=NOW)
    service = EscalationService.from_settings(
        settings, conversations, messages, TicketRepository(database)
    )
    sid, token = created.session.session_id, created.access_token

    service.submit(sid, token, form(), now=NOW)
    later = service.submit(sid, token, form(), now=NOW + timedelta(minutes=6))

    assert later.response.outcome is TicketSubmissionOutcome.CREATED


# --- Failures ---------------------------------------------------------------------------


def test_a_database_failure_is_reported_safely(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise TicketStoreError("down")

    monkeypatch.setattr(setup.tickets, "create_ticket", broken)

    with pytest.raises(SupportStoreUnavailableError):
        setup.submit()


def test_a_conversation_deleted_meanwhile_is_not_found(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_get = setup.conversations.get_conversation

    def authorise_then_delete(*args: Any, **kwargs: Any) -> Any:
        session = real_get(*args, **kwargs)
        setup.messages.delete_session(session.session_id)
        return session

    monkeypatch.setattr(setup.conversations, "get_conversation", authorise_then_delete)

    with pytest.raises(ConversationNotFoundError):
        setup.submit()
    assert setup.ticket_count() == 0


def test_logs_never_contain_contact_details_or_text(
    setup: Setup, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    setup.submit(form(name="Zelda Quinn", email="zelda@example.com", phone="+44 20 7946 0958",
                      message="Very private description"))  # fmt: skip

    logged = caplog.text
    assert "event=ticket_submitted" in logged
    for secret in ("Zelda", "zelda@example.com", "7946", "Very private", setup.token):
        assert secret not in logged


# --- The customer's own tickets ---------------------------------------------------------


def test_customers_list_and_read_only_their_own_tickets(setup: Setup, database: Database) -> None:
    mine = setup.submit(form()).response
    other = build(database)
    theirs = other.submit(form(subject="Theirs")).response

    page = setup.service.list_tickets(setup.session_id, setup.token, now=NOW)
    view = setup.service.get_ticket(
        setup.session_id, setup.token, mine.reference_number.lower(), now=NOW
    )

    assert [t.reference_number for t in page.tickets] == [mine.reference_number]
    assert (page.total, page.has_more) == (1, False)
    assert view.reference_number == mine.reference_number
    assert not {"name", "email", "phone", "description", "assigned_to"} & set(view.model_dump())
    with pytest.raises(TicketNotFoundError):
        setup.service.get_ticket(setup.session_id, setup.token, theirs.reference_number, now=NOW)
    with pytest.raises(ConversationNotFoundError):
        setup.service.list_tickets(setup.session_id, other.token, now=NOW)


def test_customer_ticket_lists_are_paginated(setup: Setup) -> None:
    for i in range(3):
        setup.submit(form(subject=f"Request {i}"), now=NOW + timedelta(minutes=i))

    first = setup.service.list_tickets(setup.session_id, setup.token, limit=2, now=NOW)
    rest = setup.service.list_tickets(setup.session_id, setup.token, offset=2, limit=2, now=NOW)

    assert [t.subject for t in first.tickets + rest.tickets] == [
        "Request 0",
        "Request 1",
        "Request 2",
    ]
    assert first.has_more and not rest.has_more
