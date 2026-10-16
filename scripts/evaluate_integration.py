"""Run the customer-journey dataset (data/integration_eval.json) end to end.

Usage (from the project root, virtual environment active):

    python scripts/evaluate_integration.py                     # all scenarios
    python scripts/evaluate_integration.py --offline           # never download the model
    python scripts/evaluate_integration.py --only INT-05 INT-27 --output journeys.json

Each scenario runs the way the Streamlit page runs it: the frontend's ``ChatState`` and
``frontend.actions`` (queueing, sending, retries, errors, closing) with its
``ChatApiClient``, against a fresh copy of the real backend (FastAPI in-process, a
temporary SQLite file, the real FAQ file and the real embedding model, loaded once and
shared). No OpenAI request is ever sent: ``llm`` selects a local stand-in (off,
faithful, obedient, timeout). ``fault`` injects a failure (embedding model unavailable,
storage failing after the start, backend offline, conversation closed).

Every check is deterministic for a given model and FAQ file: decisions from the
acceptable set, sources containing an expected FAQ, forbidden FAQs and phrases absent,
the generation method, the error the page shows, and the final conversation state (on
the page and in storage). Exit status 1 if any scenario fails.
"""

import argparse
import html
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/evaluate_integration.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import BaseModel, ConfigDict, Field, SecretStr  # noqa: E402

DATASET = PROJECT_ROOT / "data" / "integration_eval.json"
MIN_SCENARIOS = 25
REQUIRED_CATEGORIES = frozenset(
    {
        "basic_faq",
        "refund_multi_turn",
        "shipping_follow_up",
        "returns_cancellation",
        "ambiguous",
        "topic_switch",
        "unrelated",
        "missing_policy",
        "prompt_injection",
        "service_failure",
    }
)
Decision = Literal["answered", "ambiguous", "no_match", "retrieval_unavailable"]
SOURCELESS = {"no_match", "retrieval_unavailable"}


# --- The dataset ----------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Turn(_Strict):
    message: str = Field(min_length=1)
    decision: list[Decision] | None = None  # acceptable decisions
    sources_any: list[str] = []  # at least one must be among the reply's sources
    generation_method: str | None = None
    error: str | None = None  # the page's ErrorKind instead of a reply
    refused_by_page: bool = False  # the page does not send it (e.g. the conversation ended)


class ExpectedState(_Strict):
    status: Literal["active", "closed", "expired"] | None
    stored_messages: int | None  # None: not checked (storage unavailable)
    pending: bool | None = None
    draft: str | None = None


class Scenario(_Strict):
    id: str = Field(pattern=r"^INT-\d{2,}$")
    category: str
    description: str = Field(min_length=1)
    turns: list[Turn] = Field(min_length=1)
    safety: str = Field(min_length=1)
    expected_state: ExpectedState
    llm: Literal["off", "faithful", "obedient", "timeout"] = "off"
    injected_claim: str | None = None
    fault: (
        Literal[
            "embedding_unavailable",
            "database_unavailable",
            "backend_offline",
            "closed_after_first_turn",
        ]
        | None
    ) = None
    must_not_include: list[str] = []
    forbidden_sources: list[str] = []
    forbidden_sources_last_turn: list[str] = []
    # A recorded failure of the system (not of the scenario): reported as KNOWN, not as a
    # failure, until fixed. A known issue that passes is reported as FIXED?.
    known_issue: str | None = None


class Dataset(_Strict):
    description: str
    version: int
    scenarios: list[Scenario]


def load_dataset(path: Path = DATASET) -> Dataset:
    return Dataset.model_validate_json(path.read_text(encoding="utf-8"))


def validate_dataset(dataset: Dataset, active_ids: set[str], all_ids: set[str]) -> list[str]:
    """Problems with the dataset itself (not with the system under test)."""
    problems = []
    ids = [s.id for s in dataset.scenarios]
    if len(ids) != len(set(ids)):
        problems.append("duplicate scenario IDs")
    if len(ids) < MIN_SCENARIOS:
        problems.append(f"only {len(ids)} scenarios (at least {MIN_SCENARIOS})")
    missing = REQUIRED_CATEGORIES - {s.category for s in dataset.scenarios}
    if missing:
        problems.append(f"categories not covered: {sorted(missing)}")
    for scenario in dataset.scenarios:
        expected = {i for turn in scenario.turns for i in turn.sources_any}
        for faq_id in sorted(expected - active_ids):
            problems.append(f"{scenario.id}: expected source {faq_id} is not an active FAQ")
        forbidden = set(scenario.forbidden_sources) | set(scenario.forbidden_sources_last_turn)
        for faq_id in sorted(forbidden - all_ids):
            problems.append(f"{scenario.id}: forbidden source {faq_id} does not exist")
        if scenario.llm == "obedient" and not scenario.injected_claim:
            problems.append(f"{scenario.id}: an obedient LLM needs an injected_claim")
        for turn in scenario.turns:
            if [turn.decision is not None, turn.error is not None, turn.refused_by_page].count(
                True
            ) != 1:
                problems.append(f"{scenario.id}: each turn needs a decision, an error or a refusal")
            if turn.decision and set(turn.decision) <= SOURCELESS and turn.sources_any:
                problems.append(f"{scenario.id}: no_match/unavailable replies have no sources")
    return problems


# --- Local stand-ins for OpenAI ---------------------------------------------------------

_PRIMARY = re.compile(
    r'<faq id="(faq_\d+)"[^>]*role="primary">\s*<question>.*?</question>\s*'
    r"<answer>(.*?)</answer>",
    re.S,
)


class LocalModel:
    """An OpenAI client stand-in: rewords nothing (faithful), appends the scenario's
    injected claim (obedient, to check that validation rejects it) or times out."""

    def __init__(self, mode: str, injected_claim: str | None) -> None:
        self.mode, self.injected_claim = mode, injected_claim
        self.calls = 0
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        import httpx
        import openai

        self.calls += 1
        if self.mode == "timeout":
            raise openai.APITimeoutError(request=httpx.Request("POST", "https://api.openai.com"))
        match = _PRIMARY.search(str(kwargs["input"]))
        if match is None:  # pragma: no cover - the backend always sends evidence
            raise AssertionError("no approved FAQ evidence in the request")
        faq_id, answer = match.group(1), html.unescape(match.group(2))
        if self.mode == "obedient":
            answer = f"{answer} {self.injected_claim}"
        body = {"status": "answered", "answer": answer, "source_faq_ids": [faq_id], "reason": "r"}
        return SimpleNamespace(
            status="completed",
            output_text=json.dumps(body),
            output=[],
            model="local-stand-in",
            id="resp_local",
            usage=SimpleNamespace(input_tokens=0, output_tokens=0),
            _request_id="req_local",
        )

    def close(self) -> None:
        pass


# --- Running ----------------------------------------------------------------------------


class Runner:
    """Builds a fresh backend per scenario around one shared FAQ service and model."""

    def __init__(
        self,
        settings_factory: Callable[..., Any],
        faq_service: Any,
        embedding_loader: Callable[[str], Any] | None,
        workdir: Path,
    ) -> None:
        from app.services.embedding_service import EmbeddingService
        from app.services.semantic_search_service import SemanticSearchService

        self.settings_factory = settings_factory
        self.faq_service = faq_service
        self.workdir = workdir
        settings = settings_factory()
        options = {} if embedding_loader is None else {"loader": embedding_loader}
        self.semantic = SemanticSearchService(
            faq_service.list_active_faqs,
            EmbeddingService(settings.embedding_model_name, **options),
            default_min_score=settings.semantic_min_similarity,
        )
        self.count = 0

    def _backend(self, scenario: Scenario) -> tuple[Any, Any, LocalModel | None]:
        from fastapi.testclient import TestClient

        from app.database.connection import Database
        from app.main import create_app
        from app.services.embedding_service import EmbeddingService
        from app.services.llm_service import LLMService
        from app.services.semantic_search_service import SemanticSearchService

        self.count += 1
        settings = self.settings_factory()
        semantic = self.semantic
        if scenario.fault == "embedding_unavailable":

            def unavailable(name: str) -> Any:
                raise OSError("embedding model unavailable (injected)")

            semantic = SemanticSearchService(
                self.faq_service.list_active_faqs,
                EmbeddingService(settings.embedding_model_name, loader=unavailable),
            )
        model = None
        llm_service = None
        if scenario.llm != "off":
            model = LocalModel(scenario.llm, scenario.injected_claim)
            llm_service = LLMService(
                SecretStr("sk-local-" + "0" * 32),
                model="local-stand-in",
                max_retries=0,
                reasoning_effort=None,
                client_factory=lambda key: model,
            )
        database = Database(f"sqlite:///{(self.workdir / f'{self.count}.db').as_posix()}")
        app = create_app(
            settings,
            faq_service=self.faq_service,
            semantic_search_service=semantic,
            database=database,
            llm_service=llm_service,
        )
        client = TestClient(app)
        client.__enter__()
        return client, database, model

    def run(self, scenario: Scenario) -> dict[str, Any]:
        import httpx

        from frontend import actions
        from frontend.api_client import ChatApiClient
        from frontend.state import ChatState

        http, database, model = self._backend(scenario)
        failures: list[str] = []
        turns: list[dict[str, Any]] = []
        try:
            if scenario.fault == "backend_offline":

                def refuse(request: httpx.Request) -> httpx.Response:
                    raise httpx.ConnectError("connection refused (injected)")

                transport = httpx.MockTransport(refuse)
                client = ChatApiClient(
                    http_client=httpx.Client(base_url="http://offline", transport=transport),
                    sleep=lambda seconds: None,
                )
            else:
                client = ChatApiClient(http_client=http, sleep=lambda seconds: None)
            state = ChatState()
            actions.start_conversation(state, client)
            if scenario.fault == "database_unavailable":
                with database.write() as session:
                    from sqlalchemy import text

                    session.execute(text("DROP TABLE conversation_messages"))

            for index, turn in enumerate(scenario.turns):
                last = index == len(scenario.turns) - 1
                if index == 1 and scenario.fault == "closed_after_first_turn":
                    actions.close_conversation(state, client)
                started = time.perf_counter()
                queued = state.queue_message(turn.message, max_chars=4000)
                if queued is not None:
                    actions.send_pending(state, client)
                elapsed_ms = (time.perf_counter() - started) * 1000
                record, problems = _check_turn(scenario, turn, state, queued is not None, last)
                record["latency_ms"] = round(elapsed_ms, 1)
                turns.append(record)
                failures += [f"turn {index + 1}: {p}" for p in problems]

            failures += _check_state(scenario, state, client)
            if scenario.llm in ("faithful", "obedient") and model is not None:
                answered = [t for t in turns if t.get("decision") == "answered"]
                if answered and model.calls == 0:
                    failures.append("the LLM stand-in was never called for an answered turn")
        finally:
            http.__exit__(None, None, None)
            database.dispose()
        return {
            "id": scenario.id,
            "category": scenario.category,
            "passed": not failures,
            "known_issue": scenario.known_issue,
            "failures": failures,
            "turns": turns,
        }


def _check_turn(
    scenario: Scenario, turn: Turn, state: Any, sent: bool, last: bool
) -> tuple[dict[str, Any], list[str]]:
    problems: list[str] = []
    error = state.last_error.kind.value if state.last_error is not None else None
    record: dict[str, Any] = {"message": turn.message, "error": error}
    if turn.refused_by_page:
        return record, ["the page sent it"] if sent else []
    if turn.error is not None:
        if error != turn.error:
            problems.append(f"expected error {turn.error}, got {error or 'none'}")
        return record, problems
    if not sent or error is not None:
        return record, [f"expected a reply, got error {error or 'not sent'}"]
    reply = state.messages[-1]
    sources = [s.faq_id for s in reply.sources]
    record.update(
        decision=reply.decision.value if reply.decision else None,
        generation_method=reply.generation_method,
        sources=sources,
        answer=reply.content,
    )
    if reply.role != "assistant":
        return record, ["no reply was shown"]
    if turn.decision and record["decision"] not in turn.decision:
        problems.append(f"decision {record['decision']} not in {turn.decision}")
    if record["decision"] in SOURCELESS and sources:
        problems.append(f"{record['decision']} reply lists sources {sources}")
    if turn.sources_any and record["decision"] in ("answered", "ambiguous"):
        if not set(turn.sources_any) & set(sources):
            problems.append(f"sources {sources} include none of {turn.sources_any}")
    if record["decision"] == "answered" and not sources:
        problems.append("an answer without sources")
    if turn.generation_method and record["generation_method"] != turn.generation_method:
        problems.append(f"generation method {record['generation_method']}")
    forbidden = set(scenario.forbidden_sources)
    if last:
        forbidden |= set(scenario.forbidden_sources_last_turn)
    if forbidden & set(sources):
        problems.append(f"forbidden sources {sorted(forbidden & set(sources))}")
    lowered = reply.content.lower()
    for phrase in scenario.must_not_include:
        if phrase.lower() in lowered:
            problems.append(f"unsafe phrase in the reply: {phrase!r}")
    return record, problems


def _check_state(scenario: Scenario, state: Any, client: Any) -> list[str]:
    from frontend.api_client import ApiError

    expected = scenario.expected_state
    problems = []
    status = state.conversation_status.value if state.conversation_status else None
    if status != expected.status:
        problems.append(f"conversation status {status}, expected {expected.status}")
    if expected.pending is not None and (state.pending is not None) != expected.pending:
        problems.append(f"pending message: {state.pending is not None}")
    if expected.draft is not None and state.draft != expected.draft:
        problems.append("the unsent message is not back in the input")
    if expected.stored_messages is not None and state.credentials is not None:
        try:
            history = client.get_history(state.credentials)
        except ApiError as error:
            return [*problems, f"history unavailable: {error.kind}"]
        if history.total_messages != expected.stored_messages:
            problems.append(f"{history.total_messages} stored messages")
        shown = [(m.role, m.content) for m in state.messages if m.delivery == "sent"]
        if shown != [(m.role, m.content) for m in history.messages]:
            problems.append("the page differs from the stored conversation")
    return problems


def summarise(results: list[dict[str, Any]]) -> str:
    by_category: dict[str, Counter[str]] = {}
    for result in results:
        counts = by_category.setdefault(result["category"], Counter())
        counts["passed" if result["passed"] else "failed"] += 1
    latencies = sorted(t["latency_ms"] for r in results for t in r["turns"])
    lines = [f"{'category':22} passed/total"]
    for category, counts in sorted(by_category.items()):
        total = counts["passed"] + counts["failed"]
        lines.append(f"{category:22} {counts['passed']}/{total}")
    passed = sum(r["passed"] for r in results)
    lines.append(f"{'all':22} {passed}/{len(results)}")
    known = [r["id"] for r in results if r["known_issue"] and not r["passed"]]
    if known:
        lines.append(f"known issues (failing as recorded): {', '.join(known)}")
    if latencies:
        median = latencies[len(latencies) // 2]
        lines.append(
            f"turn latency (frontend client, in-process backend): median {median:.0f} ms, "
            f"max {latencies[-1]:.0f} ms over {len(latencies)} turns"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--only", nargs="*", help="scenario IDs to run")
    parser.add_argument("--offline", action="store_true", help="never download the model")
    parser.add_argument("--output", type=Path, help="write every turn's result as JSON")
    args = parser.parse_args(argv)
    if args.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"

    from app.core.config import Settings
    from app.database.faq_repository import FAQRepository
    from app.services.faq_service import FAQService

    def settings_factory() -> Settings:
        return Settings(_env_file=None, query_rewrite_use_llm=False, llm_enabled=False)

    repository = FAQRepository.from_json_file(settings_factory().faq_data_path)
    dataset = load_dataset(args.dataset)
    problems = validate_dataset(
        dataset, {f.id for f in repository.list_active()}, {f.id for f in repository.list_all()}
    )
    if problems:
        print("The dataset is invalid:\n  " + "\n  ".join(problems))
        return 2
    scenarios = [s for s in dataset.scenarios if not args.only or s.id in args.only]
    with tempfile.TemporaryDirectory() as workdir:
        runner = Runner(settings_factory, FAQService(repository), None, Path(workdir))
        print("Loading the embedding model and FAQ index (once)...")
        started = time.perf_counter()
        runner.semantic.build_index()
        print(f"Ready in {time.perf_counter() - started:.1f} s.\n")
        results = []
        for scenario in scenarios:
            result = runner.run(scenario)
            results.append(result)
            mark = "PASS" if result["passed"] else "FAIL"
            if scenario.known_issue:
                mark = "FIXED?" if result["passed"] else "KNOWN"
            print(f"{mark} {scenario.id} {scenario.category:20} {scenario.description}")
            for failure in result["failures"]:
                print(f"       - {failure}")
            if scenario.known_issue and not result["passed"]:
                print(f"       known issue: {scenario.known_issue}")
    print("\n" + summarise(results))
    if args.output:
        args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nWrote {args.output}")
    # Known issues that still fail don't fail the run; new failures do.
    return 0 if all(r["passed"] or r["known_issue"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
