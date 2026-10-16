"""Support ticket API (Stage 10, Tasks 9, 10 and 13), over HTTP against the real app."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text

from app.core.staff_auth import generate_staff_token, hash_staff_token
from app.models.errors import ErrorResponse
from app.models.support import CUSTOMER_STATUS_MESSAGES, TicketStatus
from tests import test_api_workflows as workflows
from tests.test_api_workflows import App

make_app = workflows.make_app  # the workflow tests' running-app fixture

BASE = "/api/v1/support/tickets"
MARIA = generate_staff_token()
SAM = generate_staff_token()
STAFF_HASHES = [f"maria:{hash_staff_token(MARIA)}", f"sam:{hash_staff_token(SAM)}"]
CONTACT_FIELDS = {"name", "email", "phone", "contact", "contact_preference", "contact_id"}


def staff(token: str = MARIA) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def body(sid: str, **changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session_id": sid,
        "client_request_id": str(uuid.uuid4()),
        "ticket_type": "support",
        "subject": "Refund assistance",
        "description": "I need help with a refund request.",
        "contact": {
            "name": "Example Customer",
            "email": "customer@example.com",
            "phone": None,
            "contact_preference": "email",
            "consent_given": True,
        },
        "confirmed": True,
    }
    payload.update(changes)
    return payload


def error_code(response: Any, status: int) -> str:
    assert response.status_code == status, response.text
    ErrorResponse.model_validate(response.json())
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]
    return str(response.json()["error"]["code"])


@pytest.fixture
def api(make_app: Callable[..., App]) -> App:
    return make_app(staff_token_hashes=STAFF_HASHES)


def create(api: App, **changes: Any) -> tuple[dict[str, Any], str, dict[str, str]]:
    sid, headers = api.start()
    response = api.client.post(BASE, json=body(sid, **changes), headers=headers)
    assert response.status_code == 201, response.text
    return response.json(), sid, headers


# --- Creating a ticket ------------------------------------------------------------------


def test_a_customer_creates_a_ticket(api: App) -> None:
    sid, headers = api.start()

    response = api.client.post(BASE, json=body(sid), headers=headers)

    assert response.status_code == 201
    created = response.json()
    assert created["reference_number"].startswith("SUP-")
    assert created["status"] == "open"
    assert created["status_message"] == CUSTOMER_STATUS_MESSAGES[TicketStatus.OPEN]
    assert created["outcome"] == "created"
    assert created["subject"] == "Refund assistance"
    assert created["reference_number"] in created["message"]
    assert created["expected_response"] is None
    assert not CONTACT_FIELDS & set(created)
    assert response.headers["Cache-Control"] == "no-store"
    assert "X-Request-ID" in response.headers


def test_a_retry_returns_the_same_ticket(api: App) -> None:
    sid, headers = api.start()
    payload = body(sid)

    first = api.client.post(BASE, json=payload, headers=headers)
    retry = api.client.post(BASE, json=payload, headers=headers)

    assert (first.status_code, retry.status_code) == (201, 200)
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert retry.json()["ticket_id"] == first.json()["ticket_id"]
    assert retry.json()["outcome"] == "replayed"


def test_the_same_id_with_other_details_is_a_conflict(api: App) -> None:
    sid, headers = api.start()
    payload = body(sid)
    api.client.post(BASE, json=payload, headers=headers)

    changed = api.client.post(BASE, json={**payload, "subject": "Other"}, headers=headers)

    assert error_code(changed, 422) == "TICKET_REQUEST_CONFLICT"


def test_sending_the_same_request_again_returns_the_open_ticket(api: App) -> None:
    created, sid, headers = create(api)

    again = api.client.post(BASE, json=body(sid), headers=headers)  # a new client_request_id

    assert again.status_code == 200
    assert again.json()["outcome"] == "duplicate"
    assert again.json()["ticket_id"] == created["ticket_id"]


def test_a_configured_response_time_is_returned(make_app: Callable[..., App]) -> None:
    api = make_app(support_response_time_text="We usually reply within one working day.")

    created, _, _ = create(api)

    assert created["expected_response"] == "We usually reply within one working day."


def test_the_conversation_token_is_required(api: App) -> None:
    sid, headers = api.start()
    other_sid, other_headers = api.start()

    assert error_code(api.client.post(BASE, json=body(sid)), 401) == "UNAUTHORIZED_SESSION"
    wrong = api.client.post(BASE, json=body(sid), headers=other_headers)
    assert error_code(wrong, 404) == "SESSION_NOT_FOUND"
    staff_token = api.client.post(BASE, json=body(sid), headers=staff())
    assert error_code(staff_token, 404) == "SESSION_NOT_FOUND"  # not a conversation token


def test_tokens_are_never_accepted_in_the_body_or_url(api: App) -> None:
    sid, headers = api.start()
    token = headers["Authorization"].removeprefix("Bearer ")

    in_body = api.client.post(BASE, json=body(sid, access_token=token), headers=headers)
    in_url = api.client.post(f"{BASE}?access_token={token}", json=body(sid), headers=headers)

    assert error_code(in_body, 422) == "VALIDATION_ERROR"
    assert error_code(in_url, 400) == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    ("changes", "rule"),
    [
        ({"confirmed": False}, "confirmation_required"),
        ({"confirmed": "true"}, "bool_type"),
        ({"session_id": "not-a-uuid"}, "uuid_format"),
        ({"client_request_id": "x"}, "uuid_format"),
        ({"ticket_type": "complaint"}, "enum"),
        ({"description": "   "}, "blank_text"),
        ({"subject": "a" * 151}, "string_too_long"),
        ({"lead_category": "bulk_order"}, "lead_category_not_allowed"),
        ({"status": "resolved"}, "extra_forbidden"),
    ],
)
def test_invalid_requests_are_refused(api: App, changes: dict[str, Any], rule: str) -> None:
    sid, headers = api.start()

    response = api.client.post(BASE, json=body(sid, **changes), headers=headers)

    assert error_code(response, 422) == "VALIDATION_ERROR"
    assert rule in [detail["type"] for detail in response.json()["error"]["details"]]


@pytest.mark.parametrize(
    ("contact", "rule"),
    [
        ({"consent_given": False}, "consent_required"),
        ({"consent_given": None}, "bool_type"),
        ({"email": "not-an-email"}, "value_error"),
        ({"name": ""}, "blank_text"),
        ({"contact_preference": "phone"}, "phone_required"),
        ({"phone": "abc"}, "phone_format"),
        ({"contact_preference": "fax"}, "enum"),
    ],
)
def test_invalid_contact_details_are_refused(api: App, contact: dict[str, Any], rule: str) -> None:
    sid, headers = api.start()
    payload = body(sid)
    payload["contact"] = {**payload["contact"], **contact}

    response = api.client.post(BASE, json=payload, headers=headers)

    assert error_code(response, 422) == "VALIDATION_ERROR"
    assert rule in [detail["type"] for detail in response.json()["error"]["details"]]
    assert "not-an-email" not in response.text  # values are never echoed


def test_an_expired_conversation_cannot_send_a_request(make_app: Callable[..., App]) -> None:
    api = make_app(conversation_ttl_minutes=1)
    sid, headers = api.start()
    with api.database.write() as db:  # created and expired long ago
        db.execute(
            text(
                "UPDATE conversation_sessions SET created_at = '2000-01-01 00:00:00', "
                "updated_at = '2000-01-01 00:00:00', expires_at = '2000-01-01 01:00:00'"
            )
        )

    assert error_code(api.client.post(BASE, json=body(sid), headers=headers), 410) == (
        "SESSION_EXPIRED"
    )


def test_support_requests_are_rate_limited(make_app: Callable[..., App]) -> None:
    api = make_app(rate_limit_support_requests_per_minute=2)
    sid, headers = api.start()

    codes = [
        api.client.post(BASE, json=body(sid, subject=f"S{i}"), headers=headers).status_code
        for i in range(3)
    ]

    assert codes == [201, 201, 429]


def test_the_customer_can_keep_chatting_afterwards(api: App) -> None:
    _, sid, headers = create(api)

    reply = api.chat(sid, headers, workflows.RESET)

    assert reply.status_code == 200 and reply.json()["decision"] == "answered"


# --- Sales leads (Task 13) --------------------------------------------------------------


@pytest.mark.parametrize(
    "category", ["bulk_order", "business_pricing", "partnership", "product_demo", "general_sales"]
)
def test_sales_inquiries_are_stored_as_leads_with_their_category(api: App, category: str) -> None:
    created, sid, headers = create(api, ticket_type="sales", lead_category=category)

    assert created["reference_number"].startswith("SAL-")
    assert (created["ticket_type"], created["lead_category"]) == ("sales", category)
    view = api.client.get(
        f"{BASE}/{created['ticket_id']}", params={"session_id": sid}, headers=headers
    ).json()
    assert view["lead_category"] == category


def test_a_sales_inquiry_without_a_category_is_a_general_lead(api: App) -> None:
    created, _, _ = create(api, ticket_type="sales")

    assert created["lead_category"] == "general_sales"


def test_support_requests_are_never_leads(api: App) -> None:
    created, _, _ = create(api)

    assert created["lead_category"] is None
    page = api.client.get(BASE, params={"ticket_type": "sales"}, headers=staff()).json()
    assert page["total"] == 0


def test_no_marketing_consent_is_collected_or_stored(api: App) -> None:
    sid, headers = api.start()
    payload = body(sid, ticket_type="sales")
    payload["contact"]["marketing_opt_in"] = True

    response = api.client.post(BASE, json=payload, headers=headers)

    assert error_code(response, 422) == "VALIDATION_ERROR"  # no such field exists


# --- Reading a ticket (customer) --------------------------------------------------------


def test_a_customer_reads_their_ticket_without_private_details(api: App) -> None:
    created, sid, headers = create(api)

    response = api.client.get(
        f"{BASE}/{created['ticket_id']}", params={"session_id": sid}, headers=headers
    )

    assert response.status_code == 200
    view = response.json()
    assert view["reference_number"] == created["reference_number"]
    assert view["status_message"] == CUSTOMER_STATUS_MESSAGES[TicketStatus.OPEN]
    assert not (CONTACT_FIELDS | {"description", "assigned_to", "priority"}) & set(view)


def test_a_customer_sees_status_changes_but_never_who_handles_it(api: App) -> None:
    created, sid, headers = create(api)
    api.client.patch(
        f"{BASE}/{created['ticket_id']}/status",
        json={"status": "assigned", "assigned_to": "sam"},
        headers=staff(),
    )

    view = api.client.get(
        f"{BASE}/{created['ticket_id']}", params={"session_id": sid}, headers=headers
    ).json()

    assert view["status"] == "assigned"
    assert view["status_message"] == CUSTOMER_STATUS_MESSAGES[TicketStatus.ASSIGNED]
    assert "sam" not in str(view) and "maria" not in str(view)


def test_customers_never_reach_other_conversations_tickets(api: App) -> None:
    created, sid, headers = create(api)
    other_sid, other_headers = api.start()
    url = f"{BASE}/{created['ticket_id']}"

    their_own_session = api.client.get(url, params={"session_id": other_sid}, headers=other_headers)
    borrowed_session = api.client.get(url, params={"session_id": sid}, headers=other_headers)
    no_session = api.client.get(url, headers=headers)
    no_token = api.client.get(url, params={"session_id": sid})

    assert error_code(their_own_session, 404) == "TICKET_NOT_FOUND"
    assert error_code(borrowed_session, 404) == "SESSION_NOT_FOUND"
    assert error_code(no_session, 404) == "TICKET_NOT_FOUND"
    assert error_code(no_token, 401) == "UNAUTHORIZED_SESSION"


@pytest.mark.parametrize("ticket_id", ["not-a-uuid", str(uuid.uuid4()), "SUP-ABCD-1234"])
def test_unknown_or_guessed_tickets_are_not_found(api: App, ticket_id: str) -> None:
    sid, headers = api.start()

    response = api.client.get(f"{BASE}/{ticket_id}", params={"session_id": sid}, headers=headers)

    assert error_code(response, 404) == "TICKET_NOT_FOUND"


# --- Staff authorization (Task 10) ------------------------------------------------------


STAFF_CALLS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", BASE, None),
    ("PATCH", f"{BASE}/{uuid.uuid4()}/status", {"status": "closed"}),
    ("GET", f"{BASE}/{uuid.uuid4()}/events", None),
]


@pytest.mark.parametrize(("method", "url", "payload"), STAFF_CALLS)
def test_staff_endpoints_need_a_staff_token(
    api: App, method: str, url: str, payload: dict[str, Any] | None
) -> None:
    _, customer = api.start()

    for headers in ({}, customer, staff("stf_" + "x" * 43), staff("Bearer nonsense")):
        response = api.client.request(method, url, json=payload, headers=headers)
        assert error_code(response, 401) == "STAFF_AUTH_REQUIRED"
        assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(("method", "url", "payload"), STAFF_CALLS)
def test_staff_endpoints_are_off_without_configured_staff(
    make_app: Callable[..., App], method: str, url: str, payload: dict[str, Any] | None
) -> None:
    api = make_app()  # STAFF_TOKEN_HASHES empty

    for headers in ({}, staff()):
        response = api.client.request(method, url, json=payload, headers=headers)
        assert error_code(response, 403) == "STAFF_ACCESS_DISABLED"


def test_staff_tokens_in_the_url_are_refused(api: App) -> None:
    response = api.client.get(BASE, params={"token": MARIA})

    assert error_code(response, 400) == "VALIDATION_ERROR"


def test_staff_credentials_are_never_logged(api: App, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")

    api.client.get(BASE, headers=staff())
    api.client.get(BASE, headers=staff("stf_" + "y" * 43))

    assert MARIA not in caplog.text and "stf_" + "y" * 43 not in caplog.text


# --- Staff operations -------------------------------------------------------------------


def test_staff_list_tickets_with_filters_and_pages(api: App) -> None:
    support, _, _ = create(api)
    sales, _, _ = create(api, ticket_type="sales", lead_category="bulk_order")
    third, _, _ = create(api, subject="Third")

    first_page = api.client.get(BASE, params={"limit": 2}, headers=staff()).json()
    second_page = api.client.get(BASE, params={"limit": 2, "offset": 2}, headers=staff()).json()
    leads = api.client.get(
        BASE, params={"ticket_type": "sales", "lead_category": "bulk_order"}, headers=staff()
    ).json()

    ids = [t["ticket_id"] for t in first_page["tickets"] + second_page["tickets"]]
    assert sorted(ids) == sorted([support["ticket_id"], sales["ticket_id"], third["ticket_id"]])
    assert (first_page["total"], first_page["has_more"], second_page["has_more"]) == (
        3,
        True,
        False,
    )
    assert [t["ticket_id"] for t in leads["tickets"]] == [sales["ticket_id"]]
    for ticket in first_page["tickets"]:
        assert not (CONTACT_FIELDS | {"description"}) & set(ticket)


def test_staff_filter_by_status_and_assignee(api: App) -> None:
    a, _, _ = create(api)
    create(api, subject="Other")
    api.client.patch(
        f"{BASE}/{a['ticket_id']}/status",
        json={"status": "assigned", "assigned_to": "sam"},
        headers=staff(),
    )

    by_status = api.client.get(
        BASE, params=[("status", "assigned"), ("status", "in_progress")], headers=staff()
    )
    by_assignee = api.client.get(BASE, params={"assigned_to": "sam"}, headers=staff())

    assert [t["ticket_id"] for t in by_status.json()["tickets"]] == [a["ticket_id"]]
    assert [t["ticket_id"] for t in by_assignee.json()["tickets"]] == [a["ticket_id"]]


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"status": "pending"},
        {"created_from": "2026-10-16T09:00:00"},
    ],  # fmt: skip
)
def test_invalid_staff_list_parameters_are_refused(api: App, params: dict[str, Any]) -> None:
    response = api.client.get(BASE, params=params, headers=staff())

    assert error_code(response, 422) == "VALIDATION_ERROR"


def test_staff_move_a_ticket_through_the_workflow(api: App) -> None:
    created, _, _ = create(api)
    url = f"{BASE}/{created['ticket_id']}/status"

    steps = [
        {"status": "assigned", "assigned_to": "sam", "expected_status": "open"},
        {"status": "in_progress"},
        {"status": "waiting_for_customer"},
        {"status": "in_progress"},
        {"status": "resolved"},
        {"status": "closed"},
    ]
    results = [api.client.patch(url, json=step, headers=staff()) for step in steps]

    assert [r.status_code for r in results] == [200] * len(steps)
    assert results[0].json()["assigned_to"] == "sam"
    final = results[-1].json()
    assert final["status"] == "closed" and final["closed_at"] and final["resolved_at"]
    events = api.client.get(f"{BASE}/{created['ticket_id']}/events", headers=staff()).json()
    assert [e["new_status"] for e in events["events"]] == [
        "open",
        "assigned",
        "in_progress",
        "waiting_for_customer",
        "in_progress",
        "resolved",
        "closed",
    ]
    assert events["events"][0]["actor_type"] == "customer"
    assert {e["actor_id"] for e in events["events"][1:]} == {"maria"}
    assert events["events"][1]["assigned_to"] == "sam"


@pytest.mark.parametrize(
    ("change", "status", "code"),
    [
        ({"status": "resolved"}, 409, "INVALID_STATUS_TRANSITION"),  # not assigned yet
        ({"status": "open"}, 409, "INVALID_STATUS_TRANSITION"),  # no change
        ({"status": "assigned", "assigned_to": "nobody"}, 422, "VALIDATION_ERROR"),
        ({"status": "assigned"}, 422, "VALIDATION_ERROR"),  # no assignee
        ({"status": "closed", "assigned_to": "sam"}, 422, "VALIDATION_ERROR"),
        ({"status": "closed", "expected_status": "in_progress"}, 409, "TICKET_CHANGED"),
        ({"status": "finished"}, 422, "VALIDATION_ERROR"),
    ],
)
def test_invalid_status_changes_are_refused(
    api: App, change: dict[str, Any], status: int, code: str
) -> None:
    created, _, _ = create(api)

    response = api.client.patch(
        f"{BASE}/{created['ticket_id']}/status", json=change, headers=staff()
    )

    assert error_code(response, status) == code
    ticket = api.client.get(BASE, headers=staff()).json()["tickets"][0]
    assert ticket["status"] == "open"


def test_closed_tickets_stay_closed(api: App) -> None:
    created, _, _ = create(api)
    url = f"{BASE}/{created['ticket_id']}/status"
    api.client.patch(url, json={"status": "closed"}, headers=staff())

    reopened = api.client.patch(url, json={"status": "in_progress"}, headers=staff())

    assert error_code(reopened, 409) == "INVALID_STATUS_TRANSITION"


def test_unknown_tickets_are_not_found_for_staff(api: App) -> None:
    for url in (f"{BASE}/{uuid.uuid4()}", f"{BASE}/{uuid.uuid4()}/events", f"{BASE}/x/events"):
        assert error_code(api.client.get(url, headers=staff()), 404) == "TICKET_NOT_FOUND"
    patch = api.client.patch(f"{BASE}/{uuid.uuid4()}/status", json={"status": "closed"},
                             headers=staff())  # fmt: skip
    assert error_code(patch, 404) == "TICKET_NOT_FOUND"


def test_staff_see_contact_details_only_for_a_single_ticket(api: App) -> None:
    created, _, _ = create(api)

    detail = api.client.get(f"{BASE}/{created['ticket_id']}", headers=staff(SAM))

    assert detail.status_code == 200
    view = detail.json()
    assert view["description"] == "I need help with a refund request."
    assert view["contact"]["email"] == "customer@example.com"
    assert view["contact"]["contact_preference"] == "email"
    assert view["has_conversation"] is True


def test_an_invalid_staff_token_on_the_ticket_endpoint_is_refused(api: App) -> None:
    created, _, _ = create(api)

    response = api.client.get(f"{BASE}/{created['ticket_id']}", headers=staff("stf_" + "z" * 43))

    assert error_code(response, 401) == "STAFF_AUTH_REQUIRED"


def test_existing_apis_are_unchanged(api: App) -> None:
    sid, headers = api.start()

    reply = api.chat(sid, headers, workflows.RESET)
    history = api.client.get(f"/api/v1/conversations/{sid}/messages", headers=headers)

    assert reply.status_code == 200 and history.status_code == 200
    assert api.client.get("/api/v1/health").status_code == 200
