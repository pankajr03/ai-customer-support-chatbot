"""POST /api/v1/answer: grounded answers, generation methods, usage, errors and docs.

Fixed-retrieval tests replace Stage 4 retrieval with hand-written results and the
OpenAI client with a fake, so every field can be asserted exactly. No network is used.
"""

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.dependencies import get_grounded_answer_service
from app.core.config import Settings
from app.database.faq_repository import FAQDataError
from app.main import create_app
from app.models.faq import FAQCategory
from app.models.usage import ModelPricing
from app.prompts.customer_support import SYSTEM_INSTRUCTIONS
from app.services.answer_decision_service import (
    NO_MATCH_MESSAGE,
    UNAVAILABLE_MESSAGE,
    AnswerDecisionService,
)
from app.services.context_builder import ContextBuilder
from app.services.embedding_service import EmbeddingModelLoadError, EmbeddingService
from app.services.faq_service import FAQService
from app.services.grounded_answer_service import GroundedAnswerService
from app.services.llm_service import LLMService
from app.services.semantic_search_service import MAX_QUERY_LENGTH, SemanticSearchService
from app.services.usage_tracker import UsageTracker
from tests.fakes import FAKE_MODEL_NAME, FAQS, CountingLoader, FakeHybrid, FakeKeyword
from tests.fakes import result as hybrid_spec

URL = "/api/v1/answer"
KEY = SecretStr("sk-test-0123456789abcdefghij")
RESPONSE_FIELDS = [
    "query",
    "decision",
    "answer",
    "source_faq_ids",
    "generation_method",
    "usage",
]
PRICE = ModelPricing(input_per_million_tokens=2.0, output_per_million_tokens=8.0)  # test value

SCENARIOS: dict[str, tuple[list, list | None, Exception | None]] = {
    "answered": (
        [hybrid_spec("faq_107", similarity=0.72), hybrid_spec("faq_101", similarity=0.2)],
        None,
        None,
    ),
    "ambiguous": (
        [hybrid_spec("faq_103", similarity=0.66), hybrid_spec("faq_104", similarity=0.64)],
        None,
        None,
    ),
    "no_match": ([hybrid_spec("faq_101", similarity=0.1)], None, None),
    "unavailable": (
        [],
        [("faq_103", 55, False), ("faq_104", 55, False)],
        EmbeddingModelLoadError("C:/models/secret", retry_after_seconds=12.3),
    ),
}


def reply(**overrides: Any) -> str:
    data = {
        "status": "answered",
        "answer": "Pick Forgot Password on the sign-in page, then follow the link we email you.",
        "source_faq_ids": ["faq_107"],
        "reason": "INTERNAL-REASON",
    }
    data.update(overrides)
    return json.dumps(data)


class FakeClient:
    def __init__(self, output: str, status: str = "completed") -> None:
        self.output, self.status = output, status
        self.calls: list[dict[str, Any]] = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            status=self.status,
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            output_text=self.output,
            output=[],
            model="gpt-5.4-mini-2026-03-17",
            id="resp_1",
            _request_id="req_1",
            usage=SimpleNamespace(
                input_tokens=812,
                output_tokens=64,
                total_tokens=876,
                input_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=0),
                output_tokens_details=SimpleNamespace(reasoning_tokens=20),
            ),
        )

    def close(self) -> None:
        pass


@pytest.fixture
def fixed(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> Iterator[Callable[..., tuple[TestClient, FakeClient, FakeHybrid]]]:
    clients: list[TestClient] = []

    def _make(
        scenario: str = "answered",
        output: str = "",
        *,
        status: str = "completed",
        llm_enabled: bool = True,
        tracker: UsageTracker | None = None,
    ) -> tuple[TestClient, FakeClient, FakeHybrid]:
        specs, keyword, error = SCENARIOS[scenario]
        hybrid = FakeHybrid(specs, error)
        fake = FakeClient(output, status)
        llm = LLMService(
            KEY,
            model="gpt-5.4-mini",
            enabled=llm_enabled,
            usage_tracker=tracker,
            client_factory=lambda key: fake,
        )
        service = GroundedAnswerService(
            AnswerDecisionService(hybrid, FakeKeyword(keyword)),  # type: ignore[arg-type]
            ContextBuilder(),
            llm,
        )
        app = create_app(make_settings(), faq_service=faq_service)
        app.dependency_overrides[get_grounded_answer_service] = lambda: service
        clients.append(TestClient(app))
        return clients[-1], fake, hybrid

    yield _make
    for client in clients:
        client.close()


# --- Generation methods ------------------------------------------------------------------------


def test_llm_grounded_answer_with_actual_usage(fixed: Callable[..., Any]) -> None:
    client, fake, _ = fixed("answered", reply(), tracker=UsageTracker({"gpt-5.4-mini": PRICE}))

    response = client.post(URL, json={"query": "I forgot my password", "category": None})

    assert response.status_code == 200
    body = response.json()
    assert list(body) == RESPONSE_FIELDS
    assert body == {
        "query": "I forgot my password",
        "decision": "answered",
        "answer": "Pick Forgot Password on the sign-in page, then follow the link we email you.",
        "source_faq_ids": ["faq_107"],
        "generation_method": "llm_grounded",
        "usage": {
            "input_tokens": 812,
            "output_tokens": 64,
            "total_tokens": 876,
            "estimated_cost_usd": pytest.approx((812 * 2.0 + 64 * 8.0) / 1e6),
        },
    }
    assert len(fake.calls) == 1


def test_cost_is_null_without_a_configured_price(fixed: Callable[..., Any]) -> None:
    client, _, _ = fixed("answered", reply(), tracker=UsageTracker())

    usage = client.post(URL, json={"query": "q"}).json()["usage"]

    assert (usage["input_tokens"], usage["estimated_cost_usd"]) == (812, None)


def test_cost_in_another_currency_is_not_reported_as_usd(fixed: Callable[..., Any]) -> None:
    client, _, _ = fixed(
        "answered", reply(), tracker=UsageTracker({"gpt-5.4-mini": PRICE}, currency="EUR")
    )

    assert client.post(URL, json={"query": "q"}).json()["usage"]["estimated_cost_usd"] is None


def test_llm_disabled_returns_the_approved_faq_without_usage(fixed: Callable[..., Any]) -> None:
    client, fake, _ = fixed("answered", reply(), llm_enabled=False)

    body = client.post(URL, json={"query": "I forgot my password"}).json()

    assert (body["decision"], body["generation_method"]) == ("answered", "approved_faq")
    assert body["answer"] == FAQS["faq_107"].answer
    assert (body["source_faq_ids"], body["usage"]) == (["faq_107"], None)
    assert fake.calls == []


@pytest.mark.parametrize(
    "output",
    [
        "not json",
        reply(source_faq_ids=["faq_999"]),
        reply(answer="Resets cost $5 and are guaranteed within 24 hours."),  # grounding
        reply(status="insufficient_information", source_faq_ids=[]),
    ],
)
def test_rejected_llm_output_returns_the_approved_faq_with_usage(
    fixed: Callable[..., Any], output: str
) -> None:
    client, _, _ = fixed("answered", output)

    body = client.post(URL, json={"query": "I forgot my password"}).json()

    assert (body["generation_method"], body["answer"]) == ("approved_faq", FAQS["faq_107"].answer)
    assert body["usage"]["total_tokens"] == 876  # the call still used tokens


def test_incomplete_response_reports_its_usage(fixed: Callable[..., Any]) -> None:
    client, _, _ = fixed("answered", "", status="incomplete")

    body = client.post(URL, json={"query": "q"}).json()

    assert body["generation_method"] == "approved_faq"
    assert body["usage"]["output_tokens"] == 64


def test_ambiguous_question_gets_a_clarification(fixed: Callable[..., Any]) -> None:
    client, fake, _ = fixed("ambiguous", reply())

    body = client.post(URL, json={"query": "shipping question"}).json()

    assert (body["decision"], body["generation_method"]) == ("ambiguous", "clarification")
    assert body["answer"] == (
        "Could you clarify which of these you mean: "
        f'"{FAQS["faq_103"].question}" or "{FAQS["faq_104"].question}"'
    )
    assert (body["source_faq_ids"], body["usage"]) == (["faq_103", "faq_104"], None)
    assert fake.calls == []


def test_no_match_gets_the_safe_fallback(fixed: Callable[..., Any]) -> None:
    client, fake, _ = fixed("no_match", reply())

    body = client.post(URL, json={"query": "What is the capital of France?"}).json()

    assert body == {
        "query": "What is the capital of France?",
        "decision": "no_match",
        "answer": NO_MATCH_MESSAGE,
        "source_faq_ids": [],
        "generation_method": "fallback",
        "usage": None,
    }
    assert fake.calls == []


def test_degraded_search_gets_the_safe_fallback_with_retry_after(
    fixed: Callable[..., Any],
) -> None:
    client, fake, _ = fixed("unavailable", reply())

    response = client.post(URL, json={"query": "delivery shipping"})

    assert response.status_code == 200
    assert response.headers["Retry-After"] == "13"
    body = response.json()
    assert (body["decision"], body["generation_method"]) == ("retrieval_unavailable", "fallback")
    assert body["answer"] == UNAVAILABLE_MESSAGE
    assert "secret" not in response.text
    assert fake.calls == []


def test_category_and_normalised_query_are_used(fixed: Callable[..., Any]) -> None:
    client, _, hybrid = fixed("answered", reply())

    body = client.post(
        URL, json={"query": "  I forgot \n my password ", "category": " Account "}
    ).json()

    assert body["query"] == "I forgot my password"
    assert hybrid.calls[0]["query"] == "I forgot my password"
    assert hybrid.calls[0]["category"] is FAQCategory.ACCOUNT


def test_requests_are_independent(fixed: Callable[..., Any]) -> None:
    client, fake, _ = fixed("answered", reply())

    first = client.post(URL, json={"query": "I forgot my password"}).json()
    second = client.post(URL, json={"query": "I forgot my password"}).json()

    assert first == second
    assert fake.calls[0]["input"] == fake.calls[1]["input"]  # no history carried over


# --- Nothing internal is exposed --------------------------------------------------------------


def test_responses_never_expose_prompts_reasons_or_credentials(fixed: Callable[..., Any]) -> None:
    client, _, _ = fixed("answered", reply())

    texts = [client.post(URL, json={"query": "I forgot my password"}).text]
    texts.append(fixed("answered", "not json")[0].post(URL, json={"query": "q"}).text)

    for text in texts:
        for secret in (
            KEY.get_secret_value(),
            SYSTEM_INSTRUCTIONS[:60],
            "faq_reference_data",
            "customer_question",
            "INTERNAL-REASON",
            "diagnostics",
            "fallback_reason",
            "invalid_output",
            "resp_1",
            "req_1",
        ):
            assert secret not in text


# --- Validation and errors ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({}, "query"),
        ({"query": ""}, "query"),
        ({"query": "   "}, "query"),
        ({"query": "x" * (MAX_QUERY_LENGTH + 1)}, "query"),
        ({"query": 42}, "query"),
        ({"query": "q", "category": "billing"}, "category"),
        ({"query": "q", "history": [{"role": "user", "content": "hi"}]}, "history"),
        ({"query": "q", "conversation_id": "abc"}, "conversation_id"),
        ({"query": "q", "top_k": 3}, "top_k"),
    ],
)
def test_invalid_requests_are_422(fixed: Callable[..., Any], payload: dict, field: str) -> None:
    client, fake, hybrid = fixed("answered", reply())

    response = client.post(URL, json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"][:2] == ["body", field]
    assert (fake.calls, hybrid.calls) == ([], [])  # rejected before any work


@pytest.mark.parametrize("content", ["not json", '{"query": "x",}', "[]", ""])
def test_malformed_bodies_are_422(fixed: Callable[..., Any], content: str) -> None:
    client, _, _ = fixed()

    response = client.post(URL, content=content, headers={"Content-Type": "application/json"})

    assert response.status_code == 422


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_only_post_is_allowed(fixed: Callable[..., Any], method: str) -> None:
    client, _, _ = fixed()

    assert client.request(method, URL).status_code == 405


class _Raising:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def answer(self, *args: Any, **kwargs: Any) -> Any:
        raise self.error


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (
            FAQDataError("faqs.json is corrupt", Path("C:/data/faqs.json")),
            503,
            "SERVICE_UNAVAILABLE",
        ),
        (RuntimeError("openai key sk-test-SECRET in /srv/app.py"), 500, "INTERNAL_ERROR"),
    ],
)
def test_failures_return_safe_messages(
    make_settings: Callable[..., Settings],
    faq_service: FAQService,
    error: Exception,
    status: int,
    code: str,
) -> None:
    app = create_app(make_settings(), faq_service=faq_service)
    app.dependency_overrides[get_grounded_answer_service] = lambda: _Raising(error)

    with TestClient(app) as client:
        response = client.post(URL, json={"query": "q"})

    assert response.status_code == status
    error = response.json()["error"]
    assert (error["code"], error["request_id"]) == (code, response.headers["X-Request-ID"])
    assert set(error) == {"code", "message", "request_id"}
    for leak in ("C:/", "corrupt", "sk-test", "/srv", "RuntimeError", "Traceback"):
        assert leak not in response.text


# --- The real pipeline (fake embedding model, LLM disabled by default) ----------------------


@pytest.fixture
def real_app(make_settings: Callable[..., Settings], faq_service: FAQService) -> FastAPI:
    semantic = SemanticSearchService(
        faq_service.list_active_faqs, EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader())
    )
    return create_app(make_settings(), faq_service=faq_service, semantic_search_service=semantic)


def test_real_pipeline_answers_with_the_approved_faq(real_app: FastAPI) -> None:
    with TestClient(real_app) as client:
        body = client.post(URL, json={"query": FAQS["faq_107"].question}).json()

    assert body == {
        "query": FAQS["faq_107"].question,
        "decision": "answered",
        "answer": FAQS["faq_107"].answer,
        "source_faq_ids": ["faq_107"],
        "generation_method": "approved_faq",
        "usage": None,
    }
    assert real_app.state.llm_service.client_created is False


def test_real_pipeline_never_answers_with_an_inactive_faq(real_app: FastAPI) -> None:
    with TestClient(real_app) as client:
        body = client.post(URL, json={"query": FAQS["faq_105"].question}).json()

    assert "faq_105" not in body["source_faq_ids"]
    assert body["answer"] != FAQS["faq_105"].answer


# --- Documentation and coexistence -------------------------------------------------------------


def test_endpoint_is_documented_in_openapi(real_app: FastAPI) -> None:
    with TestClient(real_app) as client:
        schema = client.get("/openapi.json").json()
    operation = schema["paths"][URL]["post"]
    components = schema["components"]["schemas"]

    assert list(schema["paths"][URL]) == ["post"]
    assert operation["tags"] == ["grounded answers"]
    assert operation["requestBody"]["required"] is True
    assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/AnswerRequest"
    )
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/AnswerResponse"
    )
    assert {"200", "422", "500", "503"} <= operation["responses"].keys()
    assert set(components["AnswerRequest"]["properties"]) == {"query", "category"}
    assert set(components["AnswerResponse"]["properties"]) == set(RESPONSE_FIELDS)
    assert set(components["GenerationMethod"]["enum"]) == {
        "llm_grounded",
        "approved_faq",
        "clarification",
        "fallback",
    }
    assert (
        "ESTIMATED" in components["UsageSummary"]["properties"]["estimated_cost_usd"]["description"]
    )
    assert "llm_grounded" in operation["description"]


def test_existing_endpoints_still_work_alongside_conversations(real_app: FastAPI) -> None:
    with TestClient(real_app) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/faqs").status_code == 200
        assert client.post("/api/v1/semantic-search", json={"query": "order"}).status_code == 200
        assert client.post("/api/v1/hybrid-search", json={"query": "order"}).status_code == 200
        paths = client.get("/openapi.json").json()["paths"]

    # /answer stays single-turn; chat exists only inside a conversation (Stage 6).
    assert "/api/v1/answer" in paths
    # Staff analytics (/admin/analytics/chat) report on chats; they are not a chat endpoint.
    customer_paths = [path for path in paths if not path.startswith("/api/v1/admin/")]
    assert [path for path in customer_paths if "chat" in path] == [
        "/api/v1/conversations/{session_id}/chat"
    ]
