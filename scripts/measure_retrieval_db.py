"""Measure retrieval and database operations on a production-sized database (Stage 13).

Usage (from the project root, virtual environment active):

    python scripts/measure_retrieval_db.py --output data/retrieval_db_profile.json
    python scripts/measure_retrieval_db.py --sessions 2000 --tickets 2000   # quicker

Everything runs in this process on a temporary SQLite file (never data/conversations.db),
with no model download and no network:

1. **Seed** ``--sessions`` conversations (2-12 messages each) and ``--tickets`` support
   tickets (with contacts and events) spread over ``--days`` days (default 365, the
   ticket retention period), with the real schema and constraints. Tickets older than
   two weeks are mostly resolved or closed, as in a working support queue.
2. **Database operations**: each one ``--repeat`` times (median reported) through the
   real repositories and services: staff analytics and the dashboard overview, ticket
   list pages and filters, a ticket's events and notes, conversation history pages and
   the recent-history read used by every chat turn, the audit log. Every SQL statement
   they run is captured and explained (``EXPLAIN QUERY PLAN``): ``SCAN <table>`` is a
   full table scan, ``SEARCH ... USING INDEX`` an index lookup, ``USE TEMP B-TREE`` a sort.
3. **Per chat turn and per FAQ edit**, through the real application (TestClient) on a
   fresh database: SQL statements, transactions and embedding calls per chat turn; texts
   embedded and index builds after an answer edit and a question edit.
4. **Retrieval scaling**: keyword search and the semantic matrix product over the 30
   real FAQs and a synthetic catalogue of ``--synthetic-faqs`` FAQs. The query embedding
   itself (about 17-28 ms with the real model, docs/performance-profile.md) is not
   repeated here: a deterministic stand-in encoder is used.

Output: timings, statement counts and plans only; no message or ticket text.
"""

import argparse
import hashlib
import json
import logging
import os
import random
import statistics
import sys
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/measure_retrieval_db.py`
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np  # noqa: E402
from sqlalchemy import event, insert  # noqa: E402

from app.database.connection import Database  # noqa: E402
from app.database.models import (  # noqa: E402
    AdminAuditEventRow,
    ConversationMessageRow,
    ConversationSessionRow,
    CustomerContactRow,
    SupportTicketRow,
    TicketEventRow,
    TicketNoteRow,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
DECISIONS = ["answered"] * 7 + ["no_match", "ambiguous", "retrieval_unavailable"]
STATUSES = ["open", "assigned", "in_progress", "waiting_for_customer", "resolved", "closed"]
STAFF = ["alex", "maria", "sam", "lee"]


# --- Seeding ---------------------------------------------------------------------------


def ticket_status(rng: random.Random, age: timedelta) -> str:
    """A status that fits the ticket's age: recent tickets in every state, older ones
    almost all resolved or closed (as a working support team would leave them)."""
    if age < timedelta(days=14):
        return rng.choice(STATUSES)
    return rng.choices(STATUSES, weights=[1, 1, 1, 2, 40, 55])[0]


def reference(prefix: str, index: int) -> str:
    """A unique, valid reference number (13 characters, e.g. SUP-0001-0001)."""
    return f"{prefix}-{index // 10000:04d}-{index % 10000:04d}"


def seed(database: Database, *, sessions: int, tickets: int, days: int, seed_value: int) -> dict:
    """Insert realistic rows in bulk (valid under every CHECK constraint)."""
    rng = random.Random(seed_value)  # noqa: S311 - test data, not security
    span = timedelta(days=days)
    started = time.perf_counter()
    session_ids: list[uuid.UUID] = []
    counts: Counter[str] = Counter()
    batch_sessions: list[dict] = []
    batch_messages: list[dict] = []

    def flush(db: Any) -> None:
        if batch_sessions:
            db.execute(insert(ConversationSessionRow), batch_sessions)
            batch_sessions.clear()
        if batch_messages:
            db.execute(insert(ConversationMessageRow), batch_messages)
            batch_messages.clear()

    with database.write() as db:
        for _ in range(sessions):
            created = NOW - span * rng.random()
            sid = uuid.uuid4()
            session_ids.append(sid)
            turns = rng.randint(1, 6)
            batch_sessions.append({
                "session_id": sid, "created_at": created,
                "updated_at": created + timedelta(minutes=turns),
                "expires_at": created + timedelta(minutes=turns + 60),
                "status": "active" if rng.random() < 0.7 else "closed",
                "access_token_hash": hashlib.sha256(sid.bytes).hexdigest(),
                "processing_key": None, "processing_until": None,
            })  # fmt: skip
            for turn in range(turns):
                at = created + timedelta(minutes=turn)
                batch_messages.append({
                    "message_id": uuid.uuid4(), "session_id": sid, "sequence": 2 * turn + 1,
                    "role": "user", "content": "How can I track my order?", "created_at": at,
                    "decision": None, "source_faq_ids": [],
                    "idempotency_key": str(uuid.uuid4()), "request_fingerprint": "f" * 64,
                    "generation_method": None, "escalation_reason": None,
                })  # fmt: skip
                decision = rng.choice(DECISIONS)
                batch_messages.append({
                    "message_id": uuid.uuid4(), "session_id": sid, "sequence": 2 * turn + 2,
                    "role": "assistant", "content": "You can track your order from the link.",
                    "created_at": at + timedelta(milliseconds=rng.randint(20, 3000)),
                    "decision": decision,
                    "source_faq_ids": ["faq_001"] if decision == "answered" else [],
                    "idempotency_key": None, "request_fingerprint": None,
                    "generation_method": "approved_faq" if decision == "answered" else "fallback",
                    "escalation_reason": "repeated_no_match" if rng.random() < 0.03 else None,
                })  # fmt: skip
                counts["messages"] += 2
            if len(batch_messages) > 20_000:
                flush(db)
        flush(db)
        counts["sessions"] = sessions

        contacts, rows, events, notes = [], [], [], []
        for index in range(tickets):
            created = NOW - span * rng.random()
            ticket_type = "sales" if rng.random() < 0.25 else "support"
            status = ticket_status(rng, NOW - created)
            assigned = rng.choice(STAFF) if status != "open" and rng.random() < 0.8 else None
            if status == "assigned" and assigned is None:
                assigned = STAFF[0]
            resolved = created + timedelta(hours=rng.uniform(1, 72))
            contact_id = uuid.uuid4()
            contacts.append({
                "contact_id": contact_id, "name": "Example Customer",
                "email": f"customer{index}@example.com", "phone": None,
                "contact_preference": "email", "consent_given": True,
                "consent_timestamp": created, "created_at": created, "updated_at": created,
                "identity_hash": hashlib.sha256(f"contact{index}".encode()).hexdigest(),
                "anonymized_at": None,
            })  # fmt: skip
            ticket_id = uuid.uuid4()
            prefix = "SAL" if ticket_type == "sales" else "SUP"
            rows.append({
                "ticket_id": ticket_id, "reference_number": reference(prefix, index),
                "session_id": rng.choice(session_ids) if rng.random() < 0.6 else None,
                "contact_id": contact_id, "ticket_type": ticket_type,
                "lead_category": "bulk_order" if ticket_type == "sales" else None,
                "escalation_reason": "customer_requested" if rng.random() < 0.4 else None,
                "subject": "Refund assistance", "description": "I need help with a refund.",
                "status": status, "priority": rng.choice(["low", "normal", "high", "urgent"]),
                "assigned_to": assigned, "assigned_at": created if assigned else None,
                "created_at": created,
                "updated_at": resolved if status in ("resolved", "closed") else created,
                "resolved_at": resolved if status in ("resolved", "closed") else None,
                "closed_at": resolved if status == "closed" else None,
                "idempotency_key": str(uuid.uuid4()), "request_fingerprint": "e" * 64,
                "anonymized_at": None,
            })  # fmt: skip
            events.append({
                "event_id": uuid.uuid4(), "ticket_id": ticket_id, "sequence": 1,
                "event_type": "created", "previous_status": None, "new_status": "open",
                "actor_type": "customer", "actor_id": None, "assigned_to": None,
                "previous_assigned_to": None, "created_at": created,
            })  # fmt: skip
            if status != "open":
                events.append({
                    "event_id": uuid.uuid4(), "ticket_id": ticket_id, "sequence": 2,
                    "event_type": "status_changed", "previous_status": "open",
                    "new_status": status, "actor_type": "staff", "actor_id": assigned or "alex",
                    "assigned_to": assigned if status == "assigned" else None,
                    "previous_assigned_to": None, "created_at": created,
                })  # fmt: skip
            for n in range(rng.choice([0, 0, 1, 2, 3])):
                notes.append({
                    "note_id": uuid.uuid4(), "ticket_id": ticket_id,
                    "client_note_id": str(uuid.uuid4()), "author_id": "alex",
                    "body": "Called the customer.", "created_at": created + timedelta(hours=n),
                })  # fmt: skip
        for table, batch in ((CustomerContactRow, contacts), (SupportTicketRow, rows),
                             (TicketEventRow, events), (TicketNoteRow, notes)):  # fmt: skip
            for start in range(0, len(batch), 5000):
                db.execute(insert(table), batch[start : start + 5000])
        audit = [
            {
                "event_id": uuid.uuid4(),
                "created_at": NOW - span * rng.random(),
                "actor_type": "staff",
                "actor_id": rng.choice(STAFF),
                "action": "ticket.status_changed",
                "resource_type": "ticket",
                "resource_id": str(uuid.uuid4()),
                "outcome": "success",
                "request_id": None,
                "details": {},
            }  # fmt: skip
            for _ in range(tickets)
        ]
        for start in range(0, len(audit), 5000):
            db.execute(insert(AdminAuditEventRow), audit[start : start + 5000])
        counts.update(tickets=tickets, contacts=len(contacts), ticket_events=len(events),
                      ticket_notes=len(notes), audit_events=len(audit))  # fmt: skip
    with database.engine.connect() as connection:
        connection.exec_driver_sql("ANALYZE")  # what a long-running database would have
    counts["seed_seconds"] = round(time.perf_counter() - started, 1)
    return dict(counts)


# --- Statement capture and plans -------------------------------------------------------


class Recorder:
    """Captures SQL statements (text and parameters) and transactions on an engine."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.statements: list[tuple[str, Any]] = []
        self.transactions = 0
        self.active = False
        event.listen(engine, "before_cursor_execute", self._statement)
        event.listen(engine, "begin", self._begin)

    def _statement(self, conn: Any, cursor: Any, statement: str, parameters: Any, *_: Any) -> None:
        if self.active and not statement.lstrip().upper().startswith(("PRAGMA", "EXPLAIN")):
            self.statements.append((statement, parameters))

    def _begin(self, conn: Any) -> None:
        if self.active:
            self.transactions += 1

    @contextmanager
    def recording(self) -> Iterator["Recorder"]:
        self.statements, self.transactions, self.active = [], 0, True
        try:
            yield self
        finally:
            self.active = False

    def plans(self) -> list[dict[str, Any]]:
        """``EXPLAIN QUERY PLAN`` for each distinct SELECT captured."""
        seen, result = set(), []
        with self.engine.connect() as connection:
            for statement, parameters in self.statements:
                if not statement.lstrip().upper().startswith(("SELECT", "WITH")):
                    continue
                if statement in seen:
                    continue
                seen.add(statement)
                rows = connection.exec_driver_sql(
                    f"EXPLAIN QUERY PLAN {statement}", parameters
                ).all()
                plan = [row[-1] for row in rows]
                result.append({
                    "sql": " ".join(statement.split())[:160],
                    "plan": plan,
                    "full_scans": [p for p in plan if p.startswith("SCAN ") and "USING" not in p],
                    "sorts": [p for p in plan if "TEMP B-TREE" in p],
                })  # fmt: skip
        return result


def timed(operation: Callable[[], Any], repeat: int) -> list[float]:
    times = []
    for _ in range(repeat):
        started = time.perf_counter()
        operation()
        times.append((time.perf_counter() - started) * 1000)
    return times


def measure_operations(database: Database, repeat: int) -> dict[str, Any]:
    from app.core.staff_auth import StaffMember, StaffRole
    from app.models.support import TicketStatus
    from app.repositories.analytics_repository import AnalyticsRepository
    from app.repositories.audit_repository import AuditRepository
    from app.repositories.conversation_repository import ConversationRepository
    from app.repositories.ticket_repository import TicketFilter, TicketRepository
    from app.services.admin_dashboard_service import AdminDashboardService
    from app.services.analytics_service import AnalyticsPeriod, AnalyticsService

    analytics = AnalyticsRepository(database)
    tickets = TicketRepository(database)
    conversations = ConversationRepository(database)
    audit = AuditRepository(database)
    staff = StaffMember("alex", StaffRole.ADMIN)
    service = AnalyticsService(analytics, conversation_ttl_minutes=60)
    dashboard = AdminDashboardService(None, tickets, analytics)
    week = AnalyticsPeriod.resolve(days=7, today=NOW.date())
    month = AnalyticsPeriod.resolve(days=30, today=NOW.date())

    with database.read() as db:
        sample_session = db.scalars(
            ConversationSessionRow.__table__.select()
            .with_only_columns(ConversationSessionRow.session_id)
            .limit(1)
        ).one()
        busy_ticket = db.scalars(
            TicketNoteRow.__table__.select().with_only_columns(TicketNoteRow.ticket_id).limit(1)
        ).one()
        total_tickets = db.query(SupportTicketRow).count()

    operations: dict[str, Callable[[], Any]] = {
        "dashboard_overview": lambda: dashboard.overview(staff, now=NOW),
        "chat_analytics_7d": lambda: service.chat_analytics(staff, period=week, now=NOW),
        "chat_analytics_30d": lambda: service.chat_analytics(staff, period=month, now=NOW),
        "ticket_analytics_30d": lambda: service.ticket_analytics(staff, month, now=NOW),
        "tickets_first_page": lambda: tickets.list_tickets(offset=0, limit=50),
        "tickets_last_page": lambda: tickets.list_tickets(
            offset=max(0, total_tickets - 50), limit=50
        ),  # fmt: skip
        "tickets_open_queue": lambda: tickets.list_tickets(
            TicketFilter(statuses=frozenset({TicketStatus.OPEN})), limit=50
        ),
        "tickets_assigned_to": lambda: tickets.list_tickets(
            TicketFilter(assigned_to="maria"), limit=50
        ),  # fmt: skip
        "tickets_created_last_7d": lambda: tickets.list_tickets(
            TicketFilter(created_from=NOW - timedelta(days=7)), limit=50
        ),
        "ticket_events": lambda: tickets.list_events(busy_ticket),
        "ticket_notes": lambda: tickets.list_notes(busy_ticket),
        "history_page": lambda: conversations.get_messages_page(
            sample_session, offset=0, limit=50
        ),  # fmt: skip
        "recent_history_for_a_turn": lambda: conversations.list_recent_messages(sample_session, 10),
        "audit_first_page": lambda: audit.list_events(offset=0, limit=50),
    }
    recorder = Recorder(database.engine)
    results: dict[str, Any] = {}
    for name, operation in operations.items():
        operation()  # warm the page cache and SQLAlchemy's statement cache
        with recorder.recording():
            operation()
        plans = recorder.plans()
        times = timed(operation, repeat)
        results[name] = {
            "median_ms": round(statistics.median(times), 2),
            "max_ms": round(max(times), 2),
            "statements": len(recorder.statements),
            "transactions": recorder.transactions,
            "full_scans": sorted({s for p in plans for s in p["full_scans"]}),
            "sorts": sorted({s for p in plans for s in p["sorts"]}),
            "plans": plans,
        }
    return results


# --- Per chat turn and per FAQ edit ----------------------------------------------------


class StandInEncoder:
    """Deterministic bag-of-words vectors; counts what it embeds."""

    dimension = 64

    def __init__(self) -> None:
        self.calls = 0
        self.texts = 0

    def get_embedding_dimension(self) -> int:
        return self.dimension

    def encode(self, sentences: list[str], **kwargs: Any) -> np.ndarray:
        self.calls += 1
        self.texts += len(sentences)
        vectors = np.zeros((len(sentences), self.dimension), dtype=np.float32)
        for row, sentence in enumerate(sentences):
            for word in sentence.lower().split():
                vectors[row, int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dimension] += 1  # noqa: S324
            vectors[row] /= max(np.linalg.norm(vectors[row]), 1e-9)
        return vectors


def measure_turns(workdir: Path, turns: int) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    from app.core.config import Settings
    from app.core.staff_auth import generate_staff_token, hash_staff_token
    from app.main import create_app
    from app.services.embedding_service import EmbeddingService
    from app.services.semantic_search_service import SemanticSearchService

    token = generate_staff_token()
    settings = Settings(
        _env_file=None,
        app_env="development",
        database_url=f"sqlite:///{(workdir / 'turns.db').as_posix()}",
        rate_limit_enabled=False,
        llm_enabled=False,
        query_rewrite_use_llm=False,
        staff_token_hashes=[f"perf:admin:{hash_staff_token(token)}"],
    )
    database = Database(settings.database_url)
    encoder = StandInEncoder()
    holder: list[Any] = []
    semantic = SemanticSearchService(
        lambda: holder[0].state.faq_service.list_active_faqs(),
        EmbeddingService("stand-in", loader=lambda name: encoder),
        catalog_revision=lambda: holder[0].state.faq_service.catalog_revision(),
    )
    app = create_app(settings, database=database, semantic_search_service=semantic)
    holder.append(app)
    recorder = Recorder(database.engine)
    staff = {"Authorization": f"Bearer {token}"}
    questions = ["How can I track my order?", "What is your return policy?",
                 "I want my money back", "Do you sell submarines?"]  # fmt: skip

    result: dict[str, Any] = {}
    with TestClient(app) as client:
        created = client.post("/api/v1/conversations").json()
        headers = {"Authorization": f"Bearer {created['access_token']}"}
        url = f"/api/v1/conversations/{created['session_id']}/chat"
        client.post(url, json={"message": questions[0]}, headers=headers)  # builds the index
        per_turn: list[dict[str, Any]] = []
        encoder.calls = encoder.texts = 0
        for index in range(turns):
            with recorder.recording():
                client.post(url, json={"message": questions[index % len(questions)]},
                            headers=headers).raise_for_status()  # fmt: skip
            kinds = Counter(s.lstrip().split()[0].upper() for s, _ in recorder.statements)
            per_turn.append({"statements": len(recorder.statements),
                             "transactions": recorder.transactions, **kinds})  # fmt: skip
        result["chat_turn"] = {
            "turns": turns,
            "statements_max": max(t["statements"] for t in per_turn),
            "statements_median": statistics.median(t["statements"] for t in per_turn),
            "transactions_median": statistics.median(t["transactions"] for t in per_turn),
            "by_kind_last_turn": {
                k: v for k, v in per_turn[-1].items() if k not in ("statements", "transactions")
            },  # fmt: skip
            "embedding_calls_per_turn": encoder.calls / turns,
            "texts_embedded_per_turn": encoder.texts / turns,
        }

        faq = client.get("/api/v1/admin/faqs/faq_001", headers=staff).json()
        index_before = semantic.get_index()
        encoder.calls = encoder.texts = 0
        client.patch("/api/v1/admin/faqs/faq_001", headers=staff, json={
            "expected_version": faq["version"], "answer": faq["answer"] + " Updated.",
        }).raise_for_status()  # fmt: skip
        answer_edit = {"texts_embedded": encoder.texts,
                       "index_replaced": semantic.get_index() is not index_before}  # fmt: skip
        faq = client.get("/api/v1/admin/faqs/faq_001", headers=staff).json()
        encoder.calls = encoder.texts = 0
        client.patch("/api/v1/admin/faqs/faq_001", headers=staff, json={
            "expected_version": faq["version"], "question": faq["question"] + " now",
        }).raise_for_status()  # fmt: skip
        question_edit = {"texts_embedded": encoder.texts}
        index_after_edit = semantic.get_index()
        for index in range(turns):
            client.post(url, json={"message": questions[index % len(questions)]}, headers=headers)
        rebuilt = semantic.get_index() is not index_after_edit
        result["faq_edits"] = {
            "answer_edit": answer_edit,
            "question_edit": question_edit,
            f"index_rebuilt_during_next_{turns}_turns": rebuilt,
        }
    database.dispose()
    return result


# --- Retrieval scaling -----------------------------------------------------------------


def measure_retrieval(synthetic: int, repeat: int) -> dict[str, Any]:
    from app.database.faq_repository import FAQRepository
    from app.models.faq import FAQ
    from app.services.keyword_search_service import KeywordSearchService

    real = FAQRepository.from_json_file(PROJECT_ROOT / "data" / "faqs.json").list_all()
    words = " ".join(f.question + " " + f.answer for f in real).split()
    rng = random.Random(7)  # noqa: S311 - test data, not security
    fake = [
        FAQ.model_validate(
            {
                **real[i % len(real)].model_dump(),
                "id": f"faq_{9000 + i}",
                "question": " ".join(rng.choice(words) for _ in range(9)) + "?",
            }
        )  # fmt: skip
        for i in range(synthetic)
    ]
    result: dict[str, Any] = {}
    for label, faqs in (("real_30", real), (f"synthetic_{synthetic}", real + fake)):
        keyword = KeywordSearchService(lambda faqs=faqs: faqs)
        keyword.search("track my order")  # prepares (caches) every FAQ once
        times = timed(lambda keyword=keyword: keyword.search("how do I return an item"), repeat)
        matrix = np.random.default_rng(1).standard_normal((len(faqs), 384))
        matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
        query = matrix[0]
        dot = timed(lambda matrix=matrix, query=query: np.argsort(-(matrix @ query))[:10], repeat)
        result[label] = {
            "faqs": len(faqs),
            "keyword_search_median_ms": round(statistics.median(times), 3),
            "semantic_matrix_product_median_ms": round(statistics.median(dot), 3),
        }
    return result


# --- Main ------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sessions", type=int, default=20_000)
    parser.add_argument("--tickets", type=int, default=20_000)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--repeat", type=int, default=7)
    parser.add_argument("--turns", type=int, default=12)
    parser.add_argument("--synthetic-faqs", type=int, default=2_000)
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    logging.disable(logging.WARNING)

    output: dict[str, Any] = {"measured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                              "python": sys.version.split()[0]}  # fmt: skip
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as workdir:
        database = Database(f"sqlite:///{(Path(workdir) / 'volume.db').as_posix()}")
        database.init_schema()
        output["volume"] = seed(database, sessions=args.sessions, tickets=args.tickets,
                                days=args.days, seed_value=args.seed)  # fmt: skip
        output["operations"] = measure_operations(database, args.repeat)
        database.dispose()
        output["per_turn"] = measure_turns(Path(workdir), args.turns)
    output["retrieval"] = measure_retrieval(args.synthetic_faqs, args.repeat * 5)

    print(f"Volume: {output['volume']}")
    print(f"\n{'operation':28} {'median ms':>10} {'stmts':>6} {'tx':>3}  full scans / sorts")
    for name, row in output["operations"].items():
        notes = ", ".join(row["full_scans"] + row["sorts"]) or "-"
        print(f"{name:28} {row['median_ms']:>10} {row['statements']:>6} "
              f"{row['transactions']:>3}  {notes}")  # fmt: skip
    print(f"\nPer chat turn: {output['per_turn']['chat_turn']}")
    print(f"FAQ edits: {output['per_turn']['faq_edits']}")
    print(f"\nRetrieval: {output['retrieval']}")
    if args.output:
        args.output.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
        print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
