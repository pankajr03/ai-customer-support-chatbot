"""Read-only aggregate queries for the staff dashboard (Stage 11).

Counts and timings computed by the database (``GROUP BY``, ``COUNT``, ``AVG``), so no
message text, ticket text or contact detail is ever loaded. Every query is one read
transaction over the existing tables: conversation sessions and messages (Stage 6),
support tickets (Stage 10).

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
from datetime import UTC, date, datetime

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database.connection import Database
from app.database.models import (
    ConversationMessageRow,
    ConversationSessionRow,
    SupportTicketRow,
)

logger = logging.getLogger(__name__)

# Server processing time of one exchange: the reply's stored time minus the customer
# message's (both written by the API: received_at and answered_at). The assistant reply
# directly follows its customer message in the same conversation.
_LATENCY_SQL = """
    SELECT (julianday(a.created_at) - julianday(u.created_at)) * 86400000.0 AS ms
    FROM conversation_messages AS a
    JOIN conversation_messages AS u
      ON u.session_id = a.session_id AND u.sequence = a.sequence - 1
    WHERE a.role = 'assistant' AND u.role = 'user' AND a.created_at >= :since
"""


class AnalyticsStoreError(Exception):
    """The aggregate queries failed (the type only is logged, never parameters)."""


@dataclass(frozen=True)
class LatencyStats:
    samples: int
    average_ms: float | None
    median_ms: float | None
    p90_ms: float | None
    max_ms: float | None


@dataclass(frozen=True)
class ConversationActivity:
    stored_conversations: int
    conversations_started: int  # in the window, still stored
    customer_messages: int
    chatbot_responses: int
    responses_by_decision: dict[str, int]
    escalation_offers_by_reason: dict[str, int]
    latency: LatencyStats


@dataclass(frozen=True)
class DailyCounts:
    day: date
    conversations: int
    responses_by_decision: dict[str, int]
    tickets: int


class AnalyticsRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def stored_conversations(self, *, since: datetime | None = None) -> int:
        with self._read("stored_conversations") as db:
            query = select(func.count()).select_from(ConversationSessionRow)
            if since is not None:
                query = query.where(ConversationSessionRow.created_at >= since)
            return db.scalar(query) or 0

    def conversation_activity(self, *, since: datetime) -> ConversationActivity:
        """Conversation, message, decision, offer and latency figures since ``since``."""
        messages = ConversationMessageRow
        with self._read("conversation_activity") as db:
            stored = db.scalar(select(func.count()).select_from(ConversationSessionRow)) or 0
            started = (
                db.scalar(
                    select(func.count())
                    .select_from(ConversationSessionRow)
                    .where(ConversationSessionRow.created_at >= since)
                )
                or 0
            )
            by_role = dict(
                db.execute(
                    select(messages.role, func.count())
                    .where(messages.created_at >= since)
                    .group_by(messages.role)
                ).all()
            )
            decisions = dict(
                db.execute(
                    select(messages.decision, func.count())
                    .where(messages.role == "assistant", messages.created_at >= since)
                    .group_by(messages.decision)
                ).all()
            )
            offers = dict(
                db.execute(
                    select(messages.escalation_reason, func.count())
                    .where(
                        messages.role == "assistant",
                        messages.created_at >= since,
                        messages.escalation_reason.is_not(None),
                    )
                    .group_by(messages.escalation_reason)
                ).all()
            )
            latency = _latency(db, since)
        return ConversationActivity(
            stored_conversations=stored,
            conversations_started=started,
            customer_messages=by_role.get("user", 0),
            chatbot_responses=by_role.get("assistant", 0),
            responses_by_decision=decisions,
            escalation_offers_by_reason=offers,
            latency=latency,
        )

    def ticket_activity(self, *, since: datetime) -> dict[str, int]:
        """Tickets created since ``since``: by type, and how many followed a chat offer."""
        with self._read("ticket_activity") as db:
            by_type = dict(
                db.execute(
                    select(SupportTicketRow.ticket_type, func.count())
                    .where(SupportTicketRow.created_at >= since)
                    .group_by(SupportTicketRow.ticket_type)
                ).all()
            )
            after_offer = (
                db.scalar(
                    select(func.count())
                    .select_from(SupportTicketRow)
                    .where(
                        SupportTicketRow.created_at >= since,
                        SupportTicketRow.escalation_reason.is_not(None),
                    )
                )
                or 0
            )
        return {
            "support": by_type.get("support", 0),
            "sales": by_type.get("sales", 0),
            "after_chat_offer": after_offer,
        }

    def daily(self, *, since: datetime) -> list[DailyCounts]:
        """Per UTC day since ``since``: conversations started, responses by decision and
        tickets created. Days without activity are included with zeros."""
        messages = ConversationMessageRow
        with self._read("daily") as db:
            conversations = dict(
                db.execute(
                    select(func.date(ConversationSessionRow.created_at), func.count())
                    .where(ConversationSessionRow.created_at >= since)
                    .group_by(func.date(ConversationSessionRow.created_at))
                ).all()
            )
            responses: dict[str, dict[str, int]] = {}
            for day, decision, number in db.execute(
                select(func.date(messages.created_at), messages.decision, func.count())
                .where(messages.role == "assistant", messages.created_at >= since)
                .group_by(func.date(messages.created_at), messages.decision)
            ).all():
                responses.setdefault(day, {})[decision] = number
            tickets = dict(
                db.execute(
                    select(func.date(SupportTicketRow.created_at), func.count())
                    .where(SupportTicketRow.created_at >= since)
                    .group_by(func.date(SupportTicketRow.created_at))
                ).all()
            )
        first = since.astimezone(UTC).date()
        last = datetime.now(UTC).date()
        days = [date.fromordinal(n) for n in range(first.toordinal(), last.toordinal() + 1)]
        return [
            DailyCounts(
                day=day,
                conversations=conversations.get(day.isoformat(), 0),
                responses_by_decision=responses.get(day.isoformat(), {}),
                tickets=tickets.get(day.isoformat(), 0),
            )
            for day in days
        ]

    @contextmanager
    def _read(self, operation: str) -> Iterator[Session]:
        try:
            with self._db.read() as db:
                yield db
        except SQLAlchemyError as exc:
            logger.error("Analytics query %s failed (%s)", operation, type(exc).__name__)
            raise AnalyticsStoreError(f"analytics are unavailable ({operation})") from exc


def _latency(db: Session, since: datetime) -> LatencyStats:
    # The text format SQLAlchemy stores DateTime values in on SQLite (compared as text).
    params = {"since": since.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")}
    durations = f"SELECT ms FROM ({_LATENCY_SQL}) WHERE ms >= 0"  # noqa: S608 - constant SQL
    count, average, maximum = db.execute(
        text(f"SELECT count(*), avg(ms), max(ms) FROM ({durations})"),  # noqa: S608
        params,
    ).one()
    if not count:
        return LatencyStats(0, None, None, None, None)

    def percentile(fraction: float) -> float:
        offset = min(count - 1, int(fraction * count))
        return float(
            db.execute(
                text(f"{durations} ORDER BY ms LIMIT 1 OFFSET :offset"),
                {**params, "offset": offset},
            ).scalar_one()
        )

    return LatencyStats(
        samples=int(count),
        average_ms=float(average),
        median_ms=percentile(0.5),
        p90_ms=percentile(0.9),
        max_ms=float(maximum),
    )
