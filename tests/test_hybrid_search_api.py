"""POST /api/v1/hybrid-search: decisions, evidence, validation and degraded retrieval.

The app is built with the fixed test FAQs and a fake embedding model (tests/fakes.py),
so no model is downloaded and results are deterministic. The "fixed retrieval" tests go
further: they replace retrieval with hand-written results, so every decision and every
response field can be asserted exactly.
"""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.dependencies import get_answer_decision_service
from app.core.config import Settings
from app.main import create_app
from app.models.faq import FAQCategory
from app.models.hybrid_search import RRF_DECIMALS, HybridSearchResponse
from app.models.semantic_search import SCORE_DECIMALS
from app.services.answer_decision_service import (
    NO_MATCH_MESSAGE,
    UNAVAILABLE_MESSAGE,
    AnswerDecisionService,
)
from app.services.embedding_service import EmbeddingModelLoadError, EmbeddingService
from app.services.faq_service import FAQService
from app.services.hybrid_search_service import MAX_TOP_K
from app.services.semantic_search_service import MAX_QUERY_LENGTH, SemanticSearchService
from tests.fakes import (
    FAKE_MODEL_NAME,
    FAQS,
    CountingLoader,
    FakeEncoder,
    FakeHybrid,
    FakeKeyword,
)
from tests.fakes import (
    result as hybrid_spec,
)
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


# --- Fixed retrieval results (exact responses for every decision) -----------------------------


@pytest.fixture
def fixed(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> Iterator[Callable[..., tuple[TestClient, FakeHybrid]]]:
    """Build a client whose decision service sees the given hand-written retrieval results."""
    clients: list[TestClient] = []

    def _make(
        specs: list[tuple],
        keyword_specs: list[tuple[str, int, bool]] | None = None,
        error: Exception | None = None,
    ) -> tuple[TestClient, FakeHybrid]:
        app = build_app(make_settings, faq_service)
        fake_hybrid = FakeHybrid(specs, error)
        service = AnswerDecisionService(fake_hybrid, FakeKeyword(keyword_specs))  # type: ignore[arg-type]
        app.dependency_overrides[get_answer_decision_service] = lambda: service
        clients.append(TestClient(app))
        return clients[-1], fake_hybrid

    yield _make
    for client in clients:
        client.close()


def evidence(faq_id: str, rank: int, **fields: Any) -> dict[str, Any]:
    faq = FAQS[faq_id]
    return {
        "rank": rank,
        "faq_id": faq_id,
        "question": faq.question,
        "category": faq.category.value,
        "semantic_similarity": None,
        "keyword_score": None,
        "exact_question_match": False,
        "rrf_score": None,
        **fields,
    }


def test_fixed_answered_response(fixed: Callable[..., Any]) -> None:
    client, _ = fixed(
        [
            hybrid_spec("faq_107", similarity=0.72345, keyword_score=15, rrf=0.03278688),
            hybrid_spec("faq_101", similarity=0.31, rrf=0.0161),
        ]
    )

    response = client.post(URL, json={"query": "I forgot my password", "top_k": 2})

    assert response.status_code == 200
    assert "Retry-After" not in response.headers
    assert response.json() == {
        "query": "I forgot my password",
        "decision": "answered",
        "retrieval_mode": "hybrid",
        "answer": FAQS["faq_107"].answer,
        "matched_faq_id": "faq_107",
        "clarification_question": None,
        "fallback_message": None,
        "results": [
            evidence(
                "faq_107", 1, semantic_similarity=0.7235, keyword_score=15, rrf_score=0.032787
            ),
            evidence("faq_101", 2, semantic_similarity=0.31, rrf_score=0.0161),
        ],
        "total_results": 2,
    }


def test_fixed_no_match_response(fixed: Callable[..., Any]) -> None:
    # Top RRF and many keyword points, but low semantic similarity: not relevant enough.
    client, _ = fixed([hybrid_spec("faq_101", similarity=0.2, keyword_score=90, rrf=0.0328)])

    body = client.post(URL, json={"query": "order something"}).json()

    assert body["decision"] == "no_match"
    assert customer_texts(body) == {
        "answer": None,
        "matched_faq_id": None,
        "clarification_question": None,
        "fallback_message": NO_MATCH_MESSAGE,
    }
    assert body["results"] == [
        evidence("faq_101", 1, semantic_similarity=0.2, keyword_score=90, rrf_score=0.0328)
    ]


def test_fixed_missing_faq_is_no_match(fixed: Callable[..., Any]) -> None:
    client, _ = fixed([])

    body = client.post(URL, json={"query": "anything"}).json()

    assert (body["decision"], body["fallback_message"], body["results"]) == (
        "no_match",
        NO_MATCH_MESSAGE,
        [],
    )


def test_fixed_ambiguous_response(fixed: Callable[..., Any]) -> None:
    client, _ = fixed(
        [hybrid_spec("faq_103", similarity=0.66), hybrid_spec("faq_104", similarity=0.64)]
    )

    body = client.post(URL, json={"query": "shipping question"}).json()

    first, second = FAQS["faq_103"].question, FAQS["faq_104"].question
    assert body["decision"] == "ambiguous"
    assert customer_texts(body) == {
        "answer": None,
        "matched_faq_id": None,
        "clarification_question": (
            f'Could you clarify which of these you mean: "{first}" or "{second}"'
        ),
        "fallback_message": None,
    }
    assert [r["faq_id"] for r in body["results"]] == ["faq_103", "faq_104"]


def test_fixed_retrieval_unavailable_response(fixed: Callable[..., Any]) -> None:
    error = EmbeddingModelLoadError("C:/models/secret-model missing", retry_after_seconds=12.3)
    client, _ = fixed([], [("faq_103", 55, False), ("faq_104", 55, False)], error)

    response = client.post(URL, json={"query": "delivery shipping"})

    assert response.status_code == 200
    assert response.headers["Retry-After"] == "13"
    body = response.json()
    assert (body["decision"], body["retrieval_mode"]) == ("retrieval_unavailable", "keyword_only")
    assert customer_texts(body) == {
        "answer": None,
        "matched_faq_id": None,
        "clarification_question": None,
        "fallback_message": UNAVAILABLE_MESSAGE,
    }
    assert body["results"] == [
        evidence("faq_103", 1, keyword_score=55),
        evidence("faq_104", 2, keyword_score=55),
    ]
    assert "secret" not in response.text
    assert "EmbeddingModelLoadError" not in response.text


def test_fixed_degraded_answer_is_flagged(fixed: Callable[..., Any]) -> None:
    client, _ = fixed([], [("faq_107", 115, True)], EmbeddingModelLoadError("down"))

    response = client.post(URL, json={"query": FAQS["faq_107"].question})

    body = response.json()
    assert (body["decision"], body["retrieval_mode"], body["matched_faq_id"]) == (
        "answered",
        "keyword_only",
        "faq_107",
    )
    assert body["answer"] == FAQS["faq_107"].answer
    assert body["results"] == [evidence("faq_107", 1, keyword_score=115, exact_question_match=True)]
    assert "Retry-After" not in response.headers


def test_fixed_category_is_passed_to_retrieval(fixed: Callable[..., Any]) -> None:
    client, fake_hybrid = fixed([hybrid_spec("faq_103", similarity=0.7)])

    client.post(URL, json={"query": "delivery", "category": " Shipping "})
    client.post(URL, json={"query": "delivery"})

    assert [call["category"] for call in fake_hybrid.calls] == [FAQCategory.SHIPPING, None]


def test_fixed_invalid_request_never_reaches_retrieval(fixed: Callable[..., Any]) -> None:
    client, fake_hybrid = fixed([hybrid_spec("faq_103", similarity=0.7)])

    for payload in ({"query": "  "}, {"query": "q", "top_k": 0}, {"query": "q", "top_k": 99}):
        assert client.post(URL, json=payload).status_code == 422
    assert fake_hybrid.calls == []


# --- Documentation and coexistence -------------------------------------------------------------


def test_endpoint_is_documented_in_openapi(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"][URL]["post"]
    components = schema["components"]["schemas"]

    assert list(schema["paths"][URL]) == ["post"]
    assert operation["tags"] == ["hybrid search"]
    assert operation["summary"] == "Hybrid FAQ search"
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert request_schema["$ref"].endswith("/HybridSearchRequest")
    assert operation["requestBody"]["required"] is True
    ok = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok["$ref"].endswith("/HybridSearchResponse")
    assert "422" in operation["responses"]
    assert set(components["HybridSearchRequest"]["properties"]) == {"query", "top_k", "category"}
    assert components["HybridSearchRequest"]["required"] == ["query"]
    assert set(components["HybridSearchResponse"]["properties"]) == set(RESPONSE_FIELDS)
    assert set(components["HybridSearchResult"]["properties"]) == set(RESULT_FIELDS)
    assert set(components["Decision"]["enum"]) == {
        "answered",
        "ambiguous",
        "no_match",
        "retrieval_unavailable",
    }
    rrf_doc = components["HybridSearchResult"]["properties"]["rrf_score"]["description"]
    assert "not a probability" in rrf_doc


def test_swagger_and_redoc_are_served(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200


def test_stage1_to_3_endpoints_still_work(client: TestClient) -> None:
    assert client.get("/").status_code == 200
    assert client.get("/api/v1/health").json()["status"] == "healthy"
    assert client.get("/api/v1/faqs").json()["total"] == 7
    assert client.get("/api/v1/faqs/categories").status_code == 200
    assert client.get("/api/v1/faqs/faq_101").json()["id"] == "faq_101"
    assert client.get("/api/v1/faqs/faq_105").status_code == 404  # inactive
    keyword = client.get("/api/v1/faqs/search", params={"q": "track order"}).json()
    assert keyword["results"][0]["id"] == "faq_101"
    semantic = client.post("/api/v1/semantic-search", json={"query": "reset my password"})
    assert semantic.json()["results"][0]["faq_id"] == "faq_107"


def test_no_chat_endpoint_exists(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert not [path for path in paths if "chat" in path or "conversation" in path]
    assert client.post("/api/v1/chat", json={"message": "hi"}).status_code == 404


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
