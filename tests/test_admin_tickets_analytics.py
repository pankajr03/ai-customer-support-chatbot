"""Ticket management, assignment, overview and chatbot analytics (Stage 11, Tasks 12-15),
over HTTP against the real app (see tests/test_admin_api.py for the app fixture)."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from app.core.staff_auth import StaffMember, StaffRole
from app.services.analytics_service import MIN_SAMPLE, rate
from tests.test_admin_api import (  # noqa: F401 - fixtures
    ADMIN,
    ALEX,
    LEGACY,
    MARIA,
    AdminApp,
    admin,
    bearer,
    error_code,
    make_admin_app,
)

TICKETS = "/api/v1/support/tickets"
ANALYTICS = f"{ADMIN}/analytics"
EMAIL = "customer@example.com"


def create_ticket(api: AdminApp, ticket_type: str = "support") -> tuple[dict[str, Any], str]:
    """A customer's ticket; returns it and the customer's conversation token."""
    created = api.client.post("/api/v1/conversations").json()
    body: dict[str, Any] = {
        "session_id": created["session_id"],
        "client_request_id": str(uuid.uuid4()),
        "ticket_type": ticket_type,
        "subject": "Refund assistance",
        "description": "I need help with a refund request.",
        "contact": {
            "name": "Example Customer",
            "email": EMAIL,
            "phone": None,
            "contact_preference": "email",
            "consent_given": True,
        },
        "confirmed": True,
    }
    if ticket_type == "sales":
        body["lead_category"] = "bulk_order"
    response = api.client.post(TICKETS, json=body, headers=bearer(created["access_token"]))
    assert response.status_code == 201, response.text
    return {**response.json(), "session_id": created["session_id"]}, created["access_token"]


def assign(api: AdminApp, ticket_id: str, to: str | None, expected: str | None, token: str = ALEX):
    return api.client.put(
        f"{TICKETS}/{ticket_id}/assignment",
        json={"assigned_to": to, "expected_assigned_to": expected},
        headers=bearer(token),
    )


def events(api: AdminApp, ticket_id: str) -> list[dict[str, Any]]:
    return list(api.get(f"{TICKETS}/{ticket_id}/events").json()["events"])


def set_status(api: AdminApp, ticket_id: str, status: str) -> None:
    response = api.client.patch(
        f"{TICKETS}/{ticket_id}/status", json={"status": status}, headers=bearer(ALEX)
    )
    assert response.status_code == 200, response.text


# --- Ticket search --------------------------------------------------------------------------


def test_tickets_are_found_by_reference(admin: AdminApp) -> None:  # noqa: F811
    ticket, _ = create_ticket(admin)
    create_ticket(admin, "sales")

    found = admin.get(TICKETS, params={"reference": ticket["reference_number"].lower()}).json()

    assert [t["ticket_id"] for t in found["tickets"]] == [ticket["ticket_id"]]
    assert admin.get(TICKETS, params={"reference": "SUP-0000-0000"}).json()["total"] == 0
    assert admin.get(TICKETS, params={"ticket_type": "sales"}).json()["total"] == 1


# --- Assignment (Task 13) -------------------------------------------------------------------


def test_the_staff_directory_lists_valid_assignees_only(admin: AdminApp) -> None:  # noqa: F811
    staff = admin.get(f"{ADMIN}/staff", token=MARIA).json()["staff"]
    assert staff == [
        {"staff_id": "alex", "role": "admin"},
        {"staff_id": "maria", "role": "support"},
        {"staff_id": "sam", "role": "support"},
    ]
    assert error_code(admin.client.get(f"{ADMIN}/staff"), 401) == "STAFF_AUTH_REQUIRED"


def test_assigning_reassigning_and_unassigning_are_recorded(admin: AdminApp) -> None:  # noqa: F811
    ticket, _ = create_ticket(admin)
    tid = ticket["ticket_id"]

    first = assign(admin, tid, "maria", None, token=MARIA)  # support staff may assign
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "assigned" and first.json()["assigned_to"] == "maria"
    assert first.json()["assigned_at"] is not None

    second = assign(admin, tid, "sam", "maria").json()
    assert (second["status"], second["assigned_to"]) == ("assigned", "sam")

    set_status(admin, tid, "in_progress")
    third = assign(admin, tid, None, "sam").json()
    assert (third["status"], third["assigned_to"], third["assigned_at"]) == (
        "in_progress",
        None,
        None,
    )

    history = events(admin, tid)
    assert [(e["event_type"], e["previous_assigned_to"], e["assigned_to"]) for e in history] == [
        ("created", None, None),
        ("status_changed", None, "maria"),
        ("assignment_changed", "maria", "sam"),
        ("status_changed", None, None),
        ("assignment_changed", "sam", None),
    ]
    assert [e["actor_id"] for e in history[1:]] == ["maria", "alex", "alex", "alex"]


def test_unassigning_an_assigned_ticket_reopens_it(admin: AdminApp) -> None:  # noqa: F811
    ticket, _ = create_ticket(admin)
    assign(admin, ticket["ticket_id"], "maria", None)
    reopened = assign(admin, ticket["ticket_id"], None, "maria").json()
    assert (reopened["status"], reopened["assigned_to"]) == ("open", None)


def test_the_same_assignment_again_records_nothing(admin: AdminApp) -> None:  # noqa: F811
    ticket, _ = create_ticket(admin)
    assign(admin, ticket["ticket_id"], "maria", None)
    count = len(events(admin, ticket["ticket_id"]))
    again = assign(admin, ticket["ticket_id"], "maria", "maria")
    assert again.status_code == 200 and len(events(admin, ticket["ticket_id"])) == count


def test_assignment_refusals(admin: AdminApp) -> None:  # noqa: F811
    ticket, customer = create_ticket(admin)
    tid = ticket["ticket_id"]
    assert error_code(assign(admin, tid, "nobody-here", None), 422) == "VALIDATION_ERROR"
    assign(admin, tid, "maria", None)
    stale = assign(admin, tid, "sam", None)  # was read as unassigned, is maria's now
    assert error_code(stale, 409) == "TICKET_CHANGED"
    assert error_code(assign(admin, tid, "sam", "maria", token=customer), 401) == (
        "STAFF_AUTH_REQUIRED"
    )
    set_status(admin, tid, "in_progress")
    set_status(admin, tid, "resolved")
    assert error_code(assign(admin, tid, "sam", "maria"), 409) == "INVALID_ASSIGNMENT"
    assert admin.get(f"{TICKETS}/{tid}").json()["assigned_to"] == "maria"


def test_assignment_needs_configured_staff(make_admin_app: Callable[..., AdminApp]) -> None:  # noqa: F811
    api = make_admin_app("nostaff", staff_token_hashes=[])
    response = assign(api, str(uuid.uuid4()), "maria", None)
    assert error_code(response, 403) == "STAFF_ACCESS_DISABLED"


# --- Internal notes (Task 12) ---------------------------------------------------------------


def add_note(api: AdminApp, tid: str, body: str, note_id: str, token: str = MARIA) -> Any:
    return api.client.post(
        f"{TICKETS}/{tid}/notes",
        json={"body": body, "client_note_id": note_id},
        headers=bearer(token),
    )


def test_notes_are_internal_and_retries_add_them_once(admin: AdminApp) -> None:  # noqa: F811
    ticket, customer = create_ticket(admin)
    tid, note_id = ticket["ticket_id"], str(uuid.uuid4())

    first = add_note(admin, tid, "  Called back, no answer.  ", note_id)
    retry = add_note(admin, tid, "Called back, no answer.", note_id)

    assert (first.status_code, retry.status_code) == (201, 200)
    assert retry.headers["Idempotent-Replayed"] == "true"
    notes = admin.get(f"{TICKETS}/{tid}/notes").json()["notes"]
    assert [(n["author_id"], n["body"]) for n in notes] == [("maria", "Called back, no answer.")]
    conflict = add_note(admin, tid, "Something else.", note_id)
    assert error_code(conflict, 422) == "TICKET_REQUEST_CONFLICT"
    assert error_code(add_note(admin, tid, "   ", str(uuid.uuid4())), 422) == "VALIDATION_ERROR"
    customer_view = admin.client.get(
        f"{TICKETS}/{tid}",
        params={"session_id": ticket["session_id"]},
        headers=bearer(customer),
    ).json()
    assert "Called back" not in str(customer_view)
    assert error_code(admin.client.get(f"{TICKETS}/{tid}/notes", headers=bearer(customer)), 401)


def test_anonymizing_removes_note_text_and_blocks_new_notes(admin: AdminApp) -> None:  # noqa: F811
    ticket, _ = create_ticket(admin)
    tid = ticket["ticket_id"]
    add_note(admin, tid, "Customer said their address is 1 Main St.", str(uuid.uuid4()))
    set_status(admin, tid, "closed")
    admin.app.state.ticket_repository.anonymize_ticket(uuid.UUID(tid))

    notes = admin.get(f"{TICKETS}/{tid}/notes").json()["notes"]
    assert [n["body"] for n in notes] == ["[removed]"]
    blocked = add_note(admin, tid, "Another note.", str(uuid.uuid4()))
    assert error_code(blocked, 409) == "TICKET_ANONYMIZED"


# --- Overview (Task 14) ---------------------------------------------------------------------


def test_the_overview_counts_conversations_tickets_and_leads(admin: AdminApp) -> None:  # noqa: F811
    first, _ = create_ticket(admin)
    create_ticket(admin, "sales")
    resolved, _ = create_ticket(admin)
    assign(admin, resolved["ticket_id"], "maria", None)
    set_status(admin, resolved["ticket_id"], "in_progress")
    set_status(admin, resolved["ticket_id"], "resolved")
    set_status(admin, first["ticket_id"], "closed")

    overview = admin.get(f"{ADMIN}/overview").json()

    tickets = overview["tickets"]
    assert (tickets["total"], tickets["open"], tickets["resolved"], tickets["closed"]) == (
        3,
        1,
        1,
        1,
    )
    assert tickets["leads"] == 1
    assert overview["conversations"] == {"stored": 3, "started_last_7_days": 3}
    assert EMAIL not in str(overview)


# --- Analytics (Task 15) --------------------------------------------------------------------


def test_analytics_on_an_empty_database(admin: AdminApp) -> None:  # noqa: F811
    data = admin.get(ANALYTICS, params={"days": 7}).json()
    assert data["chatbot_responses"] == 0 and data["customer_messages"] == 0
    assert len(data["daily"]) == 7
    assert all(r["percent"] is None and r["denominator"] == 0 for r in data["rates"])
    assert data["latency"]["samples"] == 0 and data["latency"]["median_ms"] is None
    assert any("not that the customer's issue was resolved" in x for x in data["limitations"])


def test_analytics_count_recorded_decisions_offers_and_times(admin: AdminApp) -> None:  # noqa: F811
    admin.chat("I want to track my parcel")  # answered
    admin.chat("zzz qqq")  # no match
    admin.chat("I want to speak with a human")  # offered human support
    create_ticket(admin)

    data = admin.get(ANALYTICS, params={"days": 1}).json()

    assert data["customer_messages"] == 3 and data["chatbot_responses"] == 3
    assert data["decisions"]["answered"] >= 1
    assert sum(data["decisions"].values()) == 3
    assert data["escalation_offers"] >= 1
    assert data["tickets"] == {"support": 1, "sales": 0, "after_chat_offer": 0}
    # 3 responses: too few for any rate, but the counts are given.
    answered = next(r for r in data["rates"] if r["name"] == "Answered response rate")
    assert answered["percent"] is None and answered["denominator"] == 3
    assert "fewer than" in answered["note"]
    latency = data["latency"]
    assert latency["samples"] == 3
    assert 0 <= latency["median_ms"] <= latency["p90_ms"] <= latency["max_ms"]
    assert data["daily"][-1]["chatbot_responses"] == 3
    assert data["daily"][-1]["tickets_created"] == 1


def test_analytics_respect_the_window(admin: AdminApp) -> None:  # noqa: F811
    admin.chat("I want to track my parcel")
    service = admin.app.state.analytics_service
    later = datetime.now(UTC) + timedelta(days=40)
    data = service.chat_analytics(StaffMember("alex", StaffRole.ADMIN), days=7, now=later)
    assert data.chatbot_responses == 0 and data.stored_conversations == 1


def test_analytics_are_for_admins_and_hold_no_personal_data(admin: AdminApp) -> None:  # noqa: F811
    create_ticket(admin)
    admin.chat("I want to track my parcel")
    assert error_code(admin.get(ANALYTICS, token=MARIA), 403) == "STAFF_FORBIDDEN"
    assert error_code(admin.get(ANALYTICS, token=LEGACY), 403) == "STAFF_FORBIDDEN"
    for days in (0, 366):
        assert error_code(admin.get(ANALYTICS, params={"days": days}), 422) == "VALIDATION_ERROR"
    body = admin.get(ANALYTICS).text
    assert EMAIL not in body and "track my parcel" not in body


def test_latency_comes_from_the_stored_timestamps(admin: AdminApp) -> None:  # noqa: F811
    admin.chat("I want to track my parcel")
    with admin.database.write() as db:  # make the stored reply exactly 2.5 s later
        db.execute(
            text(
                "UPDATE conversation_messages SET created_at = strftime('%Y-%m-%d %H:%M:%f', "
                "(SELECT created_at FROM conversation_messages WHERE role = 'user'), "
                "'+2.5 seconds') "
                "WHERE role = 'assistant'"
            )
        )
    latency = admin.get(ANALYTICS, params={"days": 1}).json()["latency"]
    assert latency["samples"] == 1
    assert latency["median_ms"] == pytest.approx(2500, abs=1)


@pytest.mark.parametrize(
    ("numerator", "denominator", "percent"),
    [
        (0, 0, None),
        (5, MIN_SAMPLE - 1, None),
        (10, MIN_SAMPLE, 33.3),
        (MIN_SAMPLE, MIN_SAMPLE, 100.0),
    ],
)
def test_rates_need_a_sample_and_never_divide_by_zero(
    numerator: int, denominator: int, percent: float | None
) -> None:
    result = rate("Answered response rate", numerator, denominator, "a / b x 100")
    assert result.percent == percent
    assert (result.note is None) == (percent is not None)
    assert (result.numerator, result.denominator) == (numerator, denominator)
