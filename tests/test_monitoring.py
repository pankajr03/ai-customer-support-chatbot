"""Basic monitoring (Stage 13): the in-memory metrics, where they are recorded, the staff
endpoint, the periodic log snapshot, and what they never contain. docs/monitoring.md"""

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core import metrics as metrics_module
from app.core.config import Settings
from app.core.metrics import MAX_SERIES, OTHER, Metrics, http_method, metrics
from app.core.staff_auth import generate_staff_token, hash_staff_token
from app.database.connection import Database
from app.main import create_app
from app.models.support_api import SupportTicketCreateRequest
from app.repositories.ticket_repository import TicketStoreError
from app.services.embedding_service import EmbeddingError, EmbeddingService
from app.services.faq_service import FAQService
from app.services.monitoring_service import log_snapshot, log_snapshots_every
from app.services.semantic_search_service import SemanticSearchService
from tests.fakes import FAKE_MODEL_NAME, CountingLoader
from tests.test_llm_service import generate, service

ADMIN = generate_staff_token()
SUPPORT = generate_staff_token()
MONITORING = "/api/v1/admin/monitoring"
BASE = "/api/v1/conversations"
TICKET = SupportTicketCreateRequest.model_config["json_schema_extra"]["examples"][0]


@pytest.fixture(autouse=True)
def fresh_metrics() -> Iterator[None]:
    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture
def make_app(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> Callable[..., FastAPI]:
    def _make(**settings: Any) -> FastAPI:
        staff = [
            f"alex:admin:{hash_staff_token(ADMIN)}",
            f"sam:support:{hash_staff_token(SUPPORT)}",
        ]
        return create_app(
            make_settings(staff_token_hashes=staff, **settings), faq_service=faq_service
        )

    return _make


@pytest.fixture
def setup_app(make_app: Callable[..., FastAPI], request: pytest.FixtureRequest) -> FastAPI:
    """Built during fixture setup: create_app reconfigures logging (dictConfig), which
    would drop pytest's capture handler if it ran in the test body."""
    return make_app(**getattr(request, "param", {}))


def as_admin() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN}"}


def latency(body: dict[str, Any], name: str, **labels: str) -> dict[str, Any]:
    return next(row for row in body["latencies"][name] if row["labels"] == labels)


# --- The registry ---------------------------------------------------------------------------


def test_counters_and_latencies_with_percentiles_over_a_recent_window() -> None:
    registry = Metrics()
    for value in range(1, 101):
        registry.observe_ms("work", float(value), kind="x")
    registry.increment("events", outcome="ok")
    registry.increment("events", 2, outcome="ok")

    (row,) = registry.latencies()["work"]
    assert row["labels"] == {"kind": "x"}
    assert (row["count"], row["p50_ms"], row["p95_ms"], row["p99_ms"]) == (100, 50.0, 95.0, 99.0)
    assert (row["average_ms"], row["max_ms"]) == (50.5, 100.0)
    assert registry.counter("events", outcome="ok") == 3


def test_the_window_is_bounded_but_count_and_maximum_are_all_time() -> None:
    registry = Metrics()
    registry.observe_ms("work", 10_000.0)
    for _ in range(metrics_module.LATENCY_WINDOW):
        registry.observe_ms("work", 1.0)
    (row,) = registry.latencies()["work"]
    assert row["recent_samples"] == metrics_module.LATENCY_WINDOW
    assert row["p99_ms"] == 1.0  # the old outlier left the window
    assert (row["count"], row["max_ms"]) == (metrics_module.LATENCY_WINDOW + 1, 10_000.0)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0])
def test_impossible_durations_are_ignored(value: float) -> None:
    registry = Metrics()
    registry.observe_ms("work", value)
    assert registry.latencies() == {}


def test_label_combinations_are_capped_and_folded_into_other() -> None:
    registry = Metrics()
    for i in range(MAX_SERIES + 50):
        registry.increment("http_requests", route=f"/r/{i}")
    rows = registry.counters()["http_requests"]
    assert len(rows) == MAX_SERIES + 1
    folded = next(row for row in rows if row["labels"] == {"route": OTHER})
    assert folded["count"] == 50


def test_unknown_http_methods_become_other() -> None:
    assert [http_method(m) for m in ("get", "POST", "PROPFIND", "X" * 50)] == [
        "GET",
        "POST",
        OTHER,
        OTHER,
    ]


def test_recording_is_thread_safe_and_cheap() -> None:
    registry = Metrics()

    def work() -> None:
        for _ in range(2_000):
            registry.increment("events", outcome="ok")
            registry.observe_ms("work", 1.0)

    started = time.perf_counter()
    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - started

    assert registry.counter("events", outcome="ok") == 16_000
    assert registry.latencies()["work"][0]["count"] == 16_000
    # 32,000 recordings: a generous bound, far above the measured few hundredths of a second.
    assert elapsed < 2.0


def test_uptime_counts_from_the_start() -> None:
    now = [100.0]
    registry = Metrics(clock=lambda: now[0])
    now[0] = 160.5
    assert registry.uptime_seconds() == 60.5
    assert registry.process()["uptime_seconds"] == 60.5


# --- The endpoint ---------------------------------------------------------------------------


def test_only_staff_with_the_analytics_permission_can_read_it(
    make_app: Callable[..., FastAPI],
) -> None:
    with TestClient(make_app()) as client:
        assert client.get(MONITORING).status_code == 401
        assert (
            client.get(MONITORING, headers={"Authorization": f"Bearer {SUPPORT}"}).status_code
            == 403
        )
        response = client.get(MONITORING, headers=as_admin())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_it_says_the_figures_are_process_local_and_not_durable(
    make_app: Callable[..., FastAPI],
) -> None:
    with TestClient(make_app()) as client:
        body = client.get(MONITORING, headers=as_admin()).json()
    assert "Process-local, in memory" in body["scope"]
    assert "Not durable" in body["scope"]
    assert body["process"]["uptime_seconds"] >= 0


def test_requests_statuses_and_error_categories_are_counted(
    make_app: Callable[..., FastAPI],
) -> None:
    with TestClient(make_app()) as client:
        client.get("/api/v1/faqs/categories")
        client.get("/api/v1/faqs/faq_999")
        client.get("/api/v1/faqs/faq_998")
        client.get("/api/v1/nowhere")
        body = client.get(MONITORING, headers=as_admin()).json()

    requests = body["requests"]
    assert requests["total"] == 4  # the monitoring call itself is counted after it answers
    assert (requests["client_errors"], requests["server_errors"]) == (3, 0)
    assert requests["by_status"] == {"200": 1, "404": 3}
    assert requests["by_error_category"] == {"NOT_FOUND": 3}
    faq = latency(body, "http_request_duration", method="GET", route="/api/v1/faqs/{faq_id}")
    assert faq["count"] == 2
    routes = {row["labels"]["route"] for row in body["counters"]["http_requests"]}
    assert routes == {"/api/v1/faqs/categories", "/api/v1/faqs/{faq_id}", "unmatched"}


def test_a_chat_turn_records_its_stages_retrieval_and_database_without_ids(
    make_app: Callable[..., FastAPI],
) -> None:
    with TestClient(make_app()) as client:
        created = client.post(BASE).json()
        sid, token = created["session_id"], created["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        reply = client.post(
            f"{BASE}/{sid}/chat",
            json={"message": "My email is jane@example.com. How do I reset my password?"},
            headers=headers,
        )
        response = client.get(MONITORING, headers=as_admin())

    assert reply.status_code == 200
    body = response.json()
    assert latency(body, "chat_turn_duration")["count"] == 1
    stages = {row["labels"]["stage"] for row in body["latencies"]["chat_stage_duration"]}
    assert stages == {"history", "rewrite", "pipeline", "store"}
    assert latency(body, "retrieval_duration")["count"] >= 1
    assert latency(body, "db_transaction_duration", kind="write")["count"] >= 2
    assert latency(body, "db_transaction_duration", kind="read")["count"] >= 1
    for private in (sid, token, "jane", "reset my password"):
        assert private not in response.text


def test_a_failed_support_request_is_counted(
    make_app: Callable[..., FastAPI], monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    with TestClient(app) as client:
        created = client.post(BASE).json()
        headers = {"Authorization": f"Bearer {created['access_token']}"}
        body = {
            **TICKET,
            "session_id": created["session_id"],
            "client_request_id": str(uuid.uuid4()),
        }
        assert client.post("/api/v1/support/tickets", json=body, headers=headers).status_code == 201

        def broken(*args: Any, **kwargs: Any) -> Any:
            raise TicketStoreError("ticket storage is unavailable (create)")

        monkeypatch.setattr(app.state.escalation_service._tickets, "create_ticket", broken)
        body = {**body, "client_request_id": str(uuid.uuid4())}
        assert client.post("/api/v1/support/tickets", json=body, headers=headers).status_code == 503
        snapshot = client.get(MONITORING, headers=as_admin()).json()

    outcomes = {
        r["labels"]["outcome"]: r["count"] for r in snapshot["counters"]["ticket_submissions"]
    }
    assert outcomes == {"created": 1, "failed": 1}
    assert snapshot["requests"]["by_error_category"]["SERVICE_UNAVAILABLE"] == 1


def test_index_build_failures_and_successes_are_counted(faq_service: FAQService) -> None:
    def broken(name: str) -> Any:
        raise OSError("no network")

    failing = SemanticSearchService(
        faq_service.list_active_faqs, EmbeddingService(FAKE_MODEL_NAME, loader=broken)
    )
    with pytest.raises(EmbeddingError):
        failing.build_index()
    working = SemanticSearchService(
        faq_service.list_active_faqs, EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader())
    )
    working.build_index()

    assert metrics.counter("faq_index_builds", outcome="failed") == 1
    assert metrics.counter("faq_index_builds", outcome="succeeded") == 1
    assert metrics.latencies()["faq_index_build_duration"][0]["count"] == 1


def test_llm_latency_is_recorded_and_failures_reuse_the_existing_counters(
    make_app: Callable[..., FastAPI],
) -> None:
    llm = service()
    generate(llm)
    (row,) = metrics.latencies()["llm_call_duration"]
    assert row["count"] == 1
    app = make_app()
    with TestClient(app) as client:
        body = client.get(MONITORING, headers=as_admin()).json()
    # The endpoint reports LLMService.error_counts() as it is: no second failure counter.
    assert body["llm"]["error_counts"] == app.state.llm_service.error_counts()
    assert "llm_errors" not in body["counters"]


def test_a_database_error_is_counted_but_an_application_exception_is_not() -> None:
    database = Database("sqlite://")
    database.init_schema()
    with pytest.raises(OperationalError), database.write() as session:
        session.execute(text("SELECT * FROM no_such_table"))
    with pytest.raises(LookupError), database.read():
        raise LookupError("not found")  # a business outcome, not a database failure

    assert metrics.counter("db_transaction_errors", kind="write") == 1
    assert metrics.counter("db_transaction_errors", kind="read") == 0
    kinds = {
        row["labels"]["kind"]: row["count"]
        for row in metrics.latencies()["db_transaction_duration"]
    }
    assert kinds == {"write": 1, "read": 1}


def test_random_paths_and_methods_cannot_create_series(make_app: Callable[..., FastAPI]) -> None:
    with TestClient(make_app()) as client:
        for i in range(50):
            client.get(f"/api/v1/conversations/{uuid.uuid4()}/messages")  # 401, a real route
            client.get(f"/scan/{i}/{uuid.uuid4()}")  # unmatched
            client.request(f"WEIRD{i}", "/api/v1/faqs")
        body = client.get(MONITORING, headers=as_admin()).json()

    labels = [row["labels"] for row in body["counters"]["http_requests"]]
    assert len(labels) <= 6
    assert {label["method"] for label in labels} <= {"GET", OTHER}
    assert "/scan" not in str(body) and "conversations/" + "{session_id}" in str(body)


# --- The log snapshot -----------------------------------------------------------------------


def test_the_log_snapshot_carries_the_headline_figures(
    setup_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    app = setup_app
    with TestClient(app) as client:
        client.get("/api/v1/faqs/faq_999")
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="app.services.monitoring_service"):
            log_snapshot(app.state)

    # Ours, then the final one written at shutdown.
    records = [r for r in caplog.records if r.getMessage().startswith("event=metrics_snapshot")]
    assert len(records) == 2
    fields = records[0].log_fields  # type: ignore[attr-defined]
    assert fields["event"] == "metrics_snapshot"
    assert (fields["requests_total"], fields["client_errors"]) == (1, 1)
    assert fields["errors_by_category"] == {"NOT_FOUND": 1}
    assert fields["ticket_failures"] == 0


def test_snapshots_are_logged_periodically_until_cancelled(
    setup_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    app = setup_app

    async def run() -> None:
        task = asyncio.create_task(log_snapshots_every(app.state, 0.01))
        await asyncio.sleep(0.08)
        task.cancel()

    with caplog.at_level(logging.INFO, logger="app.services.monitoring_service"):
        asyncio.run(run())
    snapshots = [r for r in caplog.records if r.getMessage().startswith("event=metrics_snapshot")]
    assert len(snapshots) >= 2


@pytest.mark.parametrize(
    ("setup_app", "expected"),
    [({"metrics_log_interval_seconds": 300}, 1), ({"metrics_log_interval_seconds": 0}, 0)],
    indirect=["setup_app"],
)
def test_shutdown_writes_a_final_snapshot_unless_disabled(
    setup_app: FastAPI, caplog: pytest.LogCaptureFixture, expected: int
) -> None:
    with caplog.at_level(logging.INFO, logger="app.services.monitoring_service"):
        with TestClient(setup_app):
            pass
    snapshots = [r for r in caplog.records if r.getMessage().startswith("event=metrics_snapshot")]
    assert len(snapshots) == expected


def test_the_interval_setting_is_bounded() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, metrics_log_interval_seconds=-1)
    assert Settings(_env_file=None).metrics_log_interval_seconds == 300
