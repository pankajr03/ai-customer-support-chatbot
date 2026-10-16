"""Support ticket endpoints (Stage 10): human handoff and sales leads.

    POST  /support/tickets                      customer (conversation token)
    GET   /support/tickets/{ticket_id}          customer (conversation token + session_id)
                                                or staff (staff token)
    GET   /support/tickets                      staff
    PATCH /support/tickets/{ticket_id}/status   staff
    GET   /support/tickets/{ticket_id}/events   staff

Authorization is checked on every endpoint, before anything is read:

- **Customers** send their conversation's token (``Authorization: Bearer cst_...``),
  exactly as for the conversation endpoints. It reaches only that conversation's
  tickets; any other ticket is ``404 TICKET_NOT_FOUND``, the same as a missing one, so a
  ticket ID or reference number alone reveals nothing. A wrong token is
  ``404 SESSION_NOT_FOUND``.
- **Staff** send a personal staff token (``Authorization: Bearer stf_...``; see
  ``app.core.staff_auth``). Conversation tokens are never accepted as staff tokens: the
  two are different formats checked against different stores. Without a valid staff
  token: ``401 STAFF_AUTH_REQUIRED``. While no staff are configured
  (``STAFF_TOKEN_HASHES`` empty), every staff endpoint answers ``403
  STAFF_ACCESS_DISABLED``: there is no public listing or status change, ever.

Tokens are never part of a body and never logged. Responses carry ``Cache-Control:
no-store``. Customers never receive contact details; staff receive them only for a
single ticket (``StaffTicketDetail``), which they need to reply.

Creating a ticket is idempotent by ``client_request_id`` (see
``app.services.escalation_service``): ``201`` for a new ticket, ``200`` with
``Idempotent-Replayed: true`` for a retry or for the same request still open.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request, Response, Security, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import AwareDatetime

from app.api.dependencies import (
    AccessTokenDep,
    AccessTokenMissingError,
    EscalationServiceDep,
    StaffMemberDep,
    TicketRepositoryDep,
    TicketWorkflowDep,
    authenticate_staff,
    conversation_token_scheme,
    limit_support_requests,
)
from app.api.errors import (
    INVALID_TRANSITION_MESSAGE,
    MISSING_AUTHORIZATION_MESSAGE,
    SESSION_EXPIRED_MESSAGE,
    SESSION_NOT_FOUND_MESSAGE,
    STAFF_AUTH_MESSAGE,
    STAFF_DISABLED_MESSAGE,
    SUPPORT_UNAVAILABLE_MESSAGE,
    TICKET_CHANGED_MESSAGE,
    TICKET_CONFLICT_MESSAGE,
    TICKET_NOT_FOUND_MESSAGE,
)
from app.api.routes.conversations import RATE_LIMITED, VALIDATION, _error
from app.core.staff_auth import STAFF_TOKEN_PREFIX
from app.models.errors import ErrorCode
from app.models.escalation import TicketType
from app.models.support import (
    CustomerTicketView,
    LeadCategory,
    TicketPriority,
    TicketStatus,
    TicketSubmissionOutcome,
)
from app.models.support_api import (
    StaffTicketDetail,
    StaffTicketList,
    StaffTicketSummary,
    SupportTicketCreatedResponse,
    SupportTicketCreateRequest,
    TicketEventList,
    TicketEventView,
    TicketStatusChangeRequest,
)
from app.repositories.ticket_repository import MAX_PAGE_SIZE, TicketFilter, TicketNotFoundError


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/support", tags=["support"], dependencies=[Depends(_no_store)])

TicketIdPath = Annotated[
    str,
    Path(
        max_length=64,
        description="The `ticket_id` returned when the request was created.",
        examples=["9a7c1f0e-3b2d-4c5e-8f6a-1b2c3d4e5f60"],
    ),
]

# --- OpenAPI error documentation ------------------------------------------------------

CUSTOMER_ERRORS: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: _error(
        "No `Authorization: Bearer <token>` header.",
        ("missing_token", ErrorCode.UNAUTHORIZED_SESSION, MISSING_AUTHORIZATION_MESSAGE),
    ),
    status.HTTP_404_NOT_FOUND: _error(
        "Unknown conversation or wrong token; or a ticket that is not this conversation's.",
        ("session", ErrorCode.SESSION_NOT_FOUND, SESSION_NOT_FOUND_MESSAGE),
        ("ticket", ErrorCode.TICKET_NOT_FOUND, TICKET_NOT_FOUND_MESSAGE),
    ),
    status.HTTP_410_GONE: _error(
        "The conversation has expired.",
        ("expired", ErrorCode.SESSION_EXPIRED, SESSION_EXPIRED_MESSAGE),
    ),
    status.HTTP_503_SERVICE_UNAVAILABLE: _error(
        "Support storage is temporarily unavailable; retrying with the same "
        "client_request_id is safe.",
        ("unavailable", ErrorCode.SERVICE_UNAVAILABLE, SUPPORT_UNAVAILABLE_MESSAGE),
    ),
    **VALIDATION,
}
STAFF_ERRORS: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: _error(
        "No valid staff token.",
        ("staff", ErrorCode.STAFF_AUTH_REQUIRED, STAFF_AUTH_MESSAGE),
    ),
    status.HTTP_403_FORBIDDEN: _error(
        "No staff are configured on this server (STAFF_TOKEN_HASHES): staff endpoints "
        "are disabled.",
        ("disabled", ErrorCode.STAFF_ACCESS_DISABLED, STAFF_DISABLED_MESSAGE),
    ),
    status.HTTP_503_SERVICE_UNAVAILABLE: _error(
        "Support storage is temporarily unavailable.",
        ("unavailable", ErrorCode.SERVICE_UNAVAILABLE, SUPPORT_UNAVAILABLE_MESSAGE),
    ),
    **VALIDATION,
}
TICKET_MISSING = {
    status.HTTP_404_NOT_FOUND: _error(
        "Unknown ticket.", ("ticket", ErrorCode.TICKET_NOT_FOUND, TICKET_NOT_FOUND_MESSAGE)
    )
}


def _ticket_uuid(ticket_id: str) -> uuid.UUID:
    """A path ticket ID; anything that is not a UUID is simply not found."""
    try:
        return uuid.UUID(ticket_id)
    except ValueError:
        raise TicketNotFoundError() from None


# --- Customer -------------------------------------------------------------------------


@router.post(
    "/tickets",
    status_code=status.HTTP_201_CREATED,
    response_model=SupportTicketCreatedResponse,
    summary="Send a support request or sales inquiry",
    responses={
        status.HTTP_200_OK: {
            "model": SupportTicketCreatedResponse,
            "description": "A retry of the same submission, or the same request already "
            "open in this conversation: the existing ticket (`Idempotent-Replayed: true`).",
        },
        **CUSTOMER_ERRORS,
        status.HTTP_422_UNPROCESSABLE_CONTENT: _error(
            "Invalid request, or a `client_request_id` reused for different details.",
            ("conflict", ErrorCode.TICKET_REQUEST_CONFLICT, TICKET_CONFLICT_MESSAGE),
        ),
        **RATE_LIMITED,
    },
    dependencies=[Depends(limit_support_requests)],
)
def create_ticket(
    body: SupportTicketCreateRequest,
    token: AccessTokenDep,
    service: EscalationServiceDep,
    response: Response,
) -> SupportTicketCreatedResponse:
    """Record a confirmed support request (or sales inquiry) for an authorised
    conversation.

    Requires the conversation's token (`Authorization: Bearer <token>`), explicit
    confirmation (`confirmed: true`) and consent (`contact.consent_given: true`). The
    conversation is not changed: the customer can keep chatting. The confirmation says
    only that the request was recorded; nobody is notified or assigned by this call.
    """
    submission = service.submit(body.session_id, token, body.to_request())
    if submission.response.outcome is not TicketSubmissionOutcome.CREATED:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replayed"] = "true"
    return SupportTicketCreatedResponse.build(submission.response, submission.ticket)


@router.get(
    "/tickets/{ticket_id}",
    response_model=CustomerTicketView | StaffTicketDetail,
    summary="Get a support request",
    responses={**CUSTOMER_ERRORS, status.HTTP_403_FORBIDDEN: STAFF_ERRORS[403]},
)
def get_ticket(
    ticket_id: TicketIdPath,
    request: Request,
    service: EscalationServiceDep,
    tickets: TicketRepositoryDep,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(conversation_token_scheme)
    ],
    session_id: Annotated[
        str | None,
        Query(
            max_length=64,
            description="Customers: the conversation the ticket belongs to (its token is "
            "required). Not used with a staff token.",
        ),
    ] = None,
) -> CustomerTicketView | StaffTicketDetail:
    """**Customers** (conversation token and `session_id`): the request's reference,
    type, status and what it means, subject and times. Only this conversation's
    tickets; never contact details or who is handling it.

    **Staff** (staff token): the ticket with the customer's description and contact
    details, to reply.
    """
    token = credentials.credentials if credentials is not None else None
    if token is not None and token.startswith(STAFF_TOKEN_PREFIX):
        authenticate_staff(request, token)
        ticket = tickets.get_ticket(_ticket_uuid(ticket_id))
        return StaffTicketDetail.from_records(ticket, tickets.get_contact(ticket.contact_id))
    if not token:
        raise AccessTokenMissingError()
    if session_id is None:
        raise TicketNotFoundError()  # a customer must name the conversation
    return service.get_ticket_by_id(session_id, token, _ticket_uuid(ticket_id))


# --- Staff ----------------------------------------------------------------------------


@router.get(
    "/tickets",
    response_model=StaffTicketList,
    summary="List support requests (staff)",
    responses=STAFF_ERRORS,
)
def list_tickets(
    _staff: StaffMemberDep,
    tickets: TicketRepositoryDep,
    status_filter: Annotated[
        list[TicketStatus] | None,
        Query(alias="status", description="Only these statuses (repeat for several)."),
    ] = None,
    ticket_type: TicketType | None = None,
    lead_category: LeadCategory | None = None,
    priority: TicketPriority | None = None,
    assigned_to: Annotated[str | None, Query(max_length=64)] = None,
    created_from: Annotated[
        AwareDatetime | None, Query(description="Created at or after (ISO 8601 with offset).")
    ] = None,
    created_before: Annotated[
        AwareDatetime | None, Query(description="Created before (ISO 8601 with offset).")
    ] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> StaffTicketList:
    """One page of tickets, oldest first (`created_at`, then ID: pages are stable).
    No contact details: open a ticket for those."""
    page = tickets.list_tickets(
        TicketFilter(
            statuses=frozenset(status_filter) if status_filter else None,
            ticket_type=ticket_type,
            lead_category=lead_category,
            priority=priority,
            assigned_to=assigned_to,
            created_from=created_from,
            created_before=created_before,
        ),
        offset=offset,
        limit=limit,
    )
    return StaffTicketList(
        tickets=[StaffTicketSummary.from_ticket(ticket) for ticket in page.tickets],
        total=page.total,
        offset=page.offset,
        limit=page.limit,
        has_more=page.has_more,
    )


@router.patch(
    "/tickets/{ticket_id}/status",
    response_model=StaffTicketSummary,
    summary="Change a ticket's status (staff)",
    responses={
        **STAFF_ERRORS,
        **TICKET_MISSING,
        status.HTTP_409_CONFLICT: _error(
            "Not allowed from the current status, or the ticket changed since it was read.",
            ("transition", ErrorCode.INVALID_STATUS_TRANSITION, INVALID_TRANSITION_MESSAGE),
            ("changed", ErrorCode.TICKET_CHANGED, TICKET_CHANGED_MESSAGE),
        ),
    },
)
def change_status(
    ticket_id: TicketIdPath,
    body: TicketStatusChangeRequest,
    staff: StaffMemberDep,
    workflow: TicketWorkflowDep,
) -> StaffTicketSummary:
    """Move a ticket along the workflow (`open` → `assigned` → `in_progress` ⇄
    `waiting_for_customer` → `resolved` → `closed`; reopening is only `resolved` →
    `in_progress`). The change and who made it are recorded as an event."""
    ticket = workflow.change_status_by_staff(
        staff,
        _ticket_uuid(ticket_id),
        body.status,
        assigned_to=body.assigned_to,
        expected_status=body.expected_status,
    )
    return StaffTicketSummary.from_ticket(ticket)


@router.get(
    "/tickets/{ticket_id}/events",
    response_model=TicketEventList,
    summary="A ticket's history (staff)",
    responses={**STAFF_ERRORS, **TICKET_MISSING},
)
def list_events(
    ticket_id: TicketIdPath, _staff: StaffMemberDep, tickets: TicketRepositoryDep
) -> TicketEventList:
    """Every status change, oldest first, with who made it."""
    ticket = tickets.get_ticket(_ticket_uuid(ticket_id))
    return TicketEventList(
        ticket_id=ticket.ticket_id,
        events=[
            TicketEventView.from_event(event) for event in tickets.list_events(ticket.ticket_id)
        ],
    )
