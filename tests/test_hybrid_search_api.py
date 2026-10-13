"""POST /api/v1/hybrid-search: decisions, evidence, validation and degraded retrieval.

The app is built with the fixed test FAQs and a fake embedding model (tests/fakes.py),
so no model is downloaded and results are deterministic.
"""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.main import create_app
from app.models.hybrid_search import RRF_DECIMALS, HybridSearchResponse
from app.models.semantic_search import SCORE_DECIMALS
from app.services.answer_decision_service import NO_MATCH_MESSAGE, UNAVAILABLE_MESSAGE
from app.services.embedding_service import EmbeddingService
from app.services.faq_service import FAQService
from app.services.hybrid_search_service import MAX_TOP_K
from app.services.semantic_search_service import MAX_QUERY_LENGTH, SemanticSearchService
from tests.fakes import FAKE_MODEL_NAME, CountingLoader, FakeEncoder
from tests.faq_test_data import ACTIVE_SHIPPING_IDS, INACTIVE_IDS, TEST_FAQ_RECORDS

URL = "/api/v1/hybrid-search"
RESPONSE_FIELDS = [
    "query",
    "decision",
    "retrieval_mode",
    "answer",
    "matched_faq_id",
    "clarification_question",
    "fallback_message",
    "results",
    "total_results",
]
RESULT_FIELDS = [
    "rank",
    "faq_id",
    "question",
    "category",
    "semantic_similarity",
    "keyword_score",
    "exact_question_match",
    "rrf_score",
]
CUSTOMER_TEXTS = ["answer", "matched_faq_id", "clarification_question", "fallback_message"]
RECORDS = {record["id"]: record for record in TEST_FAQ_RECORDS}
PASSWORD_QUESTION = RECORDS["faq_107"]["question"]


def failing_loader(model_name: str) -> FakeEncoder:
    raise OSError(f"C:/Users/someone/.cache/huggingface/{model_name} not found")


def build_app(
    make_settings: Callable[..., Settings],
    faq_service: FAQService,
    loader: Callable[[str], Any] | None = None,
    **settings: object,
) -> FastAPI:
    embeddings = EmbeddingService(
        FAKE_MODEL_NAME, loader=loader or CountingLoader(), load_retry_seconds=30
    )
    semantic = SemanticSearchService(faq_service.list_active_faqs, embeddings)
    return create_app(
        make_settings(**settings), faq_service=faq_service, semantic_search_service=semantic
    )


@pytest.fixture
def hybrid_app(make_settings: Callable[..., Settings], faq_service: FAQService) -> FastAPI:
    return build_app(make_settings, faq_service)


@pytest.fixture
def client(hybrid_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(hybrid_app) as test_client:
        yield test_client


def customer_texts(body: dict) -> dict:
    return {name: body[name] for name in CUSTOMER_TEXTS}


# --- Decisions -----------------------------------------------------------------------------------


def test_exact_question_is_answered_with_the_approved_answer(client: TestClient) -> None:
    response = client.post(URL, json={"query": PASSWORD_QUESTION, "top_k": 3})

    assert response.status_code == 200
    body = response.json()
    assert list(body) == RESPONSE_FIELDS
    assert body["decision"] == "answered"
    assert body["retrieval_mode"] == "hybrid"
    assert customer_texts(body) == {
        "answer": RECORDS["faq_107"]["answer"],
        "matched_faq_id": "faq_107",
        "clarification_question": None,
        "fallback_message": None,
    }


def test_no_relevant_faq_returns_the_safe_fallback(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    app = build_app(make_settings, faq_service, answer_min_similarity=1.0)
    with TestClient(app) as client:
        body = client.post(URL, json={"query": "xyzzy quantum"}).json()

    assert body["decision"] == "no_match"
    assert customer_texts(body) == {
        "answer": None,
        "matched_faq_id": None,
        "clarification_question": None,
        "fallback_message": NO_MATCH_MESSAGE,
    }
    assert body["results"], "evidence is still returned for evaluation"


def test_ambiguous_question_asks_for_clarification(client: TestClient) -> None:
    body = client.post(URL, json={"query": "order"}).json()

    assert body["decision"] == "ambiguous"
    assert body["clarification_question"].startswith("Could you clarify")
    assert body["answer"] is body["matched_faq_id"] is body["fallback_message"] is None


def test_configured_fallback_message_is_used(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    custom = "Please email help@example.com."
    app = build_app(
        make_settings,
        faq_service,
        answer_min_similarity=1.0,
        fallback_no_match_message=custom,
    )
    with TestClient(app) as client:
        assert client.post(URL, json={"query": "xyzzy"}).json()["fallback_message"] == custom


# --- Retrieval evidence -----------------------------------------------------------------------


def test_results_are_the_computed_hybrid_evidence(client: TestClient, hybrid_app: FastAPI) -> None:
    body = client.post(URL, json={"query": "where is my order", "top_k": 4}).json()
    expected = hybrid_app.state.hybrid_search_service.search("where is my order", top_k=4)

    assert body["total_results"] == len(body["results"]) == 4
    for position, (result, hybrid) in enumerate(zip(body["results"], expected, strict=True), 1):
        assert list(result) == RESULT_FIELDS
        assert result["rank"] == position
        assert result["faq_id"] == hybrid.faq_id
        assert result["question"] == hybrid.faq.question
        assert result["category"] == hybrid.faq.category.value
        assert result["rrf_score"] == round(hybrid.rrf_score, RRF_DECIMALS)
        similarity = None if hybrid.semantic is None else hybrid.semantic.similarity
        assert result["semantic_similarity"] == (
            None if similarity is None else round(similarity, SCORE_DECIMALS)
        )
        assert result["keyword_score"] == (None if hybrid.keyword is None else hybrid.keyword.score)


def test_results_never_contain_answers(client: TestClient) -> None:
    body = client.post(URL, json={"query": "order", "top_k": 20}).json()

    answers = {record["answer"] for record in TEST_FAQ_RECORDS}
    for result in body["results"]:
        assert "answer" not in result
        assert not answers & set(map(str, result.values()))


def test_top_k_limits_evidence_but_not_the_decision(client: TestClient) -> None:
    bodies = [
        client.post(URL, json={"query": query, "top_k": top_k}).json()
        for query in ("where is my order", PASSWORD_QUESTION, "order")
        for top_k in (1, 20)
    ]

    for small, large in zip(bodies[::2], bodies[1::2], strict=True):
        assert len(small["results"]) == 1
        assert small["results"][0] == large["results"][0]
        assert {k: v for k, v in small.items() if k not in ("results", "total_results")} == {
            k: v for k, v in large.items() if k not in ("results", "total_results")
        }


def test_category_filter_and_inactive_faqs(client: TestClient) -> None:
    shipping = client.post(URL, json={"query": "order", "top_k": 20, "category": " Shipping "})
    everything = client.post(URL, json={"query": RECORDS["faq_105"]["question"], "top_k": 20})

    assert {r["faq_id"] for r in shipping.json()["results"]} <= set(ACTIVE_SHIPPING_IDS)
    returned = {r["faq_id"] for r in everything.json()["results"]}
    assert returned.isdisjoint(INACTIVE_IDS)
    assert everything.json()["matched_faq_id"] not in INACTIVE_IDS


def test_query_whitespace_is_normalised(client: TestClient) -> None:
    body = client.post(URL, json={"query": "  How do I \n reset my   password? "}).json()

    assert body["query"] == PASSWORD_QUESTION
    assert body["matched_faq_id"] == "faq_107"


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
        ({"query": "q", "min_similarity": 0.4}, "min_similarity"),
    ],
)
def test_invalid_request_body_is_422(client: TestClient, payload: dict, field: str) -> None:
    response = client.post(URL, json=payload)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][:2] == ["body", field]


def test_top_k_above_candidate_depth_is_422(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    app = build_app(make_settings, faq_service, hybrid_candidate_depth=5)
    with TestClient(app) as client:
        assert client.post(URL, json={"query": "order", "top_k": 5}).status_code == 200
        response = client.post(URL, json={"query": "order", "top_k": 6})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "top_k"]
    assert response.json()["detail"][0]["ctx"] == {"le": 5}


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_only_post_is_allowed(client: TestClient, method: str) -> None:
    assert client.request(method, URL).status_code == 405


# --- Semantic model unavailable (degraded, still 200) ------------------------------------------


def test_model_failure_degrades_to_keyword_only_answer(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    with TestClient(build_app(make_settings, faq_service, failing_loader)) as client:
        response = client.post(URL, json={"query": PASSWORD_QUESTION})

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "answered"
    assert body["retrieval_mode"] == "keyword_only"
    assert body["matched_faq_id"] == "faq_107"
    assert body["answer"] == RECORDS["faq_107"]["answer"]
    assert all(r["semantic_similarity"] is r["rrf_score"] is None for r in body["results"])


def test_model_failure_without_strong_evidence_is_safe_and_hides_internals(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    with TestClient(build_app(make_settings, faq_service, failing_loader)) as client:
        response = client.post(URL, json={"query": "order"})

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "retrieval_unavailable"
    assert body["retrieval_mode"] == "keyword_only"
    assert customer_texts(body) == {
        "answer": None,
        "matched_faq_id": None,
        "clarification_question": None,
        "fallback_message": UNAVAILABLE_MESSAGE,
    }
    assert int(response.headers["Retry-After"]) > 0
    for leak in ("OSError", "huggingface", "C:/", FAKE_MODEL_NAME, "Traceback", "reasons"):
        assert leak not in response.text


def test_unexpected_error_is_500_not_success(hybrid_app: FastAPI) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("internal detail /srv/app/secret.py")

    hybrid_app.state.answer_decision_service.decide = explode

    with TestClient(hybrid_app) as client:
        response = client.post(URL, json={"query": "order"})

    assert response.status_code == 500
    assert "secret.py" not in response.text


# --- Documentation and coexistence -------------------------------------------------------------


def test_endpoint_is_documented_in_openapi(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"][URL]["post"]

    assert list(schema["paths"][URL]) == ["post"]
    assert operation["tags"] == ["hybrid search"]
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert request_schema["$ref"].endswith("/HybridSearchRequest")
    ok = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok["$ref"].endswith("/HybridSearchResponse")
    decision = schema["components"]["schemas"]["Decision"]
    assert set(decision["enum"]) == {"answered", "ambiguous", "no_match", "retrieval_unavailable"}


def test_existing_endpoints_and_no_chat_endpoint(client: TestClient) -> None:
    assert client.post("/api/v1/semantic-search", json={"query": "order"}).status_code == 200
    assert client.get("/api/v1/faqs/search", params={"q": "order"}).status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert not [path for path in paths if "chat" in path or "conversation" in path]


@pytest.mark.parametrize(
    ("decision", "texts"),
    [
        ("answered", {"answer": "a"}),  # missing matched_faq_id
        ("answered", {"answer": "a", "matched_faq_id": "faq_101", "fallback_message": "f"}),
        ("ambiguous", {"answer": "a"}),
        ("no_match", {"clarification_question": "c"}),
        ("retrieval_unavailable", {}),
    ],
)
def test_response_rejects_texts_that_do_not_fit_the_decision(decision: str, texts: dict) -> None:
    fields = dict.fromkeys(CUSTOMER_TEXTS) | texts
    with pytest.raises(ValidationError, match="does not match decision"):
        HybridSearchResponse(
            query="q", decision=decision, retrieval_mode="hybrid", results=[], **fields
        )
