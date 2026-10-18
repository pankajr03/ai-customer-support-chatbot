"""Stage 12 (Task 18): failure and invariant paths of high-risk code that the suite
did not reach (found with pytest-cov).

- The audit log cannot be written or read: the staff request still behaves correctly,
  and the failure is a clean 503 without details.
- Ticket storage is locked, refuses a write, or holds invalid data: typed errors, safe
  503 responses for customers, never a traceback or a value.
- Support ticket records reject impossible states (data integrity).
- Start-up problems stop the server with a readable message instead of a traceback.
"""

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, OperationalError

from app.models.support import SupportTicket, TicketStatus
from app.repositories.ticket_repository import (
    TicketStoreBusyError,
    TicketStoreError,
    _store_errors,
)
from tests.test_admin_api import (  # noqa: F401 - fixtures
    ALEX,
    TICKETS,
    AdminApp,
    bearer,
    make_admin_app,
)
from tests.test_admin_tickets_analytics import create_ticket


@pytest.fixture
def api(make_admin_app: Callable[..., AdminApp]) -> AdminApp:  # noqa: F811
    return make_admin_app("failures", rate_limit_enabled=False)


def failing(error: Exception) -> Callable[..., Any]:
    @contextmanager
    def broken(*args: Any, **kwargs: Any) -> Iterator[Any]:
        raise error
        yield  # pragma: no cover

    return broken


DISK = OperationalError("SELECT 1", {}, Exception("disk I/O error"))
LOCKED = OperationalError("UPDATE x", {}, Exception("database is locked"))


# --- Audit log failures ------------------------------------------------------------------


def test_a_failed_audit_write_is_logged_and_does_not_break_the_request(
    api: AdminApp, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ticket, _ = create_ticket(api)
    audit = api.app.state.audit_repository
    monkeypatch.setattr(audit._db, "write", failing(DISK))

    # A refused request still gets its 401; a contact view still returns the ticket.
    refused = api.client.get("/api/v1/admin/faqs", headers=bearer("stf_" + "x" * 43))
    assert refused.status_code == 401
    shown = api.get(f"{TICKETS}/{ticket['ticket_id']}")
    assert shown.status_code == 200 and shown.json()["contact"] is not None
    assert "Audit event auth.access_denied not recorded (OperationalError)" in caplog.text
    assert "Audit event ticket.contact_viewed not recorded" in caplog.text
    assert "disk I/O error" not in caplog.text


def test_an_unreadable_audit_log_is_a_clean_503(
    api: AdminApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api.app.state.audit_repository._db, "read", failing(DISK))
    response = api.get("/api/v1/admin/audit")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert "disk" not in response.text and "OperationalError" not in response.text


# --- Ticket storage failures ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (LOCKED, TicketStoreBusyError),
        (DISK, TicketStoreError),
        (IntegrityError("INSERT x", {}, Exception("CHECK constraint failed")), TicketStoreError),
    ],
)
def test_store_errors_become_typed_errors_without_their_cause_text(
    error: Exception, expected: type[Exception], caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(expected) as raised, _store_errors("test_operation"):
        raise error
    message = str(raised.value)
    assert "test_operation" in message  # which operation failed, for the logs
    for cause in ("database is locked", "disk I/O", "CHECK constraint"):
        assert cause not in message and cause not in caplog.text  # never the cause text


def test_invalid_stored_ticket_data_is_a_store_error_without_values() -> None:
    try:
        SupportTicket.model_validate({"status": "nonsense"})
    except ValidationError as exc:
        invalid = exc
    with pytest.raises(TicketStoreError, match="ticket data is invalid"), _store_errors("read"):
        raise invalid


def test_customers_get_a_clean_503_when_ticket_storage_fails(
    api: AdminApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket, token = create_ticket(api)
    monkeypatch.setattr(api.app.state.ticket_repository._db, "read", failing(DISK))
    response = api.client.get(
        f"{TICKETS}/{ticket['ticket_id']}",
        params={"session_id": ticket["session_id"]},
        headers=bearer(token),
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert "disk" not in response.text


def test_staff_get_a_clean_503_when_ticket_storage_is_locked(
    api: AdminApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api.app.state.ticket_repository._db, "read", failing(LOCKED))
    response = api.get(TICKETS)
    assert response.status_code == 503 and "locked" not in response.text


# --- Ticket record invariants (data integrity) -------------------------------------------


@pytest.fixture
def stored(api: AdminApp) -> SupportTicket:
    ticket, _ = create_ticket(api)
    return api.app.state.ticket_repository.get_ticket(uuid.UUID(ticket["ticket_id"]))


@pytest.mark.parametrize(
    ("changes", "rule"),
    [
        ({"anonymized_at": "2026-01-02T00:00:00Z"}, "only closed tickets are anonymized"),
        ({"reference_number": "SAL-AAAA-BBBB"}, "prefix does not match"),
        ({"updated_at": "2000-01-01T00:00:00Z"}, "updated_at must not be before"),
        ({"status": "resolved"}, "a resolved ticket needs resolved_at"),
        ({"resolved_at": "2026-01-02T00:00:00Z"}, "only resolved or closed"),
        ({"status": "closed"}, "closed_at is set exactly when"),
        ({"status": "assigned"}, "an assigned ticket needs assigned_to"),
        ({"lead_category": "bulk_order"}, "lead_category is set exactly for sales"),
    ],
)
def test_impossible_ticket_states_are_rejected(
    stored: SupportTicket, changes: dict[str, Any], rule: str
) -> None:
    data = {**stored.model_dump(), **changes}
    with pytest.raises(ValidationError, match=rule):
        SupportTicket.model_validate(data)
    assert stored.status is TicketStatus.OPEN  # the stored record itself is valid


# --- Start-up failures ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "says"),
    [
        ("content", "contain text addressed to the assistant"),
        ("database", "the FAQ storage could not be opened (DatabaseError)"),
    ],
)
def test_start_up_failures_stop_with_a_readable_message(
    monkeypatch: pytest.MonkeyPatch, error: str, says: str
) -> None:
    import app.main as main
    from app.database.connection import DatabaseError
    from app.repositories.faq_repository import FAQContentError

    def broken() -> Any:
        raise FAQContentError(["faq_007"]) if error == "content" else DatabaseError("x")

    monkeypatch.setattr(main, "create_app", broken)
    with pytest.raises(SystemExit) as stopped:
        main._create_default_app()
    assert says in str(stopped.value) and "Traceback" not in str(stopped.value)


def test_an_unknown_module_attribute_is_an_attribute_error() -> None:
    import app.main as main

    with pytest.raises(AttributeError):
        _ = main.not_an_attribute  # type: ignore[attr-defined]
