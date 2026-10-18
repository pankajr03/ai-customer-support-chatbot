"""Read-only aggregate queries for the staff dashboard (Stage 11).

Counts and timings computed by the database (``GROUP BY``, ``COUNT``, ``AVG``), so no
message text, ticket text or contact detail is ever loaded. Every query is bounded by a
time range ``[since, until)`` and is one read transaction over the existing tables:
conversation sessions and messages (Stage 6), support tickets (Stage 10). There is no
separate analytics store.

Time stamps are stored as naive UTC text (``UTCDateTime``), so ``date(created_at)`` is
the UTC calendar day and ``julianday`` differences are exact durations.

Data limits are the stored data's limits: expired conversations are deleted by
retention cleanup (with their messages), so conversation and message counts cover only
conversations still stored. Tickets are kept (anonymized after their own retention).
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database.connection import Database
from app.database.models import (
    ConversationMessageRow,
    ConversationSessionRow,
    SupportTicketRow,
)
from app.models.support import ACTIVE_STATUSES

logger = logging.getLogger(__name__)

# Server processing time of one exchange: the reply's stored time minus the customer
# message's (both written by the API: received_at and answered_at). The assistant reply
# directly follows its customer message in the same conversation.
_LATENCY_SQL = """
    SELECT (julianday(a.created_at) - julianday(u.created_at)) * 86400000.0 AS value
    FROM conversation_messages AS a
    JOIN conversation_messages AS u
      ON u.session_id = a.session_id AND u.sequence = a.sequence - 1
    WHERE a.role = 'assistant' AND u.role = 'user'
      AND a.created_at >= :since AND a.created_at < :until
"""
# Hours from a ticket's creation to its (latest) resolution, for tickets resolved in the
# range. A reopened ticket loses its resolved_at until it is resolved again.
_RESOLUTION_SQL = """
    SELECT (julianday(resolved_at) - julianday(created_at)) * 24.0 AS value
    FROM support_tickets
    WHERE resolved_at IS NOT NULL AND resolved_at >= :since AND resolved_at < :until
"""


class AnalyticsStoreError(Exception):
    """The aggregate queries failed (the type only is logged, never parameters)."""


@dataclass(frozen=True)
class DurationStats:
    samples: int
    average: float | None
    median: float | None
    p90: float | None
    max: float | None


# Kept for readability where the unit is milliseconds.
LatencyStats = DurationStats


@dataclass(frozen=True)
class ConversationActivity:
    stored_conversations: int
    conversations_started: int  # in the range, still stored
    customer_messages: int
    chatbot_responses: int
    responses_by_decision: dict[str, int]
    escalation_offers_by_reason: dict[str, int]
    latency: DurationStats  # milliseconds


@dataclass(frozen=True)
class DailyCounts:
    day: date
    conversations: int
    responses_by_decision: dict[str, int]
    tickets: int


@dataclass(frozen=True)
class TicketActivity:
    created_by_type: dict[str, int]
    created_by_status: dict[str, int]  # current status of the tickets created in range
    created_by_priority: dict[str, int]
    created_by_lead_category: dict[str, int]
    created_after_offer: int
    resolved: int  # resolved_at in the range
    closed: int  # closed_at in the range
    resolution_hours: DurationStats
    backlog_by_status: dict[str, int]  # now: open, assigned, in progress, waiting
    backlog_unassigned: int
    oldest_backlog_created_at: datetime | None
    daily: list[tuple[date, int, int]]  # day, created, resolved


def _text_time(value: datetime) -> str:
    """The text format SQLAlchemy stores DateTime values in on SQLite."""
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


class AnalyticsRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def stored_conversations(self, *, since: datetime | None = None) -> int:
        with self._read("stored_conversations") as db:
            query = select(func.count()).select_from(ConversationSessionRow)
            if since is not None:
                query = query.where(ConversationSessionRow.created_at >= since)
            return db.scalar(query) or 0

    def conversation_activity(self, *, since: datetime, until: datetime) -> ConversationActivity:
        """Conversation, message, decision, offer and latency figures in ``[since, until)``."""
        messages = ConversationMessageRow
        sessions = ConversationSessionRow
        in_range = (messages.created_at >= since, messages.created_at < until)
        with self._read("conversation_activity") as db:
            stored = db.scalar(select(func.count()).select_from(sessions)) or 0
            started = (
                db.scalar(
                    select(func.count())
                    .select_from(sessions)
                    .where(sessions.created_at >= since, sessions.created_at < until)
                )
                or 0
            )
            by_role = _grouped(db, messages.role, *in_range)
            decisions = _grouped(db, messages.decision, messages.role == "assistant", *in_range)
            offers = _grouped(
                db,
                messages.escalation_reason,
                messages.role == "assistant",
                messages.escalation_reason.is_not(None),
                *in_range,
            )
            latency = _durations(db, _LATENCY_SQL, since, until)
        return ConversationActivity(
            stored_conversations=stored,
            conversations_started=started,
            customer_messages=by_role.get("user", 0),
            chatbot_responses=by_role.get("assistant", 0),
            responses_by_decision=decisions,
            escalation_offers_by_reason=offers,
            latency=latency,
        )

    def ticket_activity(self, *, since: datetime, until: datetime) -> dict[str, int]:
        """Tickets created in the range: by type, and how many followed a chat offer."""
        metrics = self.ticket_metrics(since=since, until=until)
        return {
            "support": metrics.created_by_type.get("support", 0),
            "sales": metrics.created_by_type.get("sales", 0),
            "after_chat_offer": metrics.created_after_offer,
        }

    def ticket_metrics(self, *, since: datetime, until: datetime) -> TicketActivity:
        tickets = SupportTicketRow
        created = (tickets.created_at >= since, tickets.created_at < until)
        with self._read("ticket_metrics") as db:
            by_type = _grouped(db, tickets.ticket_type, *created)
            by_status = _grouped(db, tickets.status, *created)
            by_priority = _grouped(db, tickets.priority, *created)
            by_lead = _grouped(
                db, tickets.lead_category, tickets.lead_category.is_not(None), *created
            )
            after_offer = _count(db, tickets.escalation_reason.is_not(None), *created)
            resolved = _count(
                db,
                tickets.resolved_at.is_not(None),
                tickets.resolved_at >= since,
                tickets.resolved_at < until,
            )
            closed = _count(
                db,
                tickets.closed_at.is_not(None),
                tickets.closed_at >= since,
                tickets.closed_at < until,
            )
            hours = _durations(db, _RESOLUTION_SQL, since, until)
            active = [s.value for s in ACTIVE_STATUSES]
            backlog = _grouped(db, tickets.status, tickets.status.in_(active))
            unassigned = _count(db, tickets.status.in_(active), tickets.assigned_to.is_(None))
            oldest = db.scalar(
                select(func.min(tickets.created_at)).where(tickets.status.in_(active))
            )
            created_daily = dict(
                db.execute(
                    select(func.date(tickets.created_at), func.count())
                    .where(*created)
                    .group_by(func.date(tickets.created_at))
                ).all()
            )
            resolved_daily = dict(
                db.execute(
                    select(func.date(tickets.resolved_at), func.count())
                    .where(tickets.resolved_at >= since, tickets.resolved_at < until)
                    .group_by(func.date(tickets.resolved_at))
                ).all()
            )
        return TicketActivity(
            created_by_type=by_type,
            created_by_status=by_status,
            created_by_priority=by_priority,
            created_by_lead_category=by_lead,
            created_after_offer=after_offer,
            resolved=resolved,
            closed=closed,
            resolution_hours=hours,
            backlog_by_status=backlog,
            backlog_unassigned=unassigned,
            oldest_backlog_created_at=oldest.replace(tzinfo=UTC) if oldest else None,
            daily=[
                (day, created_daily.get(day.isoformat(), 0), resolved_daily.get(day.isoformat(), 0))
                for day in _days(since, until)
            ],
        )

    def daily(self, *, since: datetime, until: datetime) -> list[DailyCounts]:
        """Per UTC day in the range: conversations started, responses by decision and
        tickets created. Days without activity are included with zeros."""
        messages = ConversationMessageRow
        sessions = ConversationSessionRow
        with self._read("daily") as db:
            conversations = dict(
                db.execute(
                    select(func.date(sessions.created_at), func.count())
                    .where(sessions.created_at >= since, sessions.created_at < until)
                    .group_by(func.date(sessions.created_at))
                ).all()
            )
            responses: dict[str, dict[str, int]] = {}
            for day, decision, number in db.execute(
                select(func.date(messages.created_at), messages.decision, func.count())
                .where(
                    messages.role == "assistant",
                    messages.created_at >= since,
                    messages.created_at < until,
                )
                .group_by(func.date(messages.created_at), messages.decision)
            ).all():
                responses.setdefault(day, {})[decision] = number
            tickets = dict(
                db.execute(
                    select(func.date(SupportTicketRow.created_at), func.count())
                    .where(
                        SupportTicketRow.created_at >= since, SupportTicketRow.created_at < until
                    )
                    .group_by(func.date(SupportTicketRow.created_at))
                ).all()
            )
        return [
            DailyCounts(
                day=day,
                conversations=conversations.get(day.isoformat(), 0),
                responses_by_decision=responses.get(day.isoformat(), {}),
                tickets=tickets.get(day.isoformat(), 0),
            )
            for day in _days(since, until)
        ]

    @contextmanager
    def _read(self, operation: str) -> Iterator[Session]:
        try:
            with self._db.read() as db:
                yield db
        except SQLAlchemyError as exc:
            logger.error("Analytics query %s failed (%s)", operation, type(exc).__name__)
            raise AnalyticsStoreError(f"analytics are unavailable ({operation})") from exc


def _days(since: datetime, until: datetime) -> list[date]:
    """The UTC calendar days ``[since, until)`` touches."""
    first = since.astimezone(UTC).date()
    last = (until.astimezone(UTC) - timedelta(microseconds=1)).date()
    return [date.fromordinal(n) for n in range(first.toordinal(), last.toordinal() + 1)]


def _grouped(db: Session, column: Any, *conditions: Any) -> dict[str, int]:
    rows = db.execute(select(column, func.count()).where(*conditions).group_by(column)).all()
    return {value: number for value, number in rows}


def _count(db: Session, *conditions: Any) -> int:
    return db.scalar(select(func.count()).select_from(SupportTicketRow).where(*conditions)) or 0


def _durations(db: Session, sql: str, since: datetime, until: datetime) -> DurationStats:
    """Count, average, median, 90th percentile and maximum of ``sql``'s ``value``
    column, computed by the database (the values are never loaded)."""
    params = {"since": _text_time(since), "until": _text_time(until)}
    values = f"SELECT value FROM ({sql}) WHERE value >= 0"  # noqa: S608 - constant SQL
    count, average, maximum = db.execute(
        text(f"SELECT count(*), avg(value), max(value) FROM ({values})"),  # noqa: S608
        params,
    ).one()
    if not count:
        return DurationStats(0, None, None, None, None)

    def percentile(fraction: float) -> float:
        offset = min(count - 1, int(fraction * count))
        return float(
            db.execute(
                text(f"{values} ORDER BY value LIMIT 1 OFFSET :offset"),
                {**params, "offset": offset},
            ).scalar_one()
        )

    return DurationStats(
        samples=int(count),
        average=float(average),
        median=percentile(0.5),
        p90=percentile(0.9),
        max=float(maximum),
    )
