"""Support ticket storage (Stage 10, Task 3): schema, constraints, contacts, events and
upgrading an existing database without losing conversations."""

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.database.connection import Database
from app.models.conversation import ConversationSession
from app.models.escalation import EscalationReason
from app.models.support import (
    REFERENCE_PATTERN,
    SupportRequestCreate,
    TicketEventType,
    TicketPriority,
    TicketStatus,
    TicketType,
)
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.support_repository import (
    ConversationLinkError,
    InvalidStatusTransitionError,
    SupportRepository,
    SupportStoreError,
    TicketNotFoundError,
    contact_identity,
    new_reference_number,
)
from app.services.answer_decision_service import Decision

NOW = datetime(2026, 10, 16, 9, 30, tzinfo=UTC)
HASH = "0" * 64


@pytest.fixture
def database() -> Iterator[Database]:
    db = Database("sqlite://")
    db.init_schema()
    yield db
    db.dispose()


@pytest.fixture
def repo(database: Database) -> SupportRepository:
    return SupportRepository(database)


@pytest.fixture
def conversations(database: Database) -> ConversationRepository:
    return ConversationRepository(database)


@pytest.fixture
def session(conversations: ConversationRepository) -> ConversationSession:
    return conversations.create_session(
        ConversationSession.new(ttl=timedelta(minutes=30), now=NOW), HASH
    )


def form(**changes: Any) -> SupportRequestCreate:
    body: dict[str, Any] = {
        "ticket_type": "support",
        "name": "Alex Rivera",
        "email": "alex@example.com",
        "subject": "Order arrived damaged",
        "message": "The lid was cracked. Can you replace it?",
        "consent_given": True,
    }
    return SupportRequestCreate.model_validate({**body, **changes})


def count(database: Database, table: str) -> int:
    with database.read() as db:
        return int(db.execute(text(f"SELECT count(*) FROM {table}")).scalar() or 0)  # noqa: S608


# --- Creating tickets -----------------------------------------------------------------


def test_a_ticket_is_stored_with_its_contact_and_created_event(
    repo: SupportRepository, database: Database, session: ConversationSession
) -> None:
    ticket = repo.create_ticket(
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


def test_sales_leads_get_their_own_reference_prefix(repo: SupportRepository) -> None:
    ticket = repo.create_ticket(form(ticket_type="sales"), now=NOW)

    assert ticket.ticket_type is TicketType.SALES
    assert ticket.reference_number.startswith("SAL-")
    assert ticket.session_id is None  # a contact form need not belong to a conversation


def test_ids_and_references_are_random(repo: SupportRepository) -> None:
    tickets = [repo.create_ticket(form(), now=NOW) for _ in range(20)]

    assert len({t.ticket_id for t in tickets}) == 20
    assert all(t.ticket_id.version == 4 for t in tickets)
    assert len({t.reference_number for t in tickets}) == 20
    references = {new_reference_number(TicketType.SUPPORT) for _ in range(500)}
    assert len(references) == 500
    codes = "".join(reference[4:] for reference in references)
    assert not set("ILOU") & set(codes.replace("-", ""))  # unambiguous characters only


def test_a_ticket_cannot_link_to_an_unknown_conversation(
    repo: SupportRepository, database: Database
) -> None:
    with pytest.raises(ConversationLinkError):
        repo.create_ticket(form(), session_id=uuid.uuid4(), now=NOW)

    assert count(database, "support_tickets") == count(database, "customer_contacts") == 0


def test_unknown_tickets_are_not_found(repo: SupportRepository) -> None:
    with pytest.raises(TicketNotFoundError):
        repo.get_ticket(uuid.uuid4())
    with pytest.raises(TicketNotFoundError):
        repo.get_ticket_by_reference("SUP-0000-0000")
    assert repo.get_contact(uuid.uuid4()) is None


# --- Contacts -------------------------------------------------------------------------


def test_the_same_details_reuse_one_contact(repo: SupportRepository, database: Database) -> None:
    first = repo.create_ticket(form(phone="+44 20 7946 0958"), now=NOW)
    later = NOW + timedelta(days=2)
    second = repo.create_ticket(
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
    repo: SupportRepository, database: Database, changes: dict[str, str]
) -> None:
    first = repo.create_ticket(form(phone="+44 20 7946 0958"), now=NOW)
    second = repo.create_ticket(form(**{"phone": "+44 20 7946 0958", **changes}), now=NOW)

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


def test_status_changes_set_timestamps_and_append_events(repo: SupportRepository) -> None:
    ticket = repo.create_ticket(form(), now=NOW)
    t1, t2, t3, t4 = (NOW + timedelta(hours=h) for h in (1, 2, 3, 4))

    working = repo.change_status(ticket.ticket_id, TicketStatus.IN_PROGRESS, now=t1)
    resolved = repo.change_status(ticket.ticket_id, TicketStatus.RESOLVED, now=t2)
    reopened = repo.change_status(ticket.ticket_id, TicketStatus.OPEN, now=t3)
    repo.change_status(ticket.ticket_id, TicketStatus.RESOLVED, now=t3)
    closed = repo.change_status(ticket.ticket_id, TicketStatus.CLOSED, now=t4)

    assert (working.updated_at, working.resolved_at) == (t1, None)
    assert resolved.resolved_at == t2
    assert reopened.resolved_at is None  # reopening clears it
    assert (closed.status, closed.resolved_at, closed.closed_at) == (TicketStatus.CLOSED, t3, t4)
    events = repo.list_events(ticket.ticket_id)
    assert [e.sequence for e in events] == [1, 2, 3, 4, 5, 6]
    assert [(e.previous_status, e.new_status) for e in events[1:]] == [
        (TicketStatus.OPEN, TicketStatus.IN_PROGRESS),
        (TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED),
        (TicketStatus.RESOLVED, TicketStatus.OPEN),
        (TicketStatus.OPEN, TicketStatus.RESOLVED),
        (TicketStatus.RESOLVED, TicketStatus.CLOSED),
    ]


@pytest.mark.parametrize(
    ("path", "refused"),
    [
        ((), TicketStatus.OPEN),  # no change
        ((TicketStatus.CLOSED,), TicketStatus.OPEN),  # closed is final
        ((TicketStatus.CLOSED,), TicketStatus.RESOLVED),
        ((TicketStatus.RESOLVED,), TicketStatus.IN_PROGRESS),
    ],
)
def test_invalid_status_changes_are_refused(
    repo: SupportRepository, path: tuple[TicketStatus, ...], refused: TicketStatus
) -> None:
    ticket = repo.create_ticket(form(), now=NOW)
    for status in path:
        repo.change_status(ticket.ticket_id, status, now=NOW)

    with pytest.raises(InvalidStatusTransitionError):
        repo.change_status(ticket.ticket_id, refused, now=NOW)
    assert len(repo.list_events(ticket.ticket_id)) == 1 + len(path)


def test_changing_an_unknown_ticket_is_not_found(repo: SupportRepository) -> None:
    with pytest.raises(TicketNotFoundError):
        repo.change_status(uuid.uuid4(), TicketStatus.CLOSED, now=NOW)


# --- Foreign keys -----------------------------------------------------------------------


def test_deleting_a_conversation_keeps_its_ticket_and_clears_the_link(
    repo: SupportRepository, conversations: ConversationRepository, session: ConversationSession
) -> None:
    conversations.add_exchange(
        session.session_id, "Help", "Reply", Decision.NO_MATCH, received_at=NOW
    )
    ticket = repo.create_ticket(form(), session_id=session.session_id, now=NOW)

    conversations.delete_session(session.session_id)

    kept = repo.get_ticket(ticket.ticket_id)
    assert kept.session_id is None
    assert kept.description == ticket.description  # still actionable


def test_retention_cleanup_keeps_tickets(
    repo: SupportRepository, conversations: ConversationRepository, session: ConversationSession
) -> None:
    ticket = repo.create_ticket(form(), session_id=session.session_id, now=NOW)

    deleted = conversations.delete_expired_sessions(before=NOW + timedelta(days=1))

    assert deleted == 1
    assert repo.get_ticket(ticket.ticket_id).session_id is None


def test_a_contact_with_tickets_cannot_be_deleted(
    repo: SupportRepository, database: Database
) -> None:
    ticket = repo.create_ticket(form(), now=NOW)

    with pytest.raises(IntegrityError), database.write() as db:
        db.execute(
            text("DELETE FROM customer_contacts WHERE contact_id = :id"),
            {"id": ticket.contact_id.hex},
        )


def test_deleting_a_ticket_deletes_its_events(repo: SupportRepository, database: Database) -> None:
    ticket = repo.create_ticket(form(), now=NOW)

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
    ],
    ids=lambda changes: ",".join(f"{k}={str(v)[:12]}" for k, v in changes.items()),
)
def test_ticket_constraints(
    repo: SupportRepository, database: Database, changes: dict[str, Any]
) -> None:
    contact_id = repo.create_ticket(form(), now=NOW).contact_id
    columns = ", ".join(_ticket_values(contact_id))
    params = ", ".join(f":{name}" for name in _ticket_values(contact_id))

    with pytest.raises(IntegrityError), database.write() as db:
        db.execute(
            text(f"INSERT INTO support_tickets ({columns}) VALUES ({params})"),  # noqa: S608
            _ticket_values(contact_id, **changes),
        )


def test_a_valid_raw_ticket_row_is_accepted(repo: SupportRepository, database: Database) -> None:
    # The baseline for test_ticket_constraints: each change there is the only problem.
    contact_id = repo.create_ticket(form(), now=NOW).contact_id
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


@pytest.mark.parametrize(
    ("event_type", "previous", "new"),
    [
        ("created", "open", "open"),  # a created event has no previous status
        ("created", None, None),
        ("status_changed", None, "closed"),
        ("status_changed", "open", "open"),  # not a change
        ("note", None, "open"),
        ("status_changed", "open", "done"),
    ],
)
def test_event_constraints(
    repo: SupportRepository,
    database: Database,
    event_type: str,
    previous: str | None,
    new: str | None,
) -> None:
    ticket = repo.create_ticket(form(), now=NOW)

    with pytest.raises(IntegrityError), database.write() as db:
        db.execute(
            text(
                "INSERT INTO ticket_events (event_id, ticket_id, sequence, event_type, "
                "previous_status, new_status, created_at) VALUES (:id, :ticket, 2, :type, "
                ":previous, :new, '2026-10-16 09:30:00')"
            ),
            {
                "id": uuid.uuid4().hex,
                "ticket": ticket.ticket_id.hex,
                "type": event_type,
                "previous": previous,
                "new": new,
            },
        )


def test_naive_timestamps_are_refused(repo: SupportRepository) -> None:
    with pytest.raises(SupportStoreError):
        repo.create_ticket(form(), now=datetime(2026, 10, 16, 9, 30))  # noqa: DTZ001


def test_timestamps_come_back_as_aware_utc(repo: SupportRepository) -> None:
    from datetime import timezone

    local = datetime(2026, 10, 16, 11, 30, tzinfo=timezone(timedelta(hours=2)))
    ticket = repo.get_ticket(repo.create_ticket(form(), now=local).ticket_id)

    assert ticket.created_at == NOW and ticket.created_at.tzinfo is UTC


def test_common_ticket_queries_are_indexed(database: Database) -> None:
    indexes = {i["name"]: i for i in inspect(database.engine).get_indexes("support_tickets")}

    assert indexes["ix_ticket_status_created"]["column_names"] == ["status", "created_at"]
    assert indexes["ix_ticket_type_status_created"]["column_names"] == [
        "ticket_type",
        "status",
        "created_at",
    ]
    assert {"ix_ticket_contact", "ix_ticket_session"} <= set(indexes)
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
    ticket = SupportRepository(upgraded).create_ticket(form(), session_id=session_id, now=NOW)
    assert ticket.session_id == session_id
    upgraded.dispose()


def test_database_files_are_not_tracked_by_git() -> None:
    ignored = (Path(__file__).parents[1] / ".gitignore").read_text().splitlines()

    assert {"*.db", "*.sqlite", "*.sqlite3"} <= set(ignored)
