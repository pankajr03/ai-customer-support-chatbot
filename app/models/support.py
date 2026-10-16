"""Lead capture and human handoff models (Stage 10).

Three layers, kept separate on purpose (as for conversations):

- **Request validation** (``SupportRequestCreate``): what a customer submits in the
  support-request or contact form. It is the only way contact details enter the system,
  and only with explicit consent.
- **Storage records** (``CustomerContact``, ``SupportTicket``, ``TicketEvent``): what the
  support store keeps. Frozen, unknown fields rejected. These hold personal data and are
  **never returned by a public API**; staff tooling reads them.
- **Public response** (``SupportRequestCreated``): what the customer gets back after
  submitting: a reference number and status, never their contact details.

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
"""

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
    IN_PROGRESS = "in_progress"  # a staff member is working on it
    RESOLVED = "resolved"  # staff consider it answered; may be reopened
    CLOSED = "closed"  # finished; final


class TicketPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class TicketEventType(StrEnum):
    CREATED = "created"  # new_status is the initial status
    STATUS_CHANGED = "status_changed"  # previous_status -> new_status


class ContactPreference(StrEnum):
    EMAIL = "email"
    PHONE = "phone"


# Status changes staff may make. Closed is final; a resolved ticket can be reopened.
ALLOWED_TRANSITIONS: dict[TicketStatus, frozenset[TicketStatus]] = {
    TicketStatus.OPEN: frozenset(
        {TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED, TicketStatus.CLOSED}
    ),
    TicketStatus.IN_PROGRESS: frozenset(
        {TicketStatus.OPEN, TicketStatus.RESOLVED, TicketStatus.CLOSED}
    ),
    TicketStatus.RESOLVED: frozenset({TicketStatus.OPEN, TicketStatus.CLOSED}),
    TicketStatus.CLOSED: frozenset(),
}

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
                    "ticket_type": "support",
                    "name": "Alex Rivera",
                    "email": "alex@example.com",
                    "phone": None,
                    "contact_preference": "email",
                    "subject": "Order arrived damaged",
                    "message": "My order arrived with a cracked lid. Can you replace it?",
                    "consent_given": True,
                }
            ]
        },
    )

    ticket_type: TicketType = Field(
        description="support: help from the support team. sales: a sales or business inquiry."
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

    @model_validator(mode="after")
    def _phone_when_preferred(self) -> Self:
        if self.contact_preference is ContactPreference.PHONE and self.phone is None:
            raise PydanticCustomError(
                "phone_required", "A phone number is required when phone contact is preferred"
            )
        return self


# --- Response -------------------------------------------------------------------------


class SupportRequestCreated(BaseModel):
    """Confirmation for the customer. Contains no contact details."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "reference_number": "SUP-7K3M-9QX2",
                    "ticket_type": "support",
                    "status": "open",
                    "created_at": "2026-10-16T09:30:00Z",
                }
            ]
        },
    )

    reference_number: ReferenceNumber = Field(
        description="Quote this when contacting the business about the request."
    )
    ticket_type: TicketType
    status: TicketStatus
    created_at: UTCDatetime


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
    consent_timestamp: UTCDatetime  # the most recent consent
    created_at: UTCDatetime
    updated_at: UTCDatetime

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
    # Why the chat offered the request, when it did; None for a request made unprompted.
    escalation_reason: EscalationReason | None = None
    subject: Subject
    description: Description
    status: TicketStatus
    priority: TicketPriority
    created_at: UTCDatetime
    updated_at: UTCDatetime
    resolved_at: UTCDatetime | None = None
    closed_at: UTCDatetime | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
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
        return self


class TicketEvent(BaseModel):
    """One entry in a ticket's audit trail, in ``sequence`` order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID4
    ticket_id: UUID4
    sequence: int = Field(ge=1, strict=True)
    event_type: TicketEventType
    previous_status: TicketStatus | None = None
    new_status: TicketStatus | None = None
    created_at: UTCDatetime

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.event_type is TicketEventType.CREATED:
            if self.previous_status is not None or self.new_status is None:
                raise ValueError("a created event has only new_status")
        elif (
            self.previous_status is None
            or self.new_status is None
            or self.previous_status is self.new_status
        ):
            raise ValueError("a status change needs two different statuses")
        return self
