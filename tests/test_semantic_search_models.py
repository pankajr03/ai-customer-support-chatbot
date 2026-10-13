"""Semantic search request/response models: validation, mapping and schema accuracy."""

import json
import math
from typing import Any

import pytest
from pydantic import ValidationError

from app.models.faq import FAQ, FAQCategory
from app.models.semantic_search import (
    SCORE_DECIMALS,
    SemanticSearchRequest,
    SemanticSearchResponse,
    SemanticSearchResult,
)
from app.services.embedding_service import EmbeddingService
from app.services.faq_service import FAQService
from app.services.semantic_search_service import (
    DEFAULT_TOP_K,
    MAX_QUERY_LENGTH,
    MAX_TOP_K,
    SemanticSearchService,
)
from tests.fakes import FAKE_MODEL_NAME, CountingLoader
from tests.faq_test_data import TEST_FAQ_RECORDS

SPEC_REQUEST = {
    "query": "Where is my package?",
    "top_k": 3,
    "category": None,
    "min_similarity": 0.4,
}


def stored_faq(index: int = 0) -> FAQ:
    return FAQ.model_validate(TEST_FAQ_RECORDS[index])


def request_error_fields(payload: dict[str, Any]) -> set[str]:
    with pytest.raises(ValidationError) as exc:
        SemanticSearchRequest.model_validate(payload)
    return {str(error["loc"][0]) for error in exc.value.errors()}


# --- Request --------------------------------------------------------------------------------


def test_spec_example_request_is_valid() -> None:
    request = SemanticSearchRequest.model_validate(SPEC_REQUEST)

    assert (request.query, request.top_k, request.category, request.min_similarity) == (
        "Where is my package?",
        3,
        None,
        0.4,
    )


def test_request_defaults() -> None:
    request = SemanticSearchRequest(query="refund")

    assert request.top_k == DEFAULT_TOP_K
    assert request.category is None
    assert request.min_similarity is None


def test_request_parses_from_json() -> None:
    request = SemanticSearchRequest.model_validate_json(json.dumps(SPEC_REQUEST))

    assert request.top_k == 3


def test_query_whitespace_is_normalised() -> None:
    assert SemanticSearchRequest(query="  where \n is   my\tpackage? ").query == (
        "where is my package?"
    )


@pytest.mark.parametrize("query", ["", "   ", "\n\t", "x" * (MAX_QUERY_LENGTH + 1)])
def test_blank_or_overlong_query_is_rejected(query: str) -> None:
    assert request_error_fields({"query": query}) == {"query"}


@pytest.mark.parametrize("query", [None, 42, ["a"]])
def test_non_string_query_is_rejected(query: object) -> None:
    assert request_error_fields({"query": query}) == {"query"}


def test_missing_query_is_rejected() -> None:
    assert request_error_fields({"top_k": 3}) == {"query"}


@pytest.mark.parametrize("top_k", [1, MAX_TOP_K])
def test_top_k_boundaries_are_accepted(top_k: int) -> None:
    assert SemanticSearchRequest(query="q", top_k=top_k).top_k == top_k


@pytest.mark.parametrize("top_k", [0, -1, MAX_TOP_K + 1, "3", 3.0, 3.5, True, None])
def test_invalid_top_k_is_rejected(top_k: object) -> None:
    assert request_error_fields({"query": "q", "top_k": top_k}) == {"top_k"}


@pytest.mark.parametrize("category", ["shipping", " Shipping ", "SHIPPING"])
def test_category_is_normalised(category: str) -> None:
    assert SemanticSearchRequest(query="q", category=category).category is FAQCategory.SHIPPING


@pytest.mark.parametrize("category", ["billing", "accounts", ""])
def test_unknown_category_is_rejected(category: str) -> None:
    assert request_error_fields({"query": "q", "category": category}) == {"category"}


@pytest.mark.parametrize("value", [-1, -1.0, 0, 0.4, 1, 1.0])
def test_min_similarity_within_cosine_range_is_accepted(value: float) -> None:
    assert SemanticSearchRequest(query="q", min_similarity=value).min_similarity == value


@pytest.mark.parametrize("value", [-1.01, 1.01, 2, 100, math.nan, math.inf, -math.inf, "high"])
def test_min_similarity_outside_cosine_range_is_rejected(value: object) -> None:
    assert request_error_fields({"query": "q", "min_similarity": value}) == {"min_similarity"}


def test_unknown_fields_are_rejected() -> None:
    # e.g. the service's own parameter name used by mistake
    assert request_error_fields({"query": "q", "min_score": 0.4}) == {"min_score"}


# --- Result -----------------------------------------------------------------------------------


def test_result_from_faq_uses_the_stored_approved_answer() -> None:
    faq = stored_faq()

    result = SemanticSearchResult.from_faq(faq, 0.73519)

    assert result.model_dump(mode="json") == {
        "faq_id": faq.id,
        "question": faq.question,
        "answer": faq.answer,
        "category": faq.category.value,
        "similarity_score": 0.7352,
    }


@pytest.mark.parametrize(
    ("similarity", "expected"),
    [(0.123456, 0.1235), (1.0, 1.0), (-1.0, -1.0), (0.99999, 1.0), (0.0, 0.0)],
)
def test_similarity_is_rounded_not_replaced(similarity: float, expected: float) -> None:
    assert SemanticSearchResult.from_faq(stored_faq(), similarity).similarity_score == expected


@pytest.mark.parametrize("score", [1.01, -1.01, math.nan, math.inf])
def test_result_rejects_impossible_scores(score: float) -> None:
    with pytest.raises(ValidationError):
        SemanticSearchResult.from_faq(stored_faq(), score)


def test_result_does_not_expose_internal_fields() -> None:
    fields = set(SemanticSearchResult.from_faq(stored_faq(), 0.5).model_dump())

    assert fields == {"faq_id", "question", "answer", "category", "similarity_score"}


# --- Response ------------------------------------------------------------------------------------


def test_total_results_always_matches_results() -> None:
    results = [SemanticSearchResult.from_faq(stored_faq(i), 0.5) for i in range(3)]

    response = SemanticSearchResponse(query="q", min_similarity=None, results=results)

    assert response.total_results == 3
    assert response.model_dump(mode="json")["total_results"] == 3


def test_empty_response() -> None:
    response = SemanticSearchResponse(query="xyzzy", min_similarity=0.4, results=[])

    assert response.model_dump(mode="json") == {
        "query": "xyzzy",
        "min_similarity": 0.4,
        "results": [],
        "total_results": 0,
    }


def test_response_json_has_the_documented_shape() -> None:
    response = SemanticSearchResponse(
        query="Where is my package?",
        min_similarity=0.4,
        results=[SemanticSearchResult.from_faq(stored_faq(), 0.84)],
    )

    body = json.loads(response.model_dump_json())

    assert list(body) == ["query", "min_similarity", "results", "total_results"]
    assert list(body["results"][0]) == [
        "faq_id",
        "question",
        "answer",
        "category",
        "similarity_score",
    ]


def test_scores_come_from_the_actual_search(faq_service: FAQService) -> None:
    """End to end with the real search code (fake encoder): no fixed or invented scores."""
    semantic = SemanticSearchService(
        faq_service.list_active_faqs, EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader())
    )
    request = SemanticSearchRequest(query="  how do I   reset my password ", top_k=3)

    matches = semantic.search(request.query, top_k=request.top_k)
    response = SemanticSearchResponse(
        query=request.query,
        min_similarity=None,
        results=[SemanticSearchResult.from_faq(m.faq, m.score) for m in matches],
    )

    assert response.total_results == len(matches) == 3
    for result, match in zip(response.results, matches, strict=True):
        assert result.faq_id == match.faq.id
        assert result.answer == match.faq.answer
        assert result.similarity_score == round(match.score, SCORE_DECIMALS)
    assert len({r.similarity_score for r in response.results}) > 1  # genuinely computed


# --- Schema accuracy ----------------------------------------------------------------------


def test_request_schema_documents_validation_rules() -> None:
    properties = SemanticSearchRequest.model_json_schema()["properties"]

    assert properties["query"]["maxLength"] == MAX_QUERY_LENGTH
    assert properties["query"]["minLength"] == 1
    assert (properties["top_k"]["minimum"], properties["top_k"]["maximum"]) == (1, MAX_TOP_K)
    assert properties["top_k"]["default"] == DEFAULT_TOP_K
    numeric = next(s for s in properties["min_similarity"]["anyOf"] if s.get("type") == "number")
    assert (numeric["minimum"], numeric["maximum"]) == (-1.0, 1.0)
    assert SemanticSearchRequest.model_json_schema()["additionalProperties"] is False


def test_response_schema_marks_total_results_required_and_read_only() -> None:
    schema = SemanticSearchResponse.model_json_schema(mode="serialization")

    assert set(schema["required"]) == {"query", "min_similarity", "results", "total_results"}
    assert schema["properties"]["total_results"]["readOnly"] is True


def test_score_description_does_not_claim_probability() -> None:
    schema = SemanticSearchResult.model_json_schema()
    description = schema["properties"]["similarity_score"]["description"].lower()

    assert "cosine similarity" in description
    assert "not a probability" in description
    assert (
        schema["properties"]["similarity_score"]["minimum"],
        schema["properties"]["similarity_score"]["maximum"],
    ) == (-1.0, 1.0)


def test_schema_examples_validate_against_their_models() -> None:
    for model in (SemanticSearchRequest, SemanticSearchResult):
        for example in model.model_json_schema()["examples"]:
            model.model_validate(example)
    for example in SemanticSearchResponse.model_json_schema(mode="serialization")["examples"]:
        payload = {k: v for k, v in example.items() if k != "total_results"}
        response = SemanticSearchResponse.model_validate(payload)
        assert response.total_results == example["total_results"]


@pytest.mark.sample_data
def test_schema_example_uses_a_real_faq_record() -> None:
    """The documented example must not show an answer that does not exist in the data."""
    from app.database.faq_repository import FAQRepository

    [example] = SemanticSearchResult.model_json_schema()["examples"]
    stored = FAQRepository.from_json_file().get_by_id(example["faq_id"])

    assert stored is not None
    assert (stored.question, stored.answer, stored.category.value) == (
        example["question"],
        example["answer"],
        example["category"],
    )
