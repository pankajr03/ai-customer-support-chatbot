"""Dashboard overview for staff (Stage 11): counts of FAQs and tickets.

Analytics need the ``view_analytics`` permission (admins), checked here as well as in
the API. The overview holds counts only, never ticket text or contact details.
"""

from datetime import UTC, datetime, timedelta

from app.core.staff_auth import StaffMember, StaffPermission
from app.models.admin_api import AdminOverview, ConversationCounts, TicketCountsView
from app.models.escalation import TicketType
from app.models.support import ACTIVE_STATUSES, TicketStatus
from app.repositories.analytics_repository import AnalyticsRepository
from app.repositories.faq_repository import DatabaseFAQRepository
from app.repositories.ticket_repository import TicketRepository

RECENT_DAYS = 7
# Tickets that wait on staff rather than on the customer or nobody.
OPEN_QUEUE = (TicketStatus.OPEN, TicketStatus.ASSIGNED, TicketStatus.IN_PROGRESS)


class AdminDashboardService:
    def __init__(
        self,
        faqs: DatabaseFAQRepository | None,
        tickets: TicketRepository,
        analytics: AnalyticsRepository | None = None,
    ) -> None:
        """
        Args:
            faqs: the database FAQ catalogue, or None when FAQs are not stored in the
                database (the overview then has no FAQ section).
            tickets: support tickets.
            analytics: conversation counts (None: the overview has none).
        """
        self._faqs = faqs
        self._tickets = tickets
        self._analytics = analytics

    def overview(self, actor: StaffMember, *, now: datetime | None = None) -> AdminOverview:
        """Raises StaffPermissionError, FAQStoreError, TicketStoreError."""
        actor.require(StaffPermission.VIEW_ANALYTICS)
        now = now or datetime.now(UTC)
        recent = now - timedelta(days=RECENT_DAYS)
        counts = self._tickets.count_tickets(since=recent)
        return AdminOverview(
            generated_at=now,
            faqs=self._faqs.summary() if self._faqs is not None else None,
            tickets=TicketCountsView(
                total=counts.total,
                by_status=counts.by_status,
                by_type=counts.by_type,
                by_lead_category=counts.by_lead_category,
                created_last_7_days=counts.created_since,
                open_queue=sum(counts.by_status[status] for status in OPEN_QUEUE),
                open=sum(counts.by_status[status] for status in ACTIVE_STATUSES),
                resolved=counts.by_status[TicketStatus.RESOLVED],
                closed=counts.by_status[TicketStatus.CLOSED],
                leads=counts.by_type[TicketType.SALES],
            ),
            conversations=None
            if self._analytics is None
            else ConversationCounts(
                stored=self._analytics.stored_conversations(),
                started_last_7_days=self._analytics.stored_conversations(since=recent),
            ),
        )
