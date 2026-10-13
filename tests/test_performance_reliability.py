"""Performance and reliability safeguards for semantic and hybrid search.

- The index prepares unit-length rows once, so a search does no per-request work
  proportional to (FAQs x dimensions) beyond one matrix-vector product.
- A failed model load starts a cooldown: requests fail fast (503 + Retry-After)
  instead of each repeating a slow failing load.
"""

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.database.faq_repository import FAQRepository
from app.main import create_app
from app.services.embedding_service import (
    EmbeddingError,
    EmbeddingModelLoadError,
    EmbeddingService,
)
from app.services.faq_service import FAQService
from app.services.search_pipeline import build_search_services
from app.services.semantic_search_service import IndexStatus, SemanticSearchService
from tests.fakes import FAKE_MODEL_NAME, CountingLoader, FakeEncoder
from tests.faq_test_data import ACTIVE_IDS

URL = "/api/v1/semantic-search"


class FakeClock:
    """Manually advanced monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FailingLoader:
    """Fails a set number of times (optionally slowly), then succeeds."""

    def __init__(self, failures: int = 10**6, delay: float = 0.0) -> None:
        self.failures = failures
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, model_name: str) -> FakeEncoder:
        with self._lock:
            self.calls += 1
            attempt = self.calls
        if self.delay:
            threading.Event().wait(self.delay)
        if attempt <= self.failures:
            raise OSError("model download failed")
        return FakeEncoder()


def semantic_for(faq_service: FAQService, embeddings: EmbeddingService) -> SemanticSearchService:
    return SemanticSearchService(faq_service.list_active_faqs, embeddings)


# --- Index: prepared once, reused by every search -----------------------------------------


def test_index_stores_unit_length_rows(faq_service: FAQService) -> None:
    embeddings = EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader(), normalize=False)
    index = semantic_for(faq_service, embeddings).get_index()

    assert index.unit_embeddings.shape == index.embeddings.shape == (len(ACTIVE_IDS), 16)
    assert index.unit_embeddings.dtype == np.float64
    np.testing.assert_allclose(np.linalg.norm(index.unit_embeddings, axis=1), 1.0, atol=1e-12)
    assert not index.unit_embeddings.flags.writeable


def test_empty_index_has_empty_unit_matrix() -> None:
    semantic = SemanticSearchService(lambda: [], EmbeddingService(FAKE_MODEL_NAME))

    assert semantic.get_index().unit_embeddings.shape == (0, 0)


def test_search_does_not_redo_per_faq_work(
    faq_service: FAQService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the build, a search computes norms for the query only, never for all FAQ rows."""
    semantic = semantic_for(faq_service, EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader()))
    semantic.get_index()
    rows_normed: list[int] = []
    real_norm = np.linalg.norm

    def recording_norm(x: Any, *args: Any, **kwargs: Any) -> Any:
        array = np.asarray(x)
        rows_normed.append(array.shape[0] if array.ndim == 2 else 1)
        return real_norm(x, *args, **kwargs)

    monkeypatch.setattr(np.linalg, "norm", recording_norm)
    for _ in range(5):
        semantic.search("where is my order")

    assert rows_normed  # the query is still normalised
    assert max(rows_normed) == 1  # never a norm over all FAQ rows


def test_index_is_built_once_across_many_searches(faq_service: FAQService) -> None:
    encoder = FakeEncoder()
    semantic = semantic_for(
        faq_service, EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader(encoder))
    )

    for query in ("order", "refund", "password", "delivery", "card"):
        semantic.search(query)

    batch_calls = [call for call, _ in encoder.encode_calls if len(call) > 1]
    assert len(batch_calls) == 1  # one batch for the whole index, then one text per search
    assert len(encoder.encode_calls) == 6


@pytest.mark.parametrize("bad", [np.nan, np.inf, 0.0])
def test_unusable_embeddings_are_rejected_at_build_time(
    faq_service: FAQService, bad: float
) -> None:
    class BrokenRow(FakeEncoder):
        def encode(self, sentences: list[str], **kwargs: Any) -> np.ndarray:
            vectors = super().encode(sentences, **kwargs)
            if len(sentences) > 1:
                vectors[2] = bad
            return vectors

    semantic = semantic_for(
        faq_service,
        EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader(BrokenRow()), normalize=False),
    )

    with pytest.raises(EmbeddingError, match="indexed FAQ embedding is zero or not finite"):
        semantic.build_index()
    assert semantic.status is IndexStatus.NOT_BUILT  # nothing half-built is cached


# --- Model load failures: cooldown ------------------------------------------------------------


def test_failed_load_starts_a_cooldown() -> None:
    clock, loader = FakeClock(), FailingLoader(failures=1)
    service = EmbeddingService(FAKE_MODEL_NAME, loader=loader, load_retry_seconds=30, clock=clock)

    with pytest.raises(EmbeddingModelLoadError) as first:
        service.encode_text("hello")
    assert first.value.retry_after_seconds == 30
    assert loader.calls == 1

    clock.advance(10)
    with pytest.raises(EmbeddingModelLoadError, match="failed to load recently") as second:
        service.encode_text("hello")
    assert second.value.retry_after_seconds == pytest.approx(20)
    assert loader.calls == 1  # failed fast, no new load attempt

    clock.advance(20)
    assert service.encode_text("hello").shape == (16,)  # cooldown over: retried and loaded
    assert loader.calls == 2


def test_successful_load_clears_the_cooldown() -> None:
    clock, loader = FakeClock(), FailingLoader(failures=1)
    service = EmbeddingService(FAKE_MODEL_NAME, loader=loader, load_retry_seconds=5, clock=clock)

    with pytest.raises(EmbeddingModelLoadError):
        service.load()
    clock.advance(5)
    service.load()

    assert service.is_loaded
    assert service._failed_at is None


def test_zero_cooldown_retries_immediately() -> None:
    loader = FailingLoader(failures=2)
    service = EmbeddingService(FAKE_MODEL_NAME, loader=loader, load_retry_seconds=0)

    for _ in range(2):
        with pytest.raises(EmbeddingModelLoadError) as exc:
            service.load()
        assert exc.value.retry_after_seconds is None
    service.load()

    assert loader.calls == 3


def test_negative_cooldown_is_rejected() -> None:
    with pytest.raises(ValueError, match="load_retry_seconds"):
        EmbeddingService(FAKE_MODEL_NAME, load_retry_seconds=-1)


@pytest.mark.parametrize(("cooldown", "expected_attempts"), [(30, 1), (0, 5)])
def test_concurrent_requests_after_a_slow_failure(cooldown: float, expected_attempts: int) -> None:
    """5 queued requests vs. a load that takes 0.2 s and then fails.

    With a cooldown, only the first request pays for the failing load; the rest fail
    fast. Without one (0), every queued request repeats it, one after another.
    """
    loader = FailingLoader(delay=0.2)
    service = EmbeddingService(FAKE_MODEL_NAME, loader=loader, load_retry_seconds=cooldown)
    barrier = threading.Barrier(5)

    def request() -> None:
        barrier.wait()
        try:
            service.encode_text("hello")
        except EmbeddingModelLoadError:
            pass

    started = time.perf_counter()
    threads = [threading.Thread(target=request) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - started

    assert loader.calls == expected_attempts
    if cooldown:
        assert elapsed < 0.6  # one slow failure, then fast failures
    else:
        assert elapsed >= 0.9  # five slow failures in sequence


# --- API --------------------------------------------------------------------------------------


@pytest.fixture
def make_client(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> Iterator[Callable[..., tuple[TestClient, FailingLoader]]]:
    clients: list[TestClient] = []

    def _make(cooldown: float, failures: int = 10**6) -> tuple[TestClient, FailingLoader]:
        loader = FailingLoader(failures=failures)
        embeddings = EmbeddingService(FAKE_MODEL_NAME, loader=loader, load_retry_seconds=cooldown)
        app = create_app(
            make_settings(),
            faq_service=faq_service,
            semantic_search_service=semantic_for(faq_service, embeddings),
        )
        client = TestClient(app)
        clients.append(client)
        return client, loader

    yield _make
    for client in clients:
        client.close()


def test_api_503_includes_retry_after_during_cooldown(make_client: Callable) -> None:
    client, loader = make_client(cooldown=30)

    responses = [client.post(URL, json={"query": "order"}) for _ in range(3)]

    assert [r.status_code for r in responses] == [503, 503, 503]
    assert responses[0].headers["Retry-After"] == "30"
    assert 1 <= int(responses[2].headers["Retry-After"]) <= 30
    assert loader.calls == 1  # the model load was attempted once, not three times


def test_api_503_has_no_retry_after_without_cooldown(make_client: Callable) -> None:
    client, _ = make_client(cooldown=0)

    response = client.post(URL, json={"query": "order"})

    assert response.status_code == 503
    assert "Retry-After" not in response.headers


def test_api_model_is_loaded_once_for_many_requests(make_client: Callable) -> None:
    client, loader = make_client(cooldown=30, failures=0)

    for query in ("order", "refund", "password", "delivery", "card", "return"):
        assert client.post(URL, json={"query": query}).status_code == 200

    assert loader.calls == 1


# --- Hybrid search: shared data, model and index; no blocking; failures --------------------------

HYBRID_URL = "/api/v1/hybrid-search"


def test_faq_file_is_loaded_once_for_every_search_kind(
    make_settings: Callable[..., Settings], faq_file: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    loads: list[Any] = []
    real = FAQRepository.from_json_file.__func__  # type: ignore[attr-defined]

    def counting(cls: type[FAQRepository], path: Any) -> FAQRepository:
        loads.append(path)
        return real(cls, path)

    monkeypatch.setattr(FAQRepository, "from_json_file", classmethod(counting))
    settings = make_settings(faq_data_path=faq_file)
    app = create_app(settings)
    faqs = app.state.faq_service.list_active_faqs
    embeddings = EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader())
    app.state.semantic_search_service = SemanticSearchService(faqs, embeddings)
    app.state.answer_decision_service = build_search_services(
        settings, faqs, app.state.semantic_search_service
    ).decision

    with TestClient(app) as client:
        for _ in range(3):
            assert client.post(HYBRID_URL, json={"query": "order"}).status_code == 200
            assert client.post(URL, json={"query": "order"}).status_code == 200
            assert client.get("/api/v1/faqs/search", params={"q": "order"}).status_code == 200

    assert len(loads) == 1


def test_hybrid_requests_load_the_model_and_build_the_index_once(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    encoder = FakeEncoder()
    loader = CountingLoader(encoder)
    semantic = semantic_for(faq_service, EmbeddingService(FAKE_MODEL_NAME, loader=loader))
    app = create_app(make_settings(), faq_service=faq_service, semantic_search_service=semantic)
    queries = ["order", "refund", "password", "delivery", "card", "return"]

    with TestClient(app) as client:
        for query in queries:
            assert client.post(HYBRID_URL, json={"query": query}).status_code == 200

    batch_sizes = [len(sentences) for sentences, _ in encoder.encode_calls]
    assert loader.calls == [FAKE_MODEL_NAME]
    assert batch_sizes == [len(ACTIVE_IDS)] + [1] * len(queries)  # index once, then queries


def test_concurrent_first_hybrid_requests_build_the_index_once(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    encoder = FakeEncoder(delay=0.05)
    loader = CountingLoader(encoder, delay=0.2)  # slow load: requests pile up behind it
    semantic = semantic_for(faq_service, EmbeddingService(FAKE_MODEL_NAME, loader=loader))
    app = create_app(make_settings(), faq_service=faq_service, semantic_search_service=semantic)
    statuses: list[int] = []

    with TestClient(app) as client:

        def ask() -> None:
            statuses.append(client.post(HYBRID_URL, json={"query": "order"}).status_code)

        threads = [threading.Thread(target=ask) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    index_builds = [s for s, _ in encoder.encode_calls if len(s) == len(ACTIVE_IDS)]
    assert statuses == [200] * 8
    assert loader.calls == [FAKE_MODEL_NAME]
    assert len(index_builds) == 1


def test_hybrid_search_runs_off_the_event_loop(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    """While one hybrid search is busy for ~1.5 s, /health must still answer quickly."""
    encoder = FakeEncoder(delay=0.75)
    semantic = semantic_for(
        faq_service, EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader(encoder))
    )
    app = create_app(make_settings(), faq_service=faq_service, semantic_search_service=semantic)

    with TestClient(app) as client:
        search = threading.Thread(
            target=client.post, args=(HYBRID_URL,), kwargs={"json": {"query": "order"}}
        )
        search.start()
        time.sleep(0.2)  # let the search start and occupy its worker thread
        started = time.perf_counter()
        health = client.get("/api/v1/health")
        elapsed = time.perf_counter() - started
        still_running = search.is_alive()
        search.join()

    assert health.status_code == 200
    assert still_running, "test is invalid unless /health ran during the slow search"
    assert elapsed < 0.5, f"/health waited {elapsed:.2f}s: the event loop was blocked"


def test_hybrid_model_failure_is_degraded_and_respects_the_cooldown(
    make_client: Callable,
) -> None:
    client, loader = make_client(cooldown=30)

    responses = [client.post(HYBRID_URL, json={"query": "order"}) for _ in range(3)]

    assert [r.status_code for r in responses] == [200, 200, 200]
    assert {r.json()["retrieval_mode"] for r in responses} == {"keyword_only"}
    assert {r.json()["decision"] for r in responses} == {"retrieval_unavailable"}
    assert all(1 <= int(r.headers["Retry-After"]) <= 30 for r in responses)
    assert loader.calls == 1  # one load attempt, not one per request


def test_hybrid_recovers_after_the_cooldown(make_client: Callable) -> None:
    client, loader = make_client(cooldown=0, failures=1)

    first = client.post(HYBRID_URL, json={"query": "order"}).json()
    second = client.post(HYBRID_URL, json={"query": "order"}).json()

    assert (first["retrieval_mode"], second["retrieval_mode"]) == ("keyword_only", "hybrid")
    assert loader.calls == 2


# --- Setting ------------------------------------------------------------------------------------


def test_retry_setting_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(_env_file=None).embedding_load_retry_seconds == 30
    monkeypatch.setenv("EMBEDDING_LOAD_RETRY_SECONDS", "5")
    settings = Settings(_env_file=None)

    assert settings.embedding_load_retry_seconds == 5
    assert settings.safe_summary()["embedding_load_retry_seconds"] == 5


@pytest.mark.parametrize("value", ["-1", "3601", "nan", "soon"])
def test_invalid_retry_setting_fails_fast(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("EMBEDDING_LOAD_RETRY_SECONDS", value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
