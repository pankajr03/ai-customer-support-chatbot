"""Chatbot analytics for staff (Stage 11, Task 15): what the assistant decided, not what
customers achieved.

Every figure comes from data the application recorded: stored chatbot replies (with
their decision and any escalation offer), the stored times of each customer message and
its reply, and support tickets. Nothing is estimated or sampled, and there is no
satisfaction score: none is collected.

Formulas (``chatbot responses`` = assistant replies stored in the window, every
decision):

- Answered response rate = answered / chatbot responses x 100
- Fallback rate = (no_match + retrieval_unavailable) / chatbot responses x 100
- Clarification rate = ambiguous / chatbot responses x 100
- Escalation offer rate = replies offering human support or a sales contact /
  chatbot responses x 100

An *answered* response means the assistant replied from an approved FAQ. It does **not**
mean the customer's issue was resolved: nobody confirmed that, so no resolution rate is
reported. Likewise a ticket is a request for help, not a failure of the assistant.

A rate is reported only when its denominator has at least ``MIN_SAMPLE`` responses;
otherwise its value is null with the reason, and the raw counts are still given. A zero
denominator never divides.

Response latency is the server's processing time per exchange (from storing the
customer message to storing the reply), so it excludes network time to the customer.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from app.core.staff_auth import StaffMember, StaffPermission
from app.models.admin_api import (
    ChatAnalytics,
    DailyPoint,
    DurationView,
    LatencyView,
    RateView,
    TicketAnalytics,
    TicketBacklog,
    TicketDailyPoint,
    TicketRequestCounts,
)
from app.models.escalation import TicketType
from app.models.support import LeadCategory, TicketPriority, TicketStatus
from app.repositories.analytics_repository import AnalyticsRepository

MIN_SAMPLE = 30
DEFAULT_DAYS = 30
MAX_DAYS = 365
DECISIONS = ("answered", "no_match", "ambiguous", "retrieval_unavailable")
FALLBACK_DECISIONS = ("no_match", "retrieval_unavailable")


class InvalidPeriodError(ValueError):
    """The requested period is malformed, reversed, in the future or too long."""


@dataclass(frozen=True)
class AnalyticsPeriod:
    """Whole UTC days ``start_date`` .. ``end_date`` (inclusive), as ``[since, until)``."""

    start_date: date
    end_date: date

    @property
    def since(self) -> datetime:
        return datetime(
            self.start_date.year, self.start_date.month, self.start_date.day, tzinfo=UTC
        )

    @property
    def until(self) -> datetime:
        return self.since + timedelta(days=self.days)

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    @classmethod
    def resolve(
        cls,
        *,
        days: int | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        today: date | None = None,
    ) -> "AnalyticsPeriod":
        """Either ``days`` (the last N days, today included; default 30) or a date range
        (``end_date`` defaults to today, ``start_date`` to 29 days before it).

        Raises:
            InvalidPeriodError: both forms given, a reversed or over-long range
                (``MAX_DAYS``), or an end after today.
        """
        today = today or datetime.now(UTC).date()
        if days is not None and (start_date is not None or end_date is not None):
            raise InvalidPeriodError("use either days or start_date/end_date, not both")
        if days is not None:
            if not 1 <= days <= MAX_DAYS:
                raise InvalidPeriodError(f"days must be between 1 and {MAX_DAYS}")
            return cls(today - timedelta(days=days - 1), today)
        end = end_date or today
        start = start_date or end - timedelta(days=DEFAULT_DAYS - 1)
        if end > today:
            raise InvalidPeriodError("end_date cannot be after today (UTC)")
        if start > end:
            raise InvalidPeriodError("start_date must not be after end_date")
        if (end - start).days + 1 > MAX_DAYS:
            raise InvalidPeriodError(f"a period can cover at most {MAX_DAYS} days")
        return cls(start, end)


def rate(name: str, numerator: int, denominator: int, formula: str) -> RateView:
    """A percentage, or None with the reason when the sample is too small or empty."""
    if denominator == 0:
        return RateView(
            name=name,
            numerator=numerator,
            denominator=0,
            percent=None,
            formula=formula,
            note="No chatbot responses in this period.",
        )
    if denominator < MIN_SAMPLE:
        return RateView(
            name=name,
            numerator=numerator,
            denominator=denominator,
            percent=None,
            formula=formula,
            note=f"Not shown: fewer than {MIN_SAMPLE} responses, too few for a reliable rate.",
        )
    return RateView(
        name=name,
        numerator=numerator,
        denominator=denominator,
        percent=round(100.0 * numerator / denominator, 1),
        formula=formula,
        note=None,
    )


class AnalyticsService:
    def __init__(self, repository: AnalyticsRepository, *, conversation_ttl_minutes: int) -> None:
        self._repository = repository
        self._ttl_minutes = conversation_ttl_minutes

    def chat_analytics(
        self,
        actor: StaffMember,
        *,
        days: int | None = None,
        period: AnalyticsPeriod | None = None,
        now: datetime | None = None,
    ) -> ChatAnalytics:
        """Chatbot decisions over ``period`` (or the last ``days`` days; default 30).

        Raises:
            StaffPermissionError, InvalidPeriodError, AnalyticsStoreError.
        """
        actor.require(StaffPermission.VIEW_ANALYTICS)
        now = now or datetime.now(UTC)
        period = period or AnalyticsPeriod.resolve(days=days or DEFAULT_DAYS, today=now.date())
        since, until = period.since, period.until
        activity = self._repository.conversation_activity(since=since, until=until)
        tickets = self._repository.ticket_activity(since=since, until=until)
        decisions = {d: activity.responses_by_decision.get(d, 0) for d in DECISIONS}
        responses = activity.chatbot_responses
        offers = sum(activity.escalation_offers_by_reason.values())
        latency = activity.latency
        return ChatAnalytics(
            generated_at=now,
            days=period.days,
            since=since,
            until=until,
            start_date=period.start_date,
            end_date=period.end_date,
            stored_conversations=activity.stored_conversations,
            conversations_started=activity.conversations_started,
            customer_messages=activity.customer_messages,
            chatbot_responses=responses,
            decisions=decisions,
            escalation_offers=offers,
            escalation_offers_by_reason=activity.escalation_offers_by_reason,
            tickets=TicketRequestCounts(**tickets),
            rates=[
                rate(
                    "Answered response rate",
                    decisions["answered"],
                    responses,
                    "answered responses / chatbot responses x 100",
                ),
                rate(
                    "Fallback rate",
                    sum(decisions[d] for d in FALLBACK_DECISIONS),
                    responses,
                    "(no_match + retrieval_unavailable responses) / chatbot responses x 100",
                ),
                rate(
                    "Clarification rate",
                    decisions["ambiguous"],
                    responses,
                    "ambiguous responses / chatbot responses x 100",
                ),
                rate(
                    "Escalation offer rate",
                    offers,
                    responses,
                    "responses offering human support or sales contact / chatbot responses x 100",
                ),
            ],
            latency=LatencyView(
                samples=latency.samples,
                average_ms=_round(latency.average),
                median_ms=_round(latency.median),
                p90_ms=_round(latency.p90),
                max_ms=_round(latency.max),
                definition="Server processing time per exchange: from storing the customer "
                "message to storing the reply. Excludes network time to the customer.",
            ),
            daily=[
                DailyPoint(
                    day=point.day,
                    conversations_started=point.conversations,
                    chatbot_responses=sum(point.responses_by_decision.values()),
                    answered=point.responses_by_decision.get("answered", 0),
                    fallbacks=sum(
                        point.responses_by_decision.get(d, 0) for d in FALLBACK_DECISIONS
                    ),
                    tickets_created=point.tickets,
                )
                for point in self._repository.daily(since=since, until=until)
            ],
            min_sample=MIN_SAMPLE,
            limitations=self._limitations(),
        )

    def ticket_analytics(
        self, actor: StaffMember, period: AnalyticsPeriod, *, now: datetime | None = None
    ) -> TicketAnalytics:
        """Ticket volumes, resolutions and the current backlog. Counts and durations only.

        Raises:
            StaffPermissionError, AnalyticsStoreError.
        """
        actor.require(StaffPermission.VIEW_ANALYTICS)
        now = now or datetime.now(UTC)
        m = self._repository.ticket_metrics(since=period.since, until=period.until)
        hours = m.resolution_hours
        created = sum(m.created_by_type.values())
        return TicketAnalytics(
            generated_at=now,
            days=period.days,
            start_date=period.start_date,
            end_date=period.end_date,
            since=period.since,
            until=period.until,
            created=created,
            created_by_type={t.value: m.created_by_type.get(t.value, 0) for t in TicketType},
            created_by_current_status={
                s.value: m.created_by_status.get(s.value, 0) for s in TicketStatus
            },
            created_by_priority={
                p.value: m.created_by_priority.get(p.value, 0) for p in TicketPriority
            },
            leads_by_category={
                c.value: m.created_by_lead_category.get(c.value, 0) for c in LeadCategory
            },
            created_after_chat_offer=m.created_after_offer,
            resolved=m.resolved,
            closed=m.closed,
            time_to_resolution=DurationView(
                samples=hours.samples,
                unit="hours",
                average=_round(hours.average),
                median=_round(hours.median),
                p90=_round(hours.p90),
                max=_round(hours.max),
                definition="From creation to the latest resolution, for tickets resolved in "
                "the period. Reopened tickets count from their latest resolution only.",
            ),
            backlog=TicketBacklog(
                total=sum(m.backlog_by_status.values()),
                by_status={
                    s.value: m.backlog_by_status.get(s.value, 0)
                    for s in TicketStatus
                    if s not in (TicketStatus.RESOLVED, TicketStatus.CLOSED)
                },
                unassigned=m.backlog_unassigned,
                oldest_created_at=m.oldest_backlog_created_at,
            ),
            daily=[
                TicketDailyPoint(day=day, created=made, resolved=done)
                for day, made, done in m.daily
            ],
            limitations=[
                "Status counts are each ticket's current status, not its status during the period.",
                "There is no first-response time: replies happen outside this system "
                "(by email or phone) and are not recorded.",
                "Resolved does not mean the customer confirmed the outcome.",
                "Days are UTC calendar days.",
            ],
        )

    def _limitations(self) -> list[str]:
        return [
            "Conversation and message figures include only conversations still stored. "
            f"Conversations expire after {self._ttl_minutes} minutes and expired ones are "
            "deleted by retention cleanup, so longer periods undercount them. Ticket counts "
            "are complete.",
            "'Answered' means the assistant replied from an approved FAQ, not that the "
            "customer's issue was resolved; no resolution or satisfaction data is collected.",
            "No conversation-to-ticket rate is shown: deleted conversations would make it "
            "look higher than it is.",
            "Days are UTC calendar days.",
        ]


def _round(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None
