"""HTTP shapes of the support API (Stage 10): ``/api/v1/support/tickets``.

The validation rules are those of ``app.models.support`` (the same annotated types), so
the API and the service can never disagree about what a valid request is. These models
only arrange the fields for HTTP: the contact details are grouped under ``contact``, and
the description is called ``description``.

Who sees what:

- **Customers** (with their conversation's token) get ``SupportTicketCreatedResponse``
  and ``CustomerTicketView``: reference, type, status and what it means, subject and
  times. Never contact details, the description, the assignee or internal IDs of
  other records.
- **Staff** (with a staff token) get ``StaffTicketSummary`` in lists and
  ``StaffTicketDetail`` for one ticket. Only the detail, which staff need to reply,
  includes the description and the contact details.

The conversation's access token is never part of a body: it travels in the
``Authorization: Bearer`` header.
"""

import re
import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from app.models.conversation import UTCDatetime
from app.models.escalation import EscalationReason, TicketType
from app.models.support import (
    CUSTOMER_STATUS_MESSAGES,
    ActorType,
    ClientRequestId,
    Confirmation,
    Consent,
    ContactEmail,
    ContactPreference,
    CustomerContact,
    CustomerName,
    Description,
    LeadCategory,
    NoteBody,
    PhoneNumber,
    ReferenceNumber,
    StaffId,
    Subject,
    SupportRequestCreate,
    SupportRequestCreated,
    SupportTicket,
    TicketEvent,
    TicketEventType,
    TicketNote,
    TicketPriority,
    TicketStatus,
    TicketSubmissionOutcome,
)

__all__ = [
    "ContactDetails",
    "StaffTicketDetail",
    "StaffTicketList",
    "StaffTicketSummary",
    "SupportTicketCreateRequest",
    "SupportTicketCreatedResponse",
    "TicketEventList",
    "TicketEventView",
    "TicketStatusChangeRequest",
]

_CANONICAL_UUID = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


def _check_session_text(value: object) -> object:
    if isinstance(value, str) and not _CANONICAL_UUID.match(value):
        raise PydanticCustomError("uuid_format", "session_id must be a UUID")
    return value


SessionIdField = Annotated[uuid.UUID, BeforeValidator(_check_session_text)]


# --- Creating a ticket (customer) -----------------------------------------------------


class ContactDetails(BaseModel):
    """How to reach the customer. Stored apart from the conversation, only with consent,
    and only to answer this request (never for marketing)."""

    model_config = ConfigDict(extra="forbid")

    name: CustomerName
    email: ContactEmail
    phone: PhoneNumber = None
    contact_preference: ContactPreference = ContactPreference.EMAIL
    consent_given: Consent = Field(
        description="Must be true: the customer agreed that these details are stored to "
        "answer this request. No default; a form must not preselect it. It is not "
        "consent to marketing."
    )


class SupportTicketCreateRequest(BaseModel):
    """A support request or sales inquiry from an authorised conversation.

    Send the conversation's token as ``Authorization: Bearer <token>``; the
    ``session_id`` here names the conversation it belongs to.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "session_id": "3f2b8c1e-7a4d-4e9b-9c61-2d5f0a8e4b17",
                    "client_request_id": "4c981eb1-7847-41c0-bf6b-0a0e1b9d6d91",
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
            ]
        },
    )

    session_id: SessionIdField = Field(description="The conversation (its token is required).")
    client_request_id: ClientRequestId = Field(
        description="Client-generated UUID for this submission, sent again unchanged on "
        "every retry: a retry returns the original ticket instead of creating another."
    )
    ticket_type: TicketType
    lead_category: LeadCategory | None = Field(
        default=None,
        description="Sales inquiries: bulk_order, business_pricing, partnership, "
        "product_demo or general_sales (the default). Not allowed for support requests.",
    )
    subject: Subject
    description: Description
    contact: ContactDetails
    confirmed: Confirmation = Field(
        description="Must be true: the customer explicitly confirmed sending this request."
    )

    def to_request(self) -> SupportRequestCreate:
        """The service's request (validated again, by the same rules)."""
        return SupportRequestCreate(
            client_request_id=self.client_request_id,
            ticket_type=self.ticket_type,
            lead_category=self.lead_category,
            name=self.contact.name,
            email=self.contact.email,
            phone=self.contact.phone,
            contact_preference=self.contact.contact_preference,
            subject=self.subject,
            message=self.description,
            consent_given=self.contact.consent_given,
            confirmed=self.confirmed,
        )

    @model_validator(mode="after")
    def _cross_field_rules(self) -> Self:
        # The rules of SupportRequestCreate that span fields, with the same error types.
        if self.contact.contact_preference is ContactPreference.PHONE and not self.contact.phone:
            raise PydanticCustomError(
                "phone_required", "A phone number is required when phone contact is preferred"
            )
        if self.ticket_type is TicketType.SUPPORT and self.lead_category is not None:
            raise PydanticCustomError(
                "lead_category_not_allowed", "lead_category is for sales inquiries only"
            )
        return self


class SupportTicketCreatedResponse(BaseModel):
    """The recorded request, as the customer may see it. No contact details."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "ticket_id": "9a7c1f0e-3b2d-4c5e-8f6a-1b2c3d4e5f60",
                    "reference_number": "SUP-7K3M-9QX2",
                    "ticket_type": "support",
                    "lead_category": None,
                    "status": "open",
                    "status_message": CUSTOMER_STATUS_MESSAGES[TicketStatus.OPEN],
                    "subject": "Refund assistance",
                    "created_at": "2026-10-16T09:30:00Z",
                    "outcome": "created",
                    "message": "Your support request has been recorded. Your reference "
                    "number is SUP-7K3M-9QX2; please keep it for your records.",
                    "expected_response": None,
                }
            ]
        },
    )

    ticket_id: uuid.UUID = Field(description="For GET /support/tickets/{ticket_id}.")
    reference_number: ReferenceNumber
    ticket_type: TicketType
    lead_category: LeadCategory | None
    status: TicketStatus
    status_message: str = Field(description="What the status means for the customer.")
    subject: str
    created_at: UTCDatetime
    outcome: TicketSubmissionOutcome = Field(
        description="created (201), replayed (200: a retry of the same submission) or "
        "duplicate (200: the same request is already open in this conversation)."
    )
    message: str = Field(description="Confirmation text: the request was recorded.")
    expected_response: str | None = Field(
        description="The business's configured response-time statement, or null. Never estimated."
    )

    @classmethod
    def build(cls, created: SupportRequestCreated, ticket: SupportTicket) -> Self:
        return cls(
            ticket_id=ticket.ticket_id,
            reference_number=created.reference_number,
            ticket_type=created.ticket_type,
            lead_category=ticket.lead_category,
            status=created.status,
            status_message=CUSTOMER_STATUS_MESSAGES[created.status],
            subject=ticket.subject,
            created_at=created.created_at,
            outcome=created.outcome,
            message=created.message,
            expected_response=created.expected_response,
        )


# --- Staff views ----------------------------------------------------------------------


class StaffTicketSummary(BaseModel):
    """A ticket in a staff list: no contact details or description."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: uuid.UUID
    reference_number: ReferenceNumber
    ticket_type: TicketType
    lead_category: LeadCategory | None
    status: TicketStatus
    priority: TicketPriority
    assigned_to: str | None
    assigned_at: datetime | None = Field(
        default=None,
        description="When the current assignee was assigned (null if unassigned or "
        "assigned before this was recorded). It does not mean they have seen the ticket.",
    )
    escalation_reason: EscalationReason | None = Field(
        description="The chat's offer that led to the request; null if unprompted."
    )
    subject: str
    has_conversation: bool = Field(
        description="Whether the linked conversation still exists (it may have been "
        "deleted by the customer or by retention)."
    )
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    closed_at: datetime | None

    @classmethod
    def from_ticket(cls, ticket: SupportTicket) -> Self:
        return cls(
            ticket_id=ticket.ticket_id,
            reference_number=ticket.reference_number,
            ticket_type=ticket.ticket_type,
            lead_category=ticket.lead_category,
            status=ticket.status,
            priority=ticket.priority,
            assigned_to=ticket.assigned_to,
            assigned_at=ticket.assigned_at,
            escalation_reason=ticket.escalation_reason,
            subject=ticket.subject,
            has_conversation=ticket.session_id is not None,
            created_at=ticket.created_at,
            updated_at=ticket.updated_at,
            resolved_at=ticket.resolved_at,
            closed_at=ticket.closed_at,
        )


class StaffContactView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    email: str
    phone: str | None
    contact_preference: ContactPreference
    consent_timestamp: datetime = Field(description="When the customer last gave consent.")


class StaffTicketDetail(StaffTicketSummary):
    """One ticket for staff, with what they need to reply: the customer's description
    and contact details (stored with consent, for this request only)."""

    description: str
    contact: StaffContactView | None = Field(
        description="null only if the contact record is missing."
    )

    @classmethod
    def from_records(cls, ticket: SupportTicket, contact: CustomerContact | None) -> Self:
        summary = StaffTicketSummary.from_ticket(ticket).model_dump()
        return cls(
            **summary,
            description=ticket.description,
            contact=None
            if contact is None
            else StaffContactView(
                name=contact.name,
                email=contact.email,
                phone=contact.phone,
                contact_preference=contact.contact_preference,
                consent_timestamp=contact.consent_timestamp,
            ),
        )


class StaffTicketList(BaseModel):
    """One page of tickets, oldest first."""

    model_config = ConfigDict(extra="forbid")

    tickets: list[StaffTicketSummary]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)
    has_more: bool


class TicketStatusChangeRequest(BaseModel):
    """A staff status change. ``assigned_to`` is required for, and only for, a change
    to ``assigned``, and must name a configured staff member."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"status": "assigned", "assigned_to": "maria", "expected_status": "open"},
                {"status": "resolved", "expected_status": "in_progress"},
            ]
        },
    )

    status: TicketStatus
    assigned_to: StaffId | None = None
    expected_status: TicketStatus | None = Field(
        default=None,
        description="Optional: the status the staff member saw. If the ticket has changed "
        "since, the request is refused (409 TICKET_CHANGED) instead of overwriting.",
    )

    @model_validator(mode="after")
    def _assignee_for_assignment_only(self) -> Self:
        if (self.status is TicketStatus.ASSIGNED) != (self.assigned_to is not None):
            raise PydanticCustomError(
                "assigned_to_mismatch",
                "assigned_to is required for, and only for, a change to assigned",
            )
        return self


class TicketEventView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int
    event_type: TicketEventType
    previous_status: TicketStatus | None
    new_status: TicketStatus | None
    actor_type: ActorType
    actor_id: str | None = Field(description="The staff member, for staff changes.")
    assigned_to: str | None
    previous_assigned_to: str | None = Field(
        default=None, description="The assignee before an assignment change (Stage 11)."
    )
    created_at: datetime

    @classmethod
    def from_event(cls, event: TicketEvent) -> Self:
        return cls(
            sequence=event.sequence,
            event_type=event.event_type,
            previous_status=event.previous_status,
            new_status=event.new_status,
            actor_type=event.actor_type,
            actor_id=event.actor_id,
            assigned_to=event.assigned_to,
            previous_assigned_to=event.previous_assigned_to,
            created_at=event.created_at,
        )


class TicketEventList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: uuid.UUID
    events: list[TicketEventView]


# --- Assignment and internal notes (Stage 11) -------------------------------------------


class TicketAssignmentRequest(BaseModel):
    """Assign, reassign or unassign (``assigned_to: null``) a ticket.

    ``expected_assigned_to`` is the assignee the staff member saw (null: unassigned);
    if someone changed it meanwhile the request is refused (409 TICKET_CHANGED) instead
    of overwriting their assignment.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"assigned_to": "maria", "expected_assigned_to": None, "expected_status": "open"},
                {"assigned_to": "sam", "expected_assigned_to": "maria"},
                {"assigned_to": None, "expected_assigned_to": "sam"},
            ]
        },
    )

    assigned_to: StaffId | None
    expected_assigned_to: StaffId | None
    expected_status: TicketStatus | None = None


class TicketNoteCreateRequest(BaseModel):
    """An internal note. ``client_note_id`` (a new UUID per note) makes retries safe:
    the same ID returns the stored note instead of adding it twice."""

    model_config = ConfigDict(extra="forbid")

    body: NoteBody = Field(description="Internal text for staff; never shown to customers.")
    client_note_id: uuid.UUID


class TicketNoteView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note_id: uuid.UUID
    author_id: str
    body: str
    created_at: datetime

    @classmethod
    def from_note(cls, note: TicketNote) -> Self:
        return cls(
            note_id=note.note_id,
            author_id=note.author_id,
            body=note.body,
            created_at=note.created_at,
        )


class TicketNoteList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: uuid.UUID
    notes: list[TicketNoteView]
