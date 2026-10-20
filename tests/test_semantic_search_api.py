"""POST /api/v1/semantic-search: responses, validation, failures and non-blocking inference.

The app is built with the fixed test FAQs and a fake embedding model (tests/fakes.py),
so no model is downloaded and results are deterministic.
"""

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.errors import SEMANTIC_UNAVAILABLE_MESSAGE
from app.core.config import Settings
from app.main import create_app
from app.services.embedding_service import EmbeddingService
from app.services.faq_service import FAQService
from app.services.semantic_search_service import (
    DEFAULT_TOP_K,
    MAX_QUERY_LENGTH,
    MAX_TOP_K,
    SemanticSearchService,
)
from tests.fakes import FAKE_MODEL_NAME, CountingLoader, FakeEncoder
from tests.faq_test_data import ACTIVE_IDS, ACTIVE_SHIPPING_IDS, INACTIVE_IDS, TEST_FAQ_RECORDS

URL = "/api/v1/semantic-search"
RESULT_FIELDS = ["faq_id", "question", "answer", "category", "similarity_score"]


def build_app(
    make_settings: Callable[..., Settings],
    faq_service: FAQService,
    loader: Callable[[str], Any] | None = None,
) -> FastAPI:
    embeddings = EmbeddingService(FAKE_MODEL_NAME, loader=loader or CountingLoader())
    semantic = SemanticSearchService(faq_service.list_active_faqs, embeddings)
    return create_app(make_settings(), faq_service=faq_service, semantic_search_service=semantic)


@pytest.fixture
def semantic_app(make_settings: Callable[..., Settings], faq_service: FAQService) -> FastAPI:
    return build_app(make_settings, faq_service)


@pytest.fixture
def client(semantic_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(semantic_app) as test_client:
        yield test_client


# --- Successful searches ---------------------------------------------------------------------


def test_returns_ranked_results_with_scores(client: TestClient) -> None:
    response = client.post(URL, json={"query": "how do I reset my password", "top_k": 3})

    assert response.status_code == 200
    body = response.json()
    assert list(body) == ["query", "min_similarity", "results", "total_results"]
    assert body["total_results"] == len(body["results"]) == 3
    assert body["results"][0]["faq_id"] == "faq_107"
    assert all(list(result) == RESULT_FIELDS for result in body["results"])
    scores = [result["similarity_score"] for result in body["results"]]
    assert scores == sorted(scores, reverse=True)
    assert all(-1.0 <= score <= 1.0 for score in scores)


def test_only_query_is_required(client: TestClient) -> None:
    body = client.post(URL, json={"query": "order"}).json()

    assert body["total_results"] == DEFAULT_TOP_K


def test_spec_example_request_with_null_category(client: TestClient) -> None:
    payload = {"query": "Where is my package?", "top_k": 3, "category": None, "min_similarity": -1}

    assert client.post(URL, json=payload).json()["total_results"] == 3


def test_query_whitespace_is_normalised(client: TestClient) -> None:
    body = client.post(URL, json={"query": "  reset \n my   password "}).json()

    assert body["query"] == "reset my password"


def test_answers_are_the_stored_approved_answers(client: TestClient) -> None:
    stored = {record["id"]: record for record in TEST_FAQ_RECORDS}

    for result in client.post(URL, json={"query": "order", "top_k": MAX_TOP_K}).json()["results"]:
        record = stored[result["faq_id"]]
        assert (result["question"], result["answer"], result["category"]) == (
            record["question"],
            record["answer"],
            record["category"],
        )


def test_inactive_faqs_are_never_returned(client: TestClient) -> None:
    inactive_question = next(r["question"] for r in TEST_FAQ_RECORDS if r["id"] == "faq_105")

    results = client.post(URL, json={"query": inactive_question, "top_k": MAX_TOP_K}).json()

    returned = {result["faq_id"] for result in results["results"]}
    assert returned.isdisjoint(INACTIVE_IDS)
    assert returned == set(ACTIVE_IDS)


def test_category_filter(client: TestClient) -> None:
    body = client.post(
        URL, json={"query": "order", "top_k": MAX_TOP_K, "category": " Shipping "}
    ).json()

    assert sorted(r["faq_id"] for r in body["results"]) == sorted(ACTIVE_SHIPPING_IDS)


def test_min_similarity_filters_results(client: TestClient) -> None:
    everything = client.post(URL, json={"query": "reset my password", "top_k": MAX_TOP_K}).json()
    threshold = everything["results"][1]["similarity_score"]

    body = client.post(
        URL, json={"query": "reset my password", "top_k": MAX_TOP_K, "min_similarity": threshold}
    ).json()

    assert body["total_results"] >= 1
    assert all(r["similarity_score"] >= threshold for r in body["results"])


def test_no_match_returns_200_with_empty_results(client: TestClient) -> None:
    response = client.post(URL, json={"query": "xyzzy quantum", "min_similarity": 0.99})

    assert response.status_code == 200
    assert response.json() == {
        "query": "xyzzy quantum",
        "min_similarity": 0.99,
        "results": [],
        "total_results": 0,
    }


def test_scores_are_computed_not_fixed(client: TestClient) -> None:
    first = client.post(URL, json={"query": "reset my password", "top_k": 1}).json()
    second = client.post(URL, json={"query": "do you ship abroad", "top_k": 1}).json()

    assert first["results"][0]["similarity_score"] != second["results"][0]["similarity_score"]


# --- Validation (422) --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({}, "query"),
        ({"query": ""}, "query"),
        ({"query": "   "}, "query"),
        ({"query": "x" * (MAX_QUERY_LENGTH + 1)}, "query"),
        ({"query": 42}, "query"),
        ({"query": "q", "top_k": 0}, "top_k"),
        ({"query": "q", "top_k": MAX_TOP_K + 1}, "top_k"),
        ({"query": "q", "top_k": "3"}, "top_k"),
        ({"query": "q", "top_k": True}, "top_k"),
        ({"query": "q", "category": "billing"}, "category"),
        ({"query": "q", "min_similarity": 1.5}, "min_similarity"),
        ({"query": "q", "min_similarity": -2}, "min_similarity"),
        ({"query": "q", "min_similarity": "high"}, "min_similarity"),
        ({"query": "q", "min_score": 0.4}, "min_score"),
    ],
)
def test_invalid_request_body_is_422(client: TestClient, payload: dict, field: str) -> None:
    response = client.post(URL, json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"][:2] == ["body", field]


@pytest.mark.parametrize("content", ["not json", '{"query": "x",}', "[]", '"just a string"', ""])
def test_malformed_or_non_object_body_is_422(client: TestClient, content: str) -> None:
    response = client.post(URL, content=content, headers={"Content-Type": "application/json"})

    assert response.status_code == 422


def test_missing_body_is_422(client: TestClient) -> None:
    assert client.post(URL).status_code == 422


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_only_post_is_allowed(client: TestClient, method: str) -> None:
    assert client.request(method, URL).status_code == 405


def test_rejected_query_text_is_not_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    client.post(URL, json={"query": "jane.doe@example.com " * 60})

    assert "Rejected POST /api/v1/semantic-search: body.query (string_too_long)" in caplog.text
    assert "jane.doe" not in caplog.text


# --- Model and index failures (503 / 500) --------------------------------------------------------


def test_model_load_failure_is_503_without_internals(
    make_settings: Callable[..., Settings],
    faq_service: FAQService,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The app is created inside this test, so its log handler writes to captured stdout.
    def failing_loader(model_name: str) -> FakeEncoder:
        raise OSError(f"C:/Users/someone/.cache/huggingface/{model_name} not found")

    with TestClient(build_app(make_settings, faq_service, failing_loader)) as client:
        response = client.post(URL, json={"query": "where is my order"})

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "SERVICE_UNAVAILABLE",
            "message": SEMANTIC_UNAVAILABLE_MESSAGE,
            "request_id": response.headers["X-Request-ID"],
            "retryable": True,
        }
    }
    for leak in ("OSError", "huggingface", "C:/", FAKE_MODEL_NAME, "Traceback"):
        assert leak not in response.text
    log = capsys.readouterr().out
    assert "Semantic search unavailable on POST /api/v1/semantic-search" in log
    assert "EmbeddingModelLoadError" in log  # details stay in the server log


def test_service_recovers_after_a_failed_model_load(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    attempts: list[str] = []

    def flaky_loader(model_name: str) -> FakeEncoder:
        attempts.append(model_name)
        if len(attempts) == 1:
            raise ConnectionError("temporarily offline")
        return FakeEncoder()

    with TestClient(build_app(make_settings, faq_service, flaky_loader)) as client:
        assert client.post(URL, json={"query": "order"}).status_code == 503
        assert client.post(URL, json={"query": "order"}).status_code == 200


def test_unusable_query_embedding_is_503(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    class ZeroQuery(FakeEncoder):
        def encode(self, sentences: list[str], **kwargs: Any) -> np.ndarray:
            if sentences == ["broken"]:
                return np.zeros((1, self.dimension))
            return super().encode(sentences, **kwargs)

    app = build_app(make_settings, faq_service, CountingLoader(ZeroQuery()))
    with TestClient(app) as client:
        assert client.post(URL, json={"query": "broken"}).status_code == 503


def test_unexpected_error_is_500_not_success(semantic_app: FastAPI) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("internal detail /srv/app/secret.py")

    semantic_app.state.semantic_search_service.search = explode

    with TestClient(semantic_app) as client:
        response = client.post(URL, json={"query": "order"})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret.py" not in response.text


# --- Non-blocking inference --------------------------------------------------------------------


def test_search_runs_in_a_worker_thread(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    threads: list[str] = []

    class RecordingEncoder(FakeEncoder):
        def encode(self, sentences: list[str], **kwargs: Any) -> np.ndarray:
            threads.append(threading.current_thread().name)
            return super().encode(sentences, **kwargs)

    app = build_app(make_settings, faq_service, CountingLoader(RecordingEncoder()))
    with TestClient(app) as client:
        client.post(URL, json={"query": "order"})

    assert threads  # index build + query embedding
    assert all(name.startswith("AnyIO worker thread") for name in threads)


def test_slow_inference_does_not_block_other_requests(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    """While one semantic search is busy for ~1.5 s, /health must still answer quickly."""
    app = build_app(make_settings, faq_service, CountingLoader(FakeEncoder(delay=0.75)))

    with TestClient(app) as client:
        search = threading.Thread(target=client.post, args=(URL,), kwargs={"json": {"query": "x"}})
        search.start()
        time.sleep(0.2)  # let the search start and occupy its worker thread
        started = time.perf_counter()
        health = client.get("/api/v1/health")
        elapsed = time.perf_counter() - started
        search_still_running = search.is_alive()
        search.join()

    assert health.status_code == 200
    assert search_still_running, "test is invalid unless /health ran during the slow search"
    assert elapsed < 0.5, f"/health waited {elapsed:.2f}s: the event loop was blocked"


# --- Documentation and coexistence -------------------------------------------------------------


def test_endpoint_is_documented_in_openapi(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"][URL]["post"]

    assert list(schema["paths"][URL]) == ["post"]
    assert operation["tags"] == ["semantic search"]
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert request_schema["$ref"].endswith("/SemanticSearchRequest")
    assert operation["requestBody"]["required"] is True
    ok = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok["$ref"].endswith("/SemanticSearchResponse")
    assert {"200", "422", "503"} <= operation["responses"].keys()
    assert "not a probability" in operation["description"]


def test_existing_endpoints_still_work(client: TestClient) -> None:
    assert client.get("/api/v1/health").json()["status"] == "healthy"
    assert client.get("/api/v1/faqs").status_code == 200
    assert client.get("/api/v1/faqs/search", params={"q": "order"}).status_code == 200
    assert client.get("/api/v1/faqs/categories").status_code == 200
    assert client.get("/api/v1/faqs/faq_101").status_code == 200


def test_chat_exists_only_inside_conversations(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    chat_paths = [  # staff analytics (/admin/analytics/chat) are not a chat endpoint
        path
        for path in paths
        if ("chat" in path or "conversation" in path) and not path.startswith("/api/v1/admin/")
    ]
    assert all(path.startswith("/api/v1/conversations") for path in chat_paths)
    assert client.post("/api/v1/chat", json={"message": "hi"}).status_code == 404
