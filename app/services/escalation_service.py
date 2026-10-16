"""Human handoff and lead capture (Stage 10): from an offer to a stored ticket.

``EscalationService`` is the one place where a support request or sales inquiry becomes
a ticket. It contains no HTTP; a route passes the validated request in and the response
(or a typed error) out.

When to offer
-------------
``offer_for`` applies the escalation rules (``app.services.escalation_policy``). The
chat already calls them on every turn and stores the offer with the reply; this is the
same decision for any other caller.

Submitting (``submit``), in order:

1. **Confirmation and consent.** ``SupportRequestCreate`` only accepts ``confirmed`` and
   ``consent_given`` as the JSON value ``true``; they are checked again here, so a
   request built in code cannot skip them. Contact details were validated with it.
2. **Authorise the conversation** with its access token (an unknown conversation and a
   wrong token are the same ``ConversationNotFoundError``; an expired one is refused).
   A closed conversation may still send a request: the customer may have closed the
   chat and then wanted help.
3. **Which offer led here.** The most recent offer in the conversation's recent replies,
   if its ticket type matches the request; otherwise the request was unprompted.
4. **Store** through ``TicketRepository.create_ticket``: idempotent by
   ``client_request_id`` within the conversation (an identical retry returns the
   original ticket, a changed one is refused), duplicate-safe for the same request sent
   again while open, and atomic (contact, ticket and first event, or nothing).
5. **Respond** with the reference number, status and a confirmation that states only
   what is true: the request was recorded. It never says that someone was notified or
   assigned (there is no notification integration), never names a person and never
   promises a response time unless the business configured one
   (``SUPPORT_RESPONSE_TIME_TEXT``).

The conversation is not changed: it stays open, and the customer can keep chatting.
Logs name the ticket, type, outcome and offer, never contact details or message text.

Errors (nothing partial is ever stored):
- ``ConfirmationRequiredError`` / ``ConsentRequiredError``: not confirmed or no consent.
- ``ConversationNotFoundError``, ``ConversationExpiredError`` (from the conversation
  service).
- ``IdempotencyKeyConflictError``: the ``client_request_id`` was used for different
  details; send a new ID for a new request.
- ``SupportStoreUnavailableError``: the database failed; retrying with the same
  ``client_request_id`` is safe and never creates a second ticket.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Self

from app.core.config import Settings
from app.models.escalation import ESCALATION_TICKET_TYPES, EscalationOffer, EscalationReason
from app.models.support import (
    CustomerTicketList,
    CustomerTicketView,
    SupportRequestCreate,
    SupportRequestCreated,
    SupportTicket,
    TicketSubmissionOutcome,
    TicketType,
)
from app.repositories.conversation_repository import (
    ConversationRepository,
    ConversationStoreError,
    SessionNotFoundError,
)
from app.repositories.ticket_repository import (
    DEFAULT_DUPLICATE_WINDOW,
    ConversationLinkError,
    IdempotencyKeyConflictError,
    TicketRepository,
    TicketStoreError,
)
from app.services.answer_decision_service import Decision
from app.services.conversation_history_service import PriorReply
from app.services.conversation_service import ConversationNotFoundError, ConversationService
from app.services.escalation_policy import escalation_for

logger = logging.getLogger(__name__)

__all__ = [
    "ConfirmationRequiredError",
    "ConsentRequiredError",
    "EscalationService",
    "IdempotencyKeyConflictError",
    "SupportStoreUnavailableError",
    "TicketSubmission",
]

DEFAULT_OFFER_LOOKBACK_MESSAGES = 10


class ConfirmationRequiredError(Exception):
    def __init__(self) -> None:
        super().__init__("the customer has not confirmed the request")


class ConsentRequiredError(Exception):
    def __init__(self) -> None:
        super().__init__("consent is required before contact details can be stored")


class SupportStoreUnavailableError(Exception):
    """Tickets could not be read or stored; nothing partial was stored. Retrying with
    the same client_request_id is safe."""


@dataclass(frozen=True)
class TicketSubmission:
    """The customer's response plus the stored ticket (for logs and tests only)."""

    response: SupportRequestCreated
    ticket: SupportTicket

    @property
    def replayed(self) -> bool:
        return self.response.outcome is not TicketSubmissionOutcome.CREATED


_CONFIRMATIONS: dict[TicketType, str] = {
    TicketType.SUPPORT: "Your support request has been recorded. Your reference number is "
    "{reference}; please keep it for your records.",
    TicketType.SALES: "Thank you for your inquiry. It has been recorded with reference "
    "number {reference}; please keep it for your records.",
}
_ALREADY_SENT = (
    "You have already sent this request, and it is still open under reference number "
    "{reference}. There is no need to send it again."
)


class EscalationService:
    """Offers human support and turns a confirmed request into a ticket."""

    def __init__(
        self,
        conversations: ConversationService,
        conversation_repository: ConversationRepository,
        tickets: TicketRepository,
        *,
        response_time_text: str | None = None,
        duplicate_window: timedelta = DEFAULT_DUPLICATE_WINDOW,
        offer_lookback_messages: int = DEFAULT_OFFER_LOOKBACK_MESSAGES,
    ) -> None:
        """
        Args:
            response_time_text: The business's approved response-time statement
                (``SUPPORT_RESPONSE_TIME_TEXT``); None shows none.
            duplicate_window: ``TICKET_DUPLICATE_WINDOW_MINUTES``.
            offer_lookback_messages: How far back to look for the offer that led to a
                request (``CONVERSATION_HISTORY_MAX_MESSAGES``, as the rules do).
        """
        if offer_lookback_messages < 1:
            raise ValueError("offer_lookback_messages must be at least 1")
        self._conversations = conversations
        self._messages = conversation_repository
        self._tickets = tickets
        self._response_time_text = response_time_text
        self._duplicate_window = duplicate_window
        self._lookback = offer_lookback_messages

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        conversations: ConversationService,
        conversation_repository: ConversationRepository,
        tickets: TicketRepository,
    ) -> Self:
        return cls(
            conversations,
            conversation_repository,
            tickets,
            response_time_text=settings.support_response_time_text,
            duplicate_window=timedelta(minutes=settings.ticket_duplicate_window_minutes),
            offer_lookback_messages=settings.conversation_history_max_messages,
        )

    # --- Offering -----------------------------------------------------------------

    @staticmethod
    def offer_for(
        message: str, decision: Decision, prior_replies: Sequence[PriorReply] = ()
    ) -> EscalationOffer | None:
        """Whether to offer human support or a sales contact form for this turn (see
        ``app.services.escalation_policy``)."""
        return EscalationOffer.for_reason(escalation_for(message, decision, prior_replies))

    # --- Submitting ---------------------------------------------------------------

    def submit(
        self,
        session_id: uuid.UUID | str,
        access_token: str | None,
        request: SupportRequestCreate,
        *,
        now: datetime | None = None,
    ) -> TicketSubmission:
        """Create a ticket for a confirmed request in an authorised conversation, or
        return the ticket an identical earlier submission created (see module doc)."""
        # 1. Confirmation and consent (validated already; never skipped).
        if request.confirmed is not True:
            raise ConfirmationRequiredError()
        if request.consent_given is not True:
            raise ConsentRequiredError()
        # 2. Authorise.
        session = self._conversations.get_conversation(session_id, access_token, now)
        # 3. The offer that led here, if any.
        reason = self._offer_reason(session.session_id, request.ticket_type)
        # 4. Store.
        try:
            stored = self._tickets.create_ticket(
                request,
                session_id=session.session_id,
                escalation_reason=reason,
                duplicate_window=self._duplicate_window,
                now=now,
            )
        except ConversationLinkError:  # deleted by a concurrent request
            raise ConversationNotFoundError() from None
        except TicketStoreError as exc:
            raise SupportStoreUnavailableError("support requests are unavailable") from exc
        ticket = stored.ticket
        logger.info(
            "event=ticket_submitted session_id=%s ticket_id=%s type=%s outcome=%s offer=%s",
            session.session_id,
            ticket.ticket_id,
            ticket.ticket_type.value,
            stored.outcome.value,
            reason.value if reason is not None else "none",
        )
        # 5. Respond.
        return TicketSubmission(self._response(ticket, stored.outcome), ticket)

    # --- The customer's tickets ---------------------------------------------------

    def list_tickets(
        self,
        session_id: uuid.UUID | str,
        access_token: str | None,
        *,
        offset: int = 0,
        limit: int = 50,
        now: datetime | None = None,
    ) -> CustomerTicketList:
        """One page of this conversation's tickets, as the customer may see them."""
        session = self._conversations.get_conversation(session_id, access_token, now)
        try:
            page = self._tickets.list_session_tickets(
                session.session_id, offset=offset, limit=limit
            )
        except TicketStoreError as exc:
            raise SupportStoreUnavailableError("support requests are unavailable") from exc
        return CustomerTicketList(
            tickets=[ticket.customer_view() for ticket in page.tickets],
            total=page.total,
            offset=page.offset,
            limit=page.limit,
            has_more=page.has_more,
        )

    def get_ticket_by_id(
        self,
        session_id: uuid.UUID | str,
        access_token: str | None,
        ticket_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> CustomerTicketView:
        """One of this conversation's tickets by ID (``TicketNotFoundError`` for any
        other ticket). The ID alone grants nothing: the conversation's token is needed."""
        session = self._conversations.get_conversation(session_id, access_token, now)
        try:
            return self._tickets.get_session_ticket_by_id(
                session.session_id, ticket_id
            ).customer_view()
        except TicketStoreError as exc:
            raise SupportStoreUnavailableError("support requests are unavailable") from exc

    def get_ticket(
        self,
        session_id: uuid.UUID | str,
        access_token: str | None,
        reference_number: str,
        *,
        now: datetime | None = None,
    ) -> CustomerTicketView:
        """One of this conversation's tickets. Another conversation's ticket is
        ``TicketNotFoundError``, exactly like a missing one."""
        session = self._conversations.get_conversation(session_id, access_token, now)
        try:
            return self._tickets.get_session_ticket(
                session.session_id, reference_number
            ).customer_view()
        except TicketStoreError as exc:
            raise SupportStoreUnavailableError("support requests are unavailable") from exc

    # --- Internals ----------------------------------------------------------------

    def _offer_reason(
        self, session_id: uuid.UUID, ticket_type: TicketType
    ) -> EscalationReason | None:
        """The latest recent offer for this kind of request, or None (unprompted).

        Only metadata: losing it must never block a customer's request, so a failed
        read just records the request as unprompted.
        """
        try:
            recent = self._messages.list_recent_messages(session_id, self._lookback)
        except (ConversationStoreError, SessionNotFoundError):
            logger.warning("Could not read recent offers for session_id=%s", session_id)
            return None
        for message in reversed(recent):
            reason = message.escalation_reason
            if reason is not None and ESCALATION_TICKET_TYPES[reason] is ticket_type:
                return reason
        return None

    def _response(
        self, ticket: SupportTicket, outcome: TicketSubmissionOutcome
    ) -> SupportRequestCreated:
        template = (
            _ALREADY_SENT
            if outcome is TicketSubmissionOutcome.DUPLICATE
            else _CONFIRMATIONS[ticket.ticket_type]
        )
        return SupportRequestCreated(
            reference_number=ticket.reference_number,
            ticket_type=ticket.ticket_type,
            status=ticket.status,
            created_at=ticket.created_at,
            outcome=outcome,
            message=template.format(reference=ticket.reference_number),
            expected_response=self._response_time_text,
        )
