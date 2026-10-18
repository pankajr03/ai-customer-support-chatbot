"""Lead capture and human handoff models (Stage 10).

Three layers, kept separate on purpose (as for conversations):

- **Request validation** (``SupportRequestCreate``): what a customer submits in the
  support-request or contact form. It is the only way contact details enter the system,
  and only with explicit consent.
- **Storage records** (``CustomerContact``, ``SupportTicket``, ``TicketEvent``): what the
  support store keeps. Frozen, unknown fields rejected. These hold personal data and are
  **never returned by a public API**; staff tooling reads them.
- **Public responses** (``SupportRequestCreated``, ``CustomerTicketView``): what the
  customer gets back: a reference number, status and subject, never contact details.

Contact details are kept in their own table, apart from chat messages, and a ticket
links to its conversation by ID instead of copying the conversation into the ticket.
The ticket's description is the text the customer wrote or approved in the form.

Validation choices
------------------
- Text is trimmed of surrounding whitespace (and invisible characters); inner text is
  kept as written, in any script. Names, subjects and phone numbers are single lines;
  the message may contain line breaks. Control characters are rejected.
- Email addresses are checked for syntax with ``email-validator`` (no DNS lookups, so
  validation is fast and never depends on the network) and stored in normalised form.
- Phone numbers are optional and checked loosely: digits with the usual separators
  (spaces, ``+``, ``-``, ``.``, ``/``, brackets) and an optional extension, 5 to 15
  digits before any extension. No country rules: legitimate international numbers must
  never be rejected. A phone number is required only when the customer asks to be
  phoned.
- Consent must be the JSON value ``true``. It has no default (a form must not preselect
  it), so a submission without it, or with ``"true"``, ``1`` or ``"on"``, is refused:
  submitting the form is not consent.
- ``confirmed`` is the separate, explicit confirmation that a ticket should be created
  (the customer pressed "Send request" after seeing what will be sent). It follows the
  same strict rule.
- ``client_request_id`` is a client-generated UUID, sent again unchanged on a retry, so
  an uncertain network outcome never creates a second ticket (the Stage 7 pattern).

Status workflow
---------------
``open`` -> ``assigned`` -> ``in_progress`` <-> ``waiting_for_customer``, then
``resolved`` -> ``closed`` (``ALLOWED_TRANSITIONS``). Reopening is explicit and narrow:
only ``resolved`` -> ``in_progress``; ``closed`` is final. Customers may only withdraw an
open request or answer a question from staff (``CUSTOMER_TRANSITIONS``); they can never
assign or resolve. Nothing changes automatically (``ActorType.SYSTEM`` may make no
transition): a chatbot answer never resolves a ticket. A ticket's status is independent
of its conversation's status.
"""

import hashlib
import json
import re
import unicodedata
import uuid
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    UUID4,
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    model_validator,
)
from pydantic_core import PydanticCustomError

from app.models.chat import strip_message
from app.models.conversation import UTCDatetime
from app.models.escalation import EscalationReason, TicketType

MAX_NAME_LENGTH = 100
MAX_EMAIL_LENGTH = 254  # RFC 5321 path limit; email-validator enforces it too
MAX_PHONE_LENGTH = 32
MAX_SUBJECT_LENGTH = 150
MAX_DESCRIPTION_LENGTH = 2_000
MIN_PHONE_DIGITS = 5
MAX_PHONE_DIGITS = 15  # E.164 maximum, extension not counted


class TicketStatus(StrEnum):
    OPEN = "open"  # submitted, nobody has picked it up yet
    ASSIGNED = "assigned"  # a named staff member owns it (assigned_to)
    IN_PROGRESS = "in_progress"  # staff are working on it
    WAITING_FOR_CUSTOMER = "waiting_for_customer"  # staff asked the customer something
    RESOLVED = "resolved"  # staff consider it answered; may be reopened
    CLOSED = "closed"  # finished; final


class ActorType(StrEnum):
    """Who made a change, recorded on every ticket event."""

    CUSTOMER = "customer"  # the holder of the conversation's access token
    STAFF = "staff"  # an authenticated staff member (actor_id is their staff ID)
    SYSTEM = "system"  # the application itself: only ever creates tickets


class TicketSubmissionOutcome(StrEnum):
    CREATED = "created"  # a new ticket
    REPLAYED = "replayed"  # a retry with the same client_request_id: the original ticket
    DUPLICATE = "duplicate"  # the same request again (new ID): the existing open ticket


class TicketPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class TicketEventType(StrEnum):
    CREATED = "created"  # new_status is the initial status
    STATUS_CHANGED = "status_changed"  # previous_status -> new_status
    ANONYMIZED = "anonymized"  # personal data removed; no statuses
    # Stage 11: the assignee changed without a status change (reassigned or unassigned
    # while being worked on). previous_assigned_to -> assigned_to; no statuses.
    ASSIGNMENT_CHANGED = "assignment_changed"


# What anonymized records hold instead of personal data. The placeholder email uses the
# reserved example.com domain (RFC 2606), which never belongs to anyone.
ANONYMIZED_TEXT = "[removed]"
ANONYMIZED_EMAIL = "anonymized@example.com"


class ContactPreference(StrEnum):
    EMAIL = "email"
    PHONE = "phone"


class LeadCategory(StrEnum):
    """What a sales inquiry is about (sales tickets only; a CRM-friendly lead type)."""

    BULK_ORDER = "bulk_order"
    BUSINESS_PRICING = "business_pricing"
    PARTNERSHIP = "partnership"
    PRODUCT_DEMO = "product_demo"
    GENERAL_SALES = "general_sales"  # the default when a sales inquiry names none


# What each status means for the customer: whether the request is only recorded or
# actively handled. Never names a person or promises a time.
CUSTOMER_STATUS_MESSAGES: dict[TicketStatus, str] = {
    TicketStatus.OPEN: "Your request has been recorded. Our team has not picked it up yet.",
    TicketStatus.ASSIGNED: "A member of our team has taken on your request.",
    TicketStatus.IN_PROGRESS: "Our team is working on your request.",
    TicketStatus.WAITING_FOR_CUSTOMER: "Our team is waiting for more information from you.",
    TicketStatus.RESOLVED: "Our team has marked your request as resolved.",
    TicketStatus.CLOSED: "This request is closed.",
}


_S = TicketStatus
# Every permitted status change, by anyone. Closing is possible from every unfinished
# status (spam, duplicates, withdrawn requests); closed is final.
ALLOWED_TRANSITIONS: dict[TicketStatus, frozenset[TicketStatus]] = {
    _S.OPEN: frozenset({_S.ASSIGNED, _S.CLOSED}),
    _S.ASSIGNED: frozenset({_S.IN_PROGRESS, _S.OPEN, _S.CLOSED}),  # -> open: unassigned
    _S.IN_PROGRESS: frozenset({_S.WAITING_FOR_CUSTOMER, _S.RESOLVED, _S.CLOSED}),
    _S.WAITING_FOR_CUSTOMER: frozenset({_S.IN_PROGRESS, _S.RESOLVED, _S.CLOSED}),
    _S.RESOLVED: frozenset({_S.CLOSED, _S.IN_PROGRESS}),  # -> in_progress: reopened
    _S.CLOSED: frozenset(),
}
# The only reopening: a resolved ticket goes back to in_progress (with its assignee).
REOPEN_TRANSITIONS = frozenset({(_S.RESOLVED, _S.IN_PROGRESS)})
# The only changes a customer may make: withdraw a request nobody has picked up yet, and
# hand a ticket back to staff by answering their question. Never assign or resolve.
CUSTOMER_TRANSITIONS = frozenset({(_S.OPEN, _S.CLOSED), (_S.WAITING_FOR_CUSTOMER, _S.IN_PROGRESS)})
# Statuses in which a ticket still needs work (duplicate submissions are matched here).
ACTIVE_STATUSES = frozenset({_S.OPEN, _S.ASSIGNED, _S.IN_PROGRESS, _S.WAITING_FOR_CUSTOMER})
# Staff IDs as configured in STAFF_TOKEN_HASHES, e.g. "maria" or "agent-07".
STAFF_ID_PATTERN = r"^[A-Za-z0-9._@-]{1,64}$"
MAX_NOTE_LENGTH = 2000  # internal staff notes (Stage 11)
StaffId = Annotated[str, StringConstraints(pattern=STAFF_ID_PATTERN)]

# Customer-facing reference: a type prefix and 8 random characters from Crockford's
# base32 alphabet (no I, L, O or U, so it reads well over the phone), e.g. SUP-7K3M-9QX2.
REFERENCE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
REFERENCE_PREFIXES: dict[TicketType, str] = {TicketType.SUPPORT: "SUP", TicketType.SALES: "SAL"}
REFERENCE_PATTERN = r"^(?:SUP|SAL)-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$"
REFERENCE_LENGTH = 13


# --- Field validation -----------------------------------------------------------------

_LINE_BREAKS = frozenset("\n\r")
_PHONE_TEXT = re.compile(r"^\+?[0-9 ()./-]+?(?:\s*(?:ext\.?|x|#)\s*[0-9]{1,6})?$", re.IGNORECASE)
_PHONE_EXTENSION = re.compile(r"\s*(?:ext\.?|x|#)\s*[0-9]{1,6}$", re.IGNORECASE)


def _has_control(value: str, allowed: frozenset[str]) -> bool:
    return any(unicodedata.category(c) == "Cc" and c not in allowed for c in value)


def _single_line(value: str) -> str:
    if not value:
        raise PydanticCustomError("blank_text", "Must not be empty or only whitespace")
    if _has_control(value, frozenset()):
        raise PydanticCustomError(
            "invalid_characters", "Must be a single line without control characters"
        )
    return value


def _multi_line(value: str) -> str:
    if not value:
        raise PydanticCustomError("blank_text", "Must not be empty or only whitespace")
    if _has_control(value, frozenset("\t") | _LINE_BREAKS):
        raise PydanticCustomError(
            "invalid_characters",
            "Must not contain control characters other than tabs and line breaks",
        )
    return value


def _optional_phone(value: object) -> object:
    stripped = strip_message(value)
    return None if stripped == "" else stripped


def _check_phone(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) > MAX_PHONE_LENGTH:
        raise PydanticCustomError(
            "phone_length", f"Phone number must be at most {MAX_PHONE_LENGTH} characters"
        )
    if not _PHONE_TEXT.match(value):
        raise PydanticCustomError(
            "phone_format",
            "Phone number may contain digits, spaces, +, -, ., /, brackets and an extension",
        )
    digits = sum(c.isdigit() for c in _PHONE_EXTENSION.sub("", value))
    if not MIN_PHONE_DIGITS <= digits <= MAX_PHONE_DIGITS:
        raise PydanticCustomError(
            "phone_digits",
            f"Phone number must have {MIN_PHONE_DIGITS} to {MAX_PHONE_DIGITS} digits",
        )
    return value


def _require_consent(value: bool) -> bool:
    if value is not True:
        raise PydanticCustomError(
            "consent_required", "Consent is required before contact details can be stored"
        )
    return value


def _require_confirmation(value: bool) -> bool:
    if value is not True:
        raise PydanticCustomError(
            "confirmation_required", "The customer must confirm before a request is created"
        )
    return value


_UUID_TEXT = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


def _check_request_id(value: object) -> object:
    if isinstance(value, str) and not _UUID_TEXT.match(value):
        raise PydanticCustomError(
            "uuid_format",
            "client_request_id must be a UUID such as 7d0e4a3c-2f1b-4c5d-9e8f-0a1b2c3d4e5f",
        )
    return value


def _reject_nil(value: uuid.UUID) -> uuid.UUID:
    if value.int == 0 or value.int == (1 << 128) - 1:
        raise PydanticCustomError("uuid_reserved", "client_request_id must not be the nil UUID")
    return value


CustomerName = Annotated[
    str,
    StringConstraints(max_length=MAX_NAME_LENGTH),
    BeforeValidator(strip_message),
    AfterValidator(_single_line),
]
Subject = Annotated[
    str,
    StringConstraints(max_length=MAX_SUBJECT_LENGTH),
    BeforeValidator(strip_message),
    AfterValidator(_single_line),
]
Description = Annotated[
    str,
    StringConstraints(max_length=MAX_DESCRIPTION_LENGTH),
    BeforeValidator(strip_message),
    AfterValidator(_multi_line),
]
# email-validator also enforces the length limits (MAX_EMAIL_LENGTH overall).
ContactEmail = Annotated[EmailStr, BeforeValidator(strip_message)]
PhoneNumber = Annotated[
    str | None,
    BeforeValidator(_optional_phone),
    AfterValidator(_check_phone),
]
# Strict: only the JSON value true. "true", 1, "on" and a missing field are refused.
Consent = Annotated[bool, Field(strict=True), AfterValidator(_require_consent)]
Confirmation = Annotated[bool, Field(strict=True), AfterValidator(_require_confirmation)]
ClientRequestId = Annotated[
    uuid.UUID, BeforeValidator(_check_request_id), AfterValidator(_reject_nil)
]
ReferenceNumber = Annotated[str, StringConstraints(pattern=REFERENCE_PATTERN)]


# --- Request --------------------------------------------------------------------------


class SupportRequestCreate(BaseModel):
    """A support request or sales inquiry, submitted by the customer from a form.

    The conversation it belongs to (if any) is identified by the URL and authorised with
    the conversation's token, never by a field here; unknown fields are rejected.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "client_request_id": "7d0e4a3c-2f1b-4c5d-9e8f-0a1b2c3d4e5f",
                    "ticket_type": "support",
                    "name": "Alex Rivera",
                    "email": "alex@example.com",
                    "phone": None,
                    "contact_preference": "email",
                    "subject": "Order arrived damaged",
                    "message": "My order arrived with a cracked lid. Can you replace it?",
                    "consent_given": True,
                    "confirmed": True,
                }
            ]
        },
    )

    client_request_id: ClientRequestId = Field(
        description="Client-generated UUID for this submission (e.g. crypto.randomUUID()), "
        "sent again unchanged when retrying. A retry returns the original ticket instead "
        "of creating another; reusing it for different details is refused."
    )
    ticket_type: TicketType = Field(
        description="support: help from the support team. sales: a sales or business inquiry."
    )
    lead_category: LeadCategory | None = Field(
        default=None,
        description="Sales inquiries only: what it is about. Defaults to general_sales for "
        "a sales inquiry; must be empty for a support request.",
    )
    name: CustomerName = Field(description=f"How to address the customer (1-{MAX_NAME_LENGTH}).")
    email: ContactEmail = Field(description="Email address for the reply.")
    phone: PhoneNumber = Field(
        default=None,
        description="Optional. Required only when contact_preference is phone. Any "
        f"format with {MIN_PHONE_DIGITS}-{MAX_PHONE_DIGITS} digits (international welcome).",
    )
    contact_preference: ContactPreference = Field(
        default=ContactPreference.EMAIL, description="How the customer prefers to be contacted."
    )
    subject: Subject = Field(description=f"Short summary (1-{MAX_SUBJECT_LENGTH} characters).")
    message: Description = Field(
        description="The customer's description of what they need, as they wrote or "
        f"approved it (1-{MAX_DESCRIPTION_LENGTH} characters, line breaks allowed). This, "
        "not the chat transcript, is what staff read first."
    )
    consent_given: Consent = Field(
        description="Must be true: the customer agreed that their contact details are "
        "stored to answer this request. There is no default; a form must not preselect it."
    )
    confirmed: Confirmation = Field(
        description="Must be true: the customer explicitly confirmed that this request "
        "should be sent (for example by pressing Send after reviewing it)."
    )

    @model_validator(mode="after")
    def _phone_when_preferred(self) -> Self:
        if self.contact_preference is ContactPreference.PHONE and self.phone is None:
            raise PydanticCustomError(
                "phone_required", "A phone number is required when phone contact is preferred"
            )
        return self

    @model_validator(mode="after")
    def _lead_category_for_sales_only(self) -> Self:
        if self.ticket_type is TicketType.SUPPORT and self.lead_category is not None:
            raise PydanticCustomError(
                "lead_category_not_allowed", "lead_category is for sales inquiries only"
            )
        if self.ticket_type is TicketType.SALES and self.lead_category is None:
            # Stored explicitly, so every lead has a clear type.
            object.__setattr__(self, "lead_category", LeadCategory.GENERAL_SALES)
        return self


def request_fingerprint(request: SupportRequestCreate) -> str:
    """SHA-256 (hex) of everything that makes two submissions the same request: type,
    contact details and text, compared ignoring case (email, name) and spacing.

    ``client_request_id``, consent and confirmation are left out: a retry with the same
    ID must match, and the last two are always true. Versioned so the rule can change.
    """
    canonical = json.dumps(
        {
            "v": 1,
            "type": request.ticket_type.value,
            "name": " ".join(request.name.casefold().split()),
            "email": request.email.lower(),
            "phone": None if request.phone is None else "".join(request.phone.split()),
            "preference": request.contact_preference.value,
            "subject": " ".join(request.subject.split()),
            "message": " ".join(request.message.split()),
            # Only for sales, so support fingerprints are unchanged from version 1.
            **({"lead": request.lead_category.value} if request.lead_category else {}),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- Responses ------------------------------------------------------------------------


class SupportRequestCreated(BaseModel):
    """Confirmation for the customer. Contains no contact details.

    ``message`` states only what is true: the request was recorded under this reference.
    It never claims that someone was notified or assigned, and mentions a response time
    only when the business configured one (``SUPPORT_RESPONSE_TIME_TEXT``).
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "reference_number": "SUP-7K3M-9QX2",
                    "ticket_type": "support",
                    "status": "open",
                    "created_at": "2026-10-16T09:30:00Z",
                    "outcome": "created",
                    "message": "Your support request has been recorded. Your reference "
                    "number is SUP-7K3M-9QX2; please keep it for your records.",
                    "expected_response": None,
                }
            ]
        },
    )

    reference_number: ReferenceNumber = Field(
        description="Quote this when contacting the business about the request."
    )
    ticket_type: TicketType
    status: TicketStatus
    created_at: UTCDatetime = Field(description="When the ticket was created (UTC).")
    outcome: TicketSubmissionOutcome = Field(
        description="created: a new ticket. replayed: a retry of the same submission; the "
        "original ticket. duplicate: the same request was already submitted in this "
        "conversation and is still open; that ticket."
    )
    message: str = Field(min_length=1, description="Confirmation text to show the customer.")
    expected_response: str | None = Field(
        description="The business's configured response-time statement, or null when none "
        "is configured. Never estimated."
    )


class CustomerTicketView(BaseModel):
    """A ticket as its customer sees it: no contact details, description or staff data
    (never who it is assigned to)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticket_id: uuid.UUID = Field(description="The ticket, for GET /support/tickets/{id}.")
    reference_number: ReferenceNumber
    ticket_type: TicketType
    lead_category: LeadCategory | None = Field(description="Sales inquiries: their topic.")
    status: TicketStatus
    status_message: str = Field(
        description="What the status means: recorded only, or being handled. Never names "
        "a person or promises a time."
    )
    subject: str
    created_at: UTCDatetime
    updated_at: UTCDatetime


class CustomerTicketList(BaseModel):
    """One page of a conversation's tickets, oldest first."""

    model_config = ConfigDict(extra="forbid")

    tickets: list[CustomerTicketView]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)
    has_more: bool


# --- Storage records (never returned by public APIs) ---------------------------------


class CustomerContact(BaseModel):
    """Stored contact details, kept apart from chat messages."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contact_id: UUID4
    name: CustomerName
    email: ContactEmail
    phone: PhoneNumber = None
    contact_preference: ContactPreference
    consent_given: Literal[True]
    consent_timestamp: UTCDatetime  # the most recent consent (kept after anonymizing)
    created_at: UTCDatetime
    updated_at: UTCDatetime
    # When the personal data was removed (name, email and phone replaced).
    anonymized_at: UTCDatetime | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.contact_preference is ContactPreference.PHONE and self.phone is None:
            raise ValueError("phone contact requires a phone number")
        if self.updated_at < self.created_at or self.consent_timestamp < self.created_at:
            raise ValueError("updated_at and consent_timestamp must not be before created_at")
        return self


class SupportTicket(BaseModel):
    """A stored support request or sales lead."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticket_id: UUID4
    reference_number: ReferenceNumber
    session_id: uuid.UUID | None  # the linked conversation; None if none or deleted since
    contact_id: UUID4
    ticket_type: TicketType
    lead_category: LeadCategory | None = None  # sales tickets only
    # Why the chat offered the request, when it did; None for a request made unprompted.
    escalation_reason: EscalationReason | None = None
    subject: Subject
    description: Description
    status: TicketStatus
    priority: TicketPriority
    assigned_to: StaffId | None = None  # set only by an authenticated staff member
    # When the current assignee was assigned (Stage 11); None if unassigned, or assigned
    # before this was recorded. Says nothing about whether they have seen the ticket.
    assigned_at: UTCDatetime | None = None
    created_at: UTCDatetime
    updated_at: UTCDatetime
    resolved_at: UTCDatetime | None = None
    closed_at: UTCDatetime | None = None
    # When subject and description were removed (retention or an erasure request).
    anonymized_at: UTCDatetime | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.anonymized_at is not None and self.status is not TicketStatus.CLOSED:
            raise ValueError("only closed tickets are anonymized")
        if not self.reference_number.startswith(REFERENCE_PREFIXES[self.ticket_type] + "-"):
            raise ValueError("reference_number prefix does not match ticket_type")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be before created_at")
        finished = {TicketStatus.RESOLVED, TicketStatus.CLOSED}
        if self.status is TicketStatus.RESOLVED and self.resolved_at is None:
            raise ValueError("a resolved ticket needs resolved_at")
        if self.status not in finished and self.resolved_at is not None:
            raise ValueError("only resolved or closed tickets have resolved_at")
        if (self.status is TicketStatus.CLOSED) != (self.closed_at is not None):
            raise ValueError("closed_at is set exactly when the ticket is closed")
        if self.status is TicketStatus.ASSIGNED and self.assigned_to is None:
            raise ValueError("an assigned ticket needs assigned_to")
        if (self.ticket_type is TicketType.SALES) != (self.lead_category is not None):
            raise ValueError("lead_category is set exactly for sales tickets")
        return self

    def customer_view(self) -> CustomerTicketView:
        """What the ticket's customer may see."""
        return CustomerTicketView(
            ticket_id=self.ticket_id,
            reference_number=self.reference_number,
            ticket_type=self.ticket_type,
            lead_category=self.lead_category,
            status=self.status,
            status_message=CUSTOMER_STATUS_MESSAGES[self.status],
            subject=self.subject,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


# Statuses whose assignee can change without a status change (Stage 11): the ticket is
# being worked on. Open tickets are assigned through the change to ``assigned``;
# resolved and closed tickets are finished.
REASSIGNABLE_STATUSES = frozenset(
    {TicketStatus.ASSIGNED, TicketStatus.IN_PROGRESS, TicketStatus.WAITING_FOR_CUSTOMER}
)

NoteBody = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_NOTE_LENGTH),
]


class TicketNote(BaseModel):
    """An internal staff note on a ticket (Stage 11). Never shown to customers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    note_id: UUID4
    ticket_id: UUID4
    author_id: StaffId
    body: str = Field(min_length=1, max_length=MAX_NOTE_LENGTH)
    created_at: UTCDatetime


class TicketEvent(BaseModel):
    """One entry in a ticket's audit trail, in ``sequence`` order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID4
    ticket_id: UUID4
    sequence: int = Field(ge=1, strict=True)
    event_type: TicketEventType
    previous_status: TicketStatus | None = None
    new_status: TicketStatus | None = None
    actor_type: ActorType
    actor_id: StaffId | None = None  # the staff member, for staff changes
    assigned_to: StaffId | None = None  # for a change to assigned: who was assigned
    # Stage 11: the assignee before an assignment change (or before a change to
    # assigned/open that replaced or removed one).
    previous_assigned_to: StaffId | None = None
    created_at: UTCDatetime

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.actor_type is ActorType.STAFF) != (self.actor_id is not None):
            raise ValueError("actor_id is set exactly for staff changes")
        if self.event_type is TicketEventType.CREATED:
            if self.previous_status is not None or self.new_status is None:
                raise ValueError("a created event has only new_status")
        elif self.event_type is TicketEventType.ANONYMIZED:
            if self.previous_status is not None or self.new_status is not None:
                raise ValueError("an anonymized event has no statuses")
        elif self.event_type is TicketEventType.ASSIGNMENT_CHANGED:
            if self.previous_status is not None or self.new_status is not None:
                raise ValueError("an assignment change has no statuses")
            if self.previous_assigned_to == self.assigned_to:
                raise ValueError("an assignment change needs a different assignee")
        elif (
            self.previous_status is None
            or self.new_status is None
            or self.previous_status is self.new_status
        ):
            raise ValueError("a status change needs two different statuses")
        return self
