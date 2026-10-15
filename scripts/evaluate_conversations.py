"""Evaluate multi-turn conversations (the Stage 6 conversation API) on labelled scenarios.

Usage (from the project root, virtual environment active):

    python scripts/evaluate_conversations.py                       # LLM off: free, deterministic
    python scripts/evaluate_conversations.py --offline --output conversation_eval.json
    python scripts/evaluate_conversations.py --llm live --yes      # PAID: real OpenAI calls

Every scenario runs through the real HTTP API in-process (FastAPI ``TestClient``): access
tokens, history, query rewriting, Stage 4 retrieval and decision, the optional Stage 5
LLM, and storage in a fresh in-memory SQLite database. The embedding model is the real
one (downloaded once and cached; ``--offline`` forbids downloads). With ``--llm off``
nothing is sent to OpenAI.

Metrics (each printed with its denominator; turns labelled ``any`` are not scored where
a label is required):

- Context resolution: turns labelled ``standalone`` or ``follow_up`` where the system's
  rewrite decision matched the label (rewritten exactly when a follow-up).
- Outcome correctness (appropriate fallback behaviour): the outcome (answer /
  clarification / fallback) is one of the turn's acceptable outcomes; also reported for
  turns whose labels do not allow an answer at all.
- Retrieval correctness: answered turns with labelled sources whose source FAQs include
  an acceptable one.
- Answer relevance: answered turns whose labels give ``must_include`` groups, where
  every group has a phrase in the answer (a coarse keyword proxy).
- Grounding compliance: answered turns whose text passes the grounding checks against
  the FAQs it cites (``app/services/grounding_validator.py``); and all turns free of
  ``must_not_include`` phrases (unsupported promises, injected facts).
- Session isolation and expiry: each scripted check (status codes, stored messages,
  history used for rewriting) counted pass / total.
- History-budget compliance: chat turns whose loaded history stayed within the message,
  character and per-message limits, and (when the LLM rewrote) whose rewrite prompt
  stayed within the token budget.
- Latency: end-to-end time of each ``/chat`` request.

These are automated, partly keyword-based checks on a small, hand-written set. They
catch clear failures; read the turns in the ``--output`` JSON before drawing conclusions.
"""

import argparse
import json
import os
import statistics
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/evaluate_conversations.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.database.faq_repository import FAQRepository, resolve_data_path  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.faq import FAQ  # noqa: E402
from app.services.embedding_service import EmbeddingService  # noqa: E402
from app.services.faq_service import FAQService  # noqa: E402
from app.services.grounding_validator import check_grounding  # noqa: E402
from app.services.semantic_search_service import SemanticSearchService  # noqa: E402
from scripts.evaluate_semantic_search import environment_info, file_sha256  # noqa: E402

DEFAULT_DATASET = PROJECT_ROOT / "data" / "conversation_eval.json"
API = "/api/v1/conversations"
CAVEAT = (
    "Results describe one run of one configuration on a small, hand-written set of "
    "scenarios. They indicate behaviour; they are not proof of reliability."
)
OUTCOMES = {
    "llm_grounded": "answer",
    "approved_faq": "answer",
    "clarification": "clarification",
    "fallback": "fallback",
}

Outcome = Literal["answer", "clarification", "fallback"]


# --- Dataset --------------------------------------------------------------------------


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=1000)
    session: Literal["A", "B"] = "A"
    context: Literal["standalone", "follow_up", "any"]
    acceptable_outcomes: list[Outcome] = Field(min_length=1)
    acceptable_source_ids: list[str]
    must_include: list[list[str]]
    must_not_include: list[str]


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    explanation: str
    check: Literal["expired_session", "cross_session"] | None = None
    turns: list[Turn] = Field(min_length=1)


class Dataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    faq_data: str
    scenarios: list[Scenario] = Field(min_length=1)


def load_dataset(path: Path) -> Dataset:
    return Dataset.model_validate_json(path.read_text(encoding="utf-8"))


def contains(text: str, phrase: str) -> bool:
    return phrase.casefold() in text.casefold()


def check_labels(dataset: Dataset, faqs: Sequence[FAQ], fallback_texts: Sequence[str]) -> list[str]:
    """Label mistakes that would make a score meaningless."""
    active = {faq.id: faq for faq in faqs if faq.is_active}
    problems: list[str] = []
    ids = Counter(s.id for s in dataset.scenarios)
    problems += [f"duplicate scenario id {i}" for i, n in ids.items() if n > 1]
    for scenario in dataset.scenarios:
        sessions = {turn.session for turn in scenario.turns}
        if scenario.check != "cross_session" and sessions != {"A"}:
            problems.append(f"{scenario.id}: only cross_session scenarios may use session B")
        if scenario.check == "cross_session" and sessions != {"A", "B"}:
            problems.append(f"{scenario.id}: cross_session needs turns in sessions A and B")
        for n, turn in enumerate(scenario.turns, start=1):
            where = f"{scenario.id} turn {n}"
            for faq_id in turn.acceptable_source_ids:
                if faq_id not in active:
                    problems.append(f"{where}: {faq_id} is not an active FAQ")
            if "answer" in turn.acceptable_outcomes and not turn.acceptable_source_ids:
                problems.append(f"{where}: answers allowed but no acceptable sources")
            allowed = [active[i].answer for i in turn.acceptable_source_ids if i in active]
            for phrase in turn.must_not_include:
                if any(contains(t, phrase) for t in [*allowed, *fallback_texts]):
                    problems.append(f"{where}: forbidden phrase {phrase!r} is in an allowed reply")
    return problems


# --- Instrumentation ------------------------------------------------------------------


@dataclass
class Probe:
    """What the history, rewrite and context-window steps did during one chat request."""

    history_messages: int | None = None
    history_chars: int | None = None
    longest_history_message: int | None = None
    history_omitted_older: bool | None = None
    rewrite_query: str | None = None
    rewritten: bool | None = None
    rewrite_method: str | None = None
    rewrite_reason: str | None = None
    rewrite_prompt_tokens: list[int] = field(default_factory=list)
    rewrite_prompt_fits: list[bool] = field(default_factory=list)


def instrument(app: Any) -> list[Probe]:
    """Record history, rewrite and token-budget results per chat request (in order)."""
    probes: list[Probe] = []
    chat = app.state.conversation_chat_service
    history_service, rewriter = chat._history, chat._rewriter
    load_recent, rewrite = history_service.load_recent, rewriter.rewrite
    fit = rewriter.context_window.fit

    def recorded_load_recent(*args: Any, **kwargs: Any) -> Any:
        history = load_recent(*args, **kwargs)
        probe = Probe(
            history_messages=len(history.turns),
            history_chars=history.total_chars,
            longest_history_message=max((len(t.content) for t in history.turns), default=0),
            history_omitted_older=history.omitted_older,
        )
        probes.append(probe)
        return history

    def recorded_rewrite(*args: Any, **kwargs: Any) -> Any:
        result = rewrite(*args, **kwargs)
        if probes:
            probe = probes[-1]
            probe.rewrite_query = result.query
            probe.rewritten = result.rewritten
            probe.rewrite_method = result.method.value
            probe.rewrite_reason = result.reason.value
        return result

    def recorded_fit(*args: Any, **kwargs: Any) -> Any:
        fitted = fit(*args, **kwargs)
        if probes:
            probes[-1].rewrite_prompt_tokens.append(fitted.total_tokens)
            probes[-1].rewrite_prompt_fits.append(fitted.fits)
        return fitted

    history_service.load_recent = recorded_load_recent
    rewriter.rewrite = recorded_rewrite
    rewriter.context_window.fit = recorded_fit
    return probes


# --- Running --------------------------------------------------------------------------


@dataclass
class TurnResult:
    scenario_id: str
    scenario_type: str
    turn: int
    session: str
    message: str
    context_label: str
    status_code: int
    latency_ms: float
    outcome: str | None
    decision: str | None
    source_ids: list[str]
    answer: str | None
    generation_method: str | None
    probe: dict[str, Any]
    # Scores: None means "not applicable to this turn".
    context_correct: bool | None
    outcome_correct: bool
    fallback_only: bool
    source_correct: bool | None
    relevance_correct: bool | None
    grounding_passed: bool | None
    grounding_issues: list[str]
    forbidden_found: list[str]
    history_within_budget: bool | None


@dataclass
class CheckResult:
    scenario_id: str
    kind: str
    name: str
    passed: bool
    detail: str


@dataclass
class Limits:
    max_messages: int
    max_chars: int
    max_message_chars: int
    max_tokens: int


class Conversation:
    """One conversation through the HTTP API."""

    def __init__(self, client: TestClient) -> None:
        body = client.post(API).json()
        self.client = client
        self.session_id: str = body["session_id"]
        self.headers = {"Authorization": f"Bearer {body['access_token']}"}

    @property
    def url(self) -> str:
        return f"{API}/{self.session_id}"


def score_turn(
    scenario: Scenario,
    index: int,
    turn: Turn,
    response: Any,
    latency_ms: float,
    probe: Probe | None,
    faqs: dict[str, FAQ],
    limits: Limits,
) -> TurnResult:
    body = response.json() if response.status_code == 200 else {}
    reply = body.get("assistant_message") or {}
    method = body.get("generation_method")
    outcome = OUTCOMES.get(method) if method else None
    answer = reply.get("content")
    sources = list(reply.get("source_faq_ids") or [])
    answered = outcome == "answer" and answer is not None

    context_correct = None
    if turn.context != "any" and probe is not None and probe.rewritten is not None:
        context_correct = probe.rewritten == (turn.context == "follow_up")

    source_correct = None
    relevance_correct = None
    grounding_passed = None
    issues: list[str] = []
    if answered:
        if turn.acceptable_source_ids:
            source_correct = bool(set(sources) & set(turn.acceptable_source_ids))
        if turn.must_include:
            relevance_correct = all(
                any(contains(answer, phrase) for phrase in group) for group in turn.must_include
            )
        cited = [faqs[i].answer for i in sources if i in faqs]
        if cited:
            report = check_grounding(answer, cited, cited[0])
            grounding_passed = report.passed
            issues = list(report.kinds)
        else:
            grounding_passed = False
            issues = ["no_cited_faq"]

    forbidden = [p for p in turn.must_not_include if answer and contains(answer, p)]

    within = None
    if probe is not None and probe.history_messages is not None:
        within = (
            probe.history_messages <= limits.max_messages
            and (probe.history_chars or 0) <= limits.max_chars
            and (probe.longest_history_message or 0) <= limits.max_message_chars
            and all(
                t <= limits.max_tokens
                for t, ok in zip(
                    probe.rewrite_prompt_tokens, probe.rewrite_prompt_fits, strict=True
                )
                if ok
            )
        )

    return TurnResult(
        scenario_id=scenario.id,
        scenario_type=scenario.type,
        turn=index,
        session=turn.session,
        message=turn.message,
        context_label=turn.context,
        status_code=response.status_code,
        latency_ms=latency_ms,
        outcome=outcome,
        decision=reply.get("decision"),
        source_ids=sources,
        answer=answer,
        generation_method=method,
        probe=asdict(probe) if probe else {},
        context_correct=context_correct,
        outcome_correct=outcome in turn.acceptable_outcomes,
        fallback_only="answer" not in turn.acceptable_outcomes,
        source_correct=source_correct,
        relevance_correct=relevance_correct,
        grounding_passed=grounding_passed,
        grounding_issues=issues,
        forbidden_found=forbidden,
        history_within_budget=within,
    )


def run_scenario(
    client: TestClient,
    probes: list[Probe],
    scenario: Scenario,
    faqs: dict[str, FAQ],
    limits: Limits,
) -> tuple[list[TurnResult], list[CheckResult]]:
    conversations = {"A": Conversation(client)}
    if scenario.check == "cross_session":
        conversations["B"] = Conversation(client)
    results: list[TurnResult] = []
    for index, turn in enumerate(scenario.turns, start=1):
        conversation = conversations[turn.session]
        before = len(probes)
        started = time.perf_counter()
        response = client.post(
            f"{conversation.url}/chat", json={"message": turn.message}, headers=conversation.headers
        )
        latency_ms = (time.perf_counter() - started) * 1000
        probe = probes[-1] if len(probes) > before else None
        results.append(score_turn(scenario, index, turn, response, latency_ms, probe, faqs, limits))

    checks: list[CheckResult] = []
    if scenario.check == "expired_session":
        checks = check_expired(client, scenario, conversations["A"])
    elif scenario.check == "cross_session":
        checks = check_isolation(client, scenario, conversations, results)
    return results, checks


def _check(scenario: Scenario, kind: str, name: str, passed: bool, detail: str) -> CheckResult:
    return CheckResult(scenario.id, kind, name, passed, detail)


def check_expired(
    client: TestClient, scenario: Scenario, conversation: Conversation
) -> list[CheckResult]:
    """Move the conversation's expiry into the past, then try every endpoint."""
    past = datetime.now(UTC) - timedelta(days=1)
    stamp = "%Y-%m-%d %H:%M:%S.%f"  # how SQLAlchemy stores DATETIME values in SQLite
    database = client.app.state.database  # type: ignore[attr-defined]
    with database.write() as db:
        db.execute(
            text(
                "UPDATE conversation_sessions SET created_at = :created, updated_at = :created, "
                "expires_at = :expired WHERE session_id = :sid"
            ),
            {
                "created": (past - timedelta(hours=1)).strftime(stamp),
                "expired": past.strftime(stamp),
                "sid": conversation.session_id.replace("-", ""),
            },
        )
    h, url = conversation.headers, conversation.url
    expected = [
        ("chat after expiry", "post", f"{url}/chat", {"message": "Is it still there?"}, 410),
        ("read after expiry", "get", url, None, 410),
        ("history after expiry", "get", f"{url}/messages", None, 410),
        ("close after expiry", "post", f"{url}/close", None, 410),
        ("delete after expiry", "delete", url, None, 204),
        ("read after deletion", "get", url, None, 404),
    ]
    checks = []
    for name, method, path, body, status in expected:
        got = client.request(method, path, json=body, headers=h).status_code
        checks.append(
            _check(scenario, "expiry", name, got == status, f"expected {status}, got {got}")
        )
    return checks


def check_isolation(
    client: TestClient,
    scenario: Scenario,
    conversations: dict[str, Conversation],
    results: list[TurnResult],
) -> list[CheckResult]:
    a, b = conversations["A"], conversations["B"]
    checks = []
    attempts = [
        ("B's token cannot read A", "get", a.url, None, b.headers),
        ("B's token cannot read A's messages", "get", f"{a.url}/messages", None, b.headers),
        ("B's token cannot post to A", "post", f"{a.url}/chat", {"message": "Hi"}, b.headers),
        ("A's token cannot delete B", "delete", b.url, None, a.headers),
    ]
    for name, method, path, body, headers in attempts:
        got = client.request(method, path, json=body, headers=headers).status_code
        checks.append(_check(scenario, "isolation", name, got == 404, f"expected 404, got {got}"))

    for label, conversation in conversations.items():
        stored = client.get(f"{conversation.url}/messages", headers=conversation.headers).json()
        typed = [m["content"] for m in stored["messages"] if m["role"] == "user"]
        own = [t.message for t in scenario.turns if t.session == label]
        checks.append(
            _check(
                scenario,
                "isolation",
                f"{label} stores only its own messages",
                typed == own,
                f"{len(typed)} stored, {len(own)} sent",
            )
        )
    for label, other in (("A", "B"), ("B", "A")):
        other_text = [t.message for t in scenario.turns if t.session == other]
        queries = [r.probe.get("rewrite_query") or "" for r in results if r.session == label]
        leaked = [q for q in queries if any(o in q for o in other_text)]
        checks.append(
            _check(
                scenario,
                "isolation",
                f"{label}'s search queries use none of {other}'s messages",
                not leaked,
                f"{len(leaked)} of {len(queries)} queries contained the other conversation",
            )
        )
    return checks


# --- Summary --------------------------------------------------------------------------


def _fraction(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "n": numerator,
        "of": denominator,
        "rate": numerator / denominator if denominator else None,
    }


def _count(values: Sequence[bool | None]) -> dict[str, Any]:
    scored = [v for v in values if v is not None]
    return _fraction(sum(scored), len(scored))


def _latency(values: Sequence[float]) -> dict[str, float | None]:
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "median": statistics.median(ordered) if ordered else None,
        "p95": ordered[max(0, round(0.95 * len(ordered)) - 1)] if ordered else None,
        "max": ordered[-1] if ordered else None,
    }


def summarize(turns: Sequence[TurnResult], checks: Sequence[CheckResult]) -> dict[str, Any]:
    return {
        "context_resolution": _count([t.context_correct for t in turns]),
        "context_resolution_follow_up": _count(
            [t.context_correct for t in turns if t.context_label == "follow_up"]
        ),
        "context_resolution_standalone": _count(
            [t.context_correct for t in turns if t.context_label == "standalone"]
        ),
        "outcome_correct": _count([t.outcome_correct for t in turns]),
        "fallback_correct": _count([t.outcome_correct for t in turns if t.fallback_only]),
        "retrieval_correct": _count([t.source_correct for t in turns]),
        "answer_relevance": _count([t.relevance_correct for t in turns]),
        "grounding_passed": _count([t.grounding_passed for t in turns]),
        "no_forbidden_phrases": _fraction(sum(not t.forbidden_found for t in turns), len(turns)),
        "isolation_checks": _count([c.passed for c in checks if c.kind == "isolation"]),
        "expiry_checks": _count([c.passed for c in checks if c.kind == "expiry"]),
        "history_within_budget": _count([t.history_within_budget for t in turns]),
        "history_truncated_turns": sum(bool(t.probe.get("history_omitted_older")) for t in turns),
        "unexpected_status": sum(t.status_code != 200 for t in turns),
        "latency_ms": _latency([t.latency_ms for t in turns if t.status_code == 200]),
    }


# --- Orchestration --------------------------------------------------------------------


def evaluate(
    dataset_path: Path,
    faq_path: Path,
    settings: Settings,
    *,
    mode: str = "off",
    embedding_factory: Callable[[str], EmbeddingService] = EmbeddingService,
) -> dict[str, Any]:
    """Run every scenario and return a JSON-serialisable report."""
    dataset = load_dataset(dataset_path)
    repository = FAQRepository.from_json_file(faq_path)
    faq_list = repository.list_all()
    fallbacks = [settings.fallback_no_match_message, settings.fallback_unavailable_message]
    if problems := check_labels(dataset, faq_list, fallbacks):
        raise ValueError("Invalid evaluation labels:\n  " + "\n  ".join(problems))
    faq_service = FAQService(repository)
    semantic = SemanticSearchService(
        faq_service.list_active_faqs, embedding_factory(settings.embedding_model_name)
    )
    app = create_app(settings, faq_service=faq_service, semantic_search_service=semantic)
    limits = Limits(
        max_messages=settings.conversation_history_max_messages,
        max_chars=settings.conversation_history_max_chars,
        max_message_chars=settings.conversation_history_max_message_chars,
        max_tokens=settings.conversation_context_max_tokens,
    )
    faqs = {faq.id: faq for faq in faq_list}
    turns: list[TurnResult] = []
    checks: list[CheckResult] = []
    with TestClient(app) as client:
        started = time.perf_counter()
        semantic.build_index()  # load the model before timing anything
        warmup_seconds = time.perf_counter() - started
        probes = instrument(app)
        for scenario in dataset.scenarios:
            scenario_turns, scenario_checks = run_scenario(client, probes, scenario, faqs, limits)
            turns += scenario_turns
            checks += scenario_checks

    return {
        "meta": {
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "mode": mode,
            "scenarios": len(dataset.scenarios),
            "turns": len(turns),
            "caveat": CAVEAT,
            "llm_enabled": settings.llm_enabled,
            "openai_model": settings.openai_model if settings.llm_enabled else None,
            "query_rewrite_use_llm": settings.query_rewrite_use_llm,
            "limits": asdict(limits),
            "context_tokenizer": settings.context_tokenizer,
            "embedding_model": settings.embedding_model_name,
            "dataset": dataset_path.name,
            "dataset_sha256": file_sha256(dataset_path),
            "faq_data_sha256": file_sha256(faq_path),
            "warmup_seconds": warmup_seconds,
            "environment": environment_info(),
        },
        "metrics": summarize(turns, checks),
        "turns": [asdict(t) for t in turns],
        "checks": [asdict(c) for c in checks],
    }


def _frac(value: dict[str, Any]) -> str:
    if not value["of"]:
        return "n/a (0 scored)"
    return f"{value['n']}/{value['of']} ({100 * value['rate']:.1f}%)"


def format_report(report: dict[str, Any]) -> str:
    m, meta = report["metrics"], report["meta"]
    lat = m["latency_ms"]
    lines = [
        "Conversation evaluation",
        f"  mode: {meta['mode']} | scenarios: {meta['scenarios']} | chat turns: {meta['turns']}",
        f"  LLM: {'on (' + str(meta['openai_model']) + ')' if meta['llm_enabled'] else 'off'} | "
        f"limits: {meta['limits']}",
        "",
        f"Context resolution (labelled turns):       {_frac(m['context_resolution'])}",
        f"  follow-ups rewritten:                    {_frac(m['context_resolution_follow_up'])}",
        f"  standalone left unchanged:               {_frac(m['context_resolution_standalone'])}",
        f"Outcome correct (all turns):               {_frac(m['outcome_correct'])}",
        f"  fallback/clarification-only turns:       {_frac(m['fallback_correct'])}",
        f"Retrieval correct (answered, labelled):    {_frac(m['retrieval_correct'])}",
        f"Answer relevance (must_include):           {_frac(m['answer_relevance'])}",
        f"Grounding checks passed (answered):        {_frac(m['grounding_passed'])}",
        f"No forbidden phrases (all turns):          {_frac(m['no_forbidden_phrases'])}",
        f"Session isolation checks:                  {_frac(m['isolation_checks'])}",
        f"Expiry checks:                             {_frac(m['expiry_checks'])}",
        f"History within budget (chat turns):        {_frac(m['history_within_budget'])}",
        f"  turns where older history was omitted:   {m['history_truncated_turns']}",
        f"Unexpected HTTP status on chat turns:      {m['unexpected_status']}",
        f"Latency per /chat (n={lat['n']}):            "
        + (
            f"median {lat['median']:.0f} ms | p95 {lat['p95']:.0f} ms | max {lat['max']:.0f} ms"
            if lat["n"]
            else "n/a"
        ),
    ]
    failures = [
        t
        for t in report["turns"]
        if t["context_correct"] is False
        or not t["outcome_correct"]
        or t["source_correct"] is False
        or t["relevance_correct"] is False
        or t["grounding_passed"] is False
        or t["forbidden_found"]
    ]
    if failures:
        lines += ["", f"Turns with at least one failed check ({len(failures)}):"]
        for t in failures:
            reasons = []
            if t["context_correct"] is False:
                reasons.append(f"context (rewritten={t['probe'].get('rewritten')})")
            if not t["outcome_correct"]:
                reasons.append(f"outcome={t['outcome']}")
            if t["source_correct"] is False:
                reasons.append(f"sources={t['source_ids']}")
            if t["relevance_correct"] is False:
                reasons.append("must_include missing")
            if t["grounding_passed"] is False:
                reasons.append(f"grounding={t['grounding_issues']}")
            if t["forbidden_found"]:
                reasons.append(f"forbidden={t['forbidden_found']}")
            lines.append(
                f"  {t['scenario_id']} turn {t['turn']}: {t['message']!r} -> {'; '.join(reasons)}"
            )
    failed_checks = [c for c in report["checks"] if not c["passed"]]
    if failed_checks:
        lines += ["", "Failed scenario checks:"]
        lines += [f"  {c['scenario_id']}: {c['name']} ({c['detail']})" for c in failed_checks]
    lines += ["", meta["caveat"]]
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate multi-turn conversations.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--faq-data", type=Path)
    parser.add_argument(
        "--llm",
        choices=["off", "live"],
        default="off",
        help="off: LLM disabled (free, deterministic); live: real OpenAI calls (paid)",
    )
    parser.add_argument("--yes", action="store_true", help="confirm paid requests for --llm live")
    parser.add_argument("--output", type=Path, help="also write the full report as JSON")
    parser.add_argument(
        "--offline", action="store_true", help="use only the locally cached embedding model"
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    embedding_factory: Callable[[str], EmbeddingService] = EmbeddingService,
    settings: Settings | None = None,
) -> int:
    args = parse_args(argv)
    if args.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"  # read when huggingface_hub is first imported
    if args.llm == "live" and not args.yes:
        print(
            "--llm live makes PAID OpenAI requests (query rewrites and answers). "
            "Re-run with --yes to confirm.",
            file=sys.stderr,
        )
        return 2
    if settings is None:
        try:
            settings = Settings(
                llm_enabled=args.llm == "live",
                database_url="sqlite://",  # never touch the real conversation database
                conversation_cleanup_on_startup=False,
            )
        except ValueError as exc:  # e.g. live mode without an OPENAI_API_KEY
            print(f"Invalid configuration: {exc}", file=sys.stderr)
            return 2
    faq_path = args.faq_data or resolve_data_path(settings.faq_data_path)
    report = evaluate(
        args.dataset, faq_path, settings, mode=args.llm, embedding_factory=embedding_factory
    )
    print(format_report(report))
    if args.output:
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nFull results written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
