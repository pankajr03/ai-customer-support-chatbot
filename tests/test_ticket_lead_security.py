"""Stage 12 (Task 17): support ticket and sales lead security, end to end.

Consent, contact validation, duplicate submissions, unauthorized and cross-session access
and invalid transitions are verified in ``tests/test_security_audit.py``; anonymization
and retention in ``tests/test_support_privacy.py``; leads in ``tests/test_support_api.py``.
This file adds what those do not check directly: the customer-facing wording (no claimed
human response, no invented deadline), lead separation in staff tooling, the audit trail
of ticket changes, and contact data after anonymization.
"""

import re
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from tests.test_admin_api import (  # noqa: F401 - fixtures
    MARIA,
    TICKETS,
    AdminApp,
    bearer,
    make_admin_app,
)

CONVERSATIONS = "/api/v1/conversations"
EMAIL = "lead.customer@example.com"
# A claim that a person has already acted, or a promised time, in customer-facing text.
HUMAN_CLAIMS = re.compile(
    r"\b(has|have) (responded|replied|contacted you|been in touch|reviewed)|"
    r"\b(emailed|called) you\b|\bwe('ve| have) (sent|notified|emailed)\b|"
    r"\b(agent|team) (has|have) (seen|read|replied)",
    re.IGNORECASE,
)
DEADLINES = re.compile(
    r"\bwithin \d+|\b\d+\s*(minutes?|hours?|days?|business days?|working days?)\b|"
    r"\b(today|tomorrow|by (monday|friday|the end of))\b|\basap\b|\bshortly\b",
    re.IGNORECASE,
)


@pytest.fixture
def api(make_admin_app: Callable[..., AdminApp]) -> AdminApp:  # noqa: F811
    return make_admin_app("tickets", rate_limit_enabled=False)


def submit(api: AdminApp, ticket_type: str = "support", **extra: Any) -> tuple[dict, str, str]:
    created = api.client.post(CONVERSATIONS).json()
    body = {
        "session_id": created["session_id"],
        "client_request_id": str(uuid.uuid4()),
        "ticket_type": ticket_type,
        "subject": "Bulk order question" if ticket_type == "sales" else "Parcel not delivered",
        "description": "Synthetic request for the Stage 12 tests.",
        "contact": {
            "name": "Synthetic Lead",
            "email": EMAIL,
            "phone": None,
            "contact_preference": "email",
            "consent_given": True,
        },
        "confirmed": True,
        **extra,
    }
    response = api.client.post(TICKETS, json=body, headers=bearer(created["access_token"]))
    assert response.status_code == 201, response.text
    return response.json(), created["session_id"], created["access_token"]


@pytest.mark.parametrize("ticket_type", ["support", "sales"])
def test_the_confirmation_claims_no_human_action_and_no_deadline(
    api: AdminApp, ticket_type: str
) -> None:
    extra = {"lead_category": "bulk_order"} if ticket_type == "sales" else {}
    created, sid, token = submit(api, ticket_type, **extra)
    shown = " ".join(str(created.get(k) or "") for k in ("message", "status_message"))
    assert created["status"] == "open"
    assert not HUMAN_CLAIMS.search(shown), shown
    assert not DEADLINES.search(shown), shown
    assert created["expected_response"] is None  # SUPPORT_RESPONSE_TIME_TEXT not set
    view = api.client.get(
        f"{TICKETS}/{created['ticket_id']}", params={"session_id": sid}, headers=bearer(token)
    ).json()
    assert (
        view["status_message"]
        == "Your request has been recorded. Our team has not picked it up yet."
    )


def test_a_configured_response_time_is_the_only_promise_made(
    make_admin_app: Callable[..., AdminApp],  # noqa: F811
) -> None:
    api = make_admin_app(
        "promise",
        rate_limit_enabled=False,
        support_response_time_text="We usually reply within one working day.",
    )
    created, _, _ = submit(api)
    assert created["expected_response"] == "We usually reply within one working day."
    assert not DEADLINES.search(created["message"])  # the confirmation adds nothing


def test_the_chat_offer_of_human_help_promises_no_time(api: AdminApp) -> None:
    created = api.client.post(CONVERSATIONS).json()
    reply = api.client.post(
        f"{CONVERSATIONS}/{created['session_id']}/chat",
        json={"message": "I want to talk to a human"},
        headers=bearer(created["access_token"]),
    ).json()
    assert reply["escalation_offer"] is not None
    assert not DEADLINES.search(reply["answer"]) and not HUMAN_CLAIMS.search(reply["answer"])


def test_leads_are_kept_apart_from_support_tickets(api: AdminApp) -> None:
    support, _, _ = submit(api)
    lead, _, _ = submit(api, "sales", lead_category="bulk_order")
    assert support["reference_number"].startswith("SUP-")
    assert lead["reference_number"].startswith("SAL-")
    leads = api.get(TICKETS, params={"ticket_type": "sales"}).json()["tickets"]
    assert [t["ticket_id"] for t in leads] == [lead["ticket_id"]]
    assert {t["lead_category"] for t in leads} == {"bulk_order"}
    tickets = api.get(TICKETS, params={"ticket_type": "support"}).json()["tickets"]
    assert [t["ticket_id"] for t in tickets] == [support["ticket_id"]]
    assert tickets[0]["lead_category"] is None
    # A lead category on a support request is refused.
    created = api.client.post(CONVERSATIONS).json()
    body = {
        "session_id": created["session_id"], "client_request_id": str(uuid.uuid4()),
        "ticket_type": "support", "lead_category": "bulk_order", "subject": "x",
        "description": "x", "contact": {"name": "A", "email": EMAIL, "phone": None,
        "contact_preference": "email", "consent_given": True}, "confirmed": True,
    }  # fmt: skip
    refused = api.client.post(TICKETS, json=body, headers=bearer(created["access_token"]))
    assert refused.status_code == 422


def test_lists_and_analytics_never_carry_contact_details(api: AdminApp) -> None:
    submit(api)
    submit(api, "sales", lead_category="partnership")
    for path in (TICKETS, "/api/v1/admin/analytics/tickets", "/api/v1/admin/dashboard/summary"):
        response = api.get(path)
        assert response.status_code == 200, path
        assert EMAIL not in response.text and "Synthetic Lead" not in response.text, path


def test_a_status_change_is_audited_with_its_actor_and_a_refused_one_is_not(
    api: AdminApp,
) -> None:
    created, _, _ = submit(api)
    path = f"{TICKETS}/{created['ticket_id']}/status"
    assert api.patch(path, {"status": "closed"}, token=MARIA).status_code == 200
    assert api.patch(path, {"status": "in_progress"}, token=MARIA).status_code == 409  # final

    events = api.get(f"{TICKETS}/{created['ticket_id']}/events").json()["events"]
    staff_events = [e for e in events if e["actor_id"] == "maria"]
    assert [e["new_status"] for e in staff_events] == ["closed"]
    audit = api.get(
        "/api/v1/admin/audit",
        params={"action": "ticket.status_changed", "resource_id": created["ticket_id"]},
    ).json()["events"]
    assert [(e["actor_id"], e["details"].get("to")) for e in audit] == [("maria", "closed")]
    assert EMAIL not in str(audit)


def test_an_anonymized_ticket_shows_no_contact_details_to_anyone(api: AdminApp) -> None:
    created, sid, token = submit(api)
    api.patch(f"{TICKETS}/{created['ticket_id']}/status", {"status": "closed"})
    repository = api.app.state.ticket_repository
    assert repository.anonymize_ticket(uuid.UUID(created["ticket_id"]))

    detail = api.get(f"{TICKETS}/{created['ticket_id']}").json()
    # Replaced by placeholders (the record stays, the person is gone).
    assert detail["contact"]["name"] == "[removed]"
    assert EMAIL not in str(detail) and "Synthetic Lead" not in str(detail)
    customer = api.client.get(
        f"{TICKETS}/{created['ticket_id']}", params={"session_id": sid}, headers=bearer(token)
    )
    assert EMAIL not in customer.text
    viewed = api.get("/api/v1/admin/audit", params={"action": "ticket.contact_viewed"})
    assert viewed.json()["events"] == []  # nothing personal was shown
