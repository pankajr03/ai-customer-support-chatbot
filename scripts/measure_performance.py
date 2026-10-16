"""Measure where time goes in a customer chat turn (Stage 9, Task 14).

Usage (from the project root, virtual environment active):

    python scripts/measure_performance.py --offline
    python scripts/measure_performance.py --offline --conversations 30 --output perf.json

Everything runs in this process: the real FastAPI app through Starlette's TestClient (no
network, no uvicorn), the real FAQ file, the real embedding model, conversation storage
in a temporary SQLite file. Times are wall-clock ``perf_counter`` measurements on this
machine; nothing is estimated. What is measured how:

- **cold start**: building the app, its startup (schema, cleanup), and the first chat
  turn, which loads the embedding model and builds the FAQ index (``SEMANTIC_INDEX_PRELOAD``
  is off, as by default);
- **warm turns**: every stage is timed by wrapping the services' methods from outside
  (keyword search, semantic search, query embedding, the answer decision, the grounded
  answer, the LLM call) and every SQL statement through SQLAlchemy events;
- **generation**: with ``--llm off`` (default) no LLM is involved; with ``--llm mock`` a
  local stand-in replies instantly, so only this application's prompt building and
  validation are measured, **not** OpenAI's latency. No request is ever sent to OpenAI;
- **frontend**: the Streamlit page (``AppTest``) against the same backend: HTTP calls
  and script runs per action.

Logs record durations and counts only; this script never prints message text.
"""

import argparse
import json
import logging
import os
import statistics
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/measure_performance.py`
    sys.path.insert(0, str(PROJECT_ROOT))

# Questions with known, varied outcomes (answered, ambiguous, no match), and follow-ups.
CONVERSATIONS = [
    ["How can I track my order?", "How long does it take to arrive?"],
    ["What is your return policy?", "How do I return an item?", "Do I have to pay for it?"],
    ["I want my money back", "Where will my refund be sent?"],
    ["What's the weather like on Mars?"],
    ["How do I reset my password?", "Do you ship internationally?"],
    ["Why was my payment declined?", "What payment methods do you accept?"],
]


class Probe:
    """Collects durations (ms) per metric, and SQL statements per request."""

    def __init__(self) -> None:
        self.times: dict[str, list[float]] = defaultdict(list)
        self.current: dict[str, float] = defaultdict(float)
        self.sql: dict[str, int] = defaultdict(int)
        self.sql_per_request: list[dict[str, int]] = []
        self.prompt_chars: list[int] = []

    def wrap(self, obj: Any, method: str, metric: str) -> None:
        original = getattr(obj, method)

        def timed(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                elapsed = (time.perf_counter() - started) * 1000
                self.current[metric] += elapsed

        setattr(obj, method, timed)

    def watch_sql(self, engine: Any) -> None:
        from sqlalchemy import event

        @event.listens_for(engine, "before_cursor_execute")
        def before(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            conn.info.setdefault("probe_started", []).append(time.perf_counter())

        @event.listens_for(engine, "after_cursor_execute")
        def after(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            started = conn.info["probe_started"].pop()
            kind = "db_read" if statement.lstrip().upper().startswith("SELECT") else "db_write"
            if statement.lstrip().upper().startswith(("PRAGMA", "BEGIN")):
                kind = "db_other"
            self.current[kind] += (time.perf_counter() - started) * 1000
            self.sql[kind] += 1

    def start_request(self) -> None:
        self.current = defaultdict(float)
        self.sql = defaultdict(int)

    def end_request(self, total_metric: str, total_ms: float) -> None:
        self.times[total_metric].append(total_ms)
        for metric, ms in self.current.items():
            self.times[f"{total_metric}.{metric}"].append(ms)
        self.sql_per_request.append(dict(self.sql))


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def build_app(settings: Any, llm_mode: str, workdir: Path, probe: Probe) -> tuple[Any, Any]:
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from app.database.connection import Database
    from app.main import create_app
    from app.services.llm_service import LLMService
    from scripts.evaluate_integration import LocalModel

    llm_service = None
    if llm_mode == "mock":
        model = LocalModel("faithful", None)
        original = model.responses.create

        def create(**kwargs: Any) -> Any:
            probe.prompt_chars.append(
                len(str(kwargs.get("instructions", ""))) + len(str(kwargs["input"]))
            )
            return original(**kwargs)

        model.responses.create = create
        llm_service = LLMService(
            SecretStr("sk-local-" + "0" * 32),
            model="local-stand-in",
            max_retries=0,
            reasoning_effort=None,
            client_factory=lambda key: model,
        )
    database = Database(f"sqlite:///{(workdir / f'perf-{uuid.uuid4().hex}.db').as_posix()}")
    probe.watch_sql(database.engine)
    started = time.perf_counter()
    app = create_app(settings, database=database, llm_service=llm_service)
    probe.times["startup.create_app"].append((time.perf_counter() - started) * 1000)
    started = time.perf_counter()
    client = TestClient(app)
    client.__enter__()
    probe.times["startup.lifespan"].append((time.perf_counter() - started) * 1000)
    state = app.state
    probe.wrap(state.keyword_search_service, "search", "keyword_search")
    probe.wrap(state.semantic_search_service, "search", "semantic_search")
    probe.wrap(state.semantic_search_service._embeddings, "encode_batch", "query_embedding")
    probe.wrap(state.semantic_search_service, "build_index", "index_build")
    probe.wrap(state.answer_decision_service, "decide", "retrieval_and_decision")
    probe.wrap(state.grounded_answer_service, "answer", "grounded_answer")
    probe.wrap(state.llm_service, "generate", "llm_call")
    return app, client


def chat(
    client: Any, probe: Probe, sid: str, headers: dict[str, str], text: str, metric: str
) -> dict:
    probe.start_request()
    started = time.perf_counter()
    response = client.post(
        f"/api/v1/conversations/{sid}/chat",
        json={"message": text, "client_message_id": str(uuid.uuid4())},
        headers=headers,
    )
    probe.end_request(metric, (time.perf_counter() - started) * 1000)
    response.raise_for_status()
    return response.json()


def measure_backend(settings: Any, llm_mode: str, conversations: int, workdir: Path) -> Probe:
    probe = Probe()
    app, client = build_app(settings, llm_mode, workdir, probe)
    try:
        # Cold: the first turn loads the model and builds the index.
        probe.start_request()
        started = time.perf_counter()
        created = client.post("/api/v1/conversations").json()
        probe.end_request("create_conversation_cold", (time.perf_counter() - started) * 1000)
        headers = {"Authorization": f"Bearer {created['access_token']}"}
        chat(client, probe, created["session_id"], headers, CONVERSATIONS[0][0], "chat_first_turn")

        # Warm.
        decisions: dict[str, int] = defaultdict(int)
        for index in range(conversations):
            probe.start_request()
            started = time.perf_counter()
            created = client.post("/api/v1/conversations").json()
            probe.end_request("create_conversation", (time.perf_counter() - started) * 1000)
            headers = {"Authorization": f"Bearer {created['access_token']}"}
            for text in CONVERSATIONS[index % len(CONVERSATIONS)]:
                body = chat(client, probe, created["session_id"], headers, text, "chat_turn")
                decisions[body["decision"]] += 1
            probe.start_request()
            started = time.perf_counter()
            client.get(f"/api/v1/conversations/{created['session_id']}/messages", headers=headers)
            probe.end_request("history_read", (time.perf_counter() - started) * 1000)
        probe.times["_decisions"] = [float(v) for v in decisions.values()]
        probe.decisions = dict(decisions)  # type: ignore[attr-defined]
    finally:
        client.__exit__(None, None, None)
        app.state.database.dispose()
    return probe


def measure_frontend(settings: Any, workdir: Path) -> dict[str, Any]:
    """HTTP calls and script runs of the Streamlit page per action (in-process backend)."""
    from streamlit.testing.v1 import AppTest

    from frontend import resources
    from frontend.api_client import BackendStatus, ChatApiClient
    from frontend.config import FrontendSettings

    probe = Probe()
    app, client = build_app(settings, "off", workdir, probe)
    calls: list[str] = []
    client.event_hooks = {
        "request": [lambda r: calls.append(f"{r.method} {r.url.path}")],
        "response": [],
    }
    api = ChatApiClient(http_client=client, sleep=lambda seconds: None)
    runs = [0]
    real_settings = FrontendSettings(_env_file=None)

    def get_settings() -> FrontendSettings:
        runs[0] += 1  # called once per script run
        return real_settings

    status = lambda _client: BackendStatus.ONLINE  # noqa: E731
    status.clear = lambda: None  # type: ignore[attr-defined]
    resources.get_api_client = lambda: api  # type: ignore[assignment]
    resources.backend_status = status  # type: ignore[assignment]
    resources.get_settings = get_settings  # type: ignore[assignment]
    result: dict[str, Any] = {}
    try:
        page = AppTest.from_file(str(PROJECT_ROOT / "frontend" / "app.py"), default_timeout=300)
        page.run()
        result["page_open"] = {"http_calls": list(calls), "script_runs": runs[0]}
        for label, text in (
            ("first_message", "How can I track my order?"),
            ("follow_up", "How long does it take?"),
        ):
            calls.clear()
            runs[0] = 0
            started = time.perf_counter()
            page.chat_input[0].set_value(text).run()
            result[label] = {
                "http_calls": list(calls),
                "script_runs": runs[0],
                "page_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        calls.clear()
        runs[0] = 0
        page.run()
        result["rerun_without_action"] = {"http_calls": list(calls), "script_runs": runs[0]}
    finally:
        client.__exit__(None, None, None)
        app.state.database.dispose()
    return result


def report(probe: Probe, label: str) -> list[dict[str, Any]]:
    rows = []
    print(f"\n{label}")
    print(f"{'metric':52} {'n':>4} {'p50 ms':>9} {'p95 ms':>9} {'max ms':>9}")
    for metric in sorted(probe.times):
        values = probe.times[metric]
        if metric.startswith("_") or not values:
            continue
        row = {
            "metric": metric,
            "n": len(values),
            "p50_ms": round(statistics.median(values), 1),
            "p95_ms": round(percentile(values, 0.95), 1),
            "max_ms": round(max(values), 1),
        }
        rows.append(row)
        print(f"{metric:52} {row['n']:>4} {row['p50_ms']:>9} {row['p95_ms']:>9} {row['max_ms']:>9}")
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--conversations", type=int, default=24)
    parser.add_argument("--llm", choices=["off", "mock"], default="off")
    parser.add_argument("--offline", action="store_true", help="never download the model")
    parser.add_argument("--skip-frontend", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
    logging.disable(logging.INFO)  # keep the report readable; durations are measured here

    from app.core.config import Settings

    # Rate limits off: one client creating many conversations quickly is the point here.
    settings = Settings(
        _env_file=None, query_rewrite_use_llm=False, llm_enabled=False, rate_limit_enabled=False
    )
    output: dict[str, Any] = {
        "environment": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "llm": args.llm,
            "embedding_model": settings.embedding_model_name,
            "note": "in-process TestClient; real FAQ file and embedding model; LLM "
            + ("off" if args.llm == "off" else "mocked locally (no OpenAI latency)")
            + "; rate limits off",
        }
    }
    with tempfile.TemporaryDirectory() as workdir:
        started = time.perf_counter()
        probe = measure_backend(settings, args.llm, args.conversations, Path(workdir))
        output["wall_seconds"] = round(time.perf_counter() - started, 1)
        output["backend"] = report(
            probe, f"Backend (LLM {args.llm}), {args.conversations} conversations"
        )
        output["decisions"] = probe.decisions  # type: ignore[attr-defined]
        turns = [s for s in probe.sql_per_request if s]
        if turns:
            output["sql_statements_per_request_max"] = {
                kind: max(t.get(kind, 0) for t in turns)
                for kind in ("db_read", "db_write", "db_other")
            }
        if probe.prompt_chars:
            output["llm_prompt_chars"] = {
                "median": statistics.median(probe.prompt_chars),
                "max": max(probe.prompt_chars),
            }
        if not args.skip_frontend:
            output["frontend"] = measure_frontend(settings, Path(workdir))
    print("\nDecisions:", output["decisions"])
    print("SQL statements per request (max):", output.get("sql_statements_per_request_max"))
    if "llm_prompt_chars" in output:
        print("LLM prompt size (characters):", output["llm_prompt_chars"])
    if "frontend" in output:
        print("\nFrontend (Streamlit AppTest, in-process backend):")
        for action, data in output["frontend"].items():
            print(f"  {action:22} runs={data['script_runs']} calls={data['http_calls']}")
    if args.output:
        args.output.write_text(json.dumps(output, indent=2), encoding="utf-8")
        print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
