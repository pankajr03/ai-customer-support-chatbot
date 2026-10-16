"""The in-memory FAQ embedding index: building, lifecycle, concurrency and app wiring.

All tests use the fake encoder from tests/fakes.py: no model download, no PyTorch.
"""

import logging
import threading
from collections.abc import Callable

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import SemanticSearchServiceDep
from app.core.config import Settings
from app.database.faq_repository import FAQRepository
from app.main import create_app
from app.models.faq import FAQ
from app.services.embedding_service import EmbeddingModelLoadError, EmbeddingService
from app.services.faq_service import FAQService
from app.services.semantic_search_service import IndexStatus, SemanticSearchService
from tests.fakes import FAKE_DIMENSION, FAKE_MODEL_NAME, CountingLoader, FakeEncoder
from tests.faq_test_data import ACTIVE_IDS, INACTIVE_IDS, TEST_FAQ_RECORDS


@pytest.fixture
def encoder() -> FakeEncoder:
    return FakeEncoder()


@pytest.fixture
def loader(encoder: FakeEncoder) -> CountingLoader:
    return CountingLoader(encoder)


@pytest.fixture
def semantic(faq_service: FAQService, loader: CountingLoader) -> SemanticSearchService:
    embeddings = EmbeddingService(FAKE_MODEL_NAME, loader=loader)
    return SemanticSearchService(faq_service.list_active_faqs, embeddings)


def make_semantic(faqs: list[FAQ], encoder: FakeEncoder | None = None) -> SemanticSearchService:
    embeddings = EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader(encoder or FakeEncoder()))
    return SemanticSearchService(lambda: faqs, embeddings)


# --- Building the index ------------------------------------------------------------------


def test_index_contains_active_faqs_in_knowledge_base_order(
    semantic: SemanticSearchService,
) -> None:
    index = semantic.build_index()

    assert index.faq_ids == tuple(ACTIVE_IDS)
    assert len(index) == len(ACTIVE_IDS)
    assert index.embeddings.shape == (len(ACTIVE_IDS), FAKE_DIMENSION)
    assert index.dimension == FAKE_DIMENSION
    assert index.model_name == FAKE_MODEL_NAME


def test_inactive_faqs_are_not_indexed(semantic: SemanticSearchService) -> None:
    index = semantic.build_index()

    for inactive_id in INACTIVE_IDS:
        assert index.get_faq(inactive_id) is None
        assert index.get_embedding(inactive_id) is None


def test_inactive_faqs_are_excluded_even_if_the_provider_returns_them(
    faq_repository: FAQRepository,
) -> None:
    semantic = make_semantic(faq_repository.list_all())  # includes inactive FAQs

    assert semantic.build_index().faq_ids == tuple(ACTIVE_IDS)


def test_questions_are_embedded_in_one_batch(
    semantic: SemanticSearchService, encoder: FakeEncoder
) -> None:
    semantic.build_index()

    assert len(encoder.encode_calls) == 1
    sentences, kwargs = encoder.encode_calls[0]
    questions = {r["id"]: r["question"] for r in TEST_FAQ_RECORDS}
    assert sentences == [questions[faq_id] for faq_id in ACTIVE_IDS]
    assert kwargs["normalize_embeddings"] is True


def test_each_row_is_the_embedding_of_its_faq_question(
    semantic: SemanticSearchService, faq_service: FAQService, loader: CountingLoader
) -> None:
    index = semantic.build_index()
    reference = EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader(FakeEncoder()))

    for faq in faq_service.list_active_faqs():
        embedding = index.get_embedding(faq.id)
        assert embedding is not None
        np.testing.assert_allclose(embedding, reference.encode_text(faq.question), rtol=1e-6)


def test_faq_metadata_is_accessible_by_id(semantic: SemanticSearchService) -> None:
    index = semantic.build_index()

    faq = index.get_faq(" FAQ_101 ")

    assert faq is not None
    assert faq.question == TEST_FAQ_RECORDS[0]["question"]
    assert faq.category.value == TEST_FAQ_RECORDS[0]["category"]
    assert index.get_faq("faq_999") is None


def test_build_is_deterministic(faq_service: FAQService) -> None:
    first = make_semantic(faq_service.list_active_faqs()).build_index()
    second = make_semantic(faq_service.list_active_faqs()).build_index()

    assert first.faq_ids == second.faq_ids
    np.testing.assert_array_equal(first.embeddings, second.embeddings)


@pytest.mark.parametrize("faqs", [[], "all inactive"], ids=["empty", "all-inactive"])
def test_empty_dataset_builds_empty_index_without_loading_model(
    faq_repository: FAQRepository, faqs: list[FAQ] | str
) -> None:
    if faqs == "all inactive":
        faqs = [faq for faq in faq_repository.list_all() if not faq.is_active]
    loader = CountingLoader()
    semantic = SemanticSearchService(lambda: faqs, EmbeddingService(FAKE_MODEL_NAME, loader=loader))

    index = semantic.build_index()

    assert len(index) == 0
    assert index.embeddings.shape == (0, 0)
    assert semantic.is_ready
    assert loader.calls == []  # no model load, no download


def test_duplicate_ids_from_provider_are_rejected(faq_repository: FAQRepository) -> None:
    faq = faq_repository.list_active()[0]

    with pytest.raises(ValueError, match="duplicate FAQ ids: faq_101"):
        make_semantic([faq, faq]).build_index()


def test_build_is_logged_without_faq_text(
    semantic: SemanticSearchService, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.services.semantic_search_service"):
        semantic.build_index()

    assert f"Built semantic FAQ index: {len(ACTIVE_IDS)} FAQs, dimension 16" in caplog.text
    assert TEST_FAQ_RECORDS[0]["question"] not in caplog.text


# --- Lifecycle and reuse -------------------------------------------------------------------


def test_nothing_is_built_or_loaded_on_construction(
    semantic: SemanticSearchService, loader: CountingLoader
) -> None:
    assert semantic.status is IndexStatus.NOT_BUILT
    assert semantic.is_ready is False
    assert loader.calls == []


def test_get_index_builds_lazily_once_and_reuses(
    semantic: SemanticSearchService, encoder: FakeEncoder, loader: CountingLoader
) -> None:
    first = semantic.get_index()
    second = semantic.get_index()
    third = semantic.build_index()  # explicit build after lazy build is a no-op

    assert first is second is third
    assert semantic.status is IndexStatus.READY
    assert len(encoder.encode_calls) == 1
    assert loader.calls == [FAKE_MODEL_NAME]


def test_explicit_build_then_get_does_not_rebuild(
    semantic: SemanticSearchService, encoder: FakeEncoder
) -> None:
    built = semantic.build_index()

    assert semantic.get_index() is built
    assert len(encoder.encode_calls) == 1


def test_rebuild_creates_a_new_snapshot_and_keeps_the_old_one_intact() -> None:
    faqs: list[FAQ] = [FAQ.model_validate(TEST_FAQ_RECORDS[0])]
    semantic = make_semantic(faqs)
    old = semantic.get_index()

    faqs.append(FAQ.model_validate(TEST_FAQ_RECORDS[1]))
    new = semantic.rebuild_index()

    assert new is not old
    assert semantic.get_index() is new
    assert old.faq_ids == ("faq_101",)  # readers holding the old index are unaffected
    assert new.faq_ids == ("faq_101", "faq_102")


def test_reset_drops_the_index_and_next_use_rebuilds(
    semantic: SemanticSearchService, encoder: FakeEncoder
) -> None:
    semantic.get_index()
    semantic.reset()

    assert semantic.status is IndexStatus.NOT_BUILT
    semantic.get_index()
    assert len(encoder.encode_calls) == 2


def test_failed_build_is_not_cached_and_is_retried(faq_service: FAQService) -> None:
    attempts: list[str] = []

    def flaky_loader(model_name: str) -> FakeEncoder:
        attempts.append(model_name)
        if len(attempts) == 1:
            raise ConnectionError("model download failed")
        return FakeEncoder()

    semantic = SemanticSearchService(
        faq_service.list_active_faqs, EmbeddingService(FAKE_MODEL_NAME, loader=flaky_loader)
    )

    with pytest.raises(EmbeddingModelLoadError):
        semantic.get_index()
    assert semantic.status is IndexStatus.NOT_BUILT

    assert len(semantic.get_index()) == len(ACTIVE_IDS)
    assert len(attempts) == 2


# --- Shared state safety ----------------------------------------------------------------------


def test_index_data_is_read_only(semantic: SemanticSearchService) -> None:
    index = semantic.build_index()

    with pytest.raises(ValueError, match="read-only"):
        index.embeddings[0, 0] = 99.0
    with pytest.raises(TypeError):
        index.positions["faq_999"] = 0  # type: ignore[index]
    with pytest.raises(AttributeError):
        index.faqs = ()  # type: ignore[misc]
    assert isinstance(index.faqs, tuple)


def test_concurrent_first_use_builds_the_index_once(faq_service: FAQService) -> None:
    encoder = FakeEncoder(delay=0.2)  # slow embedding widens the race window
    loader = CountingLoader(encoder)
    semantic = SemanticSearchService(
        faq_service.list_active_faqs, EmbeddingService(FAKE_MODEL_NAME, loader=loader)
    )
    results = []
    barrier = threading.Barrier(8)

    def use_index() -> None:
        barrier.wait()  # start all threads at the same moment
        results.append(semantic.get_index())

    threads = [threading.Thread(target=use_index) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(encoder.encode_calls) == 1
    assert loader.calls == [FAKE_MODEL_NAME]
    assert len(results) == 8
    assert all(index is results[0] for index in results)


def test_readers_during_rebuild_always_see_a_complete_index(faq_service: FAQService) -> None:
    semantic = SemanticSearchService(
        faq_service.list_active_faqs,
        EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader(FakeEncoder(delay=0.05))),
    )
    semantic.build_index()
    problems: list[str] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            index = semantic.get_index()
            if index.embeddings.shape[0] != len(index.faqs) or len(index) != len(ACTIVE_IDS):
                problems.append("inconsistent index")

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for thread in readers:
        thread.start()
    for _ in range(3):
        semantic.rebuild_index()
    stop.set()
    for thread in readers:
        thread.join()

    assert problems == []


# --- Application wiring ---------------------------------------------------------------------


def test_app_creates_one_lazy_semantic_service(app: FastAPI) -> None:
    service = app.state.semantic_search_service

    assert isinstance(service, SemanticSearchService)
    assert service.status is IndexStatus.NOT_BUILT


def test_app_semantic_service_indexes_the_apps_faqs(
    faq_service: FAQService, make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[tuple[str, dict]] = []

    def fake_embedding_service(model_name: str, **kwargs: object) -> EmbeddingService:
        created.append((model_name, kwargs))
        return EmbeddingService(model_name, loader=CountingLoader(), **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("app.main.EmbeddingService", fake_embedding_service)
    settings = make_settings(embedding_model_name="my/model", embedding_load_retry_seconds=12)
    app = create_app(settings, faq_service=faq_service)

    assert created == [("my/model", {"load_retry_seconds": 12.0})]
    assert app.state.semantic_search_service.get_index().faq_ids == tuple(ACTIVE_IDS)


def test_dependency_returns_the_app_service(
    make_settings: Callable[..., Settings], semantic: SemanticSearchService
) -> None:
    app = create_app(make_settings(), semantic_search_service=semantic)

    @app.get("/probe")
    def probe(service: SemanticSearchServiceDep) -> dict[str, bool]:
        return {"same": service is semantic}

    assert TestClient(app).get("/probe").json() == {"same": True}


def test_missing_semantic_service_fails_loudly(app: FastAPI) -> None:
    del app.state.semantic_search_service

    @app.get("/probe")
    def probe(service: SemanticSearchServiceDep) -> dict[str, str]:
        return {}

    assert TestClient(app).get("/probe").status_code == 500


def test_preload_is_off_by_default(make_settings: Callable[..., Settings]) -> None:
    semantic = make_semantic([FAQ.model_validate(TEST_FAQ_RECORDS[0])])

    with TestClient(create_app(make_settings(), semantic_search_service=semantic)):
        assert semantic.is_ready is False


def test_preload_builds_index_at_startup_in_a_worker_thread(
    make_settings: Callable[..., Settings],
) -> None:
    semantic = make_semantic([FAQ.model_validate(TEST_FAQ_RECORDS[0])])
    build_threads: list[str] = []
    original_build = semantic.build_index

    def recording_build() -> object:
        build_threads.append(threading.current_thread().name)
        return original_build()

    semantic.build_index = recording_build  # type: ignore[method-assign]
    app = create_app(make_settings(semantic_index_preload=True), semantic_search_service=semantic)

    with TestClient(app) as client:
        assert semantic.is_ready is True
        assert client.get("/api/v1/health").status_code == 200

    assert len(build_threads) == 1
    assert build_threads[0] != threading.current_thread().name
    assert build_threads[0].startswith("AnyIO worker thread")


def test_preload_failure_is_logged_and_app_still_starts(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    def failing_loader(model_name: str) -> FakeEncoder:
        raise OSError("no network")

    semantic = SemanticSearchService(
        faq_service.list_active_faqs, EmbeddingService(FAKE_MODEL_NAME, loader=failing_loader)
    )
    app = create_app(make_settings(semantic_index_preload=True), semantic_search_service=semantic)

    with TestClient(app) as client:
        assert client.get("/api/v1/faqs").status_code == 200
        assert semantic.status is IndexStatus.NOT_BUILT


def test_background_preload_serves_at_once_and_loads_the_model_once(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    release = threading.Event()
    loads: list[str] = []

    def slow_loader(model_name: str) -> FakeEncoder:
        loads.append(threading.current_thread().name)
        release.wait(10)  # a slow model load
        return FakeEncoder()

    semantic = SemanticSearchService(
        faq_service.list_active_faqs, EmbeddingService(FAKE_MODEL_NAME, loader=slow_loader)
    )
    app = create_app(
        make_settings(semantic_index_preload="background"), semantic_search_service=semantic
    )
    with TestClient(app) as client:
        # Serving while the model is still loading.
        assert client.get("/api/v1/ready").json()["checks"]["semantic_search"] == "not_loaded"
        assert client.get("/api/v1/faqs").status_code == 200
        release.set()
        # A search waits for the same load instead of starting another.
        results = client.post("/api/v1/semantic-search", json={"query": "track my order"})
        assert results.status_code == 200
        assert client.get("/api/v1/ready").json()["checks"]["semantic_search"] == "ready"
    assert loads == ["semantic-index-preload"]


def test_background_preload_failure_is_retried_on_first_use(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    attempts: list[str] = []
    failed = threading.Event()

    def flaky_loader(model_name: str) -> FakeEncoder:
        attempts.append(threading.current_thread().name)
        if len(attempts) == 1:
            failed.set()
            raise OSError("no network")
        return FakeEncoder()

    semantic = SemanticSearchService(
        faq_service.list_active_faqs, EmbeddingService(FAKE_MODEL_NAME, loader=flaky_loader)
    )
    app = create_app(
        make_settings(semantic_index_preload="background"), semantic_search_service=semantic
    )
    with TestClient(app) as client:
        assert failed.wait(5)  # the background attempt failed (and was logged)
        assert client.post("/api/v1/semantic-search", json={"query": "track"}).status_code == 200
    assert attempts[0] == "semantic-index-preload" and len(attempts) == 2


def test_the_preload_setting_accepts_only_known_values(
    make_settings: Callable[..., Settings],
) -> None:
    assert make_settings(semantic_index_preload="background").semantic_index_preload == "background"
    assert make_settings(semantic_index_preload="true").semantic_index_preload is True
    with pytest.raises(ValueError):
        make_settings(semantic_index_preload="later")
