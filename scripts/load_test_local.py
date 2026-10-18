"""Controlled local performance and load test (Stage 12, Task 20).

Usage (from the project root, virtual environment active):

    python scripts/load_test_local.py
    python scripts/load_test_local.py --requests 100 --concurrency 4
    python scripts/load_test_local.py --output data/load_test_results.json
    python scripts/load_test_local.py --encoder lexical            # no PyTorch

What it does
------------
Starts the real application under uvicorn on ``127.0.0.1`` (a free port, this machine
only) with a temporary SQLite database seeded from ``data/faqs.json``, the real cached
embedding model and a **mocked language model** (the scripted model of
``scripts/ai_eval_harness.py``: no network, no paid calls). Then, over real HTTP:

1. **Cold start**: time to build the application, and the first semantic request
   (embedding-model load and FAQ index build), measured separately from warm requests.
2. **Scenarios** with bounded concurrency: health check, keyword FAQ search, hybrid
   search, conversation creation, and chat (several conversations in parallel, several
   messages each; one message at a time per conversation, as the API requires).
3. **Database queries** timed directly (history load, ticket list, FAQ catalogue reload).
4. **Memory**: the process's resident memory before and after, and its peak.

Safety limits: only ``127.0.0.1``; at most ``MAX_REQUESTS`` requests per scenario and
``MAX_CONCURRENCY`` concurrent requests; rate limits are switched off in this test
server so that capacity, not the limiter, is measured (the production defaults would
answer most of this load with 429). Nothing external is contacted.

What the numbers mean
---------------------
Latencies are client-observed over loopback HTTP on this machine, with this machine's
other load. They are not production figures: one process, SQLite, a mocked model (a
real model adds its own latency, typically seconds). Chat writes are serialised by
SQLite's single writer; under concurrency some requests wait for the lock (up to the
5-second busy timeout) and, beyond it, get ``503`` with ``Retry-After``, which is
counted as an error here.
"""

import argparse
import asyncio
import ctypes
import json
import os
import socket
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/load_test_local.py`
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

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
from scripts.evaluate_semantic_search import environment_info  # noqa: E402

HOST = "127.0.0.1"
MAX_REQUESTS = 2_000
MAX_CONCURRENCY = 32
QUESTIONS = [
    "How can I track my order?",
    "What is your return policy?",
    "How long does delivery take?",
    "How do I reset my password?",
    "What payment methods do you accept?",
    "Where will my refund be sent?",
]


# --- Memory (no extra dependency) ------------------------------------------------------------


def memory_mb() -> dict[str, float | None]:
    """Resident memory now and the peak so far, in MB, where the OS reports it."""
    if os.name == "nt":

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]  # fmt: skip

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
        psapi = ctypes.WinDLL("psapi")  # type: ignore[attr-defined]
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p  # a 64-bit pseudo-handle
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(Counters),
            ctypes.c_ulong,
        ]
        if psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            return {
                "rss_mb": round(counters.WorkingSetSize / 2**20, 1),
                "peak_mb": round(counters.PeakWorkingSetSize / 2**20, 1),
            }
        return {"rss_mb": None, "peak_mb": None}
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # KB on Linux
        divisor = 2**20 if sys.platform == "darwin" else 2**10
        return {"rss_mb": None, "peak_mb": round(peak / divisor, 1)}
    except ImportError:  # pragma: no cover
        return {"rss_mb": None, "peak_mb": None}


# --- Statistics -------------------------------------------------------------------------


def latency_summary(values_ms: Sequence[float]) -> dict[str, float | int | None]:
    if not values_ms:
        return {"samples": 0}
    ordered = sorted(values_ms)

    def pct(fraction: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(fraction * len(ordered)))], 1)

    return {
        "samples": len(ordered),
        "mean_ms": round(statistics.fmean(ordered), 1),
        "p50_ms": pct(0.5),
        "p90_ms": pct(0.9),
        "p99_ms": pct(0.99),
        "max_ms": round(ordered[-1], 1),
    }


# --- The server ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


class LocalServer:
    """The application under uvicorn in a background thread, on 127.0.0.1 only."""

    def __init__(self, app: Any) -> None:
        self.port = _free_port()
        self.server = uvicorn.Server(
            uvicorn.Config(app, host=HOST, port=self.port, log_level="warning", lifespan="on")
        )
        self.thread = threading.Thread(target=self.server.run, name="load-test-server", daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{HOST}:{self.port}"

    def __enter__(self) -> "LocalServer":
        self.thread.start()
        deadline = time.monotonic() + 60
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("the local test server did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=30)


# --- Load ---------------------------------------------------------------------------------


async def run_bounded(
    count: int, concurrency: int, make: Callable[[int], Awaitable[httpx.Response]]
) -> dict[str, Any]:
    """``count`` requests with at most ``concurrency`` in flight; latency and errors."""
    semaphore = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    statuses: Counter[str] = Counter()

    async def one(i: int) -> None:
        async with semaphore:
            started = time.perf_counter()
            try:
                response = await make(i)
                statuses[str(response.status_code)] += 1
            except httpx.HTTPError as exc:
                statuses[type(exc).__name__] += 1
            latencies.append((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(count)))
    elapsed = time.perf_counter() - started
    errors = sum(n for status, n in statuses.items() if not status.startswith("2"))
    return {
        "requests": count,
        "concurrency": concurrency,
        "seconds": round(elapsed, 2),
        "throughput_rps": round(count / elapsed, 1) if elapsed else None,
        "latency": latency_summary(latencies),
        "statuses": dict(statuses),
        "error_rate": {"n": errors, "of": count, "rate": errors / count if count else None},
    }


async def scenarios(base_url: str, requests: int, concurrency: int) -> dict[str, Any]:
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=60) as client:
        results: dict[str, Any] = {}
        results["health"] = await run_bounded(
            requests, concurrency, lambda i: client.get("/api/v1/health")
        )
        results["faq_keyword_search"] = await run_bounded(
            requests,
            concurrency,
            lambda i: client.get("/api/v1/faqs/search", params={"q": QUESTIONS[i % 6]}),
        )
        results["hybrid_search"] = await run_bounded(
            requests,
            concurrency,
            lambda i: client.post("/api/v1/hybrid-search", json={"query": QUESTIONS[i % 6]}),
        )
        results["create_conversation"] = await run_bounded(
            requests, concurrency, lambda i: client.post("/api/v1/conversations")
        )

        # Chat: `concurrency` conversations in parallel, each sending its messages in turn.
        per_conversation = max(1, requests // concurrency)
        sessions = []
        for _ in range(concurrency):
            created = (await client.post("/api/v1/conversations")).json()
            sessions.append((created["session_id"], created["access_token"]))
        latencies: list[float] = []
        statuses: Counter[str] = Counter()

        async def conversation(session_id: str, token: str) -> None:
            headers = {"Authorization": f"Bearer {token}"}
            for n in range(per_conversation):
                started = time.perf_counter()
                try:
                    response = await client.post(
                        f"/api/v1/conversations/{session_id}/chat",
                        json={"message": QUESTIONS[n % 6]},
                        headers=headers,
                    )
                    statuses[str(response.status_code)] += 1
                except httpx.HTTPError as exc:
                    statuses[type(exc).__name__] += 1
                latencies.append((time.perf_counter() - started) * 1000)

        started = time.perf_counter()
        await asyncio.gather(*(conversation(s, t) for s, t in sessions))
        elapsed = time.perf_counter() - started
        total = per_conversation * len(sessions)
        errors = sum(n for status, n in statuses.items() if status != "200")
        results["chat"] = {
            "conversations": len(sessions),
            "messages_per_conversation": per_conversation,
            "requests": total,
            "seconds": round(elapsed, 2),
            "throughput_rps": round(total / elapsed, 1) if elapsed else None,
            "latency": latency_summary(latencies),
            "statuses": dict(statuses),
            "error_rate": {"n": errors, "of": total, "rate": errors / total if total else None},
        }
        return results


def database_timings(app: Any, repeats: int = 50) -> dict[str, Any]:
    """Repository calls timed directly (no HTTP)."""
    from app.services.conversation_history_service import ConversationHistoryService  # noqa: F401

    tickets = app.state.ticket_repository
    faqs = app.state.faq_service.repository
    conversations = app.state.conversation_service
    created = conversations.create_conversation()

    def timed(call: Callable[[], Any]) -> dict[str, Any]:
        values = []
        for _ in range(repeats):
            started = time.perf_counter()
            call()
            values.append((time.perf_counter() - started) * 1000)
        return latency_summary(values)

    from app.repositories.ticket_repository import TicketFilter

    return {
        "repeats": repeats,
        "conversation_lookup": timed(
            lambda: conversations.get_conversation(created.session.session_id, created.access_token)
        ),
        "ticket_list_page": timed(lambda: tickets.list_tickets(TicketFilter(), offset=0, limit=50)),
        "faq_catalogue_reload": timed(faqs.reload),
    }


def run(
    *,
    requests: int,
    concurrency: int,
    encoder: str,
    faq_path: Path = DEFAULT_FAQ_DATA,
) -> dict[str, Any]:
    if not 1 <= requests <= MAX_REQUESTS or not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"requests 1-{MAX_REQUESTS}, concurrency 1-{MAX_CONCURRENCY}")
    memory_before = memory_mb()
    with tempfile.TemporaryDirectory(prefix="load-test-") as folder:
        url = f"sqlite:///{(Path(folder) / 'load.db').as_posix()}"
        settings = harness_settings(
            url,
            faq_path,
            hash_staff_token(generate_staff_token()),
            query_rewrite_use_llm=False,
            rate_limit_enabled=False,
        )
        embeddings = embedding_service(encoder, settings.embedding_model_name)
        model = ScriptedModel()
        llm = LLMService(
            settings.openai_api_key,
            model="mocked-model",
            max_retries=0,
            client_factory=lambda k: model,
        )
        holder: list[Any] = []
        semantic = SemanticSearchService(
            lambda: holder[0].state.faq_service.list_active_faqs(),
            embeddings,
            default_min_score=settings.semantic_min_similarity,
            catalog_revision=lambda: holder[0].state.faq_service.catalog_revision(),
        )
        database = Database(url)
        started = time.perf_counter()
        app = create_app(
            settings, semantic_search_service=semantic, llm_service=llm, database=database
        )
        holder.append(app)
        build_ms = (time.perf_counter() - started) * 1000

        with (
            LocalServer(app) as server,
            httpx.Client(base_url=server.base_url, timeout=300) as http,
        ):
            cold = {}
            for name, path, body in (
                ("first_request_health", "/api/v1/health", None),
                ("first_semantic_request", "/api/v1/hybrid-search", {"query": QUESTIONS[0]}),
                ("second_semantic_request", "/api/v1/hybrid-search", {"query": QUESTIONS[1]}),
            ):
                t0 = time.perf_counter()
                response = http.get(path) if body is None else http.post(path, json=body)
                status = response.status_code
                cold[name] = {"ms": round((time.perf_counter() - t0) * 1000, 1), "status": status}
            memory_warm = memory_mb()
            load = asyncio.run(scenarios(server.base_url, requests, concurrency))
            db = database_timings(app)
        memory_after = memory_mb()
        database.dispose()
    return {
        "meta": {
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "host": HOST,
            "requests_per_scenario": requests,
            "concurrency": concurrency,
            "encoder": encoder,
            "embedding_model": embeddings.model_name,
            "llm": "mocked (scripted model, no network)",
            "rate_limits": "off in this test server",
            "database": "SQLite (temporary file, WAL)",
            "environment": environment_info(),
            "cpu_count": os.cpu_count(),
        },
        "cold_start": {"build_app_ms": round(build_ms, 1), **cold},
        "scenarios": load,
        "database": db,
        "memory": {"before": memory_before, "after_warm_up": memory_warm, "after": memory_after},
        "model_calls": len(model.calls),
    }


def format_report(report: dict[str, Any]) -> str:
    meta, cold = report["meta"], report["cold_start"]
    lines = [
        f"Local load test, {meta['created_at']} ({meta['requests_per_scenario']} requests "
        f"per scenario, concurrency {meta['concurrency']}, {meta['embedding_model']}, "
        f"LLM {meta['llm']})",
        "",
        "Cold start:",
        f"  build application          {cold['build_app_ms']} ms",
        f"  first request (health)     {cold['first_request_health']['ms']} ms",
        f"  first semantic request     {cold['first_semantic_request']['ms']} ms "
        "(model load + index)",
        f"  second semantic request    {cold['second_semantic_request']['ms']} ms (warm)",
        "",
        f"{'Scenario':<22}{'req':>6}{'rps':>8}{'p50 ms':>9}{'p90 ms':>9}{'p99 ms':>9}"
        f"{'max ms':>9}  errors",
    ]
    for name, row in report["scenarios"].items():
        lat = row["latency"]
        err = row["error_rate"]
        lines.append(
            f"{name:<22}{row['requests']:>6}{row['throughput_rps']:>8}{lat['p50_ms']:>9}"
            f"{lat['p90_ms']:>9}{lat['p99_ms']:>9}{lat['max_ms']:>9}  "
            f"{err['n']}/{err['of']} {row['statuses']}"
        )
    lines += ["", f"Database calls ({report['database']['repeats']} each):"]
    for name, row in report["database"].items():
        if isinstance(row, dict):
            lines.append(
                f"  {name:<24} p50 {row['p50_ms']} ms, p90 {row['p90_ms']} ms, "
                f"max {row['max_ms']} ms"
            )
    mem = report["memory"]
    lines += [
        "",
        f"Memory (MB): before {mem['before']}, after warm-up {mem['after_warm_up']}, "
        f"after {mem['after']}",
        f"Mocked model calls: {report['model_calls']}",
        "",
        "Local, single-process, SQLite, mocked LLM: not production figures.",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--requests", type=int, default=200, help=f"per scenario (max {MAX_REQUESTS})"
    )
    parser.add_argument("--concurrency", type=int, default=8, help=f"(max {MAX_CONCURRENCY})")
    parser.add_argument("--encoder", choices=("model", "lexical"), default="model")
    parser.add_argument("--output", type=Path, help="also write the full report as JSON")
    args = parser.parse_args(argv)
    try:
        report = run(requests=args.requests, concurrency=args.concurrency, encoder=args.encoder)
    except ValueError as exc:
        print(exc)
        return 2
    print(format_report(report))
    if args.output:
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nFull report written to {args.output}")
    errors = sum(row["error_rate"]["n"] for row in report["scenarios"].values())
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
