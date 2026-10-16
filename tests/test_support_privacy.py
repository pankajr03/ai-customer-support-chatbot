"""Privacy, retention and integration boundaries of support tickets (Stage 10, Tasks 14,
15 and 17): anonymization, what deleting or expiring a conversation does to its
tickets, logs and error bodies without personal data, request-size limits, transaction
rollback, and the integration event sink."""

import dataclasses
import logging
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.staff_auth import StaffAuthenticator, generate_staff_token, hash_staff_token
from app.database.connection import Database
from app.models.conversation import ConversationSession
from app.models.support import (
    ANONYMIZED_EMAIL,
    ANONYMIZED_TEXT,
    ActorType,
    TicketEventType,
    TicketStatus,
    TicketSubmissionOutcome,
)
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.ticket_repository import (
    TicketRepository,
    TicketStillActiveError,
    TicketStoreError,
)
from app.services.conversation_service import ConversationService
from app.services.escalation_service import EscalationService
from app.services.ticket_integrations import (
    NullTicketEventSink,
    RecordingTicketEventSink,
    TicketEventKind,
    TicketIntegrationEvent,
    publish_safely,
)
from app.services.ticket_workflow import TicketWorkflow
from scripts import anonymize_support_data
from tests import test_api_workflows as workflows
from tests.test_api_workflows import App
from tests.test_support_api import BASE, STAFF_HASHES, body, staff
from tests.ticket_test_data import form

make_app = workflows.make_app

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


def close(repo: TicketRepository, ticket_id: uuid.UUID, when: datetime = NOW) -> None:
    repo.change_status(
        ticket_id, TicketStatus.CLOSED, actor_type=ActorType.STAFF, actor_id="maria", now=when
    )


def count(database: Database, sql: str) -> int:
    with database.read() as db:
        return int(db.execute(text(sql)).scalar() or 0)


# --- Anonymization --------------------------------------------------------------------


def test_anonymizing_a_closed_ticket_removes_its_personal_data(
    repo: TicketRepository, database: Database
) -> None:
    session = ConversationRepository(database).create_session(
        ConversationSession.new(now=NOW), HASH
    )
    ticket = repo.create_ticket(
        form(phone="+44 20 7946 0958"), session_id=session.session_id, now=NOW
    ).ticket
    close(repo, ticket.ticket_id)

    done = repo.anonymize_ticket(ticket.ticket_id, now=NOW + timedelta(days=1))

    assert (done.subject, done.description) == (ANONYMIZED_TEXT, ANONYMIZED_TEXT)
    assert done.session_id is None and done.anonymized_at == NOW + timedelta(days=1)
    assert (done.reference_number, done.status) == (ticket.reference_number, TicketStatus.CLOSED)
    contact = repo.get_contact(ticket.contact_id)
    assert contact is not None
    assert (contact.name, contact.email, contact.phone) == (ANONYMIZED_TEXT, ANONYMIZED_EMAIL, None)
    assert contact.consent_timestamp == NOW  # the record that consent was given stays
    events = repo.list_events(ticket.ticket_id)
    assert events[-1].event_type is TicketEventType.ANONYMIZED
    assert [e.event_type for e in events[:-1]] == [
        TicketEventType.CREATED,
        TicketEventType.STATUS_CHANGED,
    ]  # the audit trail is kept
    with database.read() as db:
        row = db.execute(
            text("SELECT request_fingerprint, idempotency_key FROM support_tickets")
        ).one()
    assert tuple(row) == (None, None)


def test_active_tickets_are_never_anonymized(repo: TicketRepository) -> None:
    ticket = repo.create_ticket(form(), now=NOW).ticket

    with pytest.raises(TicketStillActiveError):
        repo.anonymize_ticket(ticket.ticket_id, now=NOW)

    assert repo.get_ticket(ticket.ticket_id).subject == "Order arrived damaged"


def test_anonymizing_twice_changes_nothing(repo: TicketRepository) -> None:
    ticket = repo.create_ticket(form(), now=NOW).ticket
    close(repo, ticket.ticket_id)
    first = repo.anonymize_ticket(ticket.ticket_id, now=NOW)

    again = repo.anonymize_ticket(ticket.ticket_id, now=NOW + timedelta(days=5))

    assert again == first
    assert len(repo.list_events(ticket.ticket_id)) == 3


def test_a_contact_is_kept_while_another_ticket_still_needs_it(repo: TicketRepository) -> None:
    first = repo.create_ticket(form(subject="First"), now=NOW).ticket
    second = repo.create_ticket(form(subject="Second"), now=NOW).ticket
    assert first.contact_id == second.contact_id
    close(repo, first.ticket_id)

    repo.anonymize_ticket(first.ticket_id, now=NOW)

    contact = repo.get_contact(first.contact_id)
    assert contact is not None and contact.email == "alex@example.com"  # still needed
    close(repo, second.ticket_id)
    repo.anonymize_ticket(second.ticket_id, now=NOW)
    contact = repo.get_contact(first.contact_id)
    assert contact is not None and contact.anonymized_at is not None


def test_an_anonymized_contact_is_never_reused(repo: TicketRepository) -> None:
    old = repo.create_ticket(form(), now=NOW).ticket
    close(repo, old.ticket_id)
    repo.anonymize_ticket(old.ticket_id, now=NOW)

    new = repo.create_ticket(form(), now=NOW + timedelta(days=1)).ticket

    assert new.contact_id != old.contact_id
    contact = repo.get_contact(new.contact_id)
    assert contact is not None and contact.email == "alex@example.com"


def test_retention_anonymizes_only_tickets_closed_long_enough_ago(
    repo: TicketRepository,
) -> None:
    old = repo.create_ticket(form(subject="Old"), now=NOW - timedelta(days=400)).ticket
    recent = repo.create_ticket(form(subject="Recent"), now=NOW - timedelta(days=10)).ticket
    still_open = repo.create_ticket(form(subject="Open"), now=NOW - timedelta(days=500)).ticket
    close(repo, old.ticket_id, NOW - timedelta(days=380))
    close(repo, recent.ticket_id, NOW - timedelta(days=5))
    cutoff = NOW - timedelta(days=365)

    assert repo.anonymize_closed_before(cutoff, now=NOW, dry_run=True) == 1
    assert repo.get_ticket(old.ticket_id).anonymized_at is None  # dry run
    assert repo.anonymize_closed_before(cutoff, now=NOW) == 1

    assert repo.get_ticket(old.ticket_id).anonymized_at is not None
    assert repo.get_ticket(recent.ticket_id).anonymized_at is None
    assert repo.get_ticket(still_open.ticket_id).anonymized_at is None
    assert repo.anonymize_closed_before(cutoff, now=NOW) == 0  # nothing left


def test_audit_events_hold_no_personal_data(database: Database) -> None:
    with database.read() as db:
        columns = {r[1] for r in db.execute(text("PRAGMA table_info(ticket_events)"))}

    assert columns == {
        "event_id",
        "ticket_id",
        "sequence",
        "event_type",
        "previous_status",
        "new_status",
        "actor_type",
        "actor_id",  # a staff ID, never a customer
        "assigned_to",  # a staff ID
        "created_at",
    }


def test_the_database_refuses_anonymizing_an_open_ticket(
    repo: TicketRepository, database: Database
) -> None:
    ticket = repo.create_ticket(form(), now=NOW).ticket

    with pytest.raises(Exception, match="CHECK constraint"), database.write() as db:
        db.execute(
            text(
                "UPDATE support_tickets SET anonymized_at = '2026-10-17 00:00:00' "
                "WHERE ticket_id = :id"
            ),  # fmt: skip
            {"id": ticket.ticket_id.hex},
        )


# --- The anonymization script -----------------------------------------------------------


@pytest.fixture
def file_db(tmp_path: Path) -> tuple[str, TicketRepository, Database]:
    url = f"sqlite:///{(tmp_path / 'support.db').as_posix()}"
    database = Database(url)
    database.init_schema()
    return url, TicketRepository(database), database


def test_the_script_handles_an_erasure_request_by_reference(
    file_db: tuple[str, TicketRepository, Database], capsys: pytest.CaptureFixture[str]
) -> None:
    url, repo, database = file_db
    ticket = repo.create_ticket(form(), now=NOW).ticket

    refused = anonymize_support_data.main(
        ["--reference", ticket.reference_number, "--database-url", url]
    )
    close(repo, ticket.ticket_id)
    done = anonymize_support_data.main(
        ["--reference", ticket.reference_number.lower(), "--database-url", url]
    )

    out = capsys.readouterr()
    assert (refused, done) == (1, 0)
    assert "close it first" in out.err and f"Anonymized {ticket.reference_number}" in out.out
    assert "alex" not in (out.out + out.err).lower()  # counts and references only
    assert repo.get_ticket(ticket.ticket_id).anonymized_at is not None
    database.dispose()


def test_the_script_applies_retention(
    file_db: tuple[str, TicketRepository, Database], capsys: pytest.CaptureFixture[str]
) -> None:
    url, repo, database = file_db
    ticket = repo.create_ticket(form(), now=datetime.now(UTC) - timedelta(days=40)).ticket
    close(repo, ticket.ticket_id, datetime.now(UTC) - timedelta(days=35))

    dry = anonymize_support_data.main(
        ["--retention", "--older-than-days", "30", "--dry-run", "--database-url", url]
    )
    real = anonymize_support_data.main(
        ["--retention", "--older-than-days", "30", "--database-url", url]
    )

    out = capsys.readouterr().out
    assert (dry, real) == (0, 0)
    assert "Would anonymize 1" in out and "Anonymized 1" in out
    database.dispose()


def test_the_script_reports_unknown_references(
    file_db: tuple[str, TicketRepository, Database], capsys: pytest.CaptureFixture[str]
) -> None:
    url, _, database = file_db

    assert anonymize_support_data.main(["--reference", "SUP-0000-0000", "--database-url", url])
    assert "No ticket" in capsys.readouterr().err
    database.dispose()


# --- Conversation deletion and expiry ---------------------------------------------------


def test_deleting_a_conversation_keeps_its_open_ticket_for_staff(
    make_app: Callable[..., App],
) -> None:
    api = make_app(staff_token_hashes=STAFF_HASHES)
    sid, headers = api.start()
    created = api.client.post(BASE, json=body(sid), headers=headers).json()

    deleted = api.client.delete(f"/api/v1/conversations/{sid}", headers=headers)

    assert deleted.status_code == 204
    detail = api.client.get(f"{BASE}/{created['ticket_id']}", headers=staff()).json()
    assert detail["status"] == "open" and detail["has_conversation"] is False
    assert detail["description"] == "I need help with a refund request."  # still actionable
    # The deleted conversation's token reaches nothing any more.
    gone = api.client.get(f"{BASE}/{created['ticket_id']}", params={"session_id": sid},
                          headers=headers)  # fmt: skip
    assert gone.status_code == 404


def test_expired_conversations_are_cleaned_up_without_touching_open_tickets(
    database: Database, repo: TicketRepository
) -> None:
    conversations = ConversationService(ConversationRepository(database), timedelta(hours=1))
    created = conversations.create_conversation(now=NOW)
    ticket = repo.create_ticket(form(), session_id=created.session.session_id, now=NOW).ticket

    removed = conversations.cleanup_expired(now=NOW + timedelta(days=2))

    assert removed == 1
    kept = repo.get_ticket(ticket.ticket_id)
    assert (kept.status, kept.session_id, kept.subject) == (
        TicketStatus.OPEN,
        None,
        "Order arrived damaged",
    )


# --- Logs, error bodies and request size ------------------------------------------------


def test_no_contact_details_or_text_reach_the_logs(
    make_app: Callable[..., App], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    api = make_app(staff_token_hashes=STAFF_HASHES)
    sid, headers = api.start()
    payload = body(sid, subject="Private subject line", description="Very private details")
    payload["contact"] = {**payload["contact"], "name": "Zelda Quinn",
                          "email": "zelda@example.com", "phone": "+44 20 7946 0958"}  # fmt: skip

    created = api.client.post(BASE, json=payload, headers=headers).json()
    api.client.get(f"{BASE}/{created['ticket_id']}", headers=staff())
    api.client.post(BASE, json={**payload, "subject": "Changed"}, headers=headers)  # conflict

    for secret in ("Zelda", "zelda@example.com", "7946", "Private subject", "Very private",
                   headers["Authorization"].removeprefix("Bearer ")):  # fmt: skip
        assert secret not in caplog.text, secret


@pytest.mark.parametrize(
    "contact",
    [
        {"email": "zelda-at-example.com"},
        {"phone": "call +44 7946 0958 now"},
        {"name": "Zelda\nQuinn"},
    ],
)
def test_error_bodies_never_echo_contact_details(
    make_app: Callable[..., App], contact: dict[str, Any]
) -> None:
    api = make_app()
    sid, headers = api.start()
    payload = body(sid)
    payload["contact"] = {**payload["contact"], **contact}

    response = api.client.post(BASE, json=payload, headers=headers)

    assert response.status_code == 422
    for value in contact.values():
        assert value not in response.text
        assert "zelda" not in response.text.lower()


def test_oversized_requests_are_refused_before_parsing(make_app: Callable[..., App]) -> None:
    api = make_app()  # MAX_REQUEST_BODY_BYTES: 32,768 by default
    sid, headers = api.start()

    response = api.client.post(BASE, json=body(sid, description="x" * 40_000), headers=headers)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


# --- Transactions -------------------------------------------------------------------------


def test_a_failure_mid_transaction_stores_nothing(
    repo: TicketRepository, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise OperationalError("INSERT", {}, Exception("disk I/O error"))

    monkeypatch.setattr(TicketRepository, "_append_event", staticmethod(broken))

    with pytest.raises(TicketStoreError):
        repo.create_ticket(form(), now=NOW)

    assert count(database, "SELECT count(*) FROM support_tickets") == 0
    assert count(database, "SELECT count(*) FROM customer_contacts") == 0


# --- Integration boundary (Task 17) -------------------------------------------------------


@dataclasses.dataclass
class Services:
    service: EscalationService
    sink: RecordingTicketEventSink
    session_id: uuid.UUID
    token: str


@pytest.fixture
def services(database: Database) -> Services:
    messages = ConversationRepository(database)
    conversations = ConversationService(messages, timedelta(hours=1))
    created = conversations.create_conversation(now=NOW)
    sink = RecordingTicketEventSink()
    service = EscalationService(conversations, messages, TicketRepository(database), events=sink)
    return Services(service, sink, created.session.session_id, created.access_token)


def test_a_new_ticket_is_announced_once_after_it_is_stored(services: Services) -> None:
    request = form()

    first = services.service.submit(services.session_id, services.token, request, now=NOW)
    services.service.submit(services.session_id, services.token, request, now=NOW)  # replay

    [event] = services.sink.events
    assert event.kind is TicketEventKind.TICKET_CREATED
    assert event.ticket_id == first.ticket.ticket_id
    assert first.response.outcome is TicketSubmissionOutcome.CREATED


def test_events_carry_no_personal_data() -> None:
    fields = {f.name for f in dataclasses.fields(TicketIntegrationEvent)}

    assert not fields & {"name", "email", "phone", "subject", "description", "contact_id"}


def test_a_failing_integration_never_affects_the_ticket(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    class Broken:
        delivers_notifications = True

        def publish(self, event: TicketIntegrationEvent) -> None:
            raise ConnectionError("mail server down")

    messages = ConversationRepository(database)
    conversations = ConversationService(messages, timedelta(hours=1))
    created = conversations.create_conversation(now=NOW)
    service = EscalationService(conversations, messages, TicketRepository(database),
                                events=Broken())  # fmt: skip

    submission = service.submit(created.session.session_id, created.access_token, form(), now=NOW)

    assert submission.response.outcome is TicketSubmissionOutcome.CREATED
    assert "ConnectionError" in caplog.text and "mail server down" not in caplog.text


def test_the_default_sink_notifies_nobody_and_nothing_claims_otherwise(
    services: Services,
) -> None:
    assert NullTicketEventSink.delivers_notifications is False
    publish_safely(NullTicketEventSink(), _event())  # accepted, and goes nowhere

    message = services.service.submit(
        services.session_id, services.token, form(), now=NOW
    ).response.message.lower()
    assert "notif" not in message and "sent to" not in message


def test_status_changes_are_announced(database: Database) -> None:
    token = generate_staff_token()
    sink = RecordingTicketEventSink()
    tickets = TicketRepository(database)
    workflow = TicketWorkflow(
        tickets,
        StaffAuthenticator([f"maria:{hash_staff_token(token)}"]),
        ConversationService(ConversationRepository(database), timedelta(hours=1)),
        events=sink,
    )
    ticket = tickets.create_ticket(form(), now=NOW).ticket

    workflow.change_status_as_staff(token, ticket.ticket_id, TicketStatus.CLOSED, now=NOW)

    [event] = sink.events
    assert (event.kind, event.previous_status, event.status) == (
        TicketEventKind.STATUS_CHANGED,
        TicketStatus.OPEN,
        TicketStatus.CLOSED,
    )


def _event() -> TicketIntegrationEvent:
    return TicketIntegrationEvent(
        kind=TicketEventKind.TICKET_CREATED,
        ticket_id=uuid.uuid4(),
        reference_number="SUP-ABCD-1234",
        ticket_type="support",
        lead_category=None,
        status=TicketStatus.OPEN,
        previous_status=None,
        occurred_at=NOW,
    )
