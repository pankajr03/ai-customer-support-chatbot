"""Human support and sales contact on the Streamlit page (Stage 10, Tasks 11-13).

Three layers: form validation and ``SupportState`` on their own; the actions against
the real backend (in process); and the page itself with mocked HTTP (``AppTest``, see
tests/test_frontend_page.py for what AppTest can and cannot show).
"""

import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from app.models import support as backend_support
from app.models.support_api import SupportTicketCreateRequest
from frontend import actions, resources
from frontend import support as fs
from frontend.api_client import (
    ApiError,
    BackendStatus,
    ChatApiClient,
    ErrorKind,
    TicketCreated,
)
from frontend.components.support import FIELD_KEYS, STATUS_LABELS
from frontend.config import FrontendSettings
from frontend.state import ChatState
from frontend.support import SUPPORT_KEY, SupportForm, SupportState, validate_form
from tests.test_frontend_app import APP
from tests.test_frontend_page import CREATED, SESSION, reply

TICKET_ID = str(uuid.uuid4())


def a_form(**changes: Any) -> SupportForm:
    values: dict[str, Any] = {
        "ticket_type": "support",
        "name": "Alex Rivera",
        "email": "alex@example.com",
        "phone": "",
        "contact_preference": "email",
        "subject": "Damaged parcel",
        "description": "The parcel arrived damaged.",
        "consent": True,
        "lead_category": None,
    }
    values.update(changes)
    return SupportForm(**values)


def created_body(body: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {
        "ticket_id": TICKET_ID,
        "reference_number": "SUP-7K3M-9QX2",
        "ticket_type": body.get("ticket_type", "support"),
        "lead_category": body.get("lead_category"),
        "status": "open",
        "status_message": backend_support.CUSTOMER_STATUS_MESSAGES[
            backend_support.TicketStatus.OPEN
        ],
        "subject": body.get("subject", "Damaged parcel"),
        "created_at": "2026-10-16T09:31:00Z",
        "outcome": "created",
        "message": "Your support request has been recorded. Your reference number is "
        "SUP-7K3M-9QX2; please keep it for your records.",
        "expected_response": None,
        **fields,
    }


# --- Validation -------------------------------------------------------------------------


def test_the_frontend_limits_match_the_backend() -> None:
    assert fs.MAX_NAME_LENGTH == backend_support.MAX_NAME_LENGTH
    assert fs.MAX_PHONE_LENGTH == backend_support.MAX_PHONE_LENGTH
    assert fs.MAX_SUBJECT_LENGTH == backend_support.MAX_SUBJECT_LENGTH
    assert fs.MAX_DESCRIPTION_LENGTH == backend_support.MAX_DESCRIPTION_LENGTH
    assert (fs.MIN_PHONE_DIGITS, fs.MAX_PHONE_DIGITS) == (
        backend_support.MIN_PHONE_DIGITS,
        backend_support.MAX_PHONE_DIGITS,
    )
    assert set(fs.LEAD_CATEGORIES) == {c.value for c in backend_support.LeadCategory}
    assert set(fs.CONTACT_PREFERENCES) == {p.value for p in backend_support.ContactPreference}
    assert set(STATUS_LABELS) == {s.value for s in backend_support.TicketStatus}


def test_a_complete_form_is_valid() -> None:
    assert validate_form(a_form().cleaned()) == {}
    assert validate_form(a_form(phone="+44 20 7946 0958", contact_preference="phone")) == {}


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"name": ""}, "name"),
        ({"name": "x" * 101}, "name"),
        ({"name": "Alex\nRivera"}, "name"),
        ({"email": ""}, "email"),
        ({"email": "alex"}, "email"),
        ({"phone": "abc"}, "phone"),
        ({"phone": "12"}, "phone"),
        ({"contact_preference": "phone"}, "phone"),
        ({"contact_preference": "fax"}, "contact_preference"),
        ({"subject": ""}, "subject"),
        ({"description": ""}, "description"),
        ({"description": "x" * 2001}, "description"),
        ({"consent": False}, "consent"),
        ({"ticket_type": "sales", "lead_category": None}, "lead_category"),
    ],
)
def test_invalid_forms_name_the_field(changes: dict[str, Any], field: str) -> None:
    assert field in validate_form(a_form(**changes).cleaned())


def test_the_body_matches_the_backend_request_and_confirms() -> None:
    body = a_form(ticket_type="sales", lead_category="bulk_order").cleaned().body()

    parsed = SupportTicketCreateRequest.model_validate(
        {**body, "session_id": SESSION, "client_request_id": str(uuid.uuid4())}
    )
    assert parsed.confirmed is True and parsed.lead_category == "bulk_order"
    assert body["contact"]["phone"] is None  # blank phone sent as null


def test_support_requests_never_carry_a_lead_category() -> None:
    assert a_form(lead_category="bulk_order").cleaned().body()["lead_category"] is None


# --- SupportState -----------------------------------------------------------------------


def test_the_form_opens_only_when_asked() -> None:
    support = SupportState()

    assert not support.form_open
    support.open_form("support")
    assert support.form_open and support.form_type == "support"
    support.close_form()
    assert not support.form_open


def test_an_invalid_form_is_not_sent() -> None:
    support = SupportState()
    support.open_form("support")

    assert support.queue_submission(a_form(consent=False)) is None
    assert not support.is_submitting and "consent" in support.form_problems


def test_a_retry_with_the_same_details_keeps_the_id() -> None:
    support = SupportState()
    support.open_form("support")
    first = support.queue_submission(a_form())
    assert first is not None and support.is_submitting
    assert support.queue_submission(a_form()) is None  # already on its way
    support.record_failure(ApiError(ErrorKind.TIMEOUT))

    again = support.queue_submission(a_form(name="  Alex Rivera "))  # same once trimmed

    assert again is not None and again.client_request_id == first.client_request_id


def test_changed_details_are_a_new_request() -> None:
    support = SupportState()
    support.open_form("support")
    first = support.queue_submission(a_form())
    support.record_failure(ApiError(ErrorKind.TIMEOUT))

    changed = support.queue_submission(a_form(subject="Something else"))

    assert changed is not None and changed.client_request_id != first.client_request_id


@pytest.mark.parametrize(
    ("kind", "keeps"),
    [
        (ErrorKind.TIMEOUT, True),
        (ErrorKind.SERVICE_UNAVAILABLE, True),
        (ErrorKind.RATE_LIMITED, True),
        (ErrorKind.INVALID_MESSAGE, False),
        (ErrorKind.REQUEST_CONFLICT, False),
        (ErrorKind.SESSION_EXPIRED, False),
    ],
)
def test_failures_keep_the_request_only_when_a_retry_can_help(kind: ErrorKind, keeps: bool) -> None:
    support = SupportState()
    support.open_form("support")
    support.queue_submission(a_form())

    support.record_failure(ApiError(kind))

    assert support.can_retry is keeps
    assert support.retry() is keeps


def test_a_confirmed_success_closes_the_form_and_records_the_ticket() -> None:
    support = SupportState()
    support.open_form("support")
    support.queue_submission(a_form())

    support.record_created(TicketCreated.model_validate(created_body({})))

    assert not support.form_open and not support.is_submitting and support.clear_form
    assert support.latest is not None and support.latest.reference_number == "SUP-7K3M-9QX2"
    assert support.has_ticket("support") and not support.has_ticket("sales")


def test_offers_hide_when_dismissed_or_already_used() -> None:
    support = SupportState()

    assert support.offer_visible("m1", "support")
    support.dismiss("m1")
    assert not support.offer_visible("m1", "support")
    assert support.offer_visible("m2", "support")
    support.open_form("support")
    support.queue_submission(a_form())
    support.record_created(TicketCreated.model_validate(created_body({})))
    assert not support.offer_visible("m2", "support")  # a request was sent already
    assert support.offer_visible("m2", "sales")


def test_requests_belong_to_their_conversation() -> None:
    support = SupportState()
    first = uuid.uuid4()
    support.follow(first)
    support.open_form("support")
    support.queue_submission(a_form())
    support.record_created(TicketCreated.model_validate(created_body({})))

    support.follow(first)
    assert support.tickets
    support.follow(uuid.uuid4())  # a new conversation
    assert support.tickets == [] and not support.form_open
    support.follow(None)  # deleted
    assert support.session_id is None


# --- Actions against the real backend ---------------------------------------------------


@pytest.fixture
def live(make_backend: Callable[..., TestClient]) -> tuple[ChatApiClient, ChatState]:
    client = ChatApiClient(http_client=make_backend(), sleep=lambda seconds: None)
    state = ChatState()
    actions.start_conversation(state, client)
    return client, state


def test_a_request_is_created_and_its_status_checked(
    live: tuple[ChatApiClient, ChatState],
) -> None:
    client, state = live
    support = SupportState()
    support.open_form("support")
    support.queue_submission(a_form())

    actions.submit_support_request(state, support, client)

    ticket = support.latest
    assert ticket is not None and ticket.reference_number.startswith("SUP-")
    assert ticket.status == "open" and not support.is_submitting
    actions.check_ticket_status(state, support, client, ticket.ticket_id)
    assert support.status_error is None and ticket.status == "open"


def test_a_sales_inquiry_is_stored_as_a_lead(live: tuple[ChatApiClient, ChatState]) -> None:
    client, state = live
    support = SupportState()
    support.open_form("sales")
    support.queue_submission(a_form(ticket_type="sales", lead_category="partnership"))

    actions.submit_support_request(state, support, client)

    ticket = support.latest
    assert ticket is not None and ticket.reference_number.startswith("SAL-")
    assert ticket.lead_category == "partnership"


def test_a_retry_after_a_lost_reply_creates_one_ticket(
    live: tuple[ChatApiClient, ChatState], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, state = live
    support = SupportState()
    support.open_form("support")
    support.queue_submission(a_form())
    real = client.create_ticket
    ids: list[uuid.UUID] = []

    def lost_reply(credentials: Any, body: dict[str, Any], request_id: uuid.UUID) -> Any:
        ids.append(request_id)
        real(credentials, body, request_id)  # stored on the backend ...
        raise ApiError(ErrorKind.TIMEOUT)  # ... but the page never hears

    monkeypatch.setattr(client, "create_ticket", lost_reply)
    actions.submit_support_request(state, support, client)
    assert support.can_retry and support.latest is None
    monkeypatch.setattr(client, "create_ticket", real)

    support.retry()
    actions.submit_support_request(state, support, client)

    assert support.latest is not None and support.latest.already_sent is False
    assert support.pending is None and len(ids) == 1
    assert support.latest.ticket_id  # the original ticket, replayed


def test_another_conversations_ticket_cannot_be_checked(
    live: tuple[ChatApiClient, ChatState], make_backend: Callable[..., TestClient]
) -> None:
    client, state = live
    support = SupportState()
    support.open_form("support")
    support.queue_submission(a_form())
    actions.submit_support_request(state, support, client)
    ticket = support.latest
    assert ticket is not None
    other = ChatState()
    actions.start_conversation(other, client)

    actions.check_ticket_status(other, support, client, ticket.ticket_id)

    assert support.status_error is not None
    assert support.status_error.kind is ErrorKind.TICKET_NOT_FOUND


# --- The page ---------------------------------------------------------------------------


class Backend:
    """Plays the backend: chat replies (each a dict of reply fields) and tickets (each a
    dict of created fields or an httpx.Response)."""

    def __init__(self, chat: list[dict[str, Any]], tickets: list[Any] | None = None) -> None:
        self.chat = list(chat)
        self.tickets = list(tickets or [])
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []
        self.status = "open"

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        path = request.url.path
        if path.endswith("/conversations"):
            return httpx.Response(201, json=CREATED)
        if path.endswith("/chat"):
            return httpx.Response(200, json=reply(body or {}, **self.chat.pop(0)))
        if path.endswith("/support/tickets") and request.method == "POST":
            step = self.tickets.pop(0) if self.tickets else {}
            if isinstance(step, httpx.Response):
                return step
            return httpx.Response(201, json=created_body(body or {}, **step))
        if "/support/tickets/" in path and request.method == "GET":
            assert request.url.params["session_id"] == SESSION
            ticket = created_body({})
            ticket.pop("outcome"), ticket.pop("message"), ticket.pop("expected_response")
            ticket["status"] = self.status
            ticket["status_message"] = backend_support.CUSTOMER_STATUS_MESSAGES[
                backend_support.TicketStatus(self.status)
            ]
            ticket["updated_at"] = "2026-10-16T10:00:00Z"
            return httpx.Response(200, json=ticket)
        raise AssertionError(f"unexpected {request.method} {path}")

    def ticket_posts(self) -> list[dict[str, Any]]:
        return [
            b for m, p, b in self.requests if p.endswith("/support/tickets") and b and m == "POST"
        ]


@pytest.fixture
def open_page(monkeypatch: pytest.MonkeyPatch) -> Callable[[Backend], AppTest]:
    def _open(backend: Backend) -> AppTest:
        transport = httpx.MockTransport(backend.handle)
        client = ChatApiClient(
            http_client=httpx.Client(base_url="http://backend.test", transport=transport),
            sleep=lambda seconds: None,
        )
        status = lambda _client: BackendStatus.ONLINE  # noqa: E731
        status.clear = lambda: None  # type: ignore[attr-defined]
        monkeypatch.setattr(resources, "get_api_client", lambda: client)
        monkeypatch.setattr(resources, "backend_status", status)
        monkeypatch.setattr(resources, "get_settings", lambda: FrontendSettings(_env_file=None))
        page = AppTest.from_file(str(APP), default_timeout=30)
        page.run()
        assert not page.exception
        return page

    return _open


HUMAN_OFFER = {
    "decision": "no_match",
    "answer": "Sorry, I couldn't find a reliable answer.",
    "generation_method": "fallback",
    "escalation_offer": {"reason": "customer_requested", "ticket_type": "support"},
}
SALES_OFFER = {
    "answer": "We offer volume discounts.",
    "escalation_offer": {"reason": "sales_inquiry", "ticket_type": "sales"},
}


def say(page: AppTest, text: str) -> AppTest:
    page.chat_input[0].set_value(text).run()
    assert not page.exception
    return page


def button(page: AppTest, label: str) -> Any:
    matches = [b for b in page.button if b.label == label]
    assert matches, f"no button {label!r} in {[b.label for b in page.button]}"
    return matches[0]


def labels(page: AppTest) -> list[str]:
    return [b.label for b in page.button]


def everything(page: AppTest) -> str:
    parts = [m.value for m in page.markdown] + [i.value for i in page.info]
    parts += [s.value for s in page.success] + [c.value for c in page.caption]
    parts += [e.value for e in page.error] + labels(page)
    return " ".join(str(part) for part in parts)


def fill(page: AppTest, **values: Any) -> AppTest:
    defaults = {
        "name": "Alex Rivera",
        "email": "alex@example.com",
        "subject": "Damaged parcel",
        "description": "The parcel arrived damaged.",
        "consent": True,
    }
    defaults.update(values)
    for name, value in defaults.items():
        if name == "consent":
            page.checkbox(key=FIELD_KEYS["consent"]).set_value(value)
        elif name == "description":
            page.text_area(key=FIELD_KEYS["description"]).input(value)
        elif name == "lead_category":
            page.selectbox(key=FIELD_KEYS["lead_category"]).select(value)
        elif name == "contact_preference":
            page.radio(key=FIELD_KEYS["contact_preference"]).set_value(value)
        else:
            page.text_input(key=FIELD_KEYS[name]).input(value)
    return page


def send(page: AppTest) -> AppTest:
    button(page, "Send request").click().run()
    assert not page.exception
    return page


def support_of(page: AppTest) -> SupportState:
    return page.session_state[SUPPORT_KEY]


def test_no_form_until_the_customer_asks(open_page: Callable[[Backend], AppTest]) -> None:
    page = open_page(Backend([{"answer": "An answer."}]))
    say(page, "How do I reset my password?")

    assert not page.text_input  # no form
    assert "Contact human support" in labels(page)  # but the choice is always there
    assert "Sales inquiry" in labels(page)


def test_an_offer_after_asking_for_a_human_opens_the_form(
    open_page: Callable[[Backend], AppTest],
) -> None:
    page = open_page(Backend([HUMAN_OFFER]))
    say(page, "I want to speak with a human.")

    assert "Would you like to send a request to our support team?" in everything(page)
    button(page, "Contact human support").click().run()

    assert support_of(page).form_type == "support"
    assert page.text_input(key=FIELD_KEYS["name"]).value == ""  # nothing pre-filled
    assert page.text_input(key=FIELD_KEYS["email"]).value == ""
    assert page.checkbox(key=FIELD_KEYS["consent"]).value is False  # not preselected


def test_an_offer_can_be_declined(open_page: Callable[[Backend], AppTest]) -> None:
    page = open_page(Backend([HUMAN_OFFER]))
    say(page, "I want to speak with a human.")

    button(page, "No thanks").click().run()

    assert "No thanks" not in labels(page)
    assert not support_of(page).form_open


def test_a_complete_request_shows_the_reference_after_confirmed_success(
    open_page: Callable[[Backend], AppTest],
) -> None:
    backend = Backend([HUMAN_OFFER])
    page = open_page(backend)
    say(page, "I want to speak with a human.")
    button(page, "Contact human support").click().run()

    send(fill(page))

    [posted] = backend.ticket_posts()
    assert posted["confirmed"] is True and posted["contact"]["consent_given"] is True
    assert posted["session_id"] == SESSION
    text = everything(page)
    assert "SUP\\-7K3M\\-9QX2" in text or "SUP-7K3M-9QX2" in text
    assert "Recorded" in text  # the status, not "handled"
    assert "has not picked it up yet" in text
    assert not support_of(page).form_open


def test_an_incomplete_form_is_not_sent(open_page: Callable[[Backend], AppTest]) -> None:
    backend = Backend([])
    page = open_page(backend)
    button(page, "Contact human support").click().run()

    send(fill(page, consent=False, email="not-an-email"))

    assert backend.ticket_posts() == []
    assert "Please check the highlighted fields." in everything(page)
    problems = support_of(page).form_problems
    assert {"consent", "email"} <= set(problems)


def test_a_failed_request_keeps_its_id_for_the_retry(
    open_page: Callable[[Backend], AppTest],
) -> None:
    unavailable = httpx.Response(
        503,
        json={"error": {"code": "SERVICE_UNAVAILABLE", "message": "m", "request_id": "r1"}},
    )
    backend = Backend([], tickets=[unavailable, unavailable, unavailable, {}])
    page = open_page(backend)
    button(page, "Contact human support").click().run()

    send(fill(page))  # 1 attempt + 2 automatic retries, all 503
    assert "temporarily unavailable" in everything(page)
    button(page, "Try again").click().run()

    ids = {body["client_request_id"] for body in backend.ticket_posts()}
    assert len(backend.ticket_posts()) == 4 and len(ids) == 1
    assert support_of(page).latest is not None


def test_a_sales_inquiry_has_its_own_form(open_page: Callable[[Backend], AppTest]) -> None:
    backend = Backend([SALES_OFFER], tickets=[{"reference_number": "SAL-7K3M-9QX2"}])
    page = open_page(backend)
    say(page, "Do you offer bulk orders?")

    button(page, "Contact our sales team").click().run()
    send(fill(page, lead_category="bulk_order"))

    [posted] = backend.ticket_posts()
    assert (posted["ticket_type"], posted["lead_category"]) == ("sales", "bulk_order")


def test_checking_the_status_distinguishes_recorded_from_handled(
    open_page: Callable[[Backend], AppTest],
) -> None:
    backend = Backend([])
    page = open_page(backend)
    button(page, "Contact human support").click().run()
    send(fill(page))

    backend.status = "assigned"
    button(page, "Check status").click().run()

    text = everything(page)
    assert "Assigned to our team" in text
    assert "A member of our team has taken on your request." in text


def test_the_page_never_claims_a_person_joined_or_shows_staff_controls(
    open_page: Callable[[Backend], AppTest],
) -> None:
    page = open_page(Backend([HUMAN_OFFER]))
    say(page, "I want to speak with a human.")
    button(page, "Contact human support").click().run()
    send(fill(page))

    text = everything(page).lower()
    for claim in ("has joined", "agent is", "connected you", "within 24", "notified"):
        assert claim not in text, claim
    for control in ("Assign", "Resolve", "Change status", "All tickets"):
        assert control not in labels(page)
    assert "nobody joins this chat" in text


def test_a_new_conversation_starts_without_the_old_requests(
    open_page: Callable[[Backend], AppTest],
) -> None:
    page = open_page(Backend([{"answer": "An answer."}]))
    say(page, "Hello")
    button(page, "Contact human support").click().run()
    send(fill(page))
    assert support_of(page).tickets

    other = dict(CREATED, session_id=str(uuid.uuid4()))
    support_of(page).follow(uuid.UUID(other["session_id"]))

    assert support_of(page).tickets == []
