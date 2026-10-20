"""Bounded local concurrency evaluation: correctness and latency under load (Stage 13).

Usage (from the project root, virtual environment active):

    python scripts/evaluate_concurrency.py --output data/concurrency_results.json
    python scripts/evaluate_concurrency.py --encoder lexical --concurrency 2   # lighter

The real application runs under uvicorn on 127.0.0.1 in this process (Stage 12's
``LocalServer``), with a temporary SQLite database seeded from ``data/faqs.json``, the
locally cached embedding model (or ``--encoder lexical``) and a **mocked LLM** (the
scripted model of ``scripts/ai_eval_harness.py``: no network, no paid call; the LLM path
runs, its latency is not OpenAI's). All data is synthetic. Rate limits are off in this
server so that concurrency, not the limiter, is exercised.

Scenarios (at most ``--concurrency`` requests in flight, never more than 8):

1. ``multiple_sessions``: many conversations chatting at once, one message at a time each.
2. ``concurrent_chat``: several messages sent at once *to one conversation*, and the
   same message retried at once with one ``client_message_id``.
3. ``simultaneous_tickets``: support requests from many conversations at once, and the
   same request submitted at once several times.
4. ``faq_updates_during_retrieval``: staff edit an FAQ's answer in a loop (and two
   editors race on one FAQ) while customers ask about it.
5. ``admin_analytics_during_activity``: dashboard, analytics and monitoring reads while
   customers chat; chat latency compared with scenario 1.
6. ``database_write_contention``: conversations, chat turns, tickets and FAQ edits all
   at once, at the concurrency limit.

Then ``--soak-rounds`` repetitions of a mixed workload to watch memory growth.

Verified afterwards, directly in the database: integrity and foreign keys; every
conversation's messages numbered 1..n, alternating customer/reply, no lease left;
every acknowledged write stored exactly once and nothing stored for a refused request;
retry keys used once; one ticket per ``client_request_id``, each with its creation event;
isolation (each conversation holds only its own messages; another conversation's token
gets 404); every answered reply equal to an approved version of its FAQ's answer.

Memory is the resident memory of this process (server and client together).
"""

import argparse
import asyncio
import gc
import json
import os
import sqlite3
import sys
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/evaluate_concurrency.py`
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import httpx  # noqa: E402

from app.core.staff_auth import generate_staff_token, hash_staff_token  # noqa: E402
from app.database.connection import Database  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.llm_service import LLMService  # noqa: E402
from app.services.semantic_search_service import SemanticSearchService  # noqa: E402
from scripts.ai_eval_harness import (  # noqa: E402
    DEFAULT_FAQ_DATA,
    ScriptedModel,
    embedding_service,
    harness_settings,
)
from scripts.load_test_local import LocalServer, latency_summary  # noqa: E402

MAX_CONCURRENCY = 8
API = "/api/v1"
QUESTIONS = [
    "How can I track my order?",
    "What is your return policy?",
    "How long does delivery take?",
    "How do I reset my password?",
    "What payment methods do you accept?",
    "Where will my refund be sent?",
]
EDITED_FAQ = "faq_002"  # the FAQ staff edit in scenarios 4 and 6


def rss_mb() -> float:
    import psutil

    return round(psutil.Process().memory_info().rss / 2**20, 1)


class Recorder:
    """Latencies and outcomes of one scenario; every non-2xx response is kept."""

    def __init__(self) -> None:
        self.latencies: dict[str, list[float]] = {}
        self.statuses: dict[str, Counter[str]] = {}
        self.failures: list[dict[str, Any]] = []

    async def call(self, kind: str, request: Awaitable[httpx.Response]) -> httpx.Response | None:
        started = time.perf_counter()
        try:
            response = await request
        except httpx.HTTPError as exc:
            self._add(kind, type(exc).__name__, started)
            self.failures.append({"kind": kind, "error": type(exc).__name__})
            return None
        outcome = str(response.status_code)
        if response.status_code >= 400:
            try:
                code = response.json()["error"]["code"]
            except (ValueError, KeyError, TypeError):
                code = "-"
            outcome = f"{response.status_code} {code}"
            self.failures.append({
                "kind": kind, "status": response.status_code, "code": code,
                "retry_after": response.headers.get("Retry-After"),
            })  # fmt: skip
        self._add(kind, outcome, started)
        return response

    def _add(self, kind: str, outcome: str, started: float) -> None:
        self.latencies.setdefault(kind, []).append((time.perf_counter() - started) * 1000)
        self.statuses.setdefault(kind, Counter())[outcome] += 1

    def report(self) -> dict[str, Any]:
        return {
            kind: {"latency": latency_summary(values), "outcomes": dict(self.statuses[kind])}
            for kind, values in self.latencies.items()
        }


class Ledger:
    """What the clients were told succeeded, to check the database against."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}  # session_id -> token, marker, keys
        self.acknowledged_replies: set[str] = set()  # reply message IDs from 200s
        self.acknowledged_keys: set[tuple[str, str]] = set()  # (session, client_message_id)
        self.refused_keys: set[tuple[str, str]] = set()  # keys only ever refused
        self.tickets: dict[str, str] = {}  # client_request_id -> ticket_id
        self.answers: list[dict[str, Any]] = []  # answered replies, for the FAQ check


class Run:
    def __init__(self, client: httpx.AsyncClient, staff: dict[str, str], limit: int) -> None:
        self.client, self.staff = client, staff
        self.semaphore = asyncio.Semaphore(limit)
        self.ledger = Ledger()

    async def bounded(self, coroutine: Awaitable[Any]) -> Any:
        async with self.semaphore:
            return await coroutine

    async def new_session(self, rec: Recorder) -> str | None:
        response = await rec.call("create_conversation", self.client.post(f"{API}/conversations"))
        if response is None or response.status_code != 201:
            return None
        body = response.json()
        sid = body["session_id"]
        self.ledger.sessions[sid] = {
            "headers": {"Authorization": f"Bearer {body['access_token']}"},
            "marker": f"ref-{uuid.uuid4().hex[:10]}",
        }
        return sid

    async def chat(self, rec: Recorder, sid: str, text: str, key: str | None = None,
                   kind: str = "chat") -> httpx.Response | None:  # fmt: skip
        key = key or str(uuid.uuid4())
        session = self.ledger.sessions[sid]
        message = f"{text} ({session['marker']})"
        response = await rec.call(kind, self.client.post(
            f"{API}/conversations/{sid}/chat",
            json={"message": message, "client_message_id": key},
            headers=session["headers"],
        ))  # fmt: skip
        if response is not None and response.status_code == 200:
            body = response.json()
            self.ledger.acknowledged_replies.add(body["message_id"])
            self.ledger.acknowledged_keys.add((sid, key))
            self.ledger.refused_keys.discard((sid, key))
            if body["decision"] == "answered":
                self.ledger.answers.append({
                    "answer": body["answer"], "sources": [s["faq_id"] for s in body["sources"]],
                })  # fmt: skip
        elif (sid, key) not in self.ledger.acknowledged_keys:
            self.ledger.refused_keys.add((sid, key))
        return response

    async def ticket(self, rec: Recorder, sid: str, request_id: str) -> httpx.Response | None:
        body = {
            "session_id": sid, "client_request_id": request_id, "ticket_type": "support",
            "subject": "Refund assistance", "description": "I need help with a refund request.",
            "contact": {"name": "Synthetic Customer", "email": f"{request_id[:8]}@example.com",
                        "phone": None, "contact_preference": "email", "consent_given": True},
            "confirmed": True,
        }  # fmt: skip
        response = await rec.call("create_ticket", self.client.post(
            f"{API}/support/tickets", json=body, headers=self.ledger.sessions[sid]["headers"]
        ))  # fmt: skip
        if response is not None and response.status_code in (200, 201):
            ticket_id = response.json()["ticket_id"]
            previous = self.ledger.tickets.setdefault(request_id, ticket_id)
            if previous != ticket_id:
                rec.failures.append({"kind": "create_ticket", "error": "two tickets for one ID"})
        return response

    async def edit_faq(self, rec: Recorder, faq_id: str, suffix: str) -> httpx.Response | None:
        current = await rec.call("faq_read", self.client.get(f"{API}/admin/faqs/{faq_id}",
                                                             headers=self.staff))  # fmt: skip
        if current is None or current.status_code != 200:
            return None
        faq = current.json()
        base = faq["answer"].split(" [rev ")[0]
        return await rec.call("faq_edit", self.client.patch(
            f"{API}/admin/faqs/{faq_id}", headers=self.staff,
            json={"expected_version": faq["version"], "answer": f"{base} [rev {suffix}]"},
        ))  # fmt: skip


# --- Scenarios -------------------------------------------------------------------------


async def multiple_sessions(run: Run, sessions: int, turns: int) -> dict[str, Any]:
    rec = Recorder()
    ids = [s for s in await asyncio.gather(
        *(run.bounded(run.new_session(rec)) for _ in range(sessions))) if s]  # fmt: skip

    async def converse(sid: str) -> None:
        for n in range(turns):  # one message at a time per conversation, as the API requires
            await run.bounded(run.chat(rec, sid, QUESTIONS[n % len(QUESTIONS)]))

    started = time.perf_counter()
    await asyncio.gather(*(converse(sid) for sid in ids))
    return {"sessions": len(ids), "turns_each": turns,
            "seconds": round(time.perf_counter() - started, 2), **rec.report(),
            "failures": rec.failures}  # fmt: skip


async def concurrent_chat(run: Run, burst: int) -> dict[str, Any]:
    rec = Recorder()
    sid = await run.new_session(rec)
    if sid is None:
        return {"failures": rec.failures, "error": "no conversation"}
    # Different messages at once to one conversation: one is answered at a time.
    different = await asyncio.gather(*(
        run.bounded(run.chat(rec, sid, QUESTIONS[i % 6], kind="same_session_different_messages"))
        for i in range(burst)
    ))  # fmt: skip
    # The same message (one client_message_id) retried at once.
    key = str(uuid.uuid4())
    retries = await asyncio.gather(*(
        run.bounded(run.chat(rec, sid, QUESTIONS[0], key=key, kind="same_message_retried"))
        for _ in range(burst)
    ))  # fmt: skip
    # Afterwards, once the first finished, a retry returns the stored reply.
    await asyncio.sleep(0.2)
    late = await run.chat(rec, sid, QUESTIONS[0], key=key, kind="retry_after_completion")
    replies = {r.json()["message_id"] for r in retries if r is not None and r.status_code == 200}
    late_reply = late.json()["message_id"] if late is not None and late.status_code == 200 else None
    return {
        "burst": burst, **rec.report(), "failures": rec.failures,
        "answered_of_different_messages": sum(
            1 for r in different if r is not None and r.status_code == 200),
        "distinct_replies_for_one_retried_message": len(replies | ({late_reply} - {None})),
        "late_retry_replayed": late is not None
        and late.headers.get("Idempotent-Replayed") == "true",
    }  # fmt: skip


async def simultaneous_tickets(run: Run, count: int, duplicates: int) -> dict[str, Any]:
    rec = Recorder()
    ids = [s for s in await asyncio.gather(
        *(run.bounded(run.new_session(rec)) for _ in range(count))) if s]  # fmt: skip
    await asyncio.gather(*(run.bounded(run.ticket(rec, sid, str(uuid.uuid4()))) for sid in ids))
    request_id = str(uuid.uuid4())
    same = await asyncio.gather(*(run.bounded(run.ticket(rec, ids[0], request_id))
                                  for _ in range(duplicates)))  # fmt: skip
    created = {r.json()["ticket_id"] for r in same if r is not None and r.status_code in (200, 201)}
    return {"tickets": len(ids), "duplicate_submissions": duplicates, **rec.report(),
            "failures": rec.failures,
            "tickets_for_one_request_id": len(created)}  # fmt: skip


async def faq_updates_during_retrieval(run: Run, sessions: int, turns: int,
                                       edits: int) -> dict[str, Any]:  # fmt: skip
    rec = Recorder()
    ids = [s for s in await asyncio.gather(
        *(run.bounded(run.new_session(rec)) for _ in range(sessions))) if s]  # fmt: skip
    done = asyncio.Event()

    async def editor() -> None:
        n = 0
        while not done.is_set() and n < edits:
            await run.bounded(run.edit_faq(rec, EDITED_FAQ, f"{n:03d}"))
            n += 1

    async def converse(sid: str) -> None:
        for _ in range(turns):  # the question routed to the edited FAQ
            await run.bounded(run.chat(rec, sid, "What is your return policy?"))

    editing = asyncio.create_task(editor())
    await asyncio.gather(*(converse(sid) for sid in ids))
    done.set()
    await editing
    # Two editors racing on the same version: one must win, one get FAQ_VERSION_CONFLICT.
    race = Recorder()
    faq = (await run.client.get(f"{API}/admin/faqs/{EDITED_FAQ}", headers=run.staff)).json()
    racing = await asyncio.gather(*(race.call("faq_edit_race", run.client.patch(
        f"{API}/admin/faqs/{EDITED_FAQ}", headers=run.staff,
        json={"expected_version": faq["version"],
              "answer": faq["answer"].split(" [rev ")[0] + f" [rev race-{i}]"},
    )) for i in range(2)))  # fmt: skip
    return {"sessions": len(ids), "turns_each": turns, **rec.report(), "failures": rec.failures,
            "editor_race": {"outcomes": dict(race.statuses.get("faq_edit_race", {})),
                            "winners": sum(1 for r in racing
                                           if r and r.status_code == 200)}}  # fmt: skip


async def admin_during_activity(run: Run, sessions: int, turns: int,
                                reads: int) -> dict[str, Any]:  # fmt: skip
    rec = Recorder()
    ids = [s for s in await asyncio.gather(
        *(run.bounded(run.new_session(rec)) for _ in range(sessions))) if s]  # fmt: skip
    done = asyncio.Event()
    paths = ["/admin/overview", "/admin/analytics/chat?days=30",
             "/admin/analytics/tickets?days=30", "/admin/monitoring"]  # fmt: skip

    async def staff_reader() -> None:
        n = 0
        while not done.is_set() and n < reads:
            path = paths[n % len(paths)]
            await run.bounded(rec.call(f"staff {path.split('?')[0]}",
                                       run.client.get(API + path, headers=run.staff)))  # fmt: skip
            n += 1

    async def converse(sid: str) -> None:
        for n in range(turns):
            await run.bounded(run.chat(rec, sid, QUESTIONS[n % 6]))

    reader = asyncio.create_task(staff_reader())
    await asyncio.gather(*(converse(sid) for sid in ids))
    done.set()
    await reader
    return {"sessions": len(ids), "turns_each": turns, **rec.report(), "failures": rec.failures}


async def write_contention(run: Run, rounds: int) -> dict[str, Any]:
    rec = Recorder()
    started = time.perf_counter()
    for r in range(rounds):
        sids = [s for s in await asyncio.gather(
            *(run.bounded(run.new_session(rec)) for _ in range(4))) if s]  # fmt: skip
        work: list[Awaitable[Any]] = []
        for i, sid in enumerate(sids):
            work.append(run.bounded(run.chat(rec, sid, QUESTIONS[i % 6])))
            work.append(run.bounded(run.ticket(rec, sid, str(uuid.uuid4()))))
        work.append(run.bounded(run.edit_faq(rec, EDITED_FAQ, f"w{r:03d}")))
        await asyncio.gather(*work)
    return {"rounds": rounds, "seconds": round(time.perf_counter() - started, 2),
            **rec.report(), "failures": rec.failures}  # fmt: skip


# --- Verification ----------------------------------------------------------------------


async def isolation(run: Run, sample: int) -> dict[str, Any]:
    """Each conversation holds only its own messages; another's token is refused."""
    problems: list[str] = []
    sids = list(run.ledger.sessions)[:sample]
    for i, sid in enumerate(sids):
        own = run.ledger.sessions[sid]
        page = await run.client.get(f"{API}/conversations/{sid}/messages?limit=100",
                                    headers=own["headers"])  # fmt: skip
        if page.status_code != 200:
            problems.append(f"history {page.status_code}")
            continue
        for message in page.json()["messages"]:
            if message["role"] == "user" and own["marker"] not in message["content"]:
                problems.append("a message from another conversation")
        other = run.ledger.sessions[sids[(i + 1) % len(sids)]]
        foreign = await run.client.get(f"{API}/conversations/{sid}/messages",
                                       headers=other["headers"])  # fmt: skip
        if len(sids) > 1 and foreign.status_code != 404:
            problems.append(f"another conversation's token got {foreign.status_code}")
    return {"conversations_checked": len(sids), "problems": problems}


def consistency(database_file: Path, ledger: Ledger, answer_versions: set[str]) -> dict[str, Any]:
    """Direct checks on the database file, after the load stopped."""
    db = sqlite3.connect(database_file)
    out: dict[str, Any] = {}
    out["integrity_check"] = db.execute("PRAGMA integrity_check").fetchone()[0]
    out["foreign_key_violations"] = len(db.execute("PRAGMA foreign_key_check").fetchall())
    broken = 0
    for (sid,) in db.execute("SELECT session_id FROM conversation_sessions"):
        rows = db.execute("SELECT sequence, role FROM conversation_messages WHERE session_id = ? "
                          "ORDER BY sequence", (sid,)).fetchall()  # fmt: skip
        numbered = [s for s, _ in rows] == list(range(1, len(rows) + 1))
        alternating = all(role == ("user" if s % 2 else "assistant") for s, role in rows)
        if not (numbered and alternating):
            broken += 1
    out["conversations_misnumbered_or_not_alternating"] = broken
    out["leases_left"] = db.execute(
        "SELECT count(*) FROM conversation_sessions WHERE processing_key IS NOT NULL"
    ).fetchone()[0]

    def hexid(value: str) -> str:
        return uuid.UUID(value).hex

    stored_replies = {r for (r,) in db.execute(
        "SELECT message_id FROM conversation_messages WHERE role = 'assistant'")}  # fmt: skip
    out["acknowledged_replies"] = len(ledger.acknowledged_replies)
    out["acknowledged_replies_missing"] = len(
        {hexid(m) for m in ledger.acknowledged_replies} - stored_replies
    )
    stored_keys = {(s, k) for s, k in db.execute(
        "SELECT session_id, idempotency_key FROM conversation_messages "
        "WHERE idempotency_key IS NOT NULL")}  # fmt: skip
    out["retry_keys_stored_twice"] = db.execute(
        "SELECT count(*) FROM (SELECT 1 FROM conversation_messages WHERE idempotency_key "
        "IS NOT NULL GROUP BY session_id, idempotency_key HAVING count(*) > 1)"
    ).fetchone()[0]
    out["refused_requests_stored_anyway"] = len(
        {(hexid(s), k) for s, k in ledger.refused_keys} & stored_keys
    )
    out["tickets_acknowledged"] = len(ledger.tickets)
    stored_tickets = {t for (t,) in db.execute("SELECT ticket_id FROM support_tickets")}
    out["tickets_acknowledged_missing"] = len(
        {hexid(t) for t in ledger.tickets.values()} - stored_tickets
    )
    out["tickets_stored_without_acknowledgement"] = len(stored_tickets) - len(
        {hexid(t) for t in ledger.tickets.values()}
    )
    out["duplicate_reference_numbers"] = db.execute(
        "SELECT count(*) - count(DISTINCT reference_number) FROM support_tickets"
    ).fetchone()[0]
    out["tickets_without_creation_event"] = db.execute(
        "SELECT count(*) FROM support_tickets t WHERE NOT EXISTS (SELECT 1 FROM ticket_events e "
        "WHERE e.ticket_id = t.ticket_id AND e.sequence = 1 AND e.event_type = 'created')"
    ).fetchone()[0]
    out["faq_revisions_vs_version"] = db.execute(
        "SELECT count(*) FROM faqs f WHERE f.version <> (SELECT count(*) FROM faq_revisions r "
        "WHERE r.faq_id = f.id)"
    ).fetchone()[0]
    db.close()
    out["answered_replies_checked"] = len(ledger.answers)
    out["answered_replies_not_an_approved_version"] = sum(
        1 for a in ledger.answers if a["answer"] not in answer_versions
    )
    return out


# --- Main ------------------------------------------------------------------------------


async def evaluate(base_url: str, staff_token: str, args: argparse.Namespace,
                   memory: dict[str, float]) -> tuple[dict[str, Any], Run]:  # fmt: skip
    limits = httpx.Limits(max_connections=args.concurrency,
                          max_keepalive_connections=args.concurrency)  # fmt: skip
    async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=120) as client:
        run = Run(client, {"Authorization": f"Bearer {staff_token}"}, args.concurrency)
        results: dict[str, Any] = {}
        steps: list[tuple[str, Callable[[], Awaitable[dict[str, Any]]]]] = [
            ("multiple_sessions", lambda: multiple_sessions(run, args.sessions, args.turns)),
            ("concurrent_chat", lambda: concurrent_chat(run, args.concurrency)),
            ("simultaneous_tickets", lambda: simultaneous_tickets(run, args.sessions,
                                                                 args.concurrency)),
            ("faq_updates_during_retrieval",
             lambda: faq_updates_during_retrieval(run, args.concurrency, args.turns, 40)),
            ("admin_analytics_during_activity",
             lambda: admin_during_activity(run, args.sessions, args.turns, 40)),
            ("database_write_contention", lambda: write_contention(run, args.rounds)),
        ]  # fmt: skip
        for name, step in steps:
            results[name] = await step()
            gc.collect()
            memory[f"after_{name}"] = rss_mb()
        for n in range(args.soak_rounds):
            await multiple_sessions(run, args.sessions, args.turns)
            gc.collect()
            memory[f"after_soak_round_{n + 1}"] = rss_mb()
        results["isolation"] = await isolation(run, args.sessions)
        return results, run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--sessions", type=int, default=16)
    parser.add_argument("--turns", type=int, default=4)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--soak-rounds", type=int, default=3)
    parser.add_argument("--encoder", choices=["model", "lexical"], default="model")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.concurrency <= MAX_CONCURRENCY:
        parser.error(f"--concurrency must be 1-{MAX_CONCURRENCY} (a development machine)")

    memory: dict[str, float] = {"start": rss_mb()}
    staff_token = generate_staff_token()
    with tempfile.TemporaryDirectory(prefix="concurrency-", ignore_cleanup_errors=True) as folder:
        database_file = Path(folder) / "concurrency.db"
        url = f"sqlite:///{database_file.as_posix()}"
        settings = harness_settings(
            url,
            DEFAULT_FAQ_DATA,
            hash_staff_token(staff_token),
            query_rewrite_use_llm=False,
            rate_limit_enabled=False,
        )
        model = ScriptedModel()
        llm = LLMService(settings.openai_api_key, model="mocked-model", max_retries=0,
                         client_factory=lambda key: model)  # fmt: skip
        holder: list[Any] = []
        semantic = SemanticSearchService(
            lambda: holder[0].state.faq_service.list_active_faqs(),
            embedding_service(args.encoder, settings.embedding_model_name),
            default_min_score=settings.semantic_min_similarity,
            catalog_revision=lambda: holder[0].state.faq_service.catalog_revision(),
        )
        database = Database(url)
        app = create_app(settings, semantic_search_service=semantic, llm_service=llm,
                         database=database)  # fmt: skip
        holder.append(app)
        with LocalServer(app) as server, httpx.Client(base_url=server.base_url, timeout=600) as h:
            started = time.perf_counter()
            h.post(f"{API}/hybrid-search", json={"query": QUESTIONS[0]}).raise_for_status()
            warm_up_seconds = round(time.perf_counter() - started, 1)
            gc.collect()
            memory["after_model_warm_up"] = rss_mb()
            results, run = asyncio.run(evaluate(server.base_url, staff_token, args, memory))
            # Every answer text the edited FAQ had, plus every other FAQ's current answer.
            versions: set[str] = set()
            offset = 0
            while True:  # every answer the edited FAQ ever had (its creation included)
                page = h.get(f"{API}/admin/faqs/{EDITED_FAQ}/revisions",
                             params={"offset": offset, "limit": 100},
                             headers=run.staff).json()  # fmt: skip
                versions |= {r["new_values"]["answer"] for r in page["revisions"]
                             if "answer" in r["new_values"]}  # fmt: skip
                if not page["has_more"]:
                    break
                offset += 100
            catalogue = h.get(f"{API}/admin/faqs?limit=100", headers=run.staff).json()
            versions |= {f["answer"] for f in catalogue["faqs"]}
        database.dispose()
        results["consistency"] = consistency(database_file, run.ledger, versions)

    report = {
        "meta": {
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "concurrency": args.concurrency, "sessions": args.sessions, "turns": args.turns,
            "write_rounds": args.rounds, "soak_rounds": args.soak_rounds,
            "encoder": args.encoder, "llm": "mocked (scripted model, no network)",
            "rate_limits": "off in this test server", "database": "SQLite (temporary file)",
            "cpu_count": os.cpu_count(), "python": sys.version.split()[0],
            "model_warm_up_seconds": warm_up_seconds, "llm_calls": len(model.calls),
        },
        "scenarios": results,
        "memory_mb": memory,
    }  # fmt: skip
    print(json.dumps({k: v for k, v in report.items() if k != "scenarios"}, indent=2))
    for name, result in results.items():
        print(f"\n== {name}")
        for key, value in result.items():
            if key == "failures":
                kinds = Counter(f"{f.get('kind')}: {f.get('status', '')} "
                                f"{f.get('code', f.get('error', ''))}"
                                for f in value)  # fmt: skip
                print(f"  failures: {dict(kinds) or 'none'}")
            else:
                print(f"  {key}: {value}")
    if args.output:
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
