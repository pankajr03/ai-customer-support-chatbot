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

from datetime import UTC, datetime, timedelta

from app.core.staff_auth import StaffMember, StaffPermission
from app.models.admin_api import (
    ChatAnalytics,
    DailyPoint,
    LatencyView,
    RateView,
    TicketRequestCounts,
)
from app.repositories.analytics_repository import AnalyticsRepository

MIN_SAMPLE = 30
DEFAULT_DAYS = 30
MAX_DAYS = 365
DECISIONS = ("answered", "no_match", "ambiguous", "retrieval_unavailable")
FALLBACK_DECISIONS = ("no_match", "retrieval_unavailable")


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
        self, actor: StaffMember, *, days: int = DEFAULT_DAYS, now: datetime | None = None
    ) -> ChatAnalytics:
        """Raises StaffPermissionError, ValueError (days outside 1..MAX_DAYS),
        AnalyticsStoreError."""
        actor.require(StaffPermission.VIEW_ANALYTICS)
        if not 1 <= days <= MAX_DAYS:
            raise ValueError(f"days must be between 1 and {MAX_DAYS}")
        now = now or datetime.now(UTC)
        # Whole UTC days: today and the days - 1 before it.
        since = datetime(now.year, now.month, now.day, tzinfo=UTC) - timedelta(days=days - 1)
        activity = self._repository.conversation_activity(since=since)
        tickets = self._repository.ticket_activity(since=since)
        decisions = {d: activity.responses_by_decision.get(d, 0) for d in DECISIONS}
        responses = activity.chatbot_responses
        offers = sum(activity.escalation_offers_by_reason.values())
        latency = activity.latency
        return ChatAnalytics(
            generated_at=now,
            days=days,
            since=since,
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
                average_ms=_round(latency.average_ms),
                median_ms=_round(latency.median_ms),
                p90_ms=_round(latency.p90_ms),
                max_ms=_round(latency.max_ms),
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
                for point in self._repository.daily(since=since)
            ],
            min_sample=MIN_SAMPLE,
            limitations=self._limitations(),
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
