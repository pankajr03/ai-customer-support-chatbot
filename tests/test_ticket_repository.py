"""Support ticket storage (Stage 10, Tasks 3, 5 and 8): schema, constraints, contacts,
events, listing, conversation scope, idempotency and duplicates, and upgrading an
existing database without losing data."""

import re
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.database.connection import Database, DatabaseError
from app.models.conversation import ConversationSession
from app.models.escalation import EscalationReason
from app.models.support import (
    REFERENCE_PATTERN,
    ActorType,
    SupportRequestCreate,
    SupportTicket,
    TicketEventType,
    TicketPriority,
    TicketStatus,
    TicketSubmissionOutcome,
    TicketType,
)
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.ticket_repository import (
    ConversationLinkError,
    IdempotencyKeyConflictError,
    InvalidStatusTransitionError,
    StaleTicketStatusError,
    TicketFilter,
    TicketNotFoundError,
    TicketRepository,
    TicketStoreError,
    contact_identity,
    new_reference_number,
)
from app.services.answer_decision_service import Decision
from tests.ticket_test_data import form

NOW = datetime(2026, 10, 16, 9, 30, tzinfo=UTC)
HASH = "0" * 64


@pytest.fixture
def database() -> Iterator[Database]:
    db = Database("sqlite://")
    db.init_schema()
    yield db
    db.dispose()


@pytest.fixture
def repo(database: Database) -> TicketRepository:
    return TicketRepository(database)


@pytest.fixture
def conversations(database: Database) -> ConversationRepository:
    return ConversationRepository(database)


@pytest.fixture
def session(conversations: ConversationRepository) -> ConversationSession:
    return conversations.create_session(
        ConversationSession.new(ttl=timedelta(minutes=30), now=NOW), HASH
    )


def create(repo: TicketRepository, request: SupportRequestCreate, **options: Any) -> SupportTicket:
    """Create a ticket and return it (the outcome is checked in the idempotency tests)."""
    return repo.create_ticket(request, **options).ticket


def count(database: Database, table: str) -> int:
    with database.read() as db:
        return int(db.execute(text(f"SELECT count(*) FROM {table}")).scalar() or 0)  # noqa: S608


# --- Creating tickets -----------------------------------------------------------------


def test_a_ticket_is_stored_with_its_contact_and_created_event(
    repo: TicketRepository, database: Database, session: ConversationSession
) -> None:
    ticket = create(
        repo,
        form(),
        session_id=session.session_id,
        escalation_reason=EscalationReason.CUSTOMER_REQUESTED,
        now=NOW,
    )

    assert re.match(REFERENCE_PATTERN, ticket.reference_number)
    assert ticket.reference_number.startswith("SUP-")
    assert ticket.session_id == session.session_id
    assert (ticket.status, ticket.priority) == (TicketStatus.OPEN, TicketPriority.NORMAL)
    assert ticket.escalation_reason is EscalationReason.CUSTOMER_REQUESTED
    assert ticket.description == "The lid was cracked. Can you replace it?"
    assert ticket.created_at == ticket.updated_at == NOW
    assert ticket.resolved_at is None and ticket.closed_at is None
    contact = repo.get_contact(ticket.contact_id)
    assert contact is not None
    assert (contact.name, contact.email, contact.consent_timestamp) == (
        "Alex Rivera",
        "alex@example.com",
        NOW,
    )
    [event] = repo.list_events(ticket.ticket_id)
    assert (event.event_type, event.previous_status, event.new_status, event.sequence) == (
        TicketEventType.CREATED,
        None,
        TicketStatus.OPEN,
        1,
    )
    assert repo.get_ticket(ticket.ticket_id) == ticket
    assert repo.get_ticket_by_reference(ticket.reference_number.lower()) == ticket


def test_sales_leads_get_their_own_reference_prefix(repo: TicketRepository) -> None:
    ticket = create(repo, form(ticket_type="sales"), now=NOW)

    assert ticket.ticket_type is TicketType.SALES
    assert ticket.reference_number.startswith("SAL-")
    assert ticket.session_id is None  # a contact form need not belong to a conversation


def test_ids_and_references_are_random(repo: TicketRepository) -> None:
    tickets = [create(repo, form(), now=NOW) for _ in range(20)]

    assert len({t.ticket_id for t in tickets}) == 20
    assert all(t.ticket_id.version == 4 for t in tickets)
    assert len({t.reference_number for t in tickets}) == 20
    references = {new_reference_number(TicketType.SUPPORT) for _ in range(500)}
    assert len(references) == 500
    codes = "".join(reference[4:] for reference in references)
    assert not set("ILOU") & set(codes.replace("-", ""))  # unambiguous characters only


def test_a_ticket_cannot_link_to_an_unknown_conversation(
    repo: TicketRepository, database: Database
) -> None:
    with pytest.raises(ConversationLinkError):
        create(repo, form(), session_id=uuid.uuid4(), now=NOW)

    assert count(database, "support_tickets") == count(database, "customer_contacts") == 0


def test_unknown_tickets_are_not_found(repo: TicketRepository) -> None:
    with pytest.raises(TicketNotFoundError):
        repo.get_ticket(uuid.uuid4())
    with pytest.raises(TicketNotFoundError):
        repo.get_ticket_by_reference("SUP-0000-0000")
    assert repo.get_contact(uuid.uuid4()) is None


# --- Contacts -------------------------------------------------------------------------


def test_the_same_details_reuse_one_contact(repo: TicketRepository, database: Database) -> None:
    first = create(repo, form(phone="+44 20 7946 0958"), now=NOW)
    later = NOW + timedelta(days=2)
    second = create(
        repo,
        form(name="  alex   RIVERA ", email="ALEX@example.com", phone="+44 (20) 7946-0958"),
        now=later,
    )

    assert first.contact_id == second.contact_id
    assert count(database, "customer_contacts") == 1
    contact = repo.get_contact(first.contact_id)
    assert contact is not None
    assert contact.name == "Alex Rivera"  # the stored details are not overwritten
    assert contact.consent_timestamp == contact.updated_at == later  # the latest consent


@pytest.mark.parametrize(
    "changes",
    [
        {"email": "someone.else@example.com"},
        {"name": "Sam Rivera"},
        {"phone": "+44 20 7946 0000"},
        {"phone": "+44 20 7946 0958", "contact_preference": "phone"},
    ],
)
def test_different_details_never_overwrite_an_existing_contact(
    repo: TicketRepository, database: Database, changes: dict[str, str]
) -> None:
    first = create(repo, form(phone="+44 20 7946 0958"), now=NOW)
    second = create(repo, form(**{"phone": "+44 20 7946 0958", **changes}), now=NOW)

    assert first.contact_id != second.contact_id
    contact = repo.get_contact(first.contact_id)
    assert contact is not None and contact.email == "alex@example.com"
    assert contact.phone == "+44 20 7946 0958"


def test_contact_identity_ignores_formatting_only() -> None:
    assert contact_identity(form(phone="+1 (555) 010-9999")) == contact_identity(
        form(phone="+1 555 010 9999")
    )
    assert contact_identity(form(phone="+1 555 010 9999")) != contact_identity(
        form(phone="1 555 010 9999")
    )


def test_contact_details_are_kept_apart_from_chat_messages(database: Database) -> None:
    message_columns = {
        c["name"] for c in inspect(database.engine).get_columns("conversation_messages")
    }
    ticket_columns = {c["name"] for c in inspect(database.engine).get_columns("support_tickets")}

    assert not message_columns & {"name", "email", "phone", "contact_id"}
    assert not ticket_columns & {"name", "email", "phone", "transcript", "messages"}
    assert "session_id" in ticket_columns  # linked by ID, not copied


# --- Status changes and events ----------------------------------------------------------

STAFF = {"actor_type": ActorType.STAFF, "actor_id": "maria"}


def move(repo: TicketRepository, ticket_id: uuid.UUID, status: TicketStatus, **extra: Any) -> Any:
    if status is TicketStatus.ASSIGNED:
        extra.setdefault("assigned_to", "maria")
    return repo.change_status(ticket_id, status, **{**STAFF, **extra})


def test_the_full_workflow_sets_timestamps_and_records_every_change(
    repo: TicketRepository,
) -> None:
    ticket = create(repo, form(), now=NOW)
    t = [NOW + timedelta(hours=h) for h in range(1, 8)]

    assigned = move(repo, ticket.ticket_id, TicketStatus.ASSIGNED, assigned_to="sam", now=t[0])
    working = move(repo, ticket.ticket_id, TicketStatus.IN_PROGRESS, now=t[1])
    waiting = move(repo, ticket.ticket_id, TicketStatus.WAITING_FOR_CUSTOMER, now=t[2])
    answered = repo.change_status(
        ticket.ticket_id, TicketStatus.IN_PROGRESS, actor_type=ActorType.CUSTOMER, now=t[3]
    )
    resolved = move(repo, ticket.ticket_id, TicketStatus.RESOLVED, now=t[4])
    reopened = move(repo, ticket.ticket_id, TicketStatus.IN_PROGRESS, now=t[5])
    move(repo, ticket.ticket_id, TicketStatus.RESOLVED, now=t[5])
    closed = move(repo, ticket.ticket_id, TicketStatus.CLOSED, now=t[6])

    assert (assigned.assigned_to, assigned.updated_at) == ("sam", t[0])
    assert working.assigned_to == "sam" and working.resolved_at is None
    assert waiting.status is TicketStatus.WAITING_FOR_CUSTOMER
    assert answered.status is TicketStatus.IN_PROGRESS
    assert resolved.resolved_at == t[4]
    assert reopened.resolved_at is None  # cleared on reopening; the events keep t[4]
    assert (closed.resolved_at, closed.closed_at, closed.created_at) == (t[5], t[6], NOW)
    events = repo.list_events(ticket.ticket_id)
    assert [e.sequence for e in events] == list(range(1, 10))
    assert [(e.previous_status, e.new_status) for e in events[1:]] == [
        (TicketStatus.OPEN, TicketStatus.ASSIGNED),
        (TicketStatus.ASSIGNED, TicketStatus.IN_PROGRESS),
        (TicketStatus.IN_PROGRESS, TicketStatus.WAITING_FOR_CUSTOMER),
        (TicketStatus.WAITING_FOR_CUSTOMER, TicketStatus.IN_PROGRESS),
        (TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED),
        (TicketStatus.RESOLVED, TicketStatus.IN_PROGRESS),
        (TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED),
        (TicketStatus.RESOLVED, TicketStatus.CLOSED),
    ]
    assert (events[0].actor_type, events[0].actor_id) == (ActorType.CUSTOMER, None)
    assert (events[1].actor_type, events[1].actor_id, events[1].assigned_to) == (
        ActorType.STAFF,
        "maria",
        "sam",
    )
    assert events[4].actor_type is ActorType.CUSTOMER
    assert [e.created_at for e in events] == sorted(e.created_at for e in events)


def test_unassigning_returns_the_ticket_to_open_without_an_assignee(
    repo: TicketRepository,
) -> None:
    ticket = create(repo, form(), now=NOW)
    move(repo, ticket.ticket_id, TicketStatus.ASSIGNED, now=NOW)

    unassigned = move(repo, ticket.ticket_id, TicketStatus.OPEN, now=NOW)

    assert (unassigned.status, unassigned.assigned_to) == (TicketStatus.OPEN, None)


def test_updated_at_never_moves_backwards(repo: TicketRepository) -> None:
    ticket = create(repo, form(), now=NOW)

    changed = move(repo, ticket.ticket_id, TicketStatus.CLOSED, now=NOW - timedelta(days=1))

    assert changed.updated_at == changed.closed_at == NOW


@pytest.mark.parametrize(
    ("path", "refused"),
    [
        ((), TicketStatus.OPEN),  # no change
        ((), TicketStatus.IN_PROGRESS),  # must be assigned first
        ((), TicketStatus.RESOLVED),
        ((TicketStatus.CLOSED,), TicketStatus.OPEN),  # closed is final
        ((TicketStatus.CLOSED,), TicketStatus.IN_PROGRESS),
        ((TicketStatus.ASSIGNED,), TicketStatus.RESOLVED),  # nobody worked on it
        (
            (TicketStatus.ASSIGNED, TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED),
            TicketStatus.OPEN,
        ),  # reopening goes back to in_progress only
        (
            (TicketStatus.ASSIGNED, TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED),
            TicketStatus.WAITING_FOR_CUSTOMER,
        ),
    ],  # fmt: skip
)
def test_invalid_status_changes_are_refused(
    repo: TicketRepository, path: tuple[TicketStatus, ...], refused: TicketStatus
) -> None:
    ticket = create(repo, form(), now=NOW)
    for status in path:
        move(repo, ticket.ticket_id, status, now=NOW)

    with pytest.raises(InvalidStatusTransitionError):
        move(repo, ticket.ticket_id, refused, now=NOW)
    assert len(repo.list_events(ticket.ticket_id)) == 1 + len(path)


def test_a_change_based_on_an_outdated_status_is_refused(repo: TicketRepository) -> None:
    ticket = create(repo, form(), now=NOW)
    move(repo, ticket.ticket_id, TicketStatus.CLOSED, now=NOW)

    with pytest.raises(StaleTicketStatusError):
        move(
            repo,
            ticket.ticket_id,
            TicketStatus.ASSIGNED,
            expected_status=TicketStatus.OPEN,
            now=NOW,
        )
    assert repo.get_ticket(ticket.ticket_id).status is TicketStatus.CLOSED


def test_assignment_needs_an_assignee_and_only_assignment_takes_one(
    repo: TicketRepository,
) -> None:
    ticket = create(repo, form(), now=NOW)

    with pytest.raises(ValueError, match="assigned_to"):
        repo.change_status(ticket.ticket_id, TicketStatus.ASSIGNED, **STAFF)
    with pytest.raises(ValueError, match="assigned_to"):
        repo.change_status(ticket.ticket_id, TicketStatus.CLOSED, assigned_to="sam", **STAFF)


def test_changing_an_unknown_ticket_is_not_found(repo: TicketRepository) -> None:
    with pytest.raises(TicketNotFoundError):
        move(repo, uuid.uuid4(), TicketStatus.CLOSED, now=NOW)


# --- Listing and conversation scope -----------------------------------------------------


def test_lists_are_paginated_oldest_first_with_a_stable_order(repo: TicketRepository) -> None:
    created = [
        create(repo, form(subject=f"Ticket {i}"), now=NOW + timedelta(minutes=i // 2))
        for i in range(7)
    ]  # pairs share a timestamp: the ID breaks the tie
    expected = sorted(created, key=lambda t: (t.created_at, t.ticket_id.hex))

    first = repo.list_tickets(offset=0, limit=3)
    second = repo.list_tickets(offset=3, limit=3)
    last = repo.list_tickets(offset=6, limit=3)

    assert [t.ticket_id for t in first.tickets + second.tickets + last.tickets] == [
        t.ticket_id for t in expected
    ]
    assert (first.total, first.has_more, last.has_more) == (7, True, False)
    assert repo.list_tickets(offset=0, limit=3) == first  # the same page every time


def test_lists_can_be_filtered(repo: TicketRepository, session: ConversationSession) -> None:
    support = create(repo, form(), session_id=session.session_id, now=NOW)
    sales = create(repo, form(ticket_type="sales"), now=NOW + timedelta(hours=1))
    urgent = create(repo, form(subject="Urgent"), priority=TicketPriority.URGENT, now=NOW)
    move(repo, urgent.ticket_id, TicketStatus.ASSIGNED, assigned_to="sam", now=NOW)

    def ids(**criteria: Any) -> set[uuid.UUID]:
        return {t.ticket_id for t in repo.list_tickets(TicketFilter(**criteria)).tickets}

    assert ids(ticket_type=TicketType.SALES) == {sales.ticket_id}
    assert ids(statuses=frozenset({TicketStatus.OPEN})) == {support.ticket_id, sales.ticket_id}
    assert ids(priority=TicketPriority.URGENT) == {urgent.ticket_id}
    assert ids(assigned_to="sam") == {urgent.ticket_id}
    assert ids(session_id=session.session_id) == {support.ticket_id}
    assert ids(created_from=NOW + timedelta(minutes=30)) == {sales.ticket_id}
    assert ids(created_before=NOW + timedelta(minutes=30)) == {
        support.ticket_id,
        urgent.ticket_id,
    }


@pytest.mark.parametrize(("offset", "limit"), [(-1, 10), (0, 0), (0, 101)])
def test_invalid_pages_are_refused(repo: TicketRepository, offset: int, limit: int) -> None:
    with pytest.raises(ValueError):
        repo.list_tickets(offset=offset, limit=limit)


def test_tickets_never_leak_between_conversations(
    repo: TicketRepository, conversations: ConversationRepository, session: ConversationSession
) -> None:
    other = conversations.create_session(ConversationSession.new(now=NOW), HASH)
    mine = create(repo, form(), session_id=session.session_id, now=NOW)
    theirs = create(repo, form(subject="Theirs"), session_id=other.session_id, now=NOW)

    page = repo.list_session_tickets(session.session_id)

    assert [t.ticket_id for t in page.tickets] == [mine.ticket_id]
    assert repo.get_session_ticket(session.session_id, mine.reference_number) == mine
    with pytest.raises(TicketNotFoundError):
        repo.get_session_ticket(session.session_id, theirs.reference_number)


def test_tickets_carry_no_contact_details(repo: TicketRepository) -> None:
    ticket = create(repo, form(phone="+44 20 7946 0958"), now=NOW)

    fields = set(ticket.model_dump())

    assert not fields & {"name", "email", "phone", "contact_preference"}
    assert "contact_id" in fields  # staff look the contact up separately
    assert set(ticket.customer_view().model_dump()) == {
        "ticket_id",
        "reference_number",
        "ticket_type",
        "lead_category",
        "status",
        "status_message",
        "subject",
        "created_at",
        "updated_at",
    }


# --- Idempotency and duplicates ---------------------------------------------------------


def test_a_retry_with_the_same_id_returns_the_original_ticket(
    repo: TicketRepository, database: Database, session: ConversationSession
) -> None:
    request = form()

    first = repo.create_ticket(request, session_id=session.session_id, now=NOW)
    retry = repo.create_ticket(
        request, session_id=session.session_id, now=NOW + timedelta(seconds=5)
    )

    assert first.outcome is TicketSubmissionOutcome.CREATED
    assert retry.outcome is TicketSubmissionOutcome.REPLAYED
    assert retry.ticket == first.ticket
    assert count(database, "support_tickets") == count(database, "ticket_events") == 1


def test_a_replay_returns_the_ticket_as_it_is_now(
    repo: TicketRepository, session: ConversationSession
) -> None:
    request = form()
    first = repo.create_ticket(request, session_id=session.session_id, now=NOW).ticket
    move(repo, first.ticket_id, TicketStatus.CLOSED, now=NOW)

    retry = repo.create_ticket(request, session_id=session.session_id, now=NOW)

    assert retry.outcome is TicketSubmissionOutcome.REPLAYED
    assert retry.ticket.status is TicketStatus.CLOSED


def test_a_retry_matches_despite_spacing_and_case(
    repo: TicketRepository, session: ConversationSession
) -> None:
    key = str(uuid.uuid4())
    repo.create_ticket(form(client_request_id=key), session_id=session.session_id, now=NOW)

    retry = repo.create_ticket(
        form(client_request_id=key.upper(), name="ALEX  rivera", message="The lid was "
             "cracked.   Can you replace it?"),
        session_id=session.session_id,
        now=NOW,
    )  # fmt: skip

    assert retry.outcome is TicketSubmissionOutcome.REPLAYED


@pytest.mark.parametrize(
    "changes",
    [
        {"subject": "Something else"},
        {"message": "A different problem"},
        {"email": "other@example.com"},
        {"ticket_type": "sales"},
        {"phone": "+44 20 7946 0958"},
    ],
)
def test_the_same_id_with_different_details_is_refused(
    repo: TicketRepository,
    database: Database,
    session: ConversationSession,
    changes: dict[str, str],
) -> None:
    key = str(uuid.uuid4())
    repo.create_ticket(form(client_request_id=key), session_id=session.session_id, now=NOW)

    with pytest.raises(IdempotencyKeyConflictError):
        repo.create_ticket(
            form(client_request_id=key, **changes), session_id=session.session_id, now=NOW
        )
    assert count(database, "support_tickets") == 1


def test_ids_are_scoped_to_their_conversation(
    repo: TicketRepository, conversations: ConversationRepository, session: ConversationSession
) -> None:
    other = conversations.create_session(ConversationSession.new(now=NOW), HASH)
    key = str(uuid.uuid4())
    mine = repo.create_ticket(form(client_request_id=key), session_id=session.session_id, now=NOW)

    # The same ID elsewhere neither returns this ticket nor conflicts with it.
    theirs = repo.create_ticket(
        form(client_request_id=key, subject="Theirs"), session_id=other.session_id, now=NOW
    )

    assert theirs.outcome is TicketSubmissionOutcome.CREATED
    assert theirs.ticket.ticket_id != mine.ticket.ticket_id


def test_the_same_request_with_a_new_id_returns_the_open_ticket(
    repo: TicketRepository, database: Database, session: ConversationSession
) -> None:
    first = repo.create_ticket(form(), session_id=session.session_id, now=NOW)

    again = repo.create_ticket(
        form(), session_id=session.session_id, now=NOW + timedelta(minutes=10)
    )

    assert again.outcome is TicketSubmissionOutcome.DUPLICATE
    assert again.ticket.ticket_id == first.ticket.ticket_id
    assert count(database, "support_tickets") == 1


@pytest.mark.parametrize(
    ("later", "closed", "window"),
    [
        (timedelta(hours=25), False, timedelta(hours=24)),  # outside the window
        (timedelta(minutes=5), True, timedelta(hours=24)),  # the first one is finished
        (timedelta(seconds=1), False, None),  # duplicate detection off
    ],
)
def test_the_same_request_creates_a_new_ticket_when_not_a_duplicate(
    repo: TicketRepository,
    session: ConversationSession,
    later: timedelta,
    closed: bool,
    window: timedelta | None,
) -> None:
    first = repo.create_ticket(form(), session_id=session.session_id, now=NOW).ticket
    if closed:
        move(repo, first.ticket_id, TicketStatus.CLOSED, now=NOW)

    again = repo.create_ticket(
        form(), session_id=session.session_id, duplicate_window=window, now=NOW + later
    )

    assert again.outcome is TicketSubmissionOutcome.CREATED


def test_without_a_conversation_every_submission_creates_a_ticket(
    repo: TicketRepository, database: Database
) -> None:
    request = form()

    outcomes = [repo.create_ticket(request, now=NOW).outcome for _ in range(2)]

    assert outcomes == [TicketSubmissionOutcome.CREATED] * 2
    assert count(database, "support_tickets") == 2


def test_the_key_is_unique_per_conversation_in_the_database(
    repo: TicketRepository, database: Database, session: ConversationSession
) -> None:
    ticket = repo.create_ticket(form(), session_id=session.session_id, now=NOW).ticket
    contact = ticket.contact_id
    with database.read() as db:
        key = db.execute(
            text("SELECT idempotency_key FROM support_tickets WHERE ticket_id = :id"),
            {"id": ticket.ticket_id.hex},
        ).scalar()
    values = _ticket_values(
        contact,
        reference_number="SUP-ZZZZ-ZZZZ",
        session_id=session.session_id.hex,
        idempotency_key=key,
    )

    with pytest.raises(IntegrityError), database.write() as db:
        db.execute(
            text(
                f"INSERT INTO support_tickets ({', '.join(values)}) "  # noqa: S608
                f"VALUES ({', '.join(':' + k for k in values)})"
            ),
            values,
        )


def test_concurrent_submissions_create_one_ticket(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.db"
    setup = Database(f"sqlite:///{path.as_posix()}")
    setup.init_schema()
    session = ConversationRepository(setup).create_session(ConversationSession.new(now=NOW), HASH)
    setup.dispose()
    request = form()
    barrier = threading.Barrier(8)
    outcomes: list[TicketSubmissionOutcome] = []
    errors: list[BaseException] = []

    def submit() -> None:
        # One Database (engine) per thread, like separate processes would have.
        database = Database(f"sqlite:///{path.as_posix()}", busy_timeout_seconds=30)
        try:
            barrier.wait()
            outcomes.append(
                TicketRepository(database)
                .create_ticket(request, session_id=session.session_id, now=NOW)
                .outcome
            )
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            database.dispose()

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(outcomes) == sorted(
        [TicketSubmissionOutcome.CREATED] + [TicketSubmissionOutcome.REPLAYED] * 7
    )
    check = Database(f"sqlite:///{path.as_posix()}")
    assert count(check, "support_tickets") == 1
    check.dispose()


# --- Foreign keys -----------------------------------------------------------------------


def test_deleting_a_conversation_keeps_its_ticket_and_clears_the_link(
    repo: TicketRepository, conversations: ConversationRepository, session: ConversationSession
) -> None:
    conversations.add_exchange(
        session.session_id, "Help", "Reply", Decision.NO_MATCH, received_at=NOW
    )
    ticket = create(repo, form(), session_id=session.session_id, now=NOW)

    conversations.delete_session(session.session_id)

    kept = repo.get_ticket(ticket.ticket_id)
    assert kept.session_id is None
    assert kept.description == ticket.description  # still actionable


def test_retention_cleanup_keeps_tickets(
    repo: TicketRepository, conversations: ConversationRepository, session: ConversationSession
) -> None:
    ticket = create(repo, form(), session_id=session.session_id, now=NOW)

    deleted = conversations.delete_expired_sessions(before=NOW + timedelta(days=1))

    assert deleted == 1
    assert repo.get_ticket(ticket.ticket_id).session_id is None


def test_a_contact_with_tickets_cannot_be_deleted(
    repo: TicketRepository, database: Database
) -> None:
    ticket = create(repo, form(), now=NOW)

    with pytest.raises(IntegrityError), database.write() as db:
        db.execute(
            text("DELETE FROM customer_contacts WHERE contact_id = :id"),
            {"id": ticket.contact_id.hex},
        )


def test_deleting_a_ticket_deletes_its_events(repo: TicketRepository, database: Database) -> None:
    ticket = create(repo, form(), now=NOW)

    with database.write() as db:
        db.execute(
            text("DELETE FROM support_tickets WHERE ticket_id = :id"), {"id": ticket.ticket_id.hex}
        )

    assert count(database, "ticket_events") == 0


# --- Constraints (rows written outside the app) ---------------------------------------


def _ticket_values(contact: uuid.UUID, **changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "ticket_id": uuid.uuid4().hex,
        "reference_number": "SUP-ABCD-1234",
        "session_id": None,
        "contact_id": contact.hex,
        "ticket_type": "support",
        "escalation_reason": None,
        "subject": "Subject",
        "description": "Description",
        "status": "open",
        "priority": "normal",
        "created_at": "2026-10-16 09:30:00",
        "updated_at": "2026-10-16 09:30:00",
        "resolved_at": None,
        "closed_at": None,
        "assigned_to": None,
        "idempotency_key": None,
        "request_fingerprint": None,
    }
    values.update(changes)
    return values


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "pending"},
        {"ticket_type": "complaint"},
        {"priority": "critical"},
        {"escalation_reason": "because"},
        {"reference_number": "SAL-ABCD-1234"},  # prefix does not match the type
        {"reference_number": "SUP-1"},
        {"subject": ""},
        {"description": ""},
        {"description": "x" * 2001},
        {"status": "closed"},  # without closed_at
        {"status": "resolved"},  # without resolved_at
        {"resolved_at": "2026-10-16 10:00:00"},  # an open ticket was never resolved
        {"closed_at": "2026-10-16 10:00:00"},
        {"updated_at": "2026-10-16 09:00:00"},  # before created_at
        {"contact_id": uuid.uuid4().hex},  # no such contact
        {"session_id": uuid.uuid4().hex},  # no such conversation
        {"status": "assigned"},  # without assigned_to
        {"status": "waiting"},
        {"request_fingerprint": "abc"},
        {"assigned_to": "x" * 65},
    ],
    ids=lambda changes: ",".join(f"{k}={str(v)[:12]}" for k, v in changes.items()),
)
def test_ticket_constraints(
    repo: TicketRepository, database: Database, changes: dict[str, Any]
) -> None:
    contact_id = create(repo, form(), now=NOW).contact_id
    columns = ", ".join(_ticket_values(contact_id))
    params = ", ".join(f":{name}" for name in _ticket_values(contact_id))

    with pytest.raises(IntegrityError), database.write() as db:
        db.execute(
            text(f"INSERT INTO support_tickets ({columns}) VALUES ({params})"),  # noqa: S608
            _ticket_values(contact_id, **changes),
        )


def test_a_valid_raw_ticket_row_is_accepted(repo: TicketRepository, database: Database) -> None:
    # The baseline for test_ticket_constraints: each change there is the only problem.
    contact_id = create(repo, form(), now=NOW).contact_id
    values = _ticket_values(contact_id)
    columns, params = ", ".join(values), ", ".join(f":{name}" for name in values)

    with database.write() as db:
        db.execute(text(f"INSERT INTO support_tickets ({columns}) VALUES ({params})"), values)  # noqa: S608


@pytest.mark.parametrize(
    ("consent", "preference", "phone"),
    [(0, "email", None), (1, "phone", None), (1, "fax", None)],
)
def test_contact_constraints(
    database: Database, consent: int, preference: str, phone: str | None
) -> None:
    with pytest.raises(IntegrityError), database.write() as db:
        db.execute(
            text(
                "INSERT INTO customer_contacts (contact_id, name, email, phone, "
                "contact_preference, consent_given, consent_timestamp, created_at, "
                "updated_at, identity_hash) VALUES (:id, 'A', 'a@example.com', :phone, :pref, "
                ":consent, '2026-10-16 09:30:00', '2026-10-16 09:30:00', "
                "'2026-10-16 09:30:00', :hash)"
            ),
            {
                "id": uuid.uuid4().hex,
                "phone": phone,
                "pref": preference,
                "consent": consent,
                "hash": "1" * 64,
            },
        )


_EVENT = ("status_changed", "open", "assigned", "staff", "maria", "sam")  # a valid row


@pytest.mark.parametrize(
    ("event_type", "previous", "new", "actor", "actor_id", "assigned_to"),
    [
        _EVENT,
        ("created", None, "open", "customer", None, None),
        ("status_changed", "waiting_for_customer", "in_progress", "customer", None, None),
    ],
)
def test_valid_raw_event_rows_are_accepted(
    repo: TicketRepository,
    database: Database,
    event_type: str,
    previous: str | None,
    new: str,
    actor: str,
    actor_id: str | None,
    assigned_to: str | None,
) -> None:
    # The baseline for test_event_constraints.
    ticket = create(repo, form(), now=NOW)

    with database.write() as db:
        db.execute(
            text(
                "INSERT INTO ticket_events (event_id, ticket_id, sequence, event_type, "
                "previous_status, new_status, actor_type, actor_id, assigned_to, created_at) "
                "VALUES (:id, :ticket, 2, :type, :previous, :new, :actor, :actor_id, "
                ":assigned_to, '2026-10-16 09:30:00')"
            ),
            {
                "id": uuid.uuid4().hex,
                "ticket": ticket.ticket_id.hex,
                "type": event_type,
                "previous": previous,
                "new": new,
                "actor": actor,
                "actor_id": actor_id,
                "assigned_to": assigned_to,
            },
        )


@pytest.mark.parametrize(
    ("event_type", "previous", "new", "actor", "actor_id", "assigned_to"),
    [
        ("created", "open", "open", "customer", None, None),  # created has no previous
        ("created", None, None, "customer", None, None),
        ("status_changed", None, "closed", "staff", "maria", None),
        ("status_changed", "open", "open", "staff", "maria", None),  # not a change
        ("note", None, "open", "staff", "maria", None),
        ("status_changed", "open", "done", "staff", "maria", None),
        ("status_changed", "open", "closed", "staff", None, None),  # staff without an ID
        ("status_changed", "open", "closed", "customer", "maria", None),
        ("status_changed", "open", "closed", "robot", None, None),
        ("status_changed", "open", "closed", "staff", "maria", "sam"),  # not an assignment
    ],
)
def test_event_constraints(
    repo: TicketRepository,
    database: Database,
    event_type: str,
    previous: str | None,
    new: str | None,
    actor: str,
    actor_id: str | None,
    assigned_to: str | None,
) -> None:
    ticket = create(repo, form(), now=NOW)

    with pytest.raises(IntegrityError), database.write() as db:
        db.execute(
            text(
                "INSERT INTO ticket_events (event_id, ticket_id, sequence, event_type, "
                "previous_status, new_status, actor_type, actor_id, assigned_to, created_at) "
                "VALUES (:id, :ticket, 2, :type, :previous, :new, :actor, :actor_id, "
                ":assigned_to, '2026-10-16 09:30:00')"
            ),
            {
                "id": uuid.uuid4().hex,
                "ticket": ticket.ticket_id.hex,
                "type": event_type,
                "previous": previous,
                "new": new,
                "actor": actor,
                "actor_id": actor_id,
                "assigned_to": assigned_to,
            },
        )


def test_naive_timestamps_are_refused(repo: TicketRepository) -> None:
    with pytest.raises(TicketStoreError):
        create(repo, form(), now=datetime(2026, 10, 16, 9, 30))  # noqa: DTZ001


def test_timestamps_come_back_as_aware_utc(repo: TicketRepository) -> None:
    from datetime import timezone

    local = datetime(2026, 10, 16, 11, 30, tzinfo=timezone(timedelta(hours=2)))
    ticket = repo.get_ticket(create(repo, form(), now=local).ticket_id)

    assert ticket.created_at == NOW and ticket.created_at.tzinfo is UTC


def test_common_ticket_queries_are_indexed(database: Database) -> None:
    indexes = {i["name"]: i for i in inspect(database.engine).get_indexes("support_tickets")}

    assert indexes["ix_ticket_status_created"]["column_names"] == ["status", "created_at"]
    assert indexes["ix_ticket_type_status_created"]["column_names"] == [
        "ticket_type",
        "status",
        "created_at",
    ]
    assert indexes["ix_ticket_session_created"]["column_names"] == ["session_id", "created_at"]
    assert indexes["uq_ticket_idempotency_key"]["unique"]
    assert {"ix_ticket_contact", "ix_ticket_session_fingerprint"} <= set(indexes)
    contact_indexes = {i["name"] for i in inspect(database.engine).get_indexes("customer_contacts")}
    assert "ix_contact_email" in contact_indexes


# --- Upgrading an existing database -----------------------------------------------------


def test_a_stage_9_database_gains_support_tables_and_keeps_its_conversations(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stage9.db"
    old = Database(f"sqlite:///{path.as_posix()}")
    session_id, message_id = uuid.uuid4(), uuid.uuid4()
    with old.write() as db:  # the Stage 9 schema: no escalation column, no support tables
        db.execute(
            text(
                "CREATE TABLE conversation_sessions (session_id CHAR(32) PRIMARY KEY, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
                "expires_at DATETIME, status VARCHAR(16) NOT NULL, "
                "access_token_hash VARCHAR(64) NOT NULL, processing_key VARCHAR(64), "
                "processing_until DATETIME)"
            )
        )
        db.execute(
            text(
                "CREATE TABLE conversation_messages (message_id CHAR(32) PRIMARY KEY, "
                "session_id CHAR(32) NOT NULL REFERENCES conversation_sessions(session_id) "
                "ON DELETE CASCADE, sequence INTEGER NOT NULL, role VARCHAR(16) NOT NULL, "
                "content TEXT NOT NULL, created_at DATETIME NOT NULL, decision VARCHAR(32), "
                "source_faq_ids JSON, idempotency_key VARCHAR(64), "
                "request_fingerprint VARCHAR(64), generation_method VARCHAR(32))"
            )
        )
        db.execute(
            text(
                "INSERT INTO conversation_sessions VALUES (:sid, '2026-10-16 09:00:00', "
                "'2026-10-16 09:00:00', '2099-01-01 00:00:00', 'active', :hash, NULL, NULL)"
            ),
            {"sid": session_id.hex, "hash": HASH},
        )
        db.execute(
            text(
                "INSERT INTO conversation_messages VALUES (:mid, :sid, 1, 'user', "
                "'Where is my order?', '2026-10-16 09:00:00', NULL, '[]', NULL, NULL, NULL)"
            ),
            {"mid": message_id.hex, "sid": session_id.hex},
        )
    old.dispose()

    upgraded = Database(f"sqlite:///{path.as_posix()}")
    upgraded.init_schema()
    upgraded.init_schema()  # repeatable
    upgraded.check()

    tables = set(inspect(upgraded.engine).get_table_names())
    assert {"customer_contacts", "support_tickets", "ticket_events"} <= tables
    columns = {c["name"] for c in inspect(upgraded.engine).get_columns("conversation_messages")}
    assert "escalation_reason" in columns
    [message] = ConversationRepository(upgraded).list_messages(session_id)
    assert message.content == "Where is my order?" and message.escalation_reason is None
    ticket = create(TicketRepository(upgraded), form(), session_id=session_id, now=NOW)
    assert ticket.session_id == session_id
    upgraded.dispose()


# The support tables as first released (before the assigned / waiting_for_customer
# statuses, idempotency columns and event actors).
_FIRST_SUPPORT_SCHEMA = (
    "CREATE TABLE customer_contacts (contact_id CHAR(32) NOT NULL PRIMARY KEY, "
    "name VARCHAR(100) NOT NULL, email VARCHAR(254) NOT NULL, phone VARCHAR(32), "
    "contact_preference VARCHAR(16) NOT NULL, consent_given BOOLEAN NOT NULL, "
    "consent_timestamp DATETIME NOT NULL, created_at DATETIME NOT NULL, "
    "updated_at DATETIME NOT NULL, identity_hash VARCHAR(64) NOT NULL UNIQUE, "
    "CHECK (consent_given = 1))",
    "CREATE TABLE support_tickets (ticket_id CHAR(32) NOT NULL PRIMARY KEY, "
    "reference_number VARCHAR(13) NOT NULL UNIQUE, session_id CHAR(32) "
    "REFERENCES conversation_sessions (session_id) ON DELETE SET NULL, "
    "contact_id CHAR(32) NOT NULL REFERENCES customer_contacts (contact_id) "
    "ON DELETE RESTRICT, ticket_type VARCHAR(16) NOT NULL, escalation_reason VARCHAR(32), "
    "subject VARCHAR(150) NOT NULL, description TEXT NOT NULL, status VARCHAR(16) NOT NULL, "
    "priority VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL, "
    "updated_at DATETIME NOT NULL, resolved_at DATETIME, closed_at DATETIME, "
    "CONSTRAINT ck_ticket_status CHECK (status IN ('open', 'in_progress', 'resolved', "
    "'closed')))",
    "CREATE INDEX ix_ticket_session ON support_tickets (session_id)",
    "CREATE TABLE ticket_events (event_id CHAR(32) NOT NULL PRIMARY KEY, "
    "ticket_id CHAR(32) NOT NULL REFERENCES support_tickets (ticket_id) ON DELETE CASCADE, "
    "sequence INTEGER NOT NULL, event_type VARCHAR(32) NOT NULL, "
    "previous_status VARCHAR(16), new_status VARCHAR(16), created_at DATETIME NOT NULL, "
    "UNIQUE (ticket_id, sequence))",
)


def _first_release_database(
    path: Path, *, orphan_event: bool = False, invalid_event: bool = False
) -> uuid.UUID:
    """A database with conversations and support tables as first released, with data.
    Returns the ticket ID."""
    database = Database(f"sqlite:///{path.as_posix()}")
    database.init_schema()  # current conversation tables ...
    session = ConversationRepository(database).create_session(
        ConversationSession.new(now=NOW), HASH
    )
    ConversationRepository(database).add_exchange(
        session.session_id, "Help", "Reply", Decision.NO_MATCH, received_at=NOW
    )
    ticket_id, contact_id = uuid.uuid4(), uuid.uuid4()
    stamp = "2026-10-16 09:30:00"
    with database.engine.connect() as connection:  # ... and the old support tables
        raw = connection.connection.driver_connection  # pragmas outside a transaction
        raw.execute("PRAGMA foreign_keys = OFF")
        with connection.begin():
            for table in ("ticket_events", "support_tickets", "customer_contacts"):
                connection.exec_driver_sql(f"DROP TABLE {table}")
            for statement in _FIRST_SUPPORT_SCHEMA:
                connection.exec_driver_sql(statement)
            connection.exec_driver_sql(
                "INSERT INTO customer_contacts VALUES (?, 'Alex', 'alex@example.com', NULL, "
                "'email', 1, ?, ?, ?, ?)",
                (contact_id.hex, stamp, stamp, stamp, "a" * 64),
            )
            connection.exec_driver_sql(
                "INSERT INTO support_tickets VALUES (?, 'SUP-ABCD-1234', ?, ?, 'support', "
                "NULL, 'Subject', 'Description', 'in_progress', 'normal', ?, ?, NULL, NULL)",
                (ticket_id.hex, session.session_id.hex, contact_id.hex, stamp, stamp),
            )
            events = [(uuid.uuid4().hex, ticket_id.hex, 1, "created", None, "open")]
            events.append((uuid.uuid4().hex, ticket_id.hex, 2, "status_changed", "open",
                           "in_progress"))  # fmt: skip
            if orphan_event:  # a reference to a ticket that does not exist
                events.append((uuid.uuid4().hex, uuid.uuid4().hex, 1, "created", None, "open"))
            if invalid_event:  # allowed then, refused by the current rules
                events.append((uuid.uuid4().hex, ticket_id.hex, 3, "created", "open", "open"))
            for event in events:
                connection.exec_driver_sql(
                    "INSERT INTO ticket_events VALUES (?, ?, ?, ?, ?, ?, ?)", (*event, stamp)
                )
        raw.execute("PRAGMA foreign_keys = ON")
    database.dispose()
    return ticket_id


def test_first_release_support_tables_are_rebuilt_keeping_every_row(tmp_path: Path) -> None:
    path = tmp_path / "first.db"
    ticket_id = _first_release_database(path)

    upgraded = Database(f"sqlite:///{path.as_posix()}")
    upgraded.init_schema()
    upgraded.init_schema()  # repeatable: nothing left to rebuild
    upgraded.check()
    repo = TicketRepository(upgraded)

    ticket = repo.get_ticket(ticket_id)
    assert (ticket.reference_number, ticket.status) == ("SUP-ABCD-1234", TicketStatus.IN_PROGRESS)
    events = repo.list_events(ticket_id)
    assert [e.new_status for e in events] == [TicketStatus.OPEN, TicketStatus.IN_PROGRESS]
    assert {e.actor_type for e in events} == {ActorType.SYSTEM}  # unknown before actors
    # The new rules apply: the new statuses work, and conversations are untouched.
    assert move(repo, ticket_id, TicketStatus.WAITING_FOR_CUSTOMER, now=NOW).status is (
        TicketStatus.WAITING_FOR_CUSTOMER
    )
    assert count(upgraded, "conversation_messages") == 2
    assert count(upgraded, "customer_contacts") == 1
    with upgraded.read() as db:
        tables = [r[0] for r in db.execute(text("SELECT name FROM sqlite_master"))]
    assert not [name for name in tables if name.endswith("__old")]
    upgraded.dispose()


def test_a_broken_reference_from_before_does_not_block_the_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "orphan.db"
    _first_release_database(path, orphan_event=True)

    upgraded = Database(f"sqlite:///{path.as_posix()}")
    upgraded.init_schema()

    assert count(upgraded, "ticket_events") == 3  # copied as it was, not made worse
    upgraded.dispose()


def test_a_rebuild_that_would_break_data_changes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "broken.db"
    _first_release_database(path, invalid_event=True)

    upgraded = Database(f"sqlite:///{path.as_posix()}")
    with pytest.raises(DatabaseError, match="ticket_events"):
        upgraded.init_schema()

    # Rolled back: the old table and all its rows are still there, under the old name.
    with upgraded.read() as db:
        columns = {r[1] for r in db.execute(text("PRAGMA table_info(ticket_events)"))}
        rows = db.execute(text("SELECT count(*) FROM ticket_events")).scalar()
        tables = {r[0] for r in db.execute(text("SELECT name FROM sqlite_master"))}
    assert "actor_type" not in columns and rows == 3
    assert "ticket_events__old" not in tables
    assert count(upgraded, "conversation_messages") == 2
    upgraded.dispose()


def test_database_files_are_not_tracked_by_git() -> None:
    ignored = (Path(__file__).parents[1] / ".gitignore").read_text().splitlines()

    assert {"*.db", "*.sqlite", "*.sqlite3"} <= set(ignored)
