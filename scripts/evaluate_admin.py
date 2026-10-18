"""Run the Stage 11 admin evaluation scenarios (data/admin_eval.json).

Usage (from the project root, virtual environment active):

    python -m scripts.evaluate_admin                 # run all, print a table
    python -m scripts.evaluate_admin --json out.json # also write the results
    python -m scripts.evaluate_admin --only AE-07

Every scenario runs against a fresh in-process API: a temporary SQLite file seeded from
``data/faqs.json``, the LLM off, and a deterministic stand-in for the embedding model
(bag-of-words vectors, so nothing is downloaded and results are repeatable). Staff tokens
are generated per run. Nothing outside a temporary directory is touched, and no network
call is made.

A scenario's *actual result* is what the run observed; its status is ``pass`` only when
every expectation held. A failed check records the observation that broke it.

Exit code: 0 when every scenario passed, 1 otherwise.
"""

import argparse
import json
import sys
import tempfile
import uuid
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/evaluate_admin.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.core.staff_auth import generate_staff_token, hash_staff_token  # noqa: E402
from app.database.connection import Database  # noqa: E402
from app.main import create_app  # noqa: E402
from app.repositories.faq_repository import FAQStoreError  # noqa: E402
from app.services.answer_decision_service import RetrievalMode  # noqa: E402
from app.services.embedding_service import EmbeddingError, EmbeddingService  # noqa: E402
from app.services.semantic_search_service import SemanticSearchService  # noqa: E402

SCENARIOS = PROJECT_ROOT / "data" / "admin_eval.json"
TRACK = "How can I track my order?"  # faq_001's question in data/faqs.json
API = "/api/v1"


class CheckFailed(Exception):
    """An expectation did not hold; the message is the observation."""


def check(condition: bool, observed: str) -> None:
    if not condition:
        raise CheckFailed(observed)


class StandInEncoder:
    """Deterministic bag-of-words embeddings; can fail and run hooks on queries."""

    dimension = 64

    def __init__(self) -> None:
        self.fail = False
        self.query_hooks: list[Callable[[], None]] = []
        self.calls: list[list[str]] = []

    def get_embedding_dimension(self) -> int:
        return self.dimension

    def encode(self, sentences: list[str], **kwargs: Any) -> np.ndarray:
        if self.fail:
            raise RuntimeError("model unavailable")
        self.calls.append(list(sentences))
        if len(sentences) == 1 and self.query_hooks and not sentences[0].endswith("?"):
            self.query_hooks.pop(0)()
        vectors = np.zeros((len(sentences), self.dimension))
        for row, sentence in enumerate(sentences):
            for word in sentence.lower().split():
                vectors[row, zlib.crc32(word.encode()) % self.dimension] += 1.0
            if not vectors[row].any():
                vectors[row, 0] = 1.0
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


@dataclass
class Env:
    client: TestClient
    app: Any
    database: Database
    encoder: StandInEncoder
    admin: dict[str, str]
    support: dict[str, str]

    def get(self, path: str, headers: dict[str, str] | None = None, **params: Any) -> Any:
        return self.client.get(API + path, headers=headers or self.admin, params=params)

    def post(self, path: str, body: Any, headers: dict[str, str] | None = None) -> Any:
        return self.client.post(API + path, json=body, headers=headers or self.admin)

    def patch(self, path: str, body: Any, headers: dict[str, str] | None = None) -> Any:
        return self.client.patch(API + path, json=body, headers=headers or self.admin)

    def put(self, path: str, body: Any, headers: dict[str, str] | None = None) -> Any:
        return self.client.put(API + path, json=body, headers=headers or self.admin)

    def conversation(self) -> tuple[str, dict[str, str]]:
        created = self.client.post(f"{API}/conversations").json()
        return created["session_id"], {"Authorization": f"Bearer {created['access_token']}"}

    def chat_response(self, message: str) -> Any:
        session_id, headers = self.conversation()
        return self.client.post(
            f"{API}/conversations/{session_id}/chat", json={"message": message}, headers=headers
        )

    def chat(self, message: str) -> dict[str, Any]:
        response = self.chat_response(message)
        check(response.status_code == 200, f"chat answered HTTP {response.status_code}")
        return dict(response.json())

    def ticket(self, ticket_type: str = "support") -> dict[str, Any]:
        session_id, headers = self.conversation()
        body: dict[str, Any] = {
            "session_id": session_id,
            "client_request_id": str(uuid.uuid4()),
            "ticket_type": ticket_type,
            "subject": "Refund assistance",
            "description": "I need help with a refund request.",
            "contact": {
                "name": "Example Customer",
                "email": "customer@example.com",
                "phone": None,
                "contact_preference": "email",
                "consent_given": True,
            },
            "confirmed": True,
        }
        response = self.client.post(f"{API}/support/tickets", json=body, headers=headers)
        check(response.status_code == 201, f"ticket creation HTTP {response.status_code}")
        return dict(response.json())


def faq(env: Env, faq_id: str) -> dict[str, Any]:
    return dict(env.get(f"/admin/faqs/{faq_id}").json())


def sources(reply: dict[str, Any]) -> list[str]:
    return [source["faq_id"] for source in reply["sources"]]


def code(response: Any) -> str:
    try:
        return f"{response.status_code} {response.json()['error']['code']}"
    except (ValueError, KeyError, TypeError):
        return str(response.status_code)


NEW_FAQ = {
    "question": "Do you ship to Norway?",
    "answer": "Yes. Orders to Norway arrive within 5-7 working days.",
    "category": "shipping",
    "keywords": ["norway", "international"],
}


# --- Scenarios ---------------------------------------------------------------------------


def ae01(env: Env) -> str:
    created = env.post("/admin/faqs", NEW_FAQ)
    check(created.status_code == 201, f"create {code(created)}")
    body = created.json()
    found = env.client.get(f"{API}/faqs/search", params={"q": "norway"}).json()["results"]
    check(body["id"] in [r["id"] for r in found], "not found by public search")
    return f"201 {body['id']} version {body['version']}; public search finds it"


def ae02(env: Env) -> str:
    env.post("/admin/faqs", NEW_FAQ)
    again = env.post("/admin/faqs", {**NEW_FAQ, "question": NEW_FAQ["question"].upper()})
    total = env.get("/admin/faqs", q="norway").json()["total"]
    check(code(again) == "409 FAQ_DUPLICATE_QUESTION" and total == 1, f"{code(again)}, {total}")
    return f"{code(again)}; {total} FAQ stored"


def _refused(env: Env, change: dict[str, Any]) -> str:
    before = env.get("/admin/faqs").json()["total"]
    response = env.post("/admin/faqs", {**NEW_FAQ, **change})
    after = env.get("/admin/faqs").json()["total"]
    fields = [".".join(map(str, d["loc"])) for d in response.json()["error"].get("details", [])]
    leaked = any(str(v)[:40] in response.text for v in change.values() if v)
    check(code(response) == "422 VALIDATION_ERROR" and before == after, f"{code(response)}")
    check(not leaked, "submitted value echoed in the error")
    return f"{code(response)} on {', '.join(fields)}; nothing stored"


def ae03(env: Env) -> str:
    return _refused(env, {"answer": ""})


def ae04(env: Env) -> str:
    return _refused(env, {"answer": "x" * 5001})


def ae05(env: Env) -> str:
    return _refused(env, {"category": "pets"})


def ae06(env: Env) -> str:
    edited = env.patch("/admin/faqs/faq_001", {"expected_version": 1, "answer": "Use the tracker."})
    public = env.client.get(f"{API}/faqs/faq_001").json()["answer"]
    reply = env.chat(TRACK)
    check(edited.json()["version"] == 2, f"version {edited.json().get('version')}")
    check(
        public == reply["answer"] == "Use the tracker.",
        f"public={public!r} chat={reply['answer']!r}",
    )
    return "version 2; public FAQ and chatbot answer show the new text"


def ae07(env: Env) -> str:
    first = env.patch("/admin/faqs/faq_001", {"expected_version": 1, "answer": "First."})
    second = env.patch("/admin/faqs/faq_001", {"expected_version": 1, "answer": "Second."})
    stored = faq(env, "faq_001")["answer"]
    check(first.status_code == 200 and code(second) == "409 FAQ_VERSION_CONFLICT", code(second))
    check(stored == "First.", f"stored {stored!r}")
    return f"first 200; second {code(second)}; stored text is the first edit"


def ae08(env: Env) -> str:
    env.post("/admin/faqs/faq_001/deactivate", {"expected_version": 1})
    public = env.client.get(f"{API}/faqs/faq_001").status_code
    reply = env.chat(TRACK)
    check(public == 404 and "faq_001" not in sources(reply), f"public {public}, {sources(reply)}")
    check(faq(env, "faq_001")["is_active"] is False, "not stored")
    return f"public {public}; reply decision {reply['decision']} without faq_001; still stored"


def ae09(env: Env) -> str:
    env.post("/admin/faqs/faq_001/deactivate", {"expected_version": 1})
    env.post("/admin/faqs/faq_001/activate", {"expected_version": 2})
    public = env.client.get(f"{API}/faqs/faq_001").status_code
    reply = env.chat(TRACK)
    check(public == 200 and sources(reply) == ["faq_001"], f"public {public}, {sources(reply)}")
    return "public 200; chatbot answers with faq_001 again"


def ae10(env: Env) -> str:
    env.patch("/admin/faqs/faq_001", {"expected_version": 1, "category": "shipping"})
    shipping = [
        f["id"] for f in env.get("/admin/faqs", category="shipping", limit=100).json()["faqs"]
    ]
    orders = [f["id"] for f in env.get("/admin/faqs", category="orders", limit=100).json()["faqs"]]
    revision = env.get("/admin/faqs/faq_001/revisions").json()["revisions"][0]
    check("faq_001" in shipping and "faq_001" not in orders, "category filter wrong")
    check(revision["changed_fields"] == ["category"], f"fields {revision['changed_fields']}")
    return "listed under shipping only; revision changed_fields = ['category']"


def ae11(env: Env) -> str:
    def search() -> list[str]:
        results = env.client.get(f"{API}/faqs/search", params={"q": "zeppelin"}).json()["results"]
        return [r["id"] for r in results]

    before = search()
    tags = faq(env, "faq_001")["keywords"] + ["zeppelin"]
    env.patch("/admin/faqs/faq_001", {"expected_version": 1, "keywords": tags})
    after = search()
    check(before == [] and after[:1] == ["faq_001"], f"before {before}, after {after}")
    return "before: no result; after: faq_001 found (no restart)"


def ae12(env: Env) -> str:
    semantic = env.app.state.semantic_search_service
    semantic.get_index()
    calls = len(env.encoder.calls)
    question = "Where can I follow my parcel?"
    env.patch("/admin/faqs/faq_001", {"expected_version": 1, "question": question})
    index = semantic.get_index()
    new_calls = env.encoder.calls[calls:]
    revision = env.app.state.faq_service.latest().revision
    check(new_calls == [[question]], f"embedded {new_calls}")
    check(index.catalog_revision == revision, "index revision differs")
    check(index.get_faq("faq_001").question == question, "old question indexed")
    return "1 question embedded; index revision = catalogue revision; new question indexed"


def ae13(env: Env) -> str:
    service = env.app.state.faq_management_service
    from app.core.staff_auth import StaffMember, StaffRole

    env.encoder.query_hooks.append(
        lambda: service.deactivate_faq(StaffMember("alex", StaffRole.ADMIN), "faq_001", 1)
    )
    reply = env.chat("I want to track my order please")
    check(faq(env, "faq_001")["is_active"] is False, "hook did not run")
    check("faq_001" not in sources(reply), f"sources {sources(reply)}")
    return f"deactivated mid-request; reply ({reply['decision']}) does not cite faq_001"


def ae14(env: Env) -> str:
    service = env.app.state.faq_management_service
    from app.core.staff_auth import StaffMember, StaffRole
    from app.models.faq_admin import FAQChanges

    env.encoder.query_hooks.append(
        lambda: service.update_faq(
            StaffMember("alex", StaffRole.ADMIN),
            "faq_001",
            FAQChanges(expected_version=1, answer="Edited mid-request."),
        )
    )
    reply = env.chat("I want to track my order please")
    old = "You can track your order"
    check(old not in reply["answer"], "old answer served")
    check(
        reply["answer"] == "Edited mid-request." or "faq_001" not in sources(reply),
        f"reply {reply['answer'][:60]!r}",
    )
    return f"reply decision {reply['decision']}: {'new answer' if sources(reply) else 'no faq_001'}"


def ae15(env: Env) -> str:
    semantic = env.app.state.semantic_search_service
    semantic.get_index()
    env.encoder.fail = True
    question = "Where can I follow my parcel?"
    env.patch("/admin/faqs/faq_001", {"expected_version": 1, "question": question})
    try:
        semantic.get_index()
    except EmbeddingError:
        stale_served = False
    else:
        stale_served = True
    decision = env.app.state.answer_decision_service.decide(question)
    check(not stale_served, "stale index returned")
    check(decision.retrieval_mode is RetrievalMode.KEYWORD_ONLY, decision.retrieval_mode.value)
    if decision.faq is not None:
        check(env.app.state.faq_service.is_current(decision.faq), "outdated FAQ used")
    return f"EmbeddingError (no stale index); decision {decision.decision.value} keyword_only"


def ae16(env: Env) -> str:
    repo = env.app.state.faq_service.repository
    real = repo.reload
    left = {"n": 2}

    def flaky() -> int:
        if left["n"]:
            left["n"] -= 1
            raise FAQStoreError("unreadable")
        return real()

    repo.reload = flaky
    try:
        env.post("/admin/faqs/faq_001/deactivate", {"expected_version": 1})
        first = env.chat_response(TRACK)
        second = env.chat(TRACK)
    finally:
        repo.reload = real
    check(code(first) == "503 SERVICE_UNAVAILABLE", code(first))
    check("faq_001" not in sources(second), f"sources {sources(second)}")
    return f"first chat {code(first)} (fail closed); then answered without faq_001"


def ae17(env: Env) -> str:
    revision = env.app.state.faq_service.latest().revision
    real = env.database.write

    def broken() -> Any:
        raise OperationalError("INSERT", {}, Exception("disk I/O error"))

    env.database.write = broken  # type: ignore[method-assign]
    try:
        response = env.post("/admin/faqs", NEW_FAQ)
    finally:
        env.database.write = real  # type: ignore[method-assign]
    stored = env.get("/admin/faqs", q="norway").json()["total"]
    check(code(response) == "503 SERVICE_UNAVAILABLE", code(response))
    check("disk" not in response.text and "INSERT" not in response.text, "internals exposed")
    check(stored == 0 and env.app.state.faq_service.latest().revision == revision, "changed")
    return f"{code(response)}, no internals in the body; nothing stored"


def ae18(env: Env) -> str:
    _, customer = env.conversation()
    body = {"expected_version": 1, "answer": "Hacked."}
    results = [
        code(env.client.patch(f"{API}/admin/faqs/faq_001", json=body)),
        code(env.patch("/admin/faqs/faq_001", body, headers=customer)),
        code(env.patch("/admin/faqs/faq_001", body, headers=env.support)),
    ]
    unchanged = faq(env, "faq_001")["version"] == 1
    check(
        results == ["401 STAFF_AUTH_REQUIRED", "401 STAFF_AUTH_REQUIRED", "403 STAFF_FORBIDDEN"],
        str(results),
    )
    check(unchanged, "faq_001 changed")
    return f"{'; '.join(results)}; faq_001 unchanged"


def ae19(env: Env) -> str:
    results = [code(env.get("/admin/faqs")), code(env.get("/support/tickets"))]
    check(results == ["403 STAFF_ACCESS_DISABLED"] * 2, str(results))
    return "; ".join(results)


def ae20(env: Env) -> str:
    created = env.post("/admin/faqs", NEW_FAQ).json()["id"]
    env.patch(f"/admin/faqs/{created}", {"expected_version": 1, "answer": "Changed."})
    env.post(f"/admin/faqs/{created}/deactivate", {"expected_version": 2})
    revisions = env.get(f"/admin/faqs/{created}/revisions").json()["revisions"]
    actions = [(r["action"], r["changed_by"]) for r in revisions]
    check(
        actions == [("deactivated", "alex"), ("updated", "alex"), ("created", "alex")],
        str(actions),
    )
    check(revisions[1]["changed_fields"] == ["answer"], str(revisions[1]["changed_fields"]))
    return "deactivated, updated (answer), created; all by alex"


def ae21(env: Env) -> str:
    ticket = env.ticket()
    listing = env.get("/support/tickets", headers=env.support).json()
    detail = env.get(f"/support/tickets/{ticket['ticket_id']}", headers=env.support).json()
    listed = listing["tickets"][0]
    check("contact" not in listed and "email" not in str(listed), "contact in list")
    check(detail["contact"]["email"] == "customer@example.com", "no contact in detail")
    return "listed without contact details; detail has description and contact"


def ae22(env: Env) -> str:
    tid = env.ticket()["ticket_id"]
    env.put(
        f"/support/tickets/{tid}/assignment", {"assigned_to": "maria", "expected_assigned_to": None}
    )
    changed = env.patch(f"/support/tickets/{tid}/status", {"status": "in_progress"}).json()
    events = env.get(f"/support/tickets/{tid}/events").json()["events"]
    trail = [(e["new_status"], e["actor_id"]) for e in events[1:]]
    check(changed["status"] == "in_progress" and changed["assigned_to"] == "maria", str(changed))
    check(trail == [("assigned", "alex"), ("in_progress", "alex")], str(trail))
    return "in_progress, assigned to maria; 2 events by alex"


def ae23(env: Env) -> str:
    tid = env.ticket()["ticket_id"]
    response = env.patch(f"/support/tickets/{tid}/status", {"status": "resolved"})
    after = env.get(f"/support/tickets/{tid}").json()["status"]
    events = len(env.get(f"/support/tickets/{tid}/events").json()["events"])
    check(code(response) == "409 INVALID_STATUS_TRANSITION", code(response))
    check(after == "open" and events == 1, f"status {after}, {events} events")
    return f"{code(response)}; still open; only the creation event"


def ae24(env: Env) -> str:
    tid = env.ticket()["ticket_id"]
    path = f"/support/tickets/{tid}/assignment"
    intruder = env.put(path, {"assigned_to": "intruder", "expected_assigned_to": None})
    env.put(path, {"assigned_to": "maria", "expected_assigned_to": None})
    env.put(path, {"assigned_to": "sam", "expected_assigned_to": "maria"})
    last = env.get(f"/support/tickets/{tid}/events").json()["events"][-1]
    check(code(intruder) == "422 VALIDATION_ERROR", code(intruder))
    check(
        (last["event_type"], last["previous_assigned_to"], last["assigned_to"])
        == ("assignment_changed", "maria", "sam"),
        str(last),
    )
    return f"intruder {code(intruder)}; assignment_changed maria -> sam recorded"


def ae25(env: Env) -> str:
    for message in (TRACK, "zzz qqq", "hello there"):
        env.chat(message)
    env.ticket()
    env.ticket("sales")
    chat = env.get("/admin/analytics/chat", days=1).json()
    tickets = env.get("/admin/analytics/tickets", days=1).json()
    decisions = chat["decisions"]
    check(chat["customer_messages"] == 3 and chat["chatbot_responses"] == 3, str(chat["decisions"]))
    check(decisions["answered"] >= 1 and sum(decisions.values()) == 3, str(decisions))
    check(tickets["created"] == 2, f"tickets {tickets['created']}")
    check(all(r["percent"] is None for r in chat["rates"]), "rate shown on 3 responses")
    return f"3 messages, 3 responses {decisions}; 2 tickets; rates withheld (n=3)"


def ae26(env: Env) -> str:
    summary = env.get("/admin/dashboard/summary").json()
    chat = env.get("/admin/analytics/chat").json()
    tickets = env.get("/admin/analytics/tickets").json()
    check(summary["tickets"]["total"] == 0 and summary["conversations"]["stored"] == 0, "not zero")
    check(chat["chatbot_responses"] == 0 and tickets["created"] == 0, "not zero")
    check(all(r["percent"] is None and r["note"] for r in chat["rates"]), "rate on empty data")
    return "all counts 0; every rate null with a note"


def ae27(env: Env) -> str:
    env.patch("/admin/faqs/faq_001", {"expected_version": 1, "answer": "Audited."})
    env.patch("/admin/faqs/faq_001", {"expected_version": 2, "answer": "x"}, headers=env.support)
    events = env.get("/admin/audit", limit=200).json()["events"]
    seen = {(e["action"], e["actor_id"], e["outcome"]) for e in events}
    check(("faq.updated", "alex", "success") in seen, str(seen))
    check(("auth.access_denied", "maria", "denied") in seen, str(seen))
    check("stf_" not in json.dumps(events), "token in audit log")
    return "faq.updated by alex (success); auth.access_denied by maria (denied); no tokens"


def ae28(env: Env) -> str:
    ready = env.client.get(f"{API}/ready").json()["checks"]["knowledge_base"]
    reply = env.chat(TRACK)
    check(
        ready == "unavailable" and reply["decision"] == "no_match", f"{ready}, {reply['decision']}"
    )
    return f"knowledge_base {ready}; reply {reply['decision']}"


IMPLEMENTATIONS: dict[str, Callable[[Env], str]] = {
    f"AE-{n:02d}": fn
    for n, fn in enumerate(
        [ae01, ae02, ae03, ae04, ae05, ae06, ae07, ae08, ae09, ae10, ae11, ae12, ae13, ae14,
         ae15, ae16, ae17, ae18, ae19, ae20, ae21, ae22, ae23, ae24, ae25, ae26, ae27, ae28],
        start=1,
    )
}  # fmt: skip
NO_STAFF = {"AE-19"}
EMPTY_SEED = {"AE-28"}


def run_scenario(scenario_id: str, workdir: Path) -> tuple[str, str]:
    """(status, actual result) for one scenario on a fresh app."""
    admin_token, support_token = generate_staff_token(), generate_staff_token()
    hashes = (
        []
        if scenario_id in NO_STAFF
        else [
            f"alex:admin:{hash_staff_token(admin_token)}",
            f"maria:support:{hash_staff_token(support_token)}",
            f"sam:support:{hash_staff_token(generate_staff_token())}",
        ]
    )
    seed = PROJECT_ROOT / "data" / "faqs.json"
    if scenario_id in EMPTY_SEED:
        seed = workdir / f"{scenario_id}-empty.json"
        seed.write_text("[]", encoding="utf-8")
    database = Database(f"sqlite:///{(workdir / f'{scenario_id}.db').as_posix()}")
    encoder = StandInEncoder()
    holder: list[Any] = []
    semantic = SemanticSearchService(
        lambda: holder[0].state.faq_service.list_active_faqs(),
        EmbeddingService("stand-in/bag-of-words", loader=lambda name: encoder),
        catalog_revision=lambda: holder[0].state.faq_service.catalog_revision(),
    )
    settings = Settings(
        _env_file=None,
        database_url="sqlite://",
        faq_data_path=seed,
        staff_token_hashes=hashes,
        llm_enabled=False,
        query_rewrite_use_llm=False,
        rate_limit_enabled=False,
        log_level="WARNING",
    )
    app = create_app(settings, semantic_search_service=semantic, database=database)
    holder.append(app)
    with TestClient(app) as client:
        env = Env(
            client,
            app,
            database,
            encoder,
            {"Authorization": f"Bearer {admin_token}"},
            {"Authorization": f"Bearer {support_token}"},
        )
        try:
            return "pass", IMPLEMENTATIONS[scenario_id](env)
        except CheckFailed as exc:
            return "fail", str(exc)
        except Exception as exc:  # noqa: BLE001 - recorded as a failure, never hidden
            return "fail", f"error: {type(exc).__name__}: {exc}"
        finally:
            database.dispose()


def load_scenarios() -> list[dict[str, Any]]:
    data = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    return list(data["scenarios"])


def evaluate(only: Sequence[str] | None = None) -> list[dict[str, Any]]:
    results = []
    with tempfile.TemporaryDirectory(prefix="admin-eval-") as tmp:
        for scenario in load_scenarios():
            if only and scenario["id"] not in only:
                continue
            status, actual = run_scenario(scenario["id"], Path(tmp))
            results.append({**scenario, "actual": actual, "status": status})
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Stage 11 admin scenarios.")
    parser.add_argument("--only", nargs="*", help="Scenario IDs to run (default: all).")
    parser.add_argument("--json", type=Path, help="Also write the results to this file.")
    args = parser.parse_args(argv)
    results = evaluate(args.only)
    for r in results:
        print(f"{r['id']}  {r['status'].upper():4}  {r['description']}\n        {r['actual']}")
    passed = sum(r["status"] == "pass" for r in results)
    print(f"\n{passed}/{len(results)} scenarios passed ({datetime.now(UTC):%Y-%m-%d %H:%M} UTC).")
    if args.json:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
