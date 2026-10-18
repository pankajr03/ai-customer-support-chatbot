"""Stage 12 security audit: verification tests for docs/security-audit.md.

Each test names the finding or audit check it verifies (``SEC-nn`` or the task area).
Everything runs in-process against a temporary SQLite file (``make_admin_app``), with
synthetic customers and data only: no production database, no network, no real
customer records.

Areas:
- Task 3: customer session security and staff authorization, server-side.
- Task 4: API input validation (standard 422/400 envelope, no echoed input, Unicode).
- Task 5: SQL injection payloads, constraints, transactions, idempotency, concurrency.
- Task 6: secrets and configuration (placeholders, ignore rules, tracked files).
- Task 7: logging and privacy (no tokens or personal data in logs, redaction).
"""

import logging
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.errors import CREDENTIALS_IN_URL_MESSAGE
from app.core.config import Settings
from app.core.logging_config import REDACTED, redact
from app.core.security import ACCESS_TOKEN_PREFIX, generate_access_token
from app.core.staff_auth import generate_staff_token
from app.models.support import ALLOWED_TRANSITIONS, TicketStatus
from tests.test_admin_api import (  # noqa: F401 - fixtures
    ADMIN,
    ALEX,
    FAQS,
    MARIA,
    NEW_FAQ,
    TICKETS,
    AdminApp,
    bearer,
    make_admin_app,
)

ROOT = Path(__file__).resolve().parents[1]
CONVERSATIONS = "/api/v1/conversations"
EMAIL = "synthetic.customer@example.com"
PHONE = "+44 20 7946 0958"  # Ofcom drama range: never a real subscriber


@pytest.fixture
def api(make_admin_app: Callable[..., AdminApp]) -> AdminApp:  # noqa: F811
    """A database-backed app with an admin (ALEX) and a support member (MARIA). Rate
    limits are off so the matrices below are not cut short by 429s."""
    return make_admin_app("security", rate_limit_enabled=False)


def start(api: AdminApp) -> tuple[str, str]:
    """A new conversation: its ID and its access token."""
    created = api.client.post(CONVERSATIONS).json()
    return created["session_id"], created["access_token"]


def ticket_body(session_id: str, **changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "session_id": session_id,
        "client_request_id": str(uuid.uuid4()),
        "ticket_type": "support",
        "subject": "Parcel not delivered",
        "description": "Synthetic test request: the parcel has not arrived.",
        "contact": {
            "name": "Synthetic Customer",
            "email": EMAIL,
            "phone": None,
            "contact_preference": "email",
            "consent_given": True,
        },
        "confirmed": True,
    }
    body.update(changes)
    return body


def open_ticket(api: AdminApp, **changes: Any) -> tuple[str, str, str]:
    """A customer's ticket: (ticket ID, session ID, customer token)."""
    sid, token = start(api)
    response = api.client.post(TICKETS, json=ticket_body(sid, **changes), headers=bearer(token))
    assert response.status_code == 201, response.text
    return response.json()["ticket_id"], sid, token


def error(response: Any, status: int) -> dict[str, Any]:
    """The error envelope's code and message (the request ID differs per request)."""
    assert response.status_code == status, response.text
    body = response.json()["error"]
    return {"code": body["code"], "message": body["message"]}


def code(response: Any, status: int) -> str:
    return str(error(response, status)["code"])


# =============================================================================
# Task 3: customer session security
# =============================================================================


def customer_calls(sid: str) -> list[tuple[str, str, Any]]:
    base = f"{CONVERSATIONS}/{sid}"
    return [
        ("GET", base, None),
        ("GET", f"{base}/messages", None),
        ("POST", f"{base}/chat", {"message": "Where is my order?"}),
        ("POST", f"{base}/close", None),
        ("DELETE", base, None),
        ("POST", TICKETS, ticket_body(sid)),
    ]


def test_a_missing_session_token_is_401_everywhere(api: AdminApp) -> None:
    sid, _ = start(api)
    for method, path, body in customer_calls(sid):
        response = api.client.request(method, path, json=body)
        assert code(response, 401) == "UNAUTHORIZED_SESSION", (method, path)
        assert response.headers["WWW-Authenticate"] == "Bearer"
    ticket = api.client.get(f"{TICKETS}/{uuid.uuid4()}", params={"session_id": sid})
    assert code(ticket, 401) == "UNAUTHORIZED_SESSION"


@pytest.mark.parametrize(
    "token",
    [
        "garbage",
        ACCESS_TOKEN_PREFIX + "A" * 43,  # well-formed, never issued
        generate_access_token(),  # issued by nobody here
        ALEX,  # a staff token is not a conversation token
        "cst_" + "' OR '1'='1",
    ],
)
def test_an_invalid_session_token_looks_exactly_like_an_unknown_session(
    api: AdminApp, token: str
) -> None:
    sid, _ = start(api)
    unknown = api.client.get(f"{CONVERSATIONS}/{uuid.uuid4()}", headers=bearer(token))
    for method, path, body in customer_calls(sid)[:5]:
        response = api.client.request(method, path, json=body, headers=bearer(token))
        assert error(response, 404) == error(unknown, 404), (method, path)
        assert token not in response.text


def test_one_customer_cannot_use_another_customers_conversation(api: AdminApp) -> None:
    victim_sid, victim_token = start(api)
    api.client.post(
        f"{CONVERSATIONS}/{victim_sid}/chat",
        json={"message": "My order 12345 is late"},
        headers=bearer(victim_token),
    )
    _, attacker_token = start(api)
    for method, path, body in customer_calls(victim_sid)[:5]:
        response = api.client.request(method, path, json=body, headers=bearer(attacker_token))
        assert code(response, 404) == "SESSION_NOT_FOUND", (method, path)
    # Nothing changed: the victim's conversation is still open with one exchange.
    history = api.client.get(
        f"{CONVERSATIONS}/{victim_sid}/messages", headers=bearer(victim_token)
    ).json()
    assert history["total_messages"] == 2
    assert (
        api.client.get(f"{CONVERSATIONS}/{victim_sid}", headers=bearer(victim_token)).json()[
            "status"
        ]
        == "active"
    )


def test_one_customer_cannot_read_or_file_another_customers_ticket(api: AdminApp) -> None:
    victim_ticket, victim_sid, _ = open_ticket(api)
    attacker_sid, attacker_token = start(api)
    auth = bearer(attacker_token)

    # Own conversation, someone else's ticket: same answer as a ticket that does not exist.
    foreign = api.client.get(
        f"{TICKETS}/{victim_ticket}", params={"session_id": attacker_sid}, headers=auth
    )
    missing = api.client.get(
        f"{TICKETS}/{uuid.uuid4()}", params={"session_id": attacker_sid}, headers=auth
    )
    assert error(foreign, 404) == error(missing, 404)
    assert error(foreign, 404)["code"] == "TICKET_NOT_FOUND"
    # The victim's conversation with the attacker's token: the conversation is "not found".
    named = api.client.get(
        f"{TICKETS}/{victim_ticket}", params={"session_id": victim_sid}, headers=auth
    )
    assert code(named, 404) == "SESSION_NOT_FOUND"
    # Filing a request on the victim's conversation is refused and creates nothing.
    filed = api.client.post(TICKETS, json=ticket_body(victim_sid), headers=auth)
    assert code(filed, 404) == "SESSION_NOT_FOUND"
    assert api.get(TICKETS, token=MARIA).json()["total"] == 1
    # Customers never see contact details, even of their own ticket.
    assert EMAIL not in foreign.text + named.text


def expire_all_conversations(api: AdminApp) -> None:
    with api.database.write() as db:
        db.execute(
            text(
                "UPDATE conversation_sessions SET created_at = '2020-01-01 00:00:00', "
                "updated_at = '2020-01-01 00:00:00', expires_at = '2020-01-01 01:00:00'"
            )
        )


def test_an_expired_session_is_410_only_for_its_own_token(api: AdminApp) -> None:
    sid, token = start(api)
    expire_all_conversations(api)

    assert code(api.client.get(f"{CONVERSATIONS}/{sid}", headers=bearer(token)), 410) == (
        "SESSION_EXPIRED"
    )
    chat = api.client.post(
        f"{CONVERSATIONS}/{sid}/chat", json={"message": "hello"}, headers=bearer(token)
    )
    assert code(chat, 410) == "SESSION_EXPIRED"
    # Without the right token the expiry is not revealed: the session is simply unknown.
    wrong = api.client.get(f"{CONVERSATIONS}/{sid}", headers=bearer(generate_access_token()))
    assert code(wrong, 404) == "SESSION_NOT_FOUND"
    ticket = api.client.post(TICKETS, json=ticket_body(sid), headers=bearer(token))
    assert code(ticket, 410) == "SESSION_EXPIRED"


def test_a_closed_session_accepts_no_messages(api: AdminApp) -> None:
    sid, token = start(api)
    assert api.client.post(f"{CONVERSATIONS}/{sid}/close", headers=bearer(token)).status_code == 200

    chat = api.client.post(
        f"{CONVERSATIONS}/{sid}/chat", json={"message": "hello"}, headers=bearer(token)
    )
    assert code(chat, 409) == "SESSION_CLOSED"
    wrong = api.client.post(
        f"{CONVERSATIONS}/{sid}/chat",
        json={"message": "hello"},
        headers=bearer(generate_access_token()),
    )
    assert code(wrong, 404) == "SESSION_NOT_FOUND"  # closed is not revealed either


def test_a_deleted_session_and_its_token_are_gone_for_good(api: AdminApp) -> None:
    sid, token = start(api)
    api.client.post(
        f"{CONVERSATIONS}/{sid}/chat", json={"message": "Where is my order?"}, headers=bearer(token)
    )
    assert api.client.delete(f"{CONVERSATIONS}/{sid}", headers=bearer(token)).status_code == 204

    # Reusing the invalidated token: every endpoint answers "not found", nothing else.
    for method, path, body in customer_calls(sid):
        response = api.client.request(method, path, json=body, headers=bearer(token))
        assert code(response, 404) == "SESSION_NOT_FOUND", (method, path)
    with api.database.read() as db:
        assert db.execute(text("SELECT count(*) FROM conversation_messages")).scalar() == 0


def test_session_ids_and_tokens_cannot_be_guessed(api: AdminApp) -> None:
    sid, _ = start(api)
    assert uuid.UUID(sid).version == 4  # random, not sequential

    tokens = {generate_access_token() for _ in range(2_000)}
    assert len(tokens) == 2_000
    assert all(re.fullmatch(r"cst_[A-Za-z0-9_-]{43}", t) for t in tokens)  # 256 random bits

    # Guessing: random IDs with well-formed tokens all get the same "not found".
    answers = {
        tuple(error(api.client.get(f"{CONVERSATIONS}/{guess}", headers=bearer(t)), 404).values())
        for guess, t in [(uuid.uuid4(), generate_access_token()) for _ in range(25)]
    }
    real = api.client.get(f"{CONVERSATIONS}/{sid}", headers=bearer(generate_access_token()))
    assert answers == {tuple(error(real, 404).values())}


def test_credentials_in_the_url_are_refused_even_with_a_valid_header(api: AdminApp) -> None:
    """SEC-02 regression: the customer path of GET /support/tickets/{id} used to accept
    a request whose URL also carried the token."""
    ticket_id, sid, token = open_ticket(api)
    for name in ("access_token", "token", "authorization"):
        response = api.client.get(
            f"{TICKETS}/{ticket_id}", params={"session_id": sid, name: token}, headers=bearer(token)
        )
        assert error(response, 400) == {
            "code": "VALIDATION_ERROR",
            "message": CREDENTIALS_IN_URL_MESSAGE,
        }
        assert token not in response.text
        staff = api.client.get(f"{TICKETS}/{ticket_id}", params={name: ALEX}, headers=bearer(ALEX))
        assert code(staff, 400) == "VALIDATION_ERROR"
        conversation = api.client.get(
            f"{CONVERSATIONS}/{sid}", params={name: token}, headers=bearer(token)
        )
        assert code(conversation, 400) == "VALIDATION_ERROR"
    # Without the query parameter the same request works.
    ok = api.client.get(f"{TICKETS}/{ticket_id}", params={"session_id": sid}, headers=bearer(token))
    assert ok.status_code == 200


# =============================================================================
# Task 3: staff authorization
# =============================================================================


def ticket_staff_calls(ticket_id: str) -> list[tuple[str, str, Any]]:
    base = f"{TICKETS}/{ticket_id}"
    return [
        ("GET", TICKETS, None),
        ("GET", base, None),
        ("GET", f"{base}/events", None),
        ("PATCH", f"{base}/status", {"status": "closed"}),
        ("PUT", f"{base}/assignment", {"assigned_to": "maria", "expected_assigned_to": None}),
        ("GET", f"{base}/notes", None),
        ("POST", f"{base}/notes", {"body": "note", "client_note_id": str(uuid.uuid4())}),
        ("GET", f"{ADMIN}/staff", None),
    ]


ADMIN_ONLY_CALLS: list[tuple[str, str, Any]] = [
    ("GET", f"{ADMIN}/dashboard/summary", None),
    ("GET", f"{ADMIN}/overview", None),
    ("GET", f"{ADMIN}/analytics/chat", None),
    ("GET", f"{ADMIN}/analytics", None),
    ("GET", f"{ADMIN}/analytics/tickets", None),
    ("GET", f"{ADMIN}/audit", None),
    ("GET", FAQS, None),
    ("POST", FAQS, NEW_FAQ),
    ("GET", f"{FAQS}/faq_101", None),
    ("PATCH", f"{FAQS}/faq_101", {"expected_version": 1, "answer": "Hijacked answer."}),
    ("POST", f"{FAQS}/faq_101/deactivate", {"expected_version": 1}),
    ("POST", f"{FAQS}/faq_101/activate", {"expected_version": 1}),
    ("GET", f"{FAQS}/faq_101/revisions", None),
]


def assert_nothing_changed(api: AdminApp, ticket_id: str) -> None:
    faq = api.faq("faq_101")
    assert (faq["version"], faq["is_active"]) == (1, True)
    assert api.get(FAQS).json()["total"] == len(api.get(FAQS, params={"limit": 100}).json()["faqs"])
    ticket = api.get(f"{TICKETS}/{ticket_id}").json()
    assert (ticket["status"], ticket["assigned_to"]) == ("open", None)
    assert api.get(f"{TICKETS}/{ticket_id}/notes").json()["notes"] == []


def test_staff_endpoints_refuse_missing_invalid_and_customer_credentials(api: AdminApp) -> None:
    ticket_id, _, customer_token = open_ticket(api)
    faqs_before = api.get(FAQS).json()["total"]
    for method, path, body in ticket_staff_calls(ticket_id) + ADMIN_ONLY_CALLS:
        for headers in (
            {},
            bearer("garbage"),
            bearer(generate_staff_token()),  # well-formed, not configured
            bearer(customer_token),  # a customer's token on a staff endpoint
        ):
            response = api.client.request(method, path, json=body, headers=headers)
            if path == f"{TICKETS}/{ticket_id}" and "stf_" not in str(headers):
                # The shared ticket read treats non-staff callers as customers, who must
                # name their conversation: refused without revealing the ticket.
                assert response.status_code in (401, 404), (method, path, headers)
                assert EMAIL not in response.text
                continue
            assert code(response, 401) == "STAFF_AUTH_REQUIRED", (method, path, headers)
            assert response.headers["Cache-Control"] == "no-store"
    assert_nothing_changed(api, ticket_id)
    assert api.get(FAQS).json()["total"] == faqs_before


def test_support_staff_cannot_perform_admin_operations(api: AdminApp) -> None:
    ticket_id, _, _ = open_ticket(api)
    for method, path, body in ADMIN_ONLY_CALLS:
        response = api.client.request(method, path, json=body, headers=bearer(MARIA))
        assert code(response, 403) == "STAFF_FORBIDDEN", (method, path)
        assert "manage_faqs" not in response.text and "view_analytics" not in response.text
    assert_nothing_changed(api, ticket_id)
    # Their own work is allowed.
    assert api.get(TICKETS, token=MARIA).status_code == 200
    assert api.get(f"{TICKETS}/{ticket_id}", token=MARIA).json()["contact"]["email"] == EMAIL


def test_staff_endpoints_are_switched_off_when_no_staff_are_configured(
    make_admin_app: Callable[..., AdminApp],  # noqa: F811
) -> None:
    api = make_admin_app("nostaff", staff_token_hashes=[], rate_limit_enabled=False)
    ticket_id, _, _ = open_ticket(api)
    for method, path, body in ticket_staff_calls(ticket_id) + ADMIN_ONLY_CALLS:
        response = api.client.request(method, path, json=body, headers=bearer(ALEX))
        assert code(response, 403) == "STAFF_ACCESS_DISABLED", (method, path)


def test_unauthenticated_staff_requests_do_not_reveal_whether_a_ticket_exists(
    api: AdminApp,
) -> None:
    ticket_id, _, _ = open_ticket(api)
    fake = bearer(generate_staff_token())
    for suffix in ("", "/events", "/notes"):
        real = api.client.get(f"{TICKETS}/{ticket_id}{suffix}", headers=fake)
        unknown = api.client.get(f"{TICKETS}/{uuid.uuid4()}{suffix}", headers=fake)
        assert error(real, 401) == error(unknown, 401)


def test_refused_staff_requests_are_audited_without_the_token(api: AdminApp) -> None:
    attempt = generate_staff_token()
    api.client.get(FAQS, headers=bearer(attempt))
    api.client.patch(
        f"{FAQS}/faq_101", json={"expected_version": 1, "answer": "x"}, headers=bearer(MARIA)
    )
    log = api.get(f"{ADMIN}/audit", params={"action": "auth.access_denied"})
    events = log.json()["events"]
    assert {(e["actor_type"], e["outcome"]) for e in events} == {
        ("unauthenticated", "denied"),
        ("staff", "denied"),
    }
    assert any(e["actor_id"] == "maria" for e in events)
    assert attempt not in log.text and MARIA not in log.text


def test_the_server_enforces_roles_whatever_the_dashboard_shows(api: AdminApp) -> None:
    """The dashboard hides admin sections from support staff, but the API is the control:
    a hand-made request with a support token is still refused."""
    me = api.get(f"{ADMIN}/me", token=MARIA).json()
    assert "manage_faqs" not in me["permissions"]
    response = api.client.post(FAQS, json=NEW_FAQ, headers=bearer(MARIA))
    assert code(response, 403) == "STAFF_FORBIDDEN"


# =============================================================================
# Task 4: API input validation
# =============================================================================


def assert_validation_error(response: Any, status: int = 422) -> list[dict[str, Any]]:
    assert code(response, status) == "VALIDATION_ERROR"
    assert response.headers["content-type"].startswith("application/json")
    for leak in ("Traceback", 'File "', "sqlalchemy", "pydantic_core", "input_value"):
        assert leak not in response.text
    return list(response.json()["error"].get("details") or [])


def test_malformed_json_is_refused_without_internal_details(api: AdminApp) -> None:
    sid, token = start(api)
    ticket_id, _, _ = open_ticket(api)
    for path, method, headers in [
        (f"{CONVERSATIONS}/{sid}/chat", "POST", bearer(token)),
        (TICKETS, "POST", bearer(token)),
        (FAQS, "POST", bearer(ALEX)),
        (f"{TICKETS}/{ticket_id}/status", "PATCH", bearer(ALEX)),
    ]:
        for raw in (b"{", b'{"message": "unterminated', b"\xff\xfe", b"[1, 2,]", b"null"):
            response = api.client.request(
                method, path, content=raw, headers={**headers, "Content-Type": "application/json"}
            )
            assert response.status_code in (400, 422), (path, raw)
            assert_validation_error(response, response.status_code)


@pytest.mark.parametrize(
    ("body", "field", "rule"),
    [
        ({}, "message", "missing"),
        ({"message": 123}, "message", "string_type"),
        ({"message": ["hi"]}, "message", "string_type"),
        ({"message": None}, "message", "string_type"),
        ({"message": ""}, "message", "blank_message"),
        ({"message": "   \n\t  "}, "message", "blank_message"),
        ({"message": "​ "}, "message", "blank_message"),
        ({"message": "hi\u0000there"}, "message", "invalid_characters"),
        ({"message": "x" * 20_000}, "message", "string_too_long"),
        ({"message": "hi", "role": "system"}, "role", "extra_forbidden"),
        ({"message": "hi", "client_message_id": "123"}, "client_message_id", "uuid_format"),
        ({"message": "hi", "category": "Admin"}, "category", "enum"),
    ],
)
def test_chat_input_is_validated(api: AdminApp, body: Any, field: str, rule: str) -> None:
    sid, token = start(api)
    response = api.client.post(f"{CONVERSATIONS}/{sid}/chat", json=body, headers=bearer(token))
    details = assert_validation_error(response)
    assert any(d["loc"][-1] == field and d["type"] == rule for d in details), details


def test_oversized_bodies_are_refused_before_parsing(api: AdminApp) -> None:
    sid, token = start(api)
    limit = api.app.state.settings.max_request_body_bytes
    response = api.client.post(
        f"{CONVERSATIONS}/{sid}/chat",
        content=b'{"message": "' + b"a" * limit + b'"}',
        headers={**bearer(token), "Content-Type": "application/json"},
    )
    assert code(response, 413) == "PAYLOAD_TOO_LARGE"


SENTINEL = "SENTINEL-7Q2"


@pytest.mark.parametrize(
    ("contact", "rule"),
    [
        ({"email": f"{SENTINEL}-not-an-email"}, "value_error"),
        ({"email": f"{SENTINEL}@"}, "value_error"),
        ({"email": ""}, "value_error"),
        ({"phone": f"call me {SENTINEL}"}, "phone_format"),
        ({"phone": "+1 555 0100 0100 0100 0100"}, "phone_digits"),
        ({"name": ""}, "blank_text"),
        ({"name": "   "}, "blank_text"),
        ({"name": "Line one\nLine two"}, "invalid_characters"),
        ({"consent_given": False}, "consent_required"),
        ({"consent_given": "true"}, "bool_type"),
        ({"contact_preference": "phone", "phone": None}, "phone_required"),
        ({"contact_preference": "pigeon"}, "enum"),
        ({"fax": "123"}, "extra_forbidden"),
    ],
)
def test_contact_details_are_validated_without_echoing_them(
    api: AdminApp, contact: dict[str, Any], rule: str
) -> None:
    sid, token = start(api)
    body = ticket_body(sid)
    body["contact"] = {**body["contact"], **contact}
    response = api.client.post(TICKETS, json=body, headers=bearer(token))
    details = assert_validation_error(response)
    assert any(d["type"] == rule for d in details), details
    assert SENTINEL not in response.text
    assert api.get(TICKETS).json()["total"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"session_id": "123"},
        {"session_id": "not-a-uuid"},
        {"client_request_id": "00000000-0000-0000-0000-000000000000"},
        {"ticket_type": "refund"},
        {"subject": "   "},
        {"subject": "x" * 151},
        {"description": ""},
        {"description": "x" * 2_001},
        {"confirmed": False},
        {"confirmed": "yes"},
        {"status": "closed"},  # customers cannot set fields the server owns
        {"priority": "urgent"},
        {"assigned_to": "alex"},
    ],
)
def test_ticket_requests_are_validated(api: AdminApp, changes: dict[str, Any]) -> None:
    sid, token = start(api)
    body = {**ticket_body(sid), **changes}
    response = api.client.post(TICKETS, json=body, headers=bearer(token))
    assert_validation_error(response)
    assert api.get(TICKETS).json()["total"] == 0


def test_invalid_ids_in_paths_are_simply_not_found(api: AdminApp) -> None:
    _, token = start(api)
    for bad in ("not-a-uuid", "1", "00000000-0000-0000-0000-000000000000", "' OR 1=1 --", "%00"):
        conversation = api.client.get(f"{CONVERSATIONS}/{bad}", headers=bearer(token))
        assert code(conversation, 404) == "SESSION_NOT_FOUND", bad
        ticket = api.client.get(f"{TICKETS}/{bad}", headers=bearer(ALEX))
        assert code(ticket, 404) == "TICKET_NOT_FOUND", bad
        faq = api.client.get(f"{FAQS}/{bad}", headers=bearer(ALEX))
        assert code(faq, 404) == "FAQ_NOT_FOUND", bad
    long_id = api.client.get(f"{FAQS}/{'x' * 65}", headers=bearer(ALEX))
    assert_validation_error(long_id)


@pytest.mark.parametrize(
    "params",
    [
        {"offset": -1},
        {"limit": 0},
        {"limit": -5},
        {"limit": 1_000_000},
        {"limit": "ten"},
        {"offset": "1.5"},
    ],
)
def test_pagination_is_bounded_everywhere(api: AdminApp, params: dict[str, Any]) -> None:
    sid, token = start(api)
    for path, auth in [
        (f"{CONVERSATIONS}/{sid}/messages", bearer(token)),
        (TICKETS, bearer(ALEX)),
        (FAQS, bearer(ALEX)),
        (f"{ADMIN}/audit", bearer(ALEX)),
        (f"{FAQS}/faq_101/revisions", bearer(ALEX)),
        ("/api/v1/faqs", {}),
    ]:
        assert_validation_error(api.client.get(path, params=params, headers=auth))


@pytest.mark.parametrize(
    ("path", "params"),
    [
        (TICKETS, {"created_from": "2026-01-01T00:00:00"}),  # no offset: ambiguous
        (TICKETS, {"created_from": "yesterday"}),
        (TICKETS, {"created_before": "2026-02-30T00:00:00Z"}),
        (f"{ADMIN}/analytics/chat", {"start_date": "2026-13-01"}),
        (f"{ADMIN}/analytics/chat", {"start_date": "2026-05-10", "end_date": "2026-05-01"}),
        (f"{ADMIN}/analytics/chat", {"end_date": "2999-01-01"}),
        (f"{ADMIN}/analytics/chat", {"days": 0}),
        (f"{ADMIN}/analytics/chat", {"days": 10_000}),
        (f"{ADMIN}/analytics/tickets", {"days": 7, "start_date": "2026-01-01"}),
        (f"{ADMIN}/audit", {"start_date": "not-a-date"}),
    ],
)
def test_invalid_timestamps_and_periods_are_refused(
    api: AdminApp, path: str, params: dict[str, Any]
) -> None:
    assert_validation_error(api.get(path, params=params))


def test_invalid_ticket_status_changes_are_refused(api: AdminApp) -> None:
    ticket_id, _, _ = open_ticket(api)
    path = f"{TICKETS}/{ticket_id}/status"
    disallowed = next(
        s
        for s in TicketStatus
        if s not in ALLOWED_TRANSITIONS[TicketStatus.OPEN]
        and s not in (TicketStatus.OPEN, TicketStatus.ASSIGNED)
    )
    assert code(api.patch(path, {"status": disallowed.value}), 409) == "INVALID_STATUS_TRANSITION"
    assert_validation_error(api.patch(path, {"status": "deleted"}))
    assert_validation_error(api.patch(path, {"status": "assigned"}))  # needs an assignee
    assert_validation_error(api.patch(path, {"status": "in_progress", "assigned_to": "maria"}))
    unknown_assignee = api.patch(path, {"status": "assigned", "assigned_to": "mallory"})
    assert_validation_error(unknown_assignee)
    assert api.get(f"{TICKETS}/{ticket_id}").json()["status"] == "open"


@pytest.mark.parametrize("version", [0, -1, "one", None, 1.5, [1]])
def test_invalid_faq_versions_are_refused(api: AdminApp, version: Any) -> None:
    assert_validation_error(
        api.patch(f"{FAQS}/faq_101", {"expected_version": version, "answer": "x"})
    )
    assert_validation_error(api.post(f"{FAQS}/faq_101/deactivate", {"expected_version": version}))
    assert api.faq("faq_101")["version"] == 1


def test_a_stale_faq_version_is_a_conflict_not_an_overwrite(api: AdminApp) -> None:
    stale = api.patch(f"{FAQS}/faq_101", {"expected_version": 99, "answer": "Overwrite."})
    assert code(stale, 409) == "FAQ_VERSION_CONFLICT"
    missing = api.patch(f"{FAQS}/faq_101", {"answer": "No version."})
    assert_validation_error(missing)
    assert api.faq("faq_101")["version"] == 1


def test_unicode_is_preserved_exactly(api: AdminApp) -> None:
    text_in = "Où est ma commande? 注文はどこ 📦 مرحبا — Ñandú «ok» é vs é"
    sid, token = start(api)
    reply = api.client.post(
        f"{CONVERSATIONS}/{sid}/chat", json={"message": text_in}, headers=bearer(token)
    )
    assert reply.status_code == 200, reply.text
    history = api.client.get(f"{CONVERSATIONS}/{sid}/messages", headers=bearer(token)).json()
    assert history["messages"][0]["content"] == text_in

    name = "José Ñúñez 李雷 Ólafsdóttir"
    ticket_id, _, _ = open_ticket(
        api,
        subject="Colis non livré — 包裹",
        contact={
            "name": name,
            "email": "jose@example.com",
            "phone": None,
            "contact_preference": "email",
            "consent_given": True,
        },
    )
    stored = api.get(f"{TICKETS}/{ticket_id}").json()
    assert stored["contact"]["name"] == name
    assert stored["subject"] == "Colis non livré — 包裹"


# =============================================================================
# Task 5: SQL injection and database security (temporary database only)
# =============================================================================

PAYLOADS = [
    "' OR '1'='1",
    "'; DROP TABLE support_tickets; --",
    '" OR 1=1 --',
    "1; DELETE FROM faqs",
    "' UNION SELECT access_token_hash FROM conversation_sessions --",
    "%%%",
    "__%__",
    "\\' OR 1=1 --",
    "faq_101' --",
    "admin'/*",
]


def table_counts(api: AdminApp) -> dict[str, int]:
    with api.database.read() as db:
        names = [
            row[0]
            for row in db.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
            )
        ]
        return {
            name: int(db.execute(text(f'SELECT count(*) FROM "{name}"')).scalar() or 0)  # noqa: S608
            for name in names
        }


def test_injection_payloads_are_data_not_sql(api: AdminApp) -> None:
    ticket_id, _, _ = open_ticket(api)
    before = table_counts(api)
    for payload in PAYLOADS:
        public = api.client.get("/api/v1/faqs/search", params={"q": payload})
        assert public.status_code == 200, payload
        admin_list = api.get(FAQS, params={"q": payload})
        assert admin_list.status_code == 200 and admin_list.json()["total"] == 0, payload
        assert api.get(f"{FAQS}/{payload}").status_code in (404, 422)
        for params in ({"assigned_to": payload}, {"reference": payload[:20]}):
            listing = api.get(TICKETS, params=params)
            assert listing.status_code in (200, 422), payload
            if listing.status_code == 200:
                assert listing.json()["total"] == 0, payload
        audit = api.get(f"{ADMIN}/audit", params={"actor_id": payload, "resource_id": payload})
        assert audit.status_code == 200 and audit.json()["total"] == 0, payload
        assert api.client.get(f"{TICKETS}/{payload}", headers=bearer(ALEX)).status_code == 404
    assert table_counts(api) == before
    assert api.get(f"{TICKETS}/{ticket_id}").status_code == 200


def test_payloads_are_stored_and_returned_literally(api: AdminApp) -> None:
    sid, token = start(api)
    for payload in PAYLOADS[:4]:
        response = api.client.post(
            f"{CONVERSATIONS}/{sid}/chat", json={"message": payload}, headers=bearer(token)
        )
        assert response.status_code == 200, response.text
    history = api.client.get(f"{CONVERSATIONS}/{sid}/messages", headers=bearer(token)).json()
    customer = [m["content"] for m in history["messages"] if m["role"] == "user"]
    assert customer == PAYLOADS[:4]

    ticket_id, _, _ = open_ticket(api, subject=PAYLOADS[1], description=PAYLOADS[4])
    stored = api.get(f"{TICKETS}/{ticket_id}").json()
    assert (stored["subject"], stored["description"]) == (PAYLOADS[1], PAYLOADS[4])
    assert "support_tickets" in table_counts(api)


def test_like_wildcards_in_admin_search_match_only_themselves(api: AdminApp) -> None:
    everything = api.get(FAQS).json()["total"]
    assert everything > 0
    for wildcard in ("%", "_", "%_%"):
        assert api.get(FAQS, params={"q": wildcard}).json()["total"] < everything


def test_foreign_keys_and_secure_delete_are_on_for_every_connection(api: AdminApp) -> None:
    with api.database.read() as db:
        assert db.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert db.execute(text("PRAGMA secure_delete")).scalar() == 1
        assert db.execute(text("PRAGMA foreign_key_list(support_tickets)")).fetchall()
    with pytest.raises(SQLAlchemyError), api.database.write() as db:
        db.execute(
            text(
                "INSERT INTO ticket_notes (note_id, ticket_id, author_id, body, created_at) "
                "VALUES (:n, :t, 'alex', 'orphan', '2026-01-01 00:00:00')"
            ),
            {"n": uuid.uuid4().hex, "t": uuid.uuid4().hex},
        )


def test_a_failed_transaction_leaves_no_partial_changes(api: AdminApp) -> None:
    open_ticket(api)
    before = table_counts(api)
    with pytest.raises(RuntimeError), api.database.write() as db:
        db.execute(text("DELETE FROM ticket_events"))
        db.execute(text("UPDATE support_tickets SET subject = 'tampered'"))
        raise RuntimeError("simulated failure mid-transaction")
    assert table_counts(api) == before
    assert api.get(TICKETS).json()["tickets"][0]["subject"] == "Parcel not delivered"


def test_database_constraints_refuse_invalid_rows(api: AdminApp) -> None:
    ticket_id, _, _ = open_ticket(api)
    for statement in (
        "UPDATE support_tickets SET status = 'hacked'",
        "UPDATE support_tickets SET ticket_type = 'refund'",
        "UPDATE faqs SET category = 'Nonsense'",
    ):
        with pytest.raises(SQLAlchemyError), api.database.write() as db:
            db.execute(text(statement))
    assert api.get(f"{TICKETS}/{ticket_id}").json()["status"] == "open"


def test_the_audit_log_is_append_only_in_the_database(api: AdminApp) -> None:
    api.patch(f"{FAQS}/faq_101", {"expected_version": 1, "answer": "Changed answer."})
    for statement in (
        "UPDATE admin_audit_events SET actor_id = 'nobody'",
        "DELETE FROM admin_audit_events",
    ):
        with pytest.raises(SQLAlchemyError), api.database.write() as db:
            db.execute(text(statement))
    assert api.get(f"{ADMIN}/audit").json()["total"] >= 1


def test_a_repeated_ticket_request_creates_one_ticket(api: AdminApp) -> None:
    sid, token = start(api)
    body = ticket_body(sid)
    first = api.client.post(TICKETS, json=body, headers=bearer(token))
    again = api.client.post(TICKETS, json=body, headers=bearer(token))
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.headers["Idempotent-Replayed"] == "true"
    assert first.json()["ticket_id"] == again.json()["ticket_id"]
    changed = api.client.post(TICKETS, json={**body, "subject": "Different"}, headers=bearer(token))
    assert code(changed, 422) == "TICKET_REQUEST_CONFLICT"
    assert api.get(TICKETS).json()["total"] == 1


def test_concurrent_staff_updates_do_not_overwrite_each_other(api: AdminApp) -> None:
    ticket_id, _, _ = open_ticket(api)
    path = f"{TICKETS}/{ticket_id}/assignment"
    seen = {"expected_assigned_to": None, "expected_status": "open"}
    first = api.client.put(path, json={"assigned_to": "maria", **seen}, headers=bearer(MARIA))
    second = api.client.put(path, json={"assigned_to": "alex", **seen}, headers=bearer(ALEX))
    assert first.status_code == 200
    assert code(second, 409) == "TICKET_CHANGED"
    assert api.get(f"{TICKETS}/{ticket_id}").json()["assigned_to"] == "maria"

    faq = {"expected_version": 1}
    assert api.patch(f"{FAQS}/faq_101", {**faq, "answer": "First edit."}).status_code == 200
    assert code(api.patch(f"{FAQS}/faq_101", {**faq, "answer": "Lost edit."}), 409) == (
        "FAQ_VERSION_CONFLICT"
    )


# =============================================================================
# Task 6: secrets and configuration
# =============================================================================

SECRET_PATTERNS = {
    # Real keys are 48+ characters; the tests' fakes are short or start "sk-test-".
    "OpenAI key": re.compile(r"\bsk-[A-Za-z0-9_-]{40,}"),
    "staff token": re.compile(r"\bstf_[A-Za-z0-9_-]{43}\b"),
    "conversation token": re.compile(r"\bcst_[A-Za-z0-9_-]{43}\b"),
    "SHA-256 hash": re.compile(r"\b[0-9a-f]{60,64}\b"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


def env_example() -> dict[str, str]:
    values = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def test_env_example_holds_placeholders_only() -> None:
    """SEC-05 regression: a real staff entry was added to .env.example."""
    content = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name, pattern in SECRET_PATTERNS.items():
        assert not pattern.search(content), f".env.example contains a {name}"
    values = env_example()
    assert values["OPENAI_API_KEY"] == ""
    assert values["STAFF_TOKEN_HASHES"] == "[]"  # noqa: S105 - the empty placeholder
    assert "password" not in values.get("DATABASE_URL", "").lower()


def test_local_secret_files_and_databases_are_ignored() -> None:
    rules = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for rule in (".env", ".env.*", "!.env.example", ".streamlit/secrets.toml", "*.db", "*.log"):
        assert rule in rules, rule


def git_tracked_files() -> list[str]:
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    result = subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [git, "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return result.stdout.splitlines()


def test_no_secret_files_or_databases_are_tracked() -> None:
    tracked = git_tracked_files()
    forbidden = re.compile(
        r"(^|/)\.env$|\.env\.(?!example$)|secrets\.toml$|\.(db|sqlite3?|pem|key)$"
    )
    assert [path for path in tracked if forbidden.search(path)] == []


def test_tracked_files_contain_no_live_credentials() -> None:
    """Test fixtures may hold obviously fake keys (``sk-test-...``); nothing else may."""
    fake = re.compile(r"sk-(proj-)?test", re.IGNORECASE)
    findings = []
    for path in git_tracked_files():
        file = ROOT / path
        if file.suffix not in {".py", ".md", ".toml", ".json", ".txt", ".ps1", ".example", ""}:
            continue
        if not file.is_file():
            continue
        content = file.read_text(encoding="utf-8", errors="ignore")
        for match in SECRET_PATTERNS["OpenAI key"].finditer(content):
            if not fake.match(match.group(0)):
                findings.append(path)
        if SECRET_PATTERNS["private key"].search(content):
            findings.append(path)
    assert findings == []


def test_the_api_key_never_appears_in_settings_output() -> None:
    key = "sk-test-SECRET-should-not-leak-0123456789"
    settings = Settings(_env_file=None, openai_api_key=SecretStr(key))
    for rendered in (repr(settings), str(settings), str(settings.safe_summary())):
        assert key not in rendered


def test_responses_never_echo_credentials(api: AdminApp) -> None:
    sid, token = start(api)
    staff = generate_staff_token()
    for response in (
        api.client.get(f"{CONVERSATIONS}/{uuid.uuid4()}", headers=bearer(token)),
        api.client.get(FAQS, headers=bearer(staff)),
        api.client.get(f"{CONVERSATIONS}/{sid}", params={"token": token}, headers=bearer(token)),
        api.client.post(TICKETS, json={"bad": token}, headers=bearer(token)),
    ):
        assert token not in response.text and staff not in response.text
        assert all(token not in value for value in response.headers.values())


# =============================================================================
# Task 7: logging and privacy
# =============================================================================


def test_staff_tokens_are_redacted_from_log_text_even_without_bearer() -> None:
    """SEC-03 regression: only ``cst_`` tokens had a standalone redaction pattern."""
    staff, customer = generate_staff_token(), generate_access_token()
    line = f"pasted {staff} and {customer}; Authorization: Bearer {staff}; mail {EMAIL}"
    redacted = redact(line)
    assert staff not in redacted and customer not in redacted and EMAIL not in redacted
    assert redacted.count(REDACTED) >= 3


def test_a_full_support_flow_logs_no_credentials_or_personal_data(
    api: AdminApp, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    message = f"Hi, I'm Synthetic Person, call me on {PHONE} or mail {EMAIL} about order 98765"
    sid, token = start(api)
    api.client.post(f"{CONVERSATIONS}/{sid}/chat", json={"message": message}, headers=bearer(token))
    ticket_id, _, ticket_token = open_ticket(
        api,
        description=message,
        contact={
            "name": "Synthetic Person",
            "email": EMAIL,
            "phone": PHONE,
            "contact_preference": "phone",
            "consent_given": True,
        },
    )
    api.get(f"{TICKETS}/{ticket_id}")
    api.get(f"{TICKETS}/{ticket_id}", token=generate_staff_token())  # refused, audited
    api.client.post(TICKETS, json={"contact": {"email": EMAIL}}, headers=bearer(token))  # 422
    api.client.get(f"{CONVERSATIONS}/{sid}", params={"access_token": token})  # 400

    logged = caplog.text
    for secret in (token, ticket_token, ALEX, MARIA, EMAIL, PHONE, "Synthetic Person", "98765"):
        assert secret not in logged, secret


def test_customer_facing_errors_carry_no_diagnostics(api: AdminApp) -> None:
    sid, token = start(api)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database password=hunter2 at /srv/app/secret.py line 7")

    api.app.state.customer_chat_service.chat = broken
    response = api.client.post(
        f"{CONVERSATIONS}/{sid}/chat", json={"message": "hello"}, headers=bearer(token)
    )
    assert code(response, 500) == "INTERNAL_ERROR"
    for leak in ("hunter2", "secret.py", "RuntimeError", "Traceback"):
        assert leak not in response.text


# =============================================================================
# Task 9: CORS, headers and resource limits
# =============================================================================


def test_security_headers_are_on_every_kind_of_response(api: AdminApp) -> None:
    sid, token = start(api)
    limit = api.app.state.settings.max_request_body_bytes
    responses = {
        "200": api.client.get("/api/v1/health"),
        "401": api.client.get(f"{CONVERSATIONS}/{sid}"),
        "404": api.client.get("/api/v1/nowhere"),
        "413": api.client.post(
            f"{CONVERSATIONS}/{sid}/chat",
            content=b"x" * (limit + 1),
            headers={**bearer(token), "Content-Type": "application/json"},
        ),
        "422": api.client.post(f"{CONVERSATIONS}/{sid}/chat", json={}, headers=bearer(token)),
    }
    for status, response in responses.items():
        assert response.status_code == int(status), status
        assert response.headers["X-Content-Type-Options"] == "nosniff", status
        assert response.headers["X-Frame-Options"] == "DENY", status
        assert response.headers["Referrer-Policy"] == "no-referrer", status
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"], status


def test_the_interactive_docs_keep_working_without_the_strict_csp(api: AdminApp) -> None:
    docs = api.client.get("/docs")
    assert docs.status_code == 200
    assert "Content-Security-Policy" not in docs.headers
    assert docs.headers["X-Frame-Options"] == "DENY"


def test_a_500_carries_the_security_headers(api: AdminApp) -> None:
    sid, token = start(api)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    api.app.state.customer_chat_service.chat = broken
    response = api.client.post(
        f"{CONVERSATIONS}/{sid}/chat", json={"message": "hi"}, headers=bearer(token)
    )
    assert response.status_code == 500
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_cors_allows_only_configured_origins_without_credentials(
    make_admin_app: Callable[..., AdminApp],  # noqa: F811
) -> None:
    api = make_admin_app("cors", cors_origins=["https://shop.example"], rate_limit_enabled=False)
    preflight = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "Authorization, Content-Type",
    }
    allowed = api.client.options(
        CONVERSATIONS, headers={"Origin": "https://shop.example", **preflight}
    )
    assert allowed.headers["access-control-allow-origin"] == "https://shop.example"
    assert "access-control-allow-credentials" not in allowed.headers
    for origin in ("https://evil.example", "null", "https://shop.example.evil.example"):
        refused = api.client.options(CONVERSATIONS, headers={"Origin": origin, **preflight})
        assert "access-control-allow-origin" not in refused.headers, origin
    # A browser request from a refused origin still needs a valid token: CORS is not auth.
    sid, _ = start(api)
    direct = api.client.get(f"{CONVERSATIONS}/{sid}", headers={"Origin": "https://evil.example"})
    assert code(direct, 401) == "UNAUTHORIZED_SESSION"


@pytest.mark.parametrize("origins", [["*"], ["https://shop.example/"], ["shop.example"]])
def test_permissive_or_malformed_cors_origins_are_refused(origins: list[str]) -> None:
    with pytest.raises(ValueError, match="CORS origin"):
        Settings(_env_file=None, cors_origins=origins)


@pytest.mark.parametrize(
    ("setting", "too_large"),
    [
        ("max_request_body_bytes", 1_048_577),
        ("max_customer_message_characters", 1_000_000),
        ("openai_max_output_tokens", 4_097),
        ("openai_max_retries", 6),
        ("openai_timeout_seconds", 301),
        ("llm_context_max_faqs", 11),
        ("llm_context_max_chars", 50_001),
        ("conversation_history_max_messages", 51),
        ("conversation_history_max_chars", 50_001),
        ("query_rewrite_max_turns", 21),
        ("hybrid_candidate_depth", 51),
        ("answer_candidates", 51),
        ("rate_limit_requests_per_minute", 100_001),
    ],
)
def test_every_resource_limit_has_a_ceiling(setting: str, too_large: float) -> None:
    """No configuration can make sizes, tokens, retries or timeouts unbounded."""
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{setting: too_large})


def test_history_retrieval_is_paged_and_bounded(api: AdminApp) -> None:
    sid, token = start(api)
    page = api.client.get(
        f"{CONVERSATIONS}/{sid}/messages", params={"limit": 100}, headers=bearer(token)
    )
    assert page.status_code == 200
    too_many = api.client.get(
        f"{CONVERSATIONS}/{sid}/messages", params={"limit": 101}, headers=bearer(token)
    )
    assert_validation_error(too_many)


def test_frontend_retries_and_waits_are_bounded() -> None:
    from frontend.config import FrontendSettings

    for setting, value in (
        ("backend_max_retries", 6),
        ("backend_max_retry_wait_seconds", 31),
        ("backend_read_timeout_seconds", 601),
        ("admin_idle_timeout_minutes", 481),
        ("admin_max_session_hours", 25),
    ):
        with pytest.raises(ValueError):
            FrontendSettings(_env_file=None, **{setting: value})
