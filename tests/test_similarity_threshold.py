"""Similarity threshold: configurable default, per-request override, no fallback results."""

import math
import sys
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.main import create_app
from app.services.embedding_service import EmbeddingService
from app.services.faq_service import FAQService
from app.services.semantic_search_service import MAX_TOP_K, SemanticSearchService
from tests.fakes import FAKE_MODEL_NAME, CountingLoader
from tests.faq_test_data import ACTIVE_IDS, TEST_FAQ_RECORDS

URL = "/api/v1/semantic-search"
PASSWORD_QUESTION = TEST_FAQ_RECORDS[6]["question"]  # "How do I reset my password?"


def make_semantic(faq_service: FAQService, default: float | None) -> SemanticSearchService:
    embeddings = EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader())
    return SemanticSearchService(
        faq_service.list_active_faqs, embeddings, default_min_score=default
    )


# --- Setting -----------------------------------------------------------------------------------


def test_default_threshold_is_the_suggested_experimental_value() -> None:
    settings = Settings(_env_file=None)

    assert settings.semantic_min_similarity == 0.40
    assert settings.safe_summary()["semantic_min_similarity"] == 0.40


@pytest.mark.parametrize(("value", "expected"), [("0.55", 0.55), ("-1", -1.0), ("1", 1.0)])
def test_threshold_is_configurable(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: float
) -> None:
    monkeypatch.setenv("SEMANTIC_MIN_SIMILARITY", value)

    assert Settings(_env_file=None).semantic_min_similarity == expected


@pytest.mark.parametrize("value", ["1.01", "-1.5", "nan", "inf", "high", ""])
def test_invalid_threshold_setting_fails_fast(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SEMANTIC_MIN_SIMILARITY", value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


# --- Service --------------------------------------------------------------------------------------


def test_no_default_means_no_threshold(faq_service: FAQService) -> None:
    semantic = make_semantic(faq_service, default=None)

    assert len(semantic.search("xyzzy order", top_k=MAX_TOP_K)) == len(ACTIVE_IDS)


def test_default_threshold_is_applied_when_none_is_given(faq_service: FAQService) -> None:
    semantic = make_semantic(faq_service, default=0.5)

    results = semantic.search("reset my password", top_k=MAX_TOP_K)

    assert results
    assert all(match.score >= 0.5 for match in results)
    assert len(results) < len(ACTIVE_IDS)


def test_explicit_threshold_overrides_the_default(faq_service: FAQService) -> None:
    semantic = make_semantic(faq_service, default=0.99)

    looser = semantic.search("reset my password", top_k=MAX_TOP_K, min_score=0.1)
    disabled = semantic.search("reset my password", top_k=MAX_TOP_K, min_score=-1.0)

    assert len(semantic.search("reset my password", top_k=MAX_TOP_K)) == 0
    assert len(looser) > 0
    assert len(disabled) == len(ACTIVE_IDS)


def test_threshold_is_inclusive(faq_service: FAQService) -> None:
    # The exact question scores 1.0, so a threshold of exactly 1.0 still keeps it.
    [match] = make_semantic(faq_service, default=1.0).search(PASSWORD_QUESTION)

    assert match.faq.id == "faq_107"
    assert match.score == pytest.approx(1.0)


def test_nothing_above_threshold_returns_empty_list_not_a_fallback(
    faq_service: FAQService,
) -> None:
    semantic = make_semantic(faq_service, default=0.9)

    best_available = semantic.search("xyzzy quantum order", top_k=1, min_score=-1.0)
    with_default = semantic.search("xyzzy quantum order", top_k=MAX_TOP_K)

    assert best_available and best_available[0].score < 0.9  # a "closest" FAQ does exist...
    assert with_default == []  # ...but it is not returned as a fallback


def test_resolve_min_score(faq_service: FAQService) -> None:
    semantic = make_semantic(faq_service, default=0.4)

    assert semantic.resolve_min_score(None) == 0.4
    assert semantic.resolve_min_score(0.7) == 0.7
    assert semantic.resolve_min_score(-1.0) == -1.0
    assert make_semantic(faq_service, default=None).resolve_min_score(None) is None


@pytest.mark.parametrize("default", [1.5, -1.01, math.nan, math.inf, "0.4", True])
def test_invalid_default_threshold_is_rejected(faq_service: FAQService, default: object) -> None:
    with pytest.raises(ValueError, match="min_score"):
        make_semantic(faq_service, default=default)  # type: ignore[arg-type]


# --- API ----------------------------------------------------------------------------------------


@pytest.fixture
def make_client(
    make_settings: Callable[..., Settings],
    faq_service: FAQService,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[float], TestClient]]:
    """Client for an app wired exactly like production, but with the fake model."""
    monkeypatch.setattr(
        "app.main.EmbeddingService",
        lambda name, **kwargs: EmbeddingService(name, loader=CountingLoader(), **kwargs),
    )
    clients: list[TestClient] = []

    def _make(threshold: float) -> TestClient:
        settings = make_settings(semantic_min_similarity=threshold)
        client = TestClient(create_app(settings, faq_service=faq_service))
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.close()


@pytest.mark.parametrize("payload", [{}, {"min_similarity": None}], ids=["omitted", "null"])
def test_api_applies_configured_default(
    make_client: Callable[[float], TestClient], payload: dict
) -> None:
    body = make_client(0.5).post(URL, json={"query": "reset my password", **payload}).json()

    assert body["min_similarity"] == 0.5
    assert body["results"]
    assert all(result["similarity_score"] >= 0.5 for result in body["results"])


def test_api_caller_can_override_within_limits(
    make_client: Callable[[float], TestClient],
) -> None:
    client = make_client(0.99)
    query = {"query": "reset my password", "top_k": MAX_TOP_K}

    strict = client.post(URL, json=query).json()
    loose = client.post(URL, json={**query, "min_similarity": 0.2}).json()
    disabled = client.post(URL, json={**query, "min_similarity": -1}).json()

    assert (strict["min_similarity"], loose["min_similarity"], disabled["min_similarity"]) == (
        0.99,
        0.2,
        -1.0,
    )
    assert strict["total_results"] < loose["total_results"] <= disabled["total_results"]
    assert disabled["total_results"] == len(ACTIVE_IDS)


@pytest.mark.parametrize("value", [1.01, -1.01, "0.4x"])
def test_api_rejects_override_outside_cosine_range(
    make_client: Callable[[float], TestClient], value: object
) -> None:
    response = make_client(0.4).post(URL, json={"query": "q", "min_similarity": value})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "min_similarity"]


def test_api_returns_empty_results_without_fallback_or_llm(
    make_client: Callable[[float], TestClient],
) -> None:
    response = make_client(0.9).post(URL, json={"query": "xyzzy quantum order"})

    assert response.status_code == 200
    assert response.json() == {
        "query": "xyzzy quantum order",
        "min_similarity": 0.9,
        "results": [],
        "total_results": 0,
    }
    assert not any(name in sys.modules for name in ("openai", "anthropic", "langchain"))


def test_api_default_threshold_without_override_matches_service(
    make_client: Callable[[float], TestClient],
) -> None:
    client = make_client(0.4)
    body = client.post(URL, json={"query": PASSWORD_QUESTION, "top_k": MAX_TOP_K}).json()

    assert body["results"][0]["faq_id"] == "faq_107"
    assert body["results"][0]["similarity_score"] == 1.0
    assert all(result["similarity_score"] >= 0.4 for result in body["results"])
