"""Regression tests for the Stage 13 retrieval and database optimizations (Task 19).

Measured in docs/retrieval-and-database.md; these tests keep the gains and, above all,
the results:

- Analytics give exactly what a plain Python computation over the same rows gives,
  after their queries were merged and the duration statistics moved into one statement.
- The number of statements per analytics view stays down (the chat view used to run
  the whole ticket report for three numbers).
- The new indexes exist, are created on an existing database, and are the ones SQLite
  picks for the ticket list, the assignee filter and the time-range counts.
"""

import statistics
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text

from app.core.staff_auth import StaffMember, StaffRole
from app.database.connection import Database
from app.database.models import ConversationMessageRow, ConversationSessionRow, SupportTicketRow
from app.models.support import ACTIVE_STATUSES
from app.repositories.analytics_repository import (
    _LATENCY_SQL,
    _RESOLUTION_SQL,
    AnalyticsRepository,
    DurationStats,
    _durations,
)
from app.repositories.ticket_repository import TicketFilter, TicketRepository
from app.services.analytics_service import AnalyticsPeriod, AnalyticsService
from scripts.measure_retrieval_db import NOW, Recorder, seed

SINCE = NOW - timedelta(days=20)
UNTIL = NOW + timedelta(days=1)
NEW_INDEXES = {
    "conversation_sessions": {"ix_session_created_at"},
    "support_tickets": {
        "ix_ticket_created",
        "ix_ticket_assignee_created",
        "ix_ticket_resolved_at",
        "ix_ticket_closed_at",
    },
}


@pytest.fixture(scope="module")
def database() -> Any:
    db = Database("sqlite://")
    db.init_schema()
    seed(db, sessions=150, tickets=200, days=30, seed_value=5)
    yield db
    db.dispose()


def rows(database: Database, model: Any) -> list[Any]:
    with database.read() as db:
        result = list(db.scalars(select(model)))
        db.expunge_all()
        return result


def in_range(moment: datetime | None) -> bool:
    return moment is not None and SINCE <= moment < UNTIL


def expected_stats(values: list[float]) -> DurationStats:
    """The documented definition: positions int(0.5 n) and int(0.9 n), ascending."""
    values = sorted(v for v in values if v >= 0)
    if not values:
        return DurationStats(0, None, None, None, None)
    n = len(values)
    return DurationStats(
        samples=n,
        average=statistics.fmean(values),
        median=values[min(n - 1, int(0.5 * n))],
        p90=values[min(n - 1, int(0.9 * n))],
        max=values[-1],
    )


def assert_same_stats(actual: DurationStats, expected: DurationStats) -> None:
    assert actual.samples == expected.samples
    for field in ("average", "median", "p90", "max"):
        got, want = getattr(actual, field), getattr(expected, field)
        assert got == pytest.approx(want, rel=1e-9, abs=1e-9), field


# --- Results are unchanged ---------------------------------------------------------------


@pytest.mark.parametrize("sql", [_LATENCY_SQL, _RESOLUTION_SQL], ids=["latency", "resolution"])
def test_one_pass_duration_statistics_follow_the_definition(database: Database, sql: str) -> None:
    params = {"since": SINCE.strftime("%Y-%m-%d %H:%M:%S.%f"),
              "until": UNTIL.strftime("%Y-%m-%d %H:%M:%S.%f")}  # fmt: skip
    with database.read() as db:
        values = [v for (v,) in db.execute(text(sql), params)]
        actual = _durations(db, sql, SINCE, UNTIL)

    assert len(values) > 20
    assert_same_stats(actual, expected_stats(values))


@pytest.mark.parametrize(
    "values",
    [[], [5.0], [3.0, 1.0], [2.0, 2.0, 2.0], [-1.0, 4.0, 9.0], [float(v) for v in range(1, 11)]],
    ids=["none", "one", "two", "ties", "negative dropped", "ten"],
)
def test_duration_statistics_edge_cases(values: list[float]) -> None:
    rows_sql = " UNION ALL ".join(f"SELECT {v} AS value" for v in values) or (
        "SELECT 1.0 AS value WHERE 0"
    )
    sql = f"SELECT value FROM ({rows_sql}) WHERE :since <> :until"  # noqa: S608 - literals
    db = Database("sqlite://")
    with db.read() as session:
        actual = _durations(session, sql, SINCE, UNTIL)
    db.dispose()

    assert_same_stats(actual, expected_stats(values))


def test_conversation_activity_matches_the_stored_rows(database: Database) -> None:
    sessions = rows(database, ConversationSessionRow)
    messages = rows(database, ConversationMessageRow)
    replies = [m for m in messages if m.role == "assistant" and in_range(m.created_at)]

    activity = AnalyticsRepository(database).conversation_activity(since=SINCE, until=UNTIL)

    assert activity.stored_conversations == len(sessions)
    assert activity.conversations_started == sum(in_range(s.created_at) for s in sessions)
    assert activity.customer_messages == sum(
        m.role == "user" and in_range(m.created_at) for m in messages
    )
    assert activity.chatbot_responses == len(replies)
    assert activity.responses_by_decision == dict(Counter(m.decision for m in replies))
    assert activity.escalation_offers_by_reason == dict(
        Counter(m.escalation_reason for m in replies if m.escalation_reason)
    )


def test_ticket_metrics_match_the_stored_rows(database: Database) -> None:
    tickets = rows(database, SupportTicketRow)
    created = [t for t in tickets if in_range(t.created_at)]
    active = {s.value for s in ACTIVE_STATUSES}
    backlog = [t for t in tickets if t.status in active]

    m = AnalyticsRepository(database).ticket_metrics(since=SINCE, until=UNTIL)

    assert m.created_by_type == dict(Counter(t.ticket_type for t in created))
    assert m.created_by_status == dict(Counter(t.status for t in created))
    assert m.created_by_priority == dict(Counter(t.priority for t in created))
    assert m.created_by_lead_category == dict(
        Counter(t.lead_category for t in created if t.lead_category)
    )
    assert m.created_after_offer == sum(t.escalation_reason is not None for t in created)
    assert m.resolved == sum(in_range(t.resolved_at) for t in tickets)
    assert m.closed == sum(in_range(t.closed_at) for t in tickets)
    assert m.backlog_by_status == dict(Counter(t.status for t in backlog))
    assert m.backlog_unassigned == sum(t.assigned_to is None for t in backlog)
    assert m.oldest_backlog_created_at == min(t.created_at for t in backlog)
    daily_created = Counter(t.created_at.date() for t in created)
    daily_resolved = Counter(t.resolved_at.date() for t in tickets if in_range(t.resolved_at))
    assert m.daily == [
        (day, daily_created.get(day, 0), daily_resolved.get(day, 0)) for day, _, _ in m.daily
    ]
    assert sum(created for _, created, _ in m.daily) == len(created)


def test_the_chat_view_ticket_counts_match_the_ticket_report(database: Database) -> None:
    repository = AnalyticsRepository(database)

    counts = repository.ticket_activity(since=SINCE, until=UNTIL)
    report = repository.ticket_metrics(since=SINCE, until=UNTIL)

    assert counts == {
        "support": report.created_by_type.get("support", 0),
        "sales": report.created_by_type.get("sales", 0),
        "after_chat_offer": report.created_after_offer,
    }


# --- Fewer statements ------------------------------------------------------------------


def statements(database: Database, operation: Any) -> int:
    """SELECT statements run by ``operation`` (BEGIN and COMMIT are not queries)."""
    recorder = Recorder(database.engine)
    with recorder.recording():
        operation()
    return sum(sql.lstrip().upper().startswith("SELECT") for sql, _ in recorder.statements)


def test_analytics_run_few_statements(database: Database) -> None:
    repository = AnalyticsRepository(database)
    service = AnalyticsService(repository, conversation_ttl_minutes=60)
    staff = StaffMember("alex", StaffRole.ADMIN)
    period = AnalyticsPeriod.resolve(days=30, today=NOW.date())

    # Was 14: one grouped pass for every breakdown of the created tickets.
    assert statements(database, lambda: repository.ticket_metrics(since=SINCE, until=UNTIL)) == 5
    # Was the whole ticket report (14) for three numbers.
    assert statements(database, lambda: repository.ticket_activity(since=SINCE, until=UNTIL)) == 1
    # Was 29.
    assert statements(database, lambda: service.chat_analytics(staff, period=period, now=NOW)) <= 12


# --- Indexes ---------------------------------------------------------------------------


def index_names(database: Database, table: str) -> set[str]:
    with database.engine.connect() as connection:
        return {
            name
            for (name,) in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?", (table,)
            )
        }


def test_an_existing_database_gets_the_new_indexes_at_startup(tmp_path: Any) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    database.init_schema()
    with database.engine.begin() as connection:
        for names in NEW_INDEXES.values():
            for name in names:
                connection.exec_driver_sql(f"DROP INDEX {name}")

    database.init_schema()  # what every start does

    for table, names in NEW_INDEXES.items():
        assert names <= index_names(database, table)
    database.dispose()


def plans(database: Database, operation: Any) -> list[str]:
    recorder = Recorder(database.engine)
    with recorder.recording():
        operation()
    return [step for plan in recorder.plans() for step in plan["plan"]]


def test_the_ticket_list_reads_one_page_in_index_order(database: Database) -> None:
    tickets = TicketRepository(database)

    steps = plans(database, lambda: tickets.list_tickets(offset=100, limit=50))

    assert any("USING INDEX ix_ticket_created" in step for step in steps)
    assert not any("TEMP B-TREE" in step for step in steps)  # no sort of the whole table


def test_the_assignee_filter_uses_its_index(database: Database) -> None:
    tickets = TicketRepository(database)

    steps = plans(database, lambda: tickets.list_tickets(TicketFilter(assigned_to="maria")))

    assert any("ix_ticket_assignee_created" in step for step in steps)
    assert not any(step.startswith("SCAN support_tickets") for step in steps)


def test_time_range_counts_use_indexes_not_full_scans(database: Database) -> None:
    repository = AnalyticsRepository(database)

    started = plans(database, lambda: repository.stored_conversations(since=SINCE))
    report = plans(database, lambda: repository.ticket_metrics(since=SINCE, until=UNTIL))

    assert any("ix_session_created_at" in step for step in started)
    assert any("ix_ticket_resolved_at" in step for step in report)
    assert any("ix_ticket_closed_at" in step for step in report)


def test_listing_still_pages_correctly_in_creation_order(database: Database) -> None:
    tickets = TicketRepository(database)
    everything = sorted(rows(database, SupportTicketRow), key=lambda t: (t.created_at, t.ticket_id))

    pages = [tickets.list_tickets(offset=start, limit=50) for start in range(0, 200, 50)]

    assert [t.ticket_id for page in pages for t in page.tickets] == [
        t.ticket_id for t in everything
    ]
    assert {page.total for page in pages} == {200}
