"""Human support and sales contact on the page (Stage 10): state and form validation.

No Streamlit here, so every flow can be tested directly. ``SupportState`` is kept per
browser session (``SUPPORT_KEY`` in ``st.session_state``) and belongs to one
conversation: when the page switches to another conversation, or has none, it is
cleared (``follow``), because tickets can only be looked up with that conversation's
token.

When the form appears
---------------------
Only when the customer chooses it: from a reply that offers it (the backend's
``escalation_offer``, e.g. after "I want to speak with a human" or a second unanswered
question), or from the sidebar ("Contact human support", "Sales inquiry"). It is never
opened automatically, and an offer can be dismissed ("No thanks"). Nothing is
pre-filled: the page knows nothing about the customer.

Submitting
----------
1. ``queue_submission`` validates the fields with the backend's rules (so the customer
   sees problems at once) and, if they are valid, makes the request pending with a
   ``client_request_id``. While it is being sent, the form cannot be submitted again.
2. A retry after a failure whose outcome is unknown (timeout, broken connection) or
   temporary (busy, rate limit, 503) reuses the same ``client_request_id`` *as long as
   the details are unchanged*, so the backend creates at most one ticket. Changed
   details are a new request with a new ID.
3. Only a confirmed success (the backend's reply) shows the reference number.

Pressing **Send request** is the customer's explicit confirmation (sent as
``confirmed: true``); the separate, unticked consent box is their consent to storing the
contact details.
"""

import hashlib
import json
import re
import unicodedata
import uuid
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import streamlit as st

from frontend.api_client import RETRYABLE, ApiError, CustomerTicket, TicketCreated
from frontend.state import ErrorNotice, strip_message

# The backend's limits (app.models.support); tests/test_frontend_support.py keeps them equal.
MAX_NAME_LENGTH = 100
MAX_PHONE_LENGTH = 32
MAX_SUBJECT_LENGTH = 150
MAX_DESCRIPTION_LENGTH = 2_000
MIN_PHONE_DIGITS = 5
MAX_PHONE_DIGITS = 15
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE = re.compile(r"^\+?[0-9 ()./-]+?(?:\s*(?:ext\.?|x|#)\s*[0-9]{1,6})?$", re.IGNORECASE)
_PHONE_EXTENSION = re.compile(r"\s*(?:ext\.?|x|#)\s*[0-9]{1,6}$", re.IGNORECASE)

SUPPORT_KEY = "support_requests"
SUPPORT = "support"
SALES = "sales"
LEAD_CATEGORIES = {
    "general_sales": "General sales question",
    "bulk_order": "Bulk order",
    "business_pricing": "Business pricing",
    "partnership": "Partnership",
    "product_demo": "Product demonstration",
}
CONTACT_PREFERENCES = {"email": "Email", "phone": "Phone"}


class SubmitStatus(StrEnum):
    IDLE = "idle"
    SUBMITTING = "submitting"  # being sent; the form cannot be submitted again
    FAILED = "failed"  # see ``last_error``


@dataclass(frozen=True)
class SupportForm:
    """What the customer typed. ``ticket_type`` is support or sales."""

    ticket_type: str
    name: str
    email: str
    phone: str
    contact_preference: str
    subject: str
    description: str
    consent: bool
    lead_category: str | None = None

    def cleaned(self) -> "SupportForm":
        """Surrounding whitespace removed, as the backend does."""
        return SupportForm(
            ticket_type=self.ticket_type,
            name=strip_message(self.name),
            email=strip_message(self.email),
            phone=strip_message(self.phone),
            contact_preference=self.contact_preference,
            subject=strip_message(self.subject),
            description=strip_message(self.description),
            consent=self.consent,
            lead_category=self.lead_category if self.ticket_type == SALES else None,
        )

    def body(self) -> dict[str, Any]:
        """The request body (without session_id and client_request_id). ``confirmed`` is
        true because it is only built when the customer presses Send."""
        return {
            "ticket_type": self.ticket_type,
            "lead_category": self.lead_category,
            "subject": self.subject,
            "description": self.description,
            "contact": {
                "name": self.name,
                "email": self.email,
                "phone": self.phone or None,
                "contact_preference": self.contact_preference,
                "consent_given": self.consent,
            },
            "confirmed": True,
        }

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.body(), sort_keys=True).encode()).hexdigest()


def validate_form(form: SupportForm) -> dict[str, str]:
    """Problems with a cleaned form, by field (empty when it can be sent)."""
    problems: dict[str, str] = {}

    def text(field_name: str, value: str, label: str, limit: int, *, single_line: bool) -> None:
        allowed = frozenset() if single_line else frozenset("\t\n\r")
        if not value:
            problems[field_name] = f"Please enter {label}."
        elif len(value) > limit:
            problems[field_name] = f"Please shorten {label} to {limit:,} characters."
        elif any(unicodedata.category(c) == "Cc" and c not in allowed for c in value):
            problems[field_name] = f"Please remove special characters from {label}."

    text("name", form.name, "your name", MAX_NAME_LENGTH, single_line=True)
    if not form.email:
        problems["email"] = "Please enter your email address."
    elif not _EMAIL.match(form.email):
        problems["email"] = "Please enter a valid email address."
    if form.phone:
        digits = sum(c.isdigit() for c in _PHONE_EXTENSION.sub("", form.phone))
        if (
            len(form.phone) > MAX_PHONE_LENGTH
            or not _PHONE.match(form.phone)
            or not MIN_PHONE_DIGITS <= digits <= MAX_PHONE_DIGITS
        ):
            problems["phone"] = "Please enter a phone number with 5 to 15 digits."
    if form.contact_preference not in CONTACT_PREFERENCES:
        problems["contact_preference"] = "Please choose how we should contact you."
    elif form.contact_preference == "phone" and not form.phone:
        problems["phone"] = "Please enter a phone number, or choose email."
    text("subject", form.subject, "a short summary", MAX_SUBJECT_LENGTH, single_line=True)
    text(
        "description",
        form.description,
        "a description of what you need",
        MAX_DESCRIPTION_LENGTH,
        single_line=False,
    )
    if form.ticket_type == SALES and form.lead_category not in LEAD_CATEGORIES:
        problems["lead_category"] = "Please choose what your inquiry is about."
    if not form.consent:
        problems["consent"] = "Please agree to us storing your contact details, or cancel."
    return problems


@dataclass(frozen=True)
class PendingRequest:
    """A request being sent. Retries with the same details reuse ``client_request_id``."""

    client_request_id: uuid.UUID
    form: SupportForm
    fingerprint: str


@dataclass
class TicketRecord:
    """A request the backend confirmed, as the customer may see it."""

    ticket_id: uuid.UUID
    reference_number: str
    ticket_type: str
    lead_category: str | None
    status: str
    status_message: str
    subject: str
    message: str  # the backend's confirmation
    expected_response: str | None
    already_sent: bool = False  # the backend returned an earlier, still open ticket

    def update(self, ticket: CustomerTicket) -> None:
        self.status = ticket.status
        self.status_message = ticket.status_message


@dataclass
class SupportState:
    # The conversation these requests belong to.
    session_id: uuid.UUID | None = None
    form_type: str | None = None  # support / sales while the form is open
    pending: PendingRequest | None = None
    status: SubmitStatus = SubmitStatus.IDLE
    last_error: ErrorNotice | None = None
    form_problems: dict[str, str] = field(default_factory=dict)
    tickets: list[TicketRecord] = field(default_factory=list)
    # Replies whose offer the customer dismissed ("No thanks").
    dismissed: set[str] = field(default_factory=set)
    # Set after a success: the form's fields are cleared on the next run.
    clear_form: bool = False
    # A status check that failed (shown next to the tickets).
    status_error: ErrorNotice | None = None

    @property
    def form_open(self) -> bool:
        return self.form_type is not None

    @property
    def is_submitting(self) -> bool:
        return self.status is SubmitStatus.SUBMITTING

    @property
    def can_retry(self) -> bool:
        return self.status is SubmitStatus.FAILED and self.pending is not None

    @property
    def latest(self) -> TicketRecord | None:
        return self.tickets[-1] if self.tickets else None

    def has_ticket(self, ticket_type: str) -> bool:
        return any(ticket.ticket_type == ticket_type for ticket in self.tickets)

    def offer_visible(self, message_id: str, ticket_type: str) -> bool:
        """Whether a reply's offer is still shown: not dismissed, the form not already
        open, and no request of that kind sent in this conversation yet."""
        return (
            message_id not in self.dismissed
            and not self.form_open
            and not self.has_ticket(ticket_type)
        )

    # -- The form ----------------------------------------------------------------------

    def open_form(self, ticket_type: str) -> None:
        if self.is_submitting:
            return
        if self.form_type != ticket_type:
            self.pending = None  # a different kind of request is a new request
        self.form_type = ticket_type
        self.status = SubmitStatus.IDLE
        self.last_error = None
        self.form_problems = {}

    def close_form(self) -> None:
        if self.is_submitting:
            return
        self.form_type = None
        self.pending = None
        self.status = SubmitStatus.IDLE
        self.last_error = None
        self.form_problems = {}

    def dismiss(self, message_id: str) -> None:
        self.dismissed.add(message_id)

    def queue_submission(self, form: SupportForm) -> PendingRequest | None:
        """Validate and make the request pending (see the module docstring). Returns
        None if it is not sent: invalid, or a request is already on its way."""
        if self.is_submitting or not self.form_open:
            return None
        form = form.cleaned()
        self.form_problems = validate_form(form)
        if self.form_problems:
            self.last_error = None
            return None
        fingerprint = form.fingerprint()
        if self.pending is not None and self.pending.fingerprint == fingerprint:
            pending = self.pending  # a retry of the same request: same ID
        else:
            pending = PendingRequest(uuid.uuid4(), form, fingerprint)
        self.pending = pending
        self.status = SubmitStatus.SUBMITTING
        self.last_error = None
        return pending

    def retry(self) -> bool:
        """Send the failed request again, unchanged, with the same ID."""
        if not self.can_retry:
            return False
        self.status = SubmitStatus.SUBMITTING
        self.last_error = None
        return True

    # -- Results -----------------------------------------------------------------------

    def record_created(self, created: TicketCreated) -> None:
        if self.pending is None:  # pragma: no cover - only while a request is pending
            return
        if not any(ticket.ticket_id == created.ticket_id for ticket in self.tickets):
            self.tickets.append(
                TicketRecord(
                    ticket_id=created.ticket_id,
                    reference_number=created.reference_number,
                    ticket_type=created.ticket_type,
                    lead_category=created.lead_category,
                    status=created.status,
                    status_message=created.status_message,
                    subject=created.subject,
                    message=created.message,
                    expected_response=created.expected_response,
                    already_sent=created.outcome == "duplicate",
                )
            )
        self.pending = None
        self.form_type = None
        self.status = SubmitStatus.IDLE
        self.last_error = None
        self.form_problems = {}
        self.clear_form = True

    def record_failure(self, error: ApiError) -> None:
        """A retryable failure keeps the request (and its ID) for "Try again"; after any
        other the backend stored nothing, and the next submission gets a new ID."""
        self.last_error = ErrorNotice.from_error(error)
        self.status = SubmitStatus.FAILED
        if error.kind not in RETRYABLE:
            self.pending = None

    def record_status(self, ticket: CustomerTicket) -> None:
        for record in self.tickets:
            if record.ticket_id == ticket.ticket_id:
                record.update(ticket)
        self.status_error = None

    def record_status_failure(self, error: ApiError) -> None:
        self.status_error = ErrorNotice.from_error(error)

    def follow(self, session_id: uuid.UUID | None) -> None:
        """Belong to the page's current conversation. Another conversation (or none:
        deleted, lost) starts empty: the old requests can no longer be looked up."""
        if session_id != self.session_id:
            fresh = SupportState(session_id=session_id)
            self.__dict__.update(fresh.__dict__)


def get_support_state(store: MutableMapping[str, Any] | None = None) -> SupportState:
    """This browser session's support state (``store`` defaults to st.session_state)."""
    if store is None:
        store = st.session_state
    if SUPPORT_KEY not in store:
        store[SUPPORT_KEY] = SupportState()
    return store[SUPPORT_KEY]
