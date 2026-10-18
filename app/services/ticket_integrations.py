"""Where future integrations plug in (Stage 10): email, CRM, helpdesk, staff dashboards.

Ticket creation and status changes never depend on anything outside the database. Once
a change is committed, the service that made it hands a ``TicketIntegrationEvent`` to
the configured ``TicketEventSink`` (``publish_safely``). Whatever the sink does, and
whether it fails, cannot undo or block the ticket change: failures are logged (type
only) and swallowed.

The default sink, ``NullTicketEventSink``, does nothing, and says so
(``delivers_notifications = False``). Nothing in the application claims that anyone was
notified, so the customer's confirmation stays true whichever sink is configured.

Writing an integration later means writing a sink, without touching the ticket tables
or the chat:

- **Email notifications**: a sink that queues an email to the team for
  ``ticket_created`` (and to the customer, if the business wants that), looking up the
  contact through ``TicketRepository.get_contact``.
- **CRM**: a sink that exports ``ticket_type == "sales"`` events, using ``lead_category``
  as the lead type.
- **Helpdesk platforms**: a sink that creates or updates the external ticket and keeps
  its ID in the integration's own table, keyed by ``ticket_id``.
- **Staff dashboards**: read the staff API (``GET /api/v1/support/tickets``); a sink can
  push live updates.

Events carry IDs, types and statuses only, never contact details or customer text: a
sink that needs more must read it through the repository, where access can be
controlled. For delivery that must survive a crash between the commit and the sink, a
later stage can add a transactional outbox table written in the same transaction; the
event shape stays the same.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, Self, runtime_checkable

from app.models.support import SupportTicket, TicketStatus

logger = logging.getLogger(__name__)


class TicketEventKind(StrEnum):
    TICKET_CREATED = "ticket_created"
    STATUS_CHANGED = "status_changed"
    ASSIGNMENT_CHANGED = "assignment_changed"  # Stage 11: who owns it changed


@dataclass(frozen=True)
class TicketIntegrationEvent:
    """A committed change, for integrations. No personal data."""

    kind: TicketEventKind
    ticket_id: uuid.UUID
    reference_number: str
    ticket_type: str
    lead_category: str | None
    status: TicketStatus
    previous_status: TicketStatus | None
    occurred_at: datetime

    @classmethod
    def from_ticket(
        cls,
        kind: TicketEventKind,
        ticket: SupportTicket,
        *,
        previous_status: TicketStatus | None = None,
    ) -> Self:
        return cls(
            kind=kind,
            ticket_id=ticket.ticket_id,
            reference_number=ticket.reference_number,
            ticket_type=ticket.ticket_type.value,
            lead_category=ticket.lead_category.value if ticket.lead_category else None,
            status=ticket.status,
            previous_status=previous_status,
            occurred_at=ticket.updated_at,
        )


@runtime_checkable
class TicketEventSink(Protocol):
    """Receives committed ticket changes. Must not raise for the caller's sake (failures
    are swallowed by ``publish_safely`` anyway) and should return quickly."""

    #: Whether this sink actually notifies people. Only then may anything say so.
    delivers_notifications: bool

    def publish(self, event: TicketIntegrationEvent) -> None: ...


class NullTicketEventSink:
    """The default: no integration configured. Notifies nobody."""

    delivers_notifications = False

    def publish(self, event: TicketIntegrationEvent) -> None:
        return None


@dataclass
class RecordingTicketEventSink:
    """Keeps events in memory (development and tests). Notifies nobody."""

    delivers_notifications: bool = False
    events: list[TicketIntegrationEvent] = field(default_factory=list)

    def publish(self, event: TicketIntegrationEvent) -> None:
        self.events.append(event)


def publish_safely(sink: TicketEventSink, event: TicketIntegrationEvent) -> None:
    """Hand a committed change to the sink; a failing sink never affects the ticket."""
    try:
        sink.publish(event)
    except Exception as exc:  # noqa: BLE001 - integrations must never break ticketing
        logger.error(
            "Ticket integration failed for %s %s (%s)",
            event.kind.value,
            event.ticket_id,
            type(exc).__name__,
        )
