"""Ticket status workflow (Stage 10): who may move a ticket from one status to another.

Two layers:

- ``check_transition`` is the pure rule: a change must be in ``ALLOWED_TRANSITIONS``
  (``app.models.support``), and the actor must be allowed to make it. Staff may make any
  allowed change; customers only withdraw an open request or answer staff
  (``CUSTOMER_TRANSITIONS``), never assign or resolve; the system makes none, so nothing
  (a chatbot answer included) ever resolves a ticket automatically.
- ``TicketWorkflow`` applies changes for an authenticated actor: staff by their staff
  token (``StaffAuthenticator``), customers by their conversation's access token, and
  only for that conversation's tickets. Each change is recorded as a ticket event with
  the actor, in the same transaction, and only if the ticket is still in the status the
  decision was based on (``expected_status``).

A ticket's status is independent of its conversation: closing or expiring a conversation
does not change its tickets, and a ticket changing status does not touch the
conversation.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Self

from app.core.staff_auth import StaffAuthenticator, StaffMember
from app.models.support import (
    ALLOWED_TRANSITIONS,
    CUSTOMER_TRANSITIONS,
    ActorType,
    CustomerTicketView,
    SupportTicket,
    TicketStatus,
)
from app.repositories.ticket_repository import StaleTicketStatusError, TicketRepository
from app.services.conversation_service import ConversationService
from app.services.ticket_integrations import (
    NullTicketEventSink,
    TicketEventKind,
    TicketEventSink,
    TicketIntegrationEvent,
    publish_safely,
)

logger = logging.getLogger(__name__)


class TransitionNotAllowedError(Exception):
    """The workflow has no such status change (e.g. closed -> open)."""

    def __init__(self, current: TicketStatus, requested: TicketStatus) -> None:
        super().__init__(f"a {current} ticket cannot become {requested}")
        self.current = current
        self.requested = requested


class ActorNotPermittedError(Exception):
    """The change exists, but this kind of actor may not make it."""

    def __init__(self, actor: ActorType, current: TicketStatus, requested: TicketStatus) -> None:
        super().__init__(f"a {actor} may not change a {current} ticket to {requested}")
        self.actor = actor
        self.current = current
        self.requested = requested


class UnknownAssigneeError(Exception):
    """Tickets can only be assigned to a configured staff member."""

    def __init__(self) -> None:
        super().__init__("tickets can only be assigned to a configured staff member")


@dataclass(frozen=True)
class Actor:
    """Who is making a change. Create staff actors from an authenticated member only."""

    actor_type: ActorType
    staff_id: str | None = None

    @classmethod
    def customer(cls) -> Self:
        return cls(ActorType.CUSTOMER)

    @classmethod
    def system(cls) -> Self:
        return cls(ActorType.SYSTEM)

    @classmethod
    def staff(cls, member: StaffMember) -> Self:
        return cls(ActorType.STAFF, member.staff_id)


def check_transition(current: TicketStatus, requested: TicketStatus, actor: ActorType) -> None:
    """Raise unless ``actor`` may change a ``current`` ticket to ``requested``.

    Raises:
        TransitionNotAllowedError: not a change the workflow has (including no change).
        ActorNotPermittedError: a valid change this actor may not make.
    """
    if requested not in ALLOWED_TRANSITIONS[current]:
        raise TransitionNotAllowedError(current, requested)
    if actor is ActorType.STAFF:
        return
    if actor is ActorType.CUSTOMER and (current, requested) in CUSTOMER_TRANSITIONS:
        return
    raise ActorNotPermittedError(actor, current, requested)


def allowed_next(current: TicketStatus, actor: ActorType) -> frozenset[TicketStatus]:
    """The statuses ``actor`` may move a ``current`` ticket to (e.g. to show buttons)."""
    allowed = set()
    for requested in ALLOWED_TRANSITIONS[current]:
        try:
            check_transition(current, requested, actor)
        except ActorNotPermittedError:
            continue
        allowed.add(requested)
    return frozenset(allowed)


class TicketWorkflow:
    """Applies status changes for authenticated staff and for customers."""

    def __init__(
        self,
        tickets: TicketRepository,
        staff: StaffAuthenticator,
        conversations: ConversationService,
        events: TicketEventSink | None = None,
    ) -> None:
        self._tickets = tickets
        self._staff = staff
        self._conversations = conversations
        self._events = events or NullTicketEventSink()

    def change_status_as_staff(
        self,
        staff_token: str | None,
        ticket_id: uuid.UUID,
        new_status: TicketStatus,
        *,
        assigned_to: str | None = None,
        now: datetime | None = None,
    ) -> SupportTicket:
        """A staff change, authenticating the staff token first.

        Raises:
            StaffAuthenticationError: not a staff token. And as ``change_status_by_staff``.
        """
        member = self._staff.authenticate(staff_token)
        return self.change_status_by_staff(
            member, ticket_id, new_status, assigned_to=assigned_to, now=now
        )

    def change_status_by_staff(
        self,
        member: StaffMember,
        ticket_id: uuid.UUID,
        new_status: TicketStatus,
        *,
        assigned_to: str | None = None,
        expected_status: TicketStatus | None = None,
        now: datetime | None = None,
    ) -> SupportTicket:
        """A change by an authenticated staff member. ``assigned_to`` names the assignee
        for a change to assigned and must be a configured staff member (an assignment is
        never invented). With ``expected_status``, the change applies only if the ticket
        is still in that status (what the staff member saw).

        Raises:
            UnknownAssigneeError, TransitionNotAllowedError, TicketNotFoundError,
            StaleTicketStatusError (changed meanwhile; re-read and retry),
            ValueError (assigned without an assignee, or an assignee for another status).
        """
        if assigned_to is not None and assigned_to not in self._staff.staff_ids:
            raise UnknownAssigneeError()
        ticket = self._tickets.get_ticket(ticket_id)
        if expected_status is not None and ticket.status is not expected_status:
            raise StaleTicketStatusError(expected_status, ticket.status)
        changed = self._apply(ticket, new_status, Actor.staff(member), assigned_to, now)
        logger.info(
            "event=ticket_status_changed ticket_id=%s actor=staff:%s %s->%s",
            ticket.ticket_id,
            member.staff_id,
            ticket.status.value,
            new_status.value,
        )
        return changed

    def change_status_as_customer(
        self,
        session_id: uuid.UUID | str,
        access_token: str | None,
        reference_number: str,
        new_status: TicketStatus,
        *,
        now: datetime | None = None,
    ) -> CustomerTicketView:
        """A customer change to one of their conversation's tickets (withdraw an open
        request, or answer a question from staff).

        Raises:
            ConversationNotFoundError (also for another conversation's ticket: a
            ``TicketNotFoundError``), ConversationExpiredError, ActorNotPermittedError,
            TransitionNotAllowedError, StaleTicketStatusError.
        """
        session = self._conversations.get_conversation(session_id, access_token, now)
        ticket = self._tickets.get_session_ticket(session.session_id, reference_number)
        changed = self._apply(ticket, new_status, Actor.customer(), None, now)
        logger.info(
            "event=ticket_status_changed ticket_id=%s actor=customer %s->%s",
            ticket.ticket_id,
            ticket.status.value,
            new_status.value,
        )
        return changed.customer_view()

    def _apply(
        self,
        ticket: SupportTicket,
        new_status: TicketStatus,
        actor: Actor,
        assigned_to: str | None,
        now: datetime | None,
    ) -> SupportTicket:
        check_transition(ticket.status, new_status, actor.actor_type)
        changed = self._tickets.change_status(
            ticket.ticket_id,
            new_status,
            actor_type=actor.actor_type,
            actor_id=actor.staff_id,
            assigned_to=assigned_to,
            expected_status=ticket.status,
            now=now,
        )
        publish_safely(  # after the commit; a failing integration changes nothing
            self._events,
            TicketIntegrationEvent.from_ticket(
                TicketEventKind.STATUS_CHANGED, changed, previous_status=ticket.status
            ),
        )
        return changed
