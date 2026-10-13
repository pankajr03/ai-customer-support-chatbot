"""SemanticSearchService.search: validation, cosine scoring, filtering, ordering, limits.

Uses the deterministic fake encoder (bag-of-words vectors), so scores are predictable
and no model is downloaded. A final opt-in test checks behaviour with the real model.
"""

import logging
import math
from typing import Any

import numpy as np
import pytest

from app.models.faq import FAQ, FAQCategory
from app.services.embedding_service import EmbeddingError, EmbeddingService
from app.services.faq_service import FAQService
from app.services.semantic_search_service import (
    MAX_QUERY_LENGTH,
    MAX_TOP_K,
    InvalidSemanticQueryError,
    SemanticMatch,
    SemanticSearchService,
)
from tests.fakes import FAKE_MODEL_NAME, CountingLoader, FakeEncoder
from tests.faq_test_data import ACTIVE_IDS, ACTIVE_SHIPPING_IDS, INACTIVE_IDS, TEST_FAQ_RECORDS


@pytest.fixture
def encoder() -> FakeEncoder:
    return FakeEncoder()


@pytest.fixture
def semantic(faq_service: FAQService, encoder: FakeEncoder) -> SemanticSearchService:
    embeddings = EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader(encoder))
    return SemanticSearchService(faq_service.list_active_faqs, embeddings)


def make_semantic(faqs: list[FAQ], encoder: FakeEncoder | None = None, **kwargs: Any):
    embeddings = EmbeddingService(
        FAKE_MODEL_NAME, loader=CountingLoader(encoder or FakeEncoder()), **kwargs
    )
    return SemanticSearchService(lambda: faqs, embeddings)


def ids(matches: list[SemanticMatch]) -> list[str]:
    return [match.faq.id for match in matches]


def faq(faq_id: str, question: str, category: str = "general") -> FAQ:
    return FAQ(
        id=faq_id,
        question=question,
        answer=f"Approved answer for {faq_id}.",
        category=category,
        keywords=["test"],
    )


# --- Basic behaviour ----------------------------------------------------------------------


def test_best_match_is_returned_first(semantic: SemanticSearchService) -> None:
    results = semantic.search("how do I reset my password")

    assert results[0].faq.id == "faq_107"


def test_results_are_sorted_by_descending_score(semantic: SemanticSearchService) -> None:
    scores = [match.score for match in semantic.search("my order", top_k=MAX_TOP_K)]

    assert len(scores) == len(ACTIVE_IDS)
    assert scores == sorted(scores, reverse=True)


def test_returns_the_stored_approved_answer_verbatim(
    semantic: SemanticSearchService, faq_service: FAQService
) -> None:
    [top] = semantic.search("how do I reset my password", top_k=1)

    stored = faq_service.get_active_faq("faq_107")
    assert top.faq is stored  # the very same approved record, nothing generated
    assert top.faq.answer == TEST_FAQ_RECORDS[6]["answer"]


def test_inactive_faqs_are_never_returned(semantic: SemanticSearchService) -> None:
    results = semantic.search("free shipping during the holiday sale", top_k=MAX_TOP_K)

    assert set(ids(results)).isdisjoint(INACTIVE_IDS)


def test_no_duplicate_faqs(semantic: SemanticSearchService) -> None:
    results = semantic.search("order", top_k=MAX_TOP_K)

    assert len(ids(results)) == len(set(ids(results)))


def test_each_search_embeds_only_the_query(
    semantic: SemanticSearchService, encoder: FakeEncoder
) -> None:
    semantic.search("track my order")
    semantic.search("reset password")

    # 1 batch for the index (built once) + 1 single-text call per search.
    assert len(encoder.encode_calls) == 3
    assert [calls[0] for calls in encoder.encode_calls[1:]] == [
        ["track my order"],
        ["reset password"],
    ]


def test_search_is_deterministic(semantic: SemanticSearchService) -> None:
    runs = {
        tuple((m.faq.id, m.score) for m in semantic.search("order help", top_k=10))
        for _ in range(10)
    }

    assert len(runs) == 1


# --- Query validation and normalisation ------------------------------------------------------


def test_whitespace_is_normalized_before_embedding(
    semantic: SemanticSearchService, encoder: FakeEncoder
) -> None:
    messy = semantic.search("  reset \n\t my    password  ")
    clean = semantic.search("reset my password")

    assert encoder.encode_calls[1][0] == ["reset my password"]
    assert [(m.faq.id, m.score) for m in messy] == [(m.faq.id, m.score) for m in clean]


@pytest.mark.parametrize("query", ["", "   ", "\n\t"])
def test_blank_query_is_rejected_before_any_model_work(faq_service: FAQService, query: str) -> None:
    loader = CountingLoader()
    semantic = SemanticSearchService(
        faq_service.list_active_faqs, EmbeddingService(FAKE_MODEL_NAME, loader=loader)
    )

    with pytest.raises(InvalidSemanticQueryError, match="empty or whitespace-only"):
        semantic.search(query)

    assert loader.calls == []
    assert semantic.is_ready is False


@pytest.mark.parametrize("query", [None, 42, b"bytes", ["a"]])
def test_non_string_query_is_rejected(semantic: SemanticSearchService, query: object) -> None:
    with pytest.raises(InvalidSemanticQueryError, match="must be a string"):
        semantic.search(query)  # type: ignore[arg-type]


def test_overlong_query_is_rejected(semantic: SemanticSearchService) -> None:
    semantic.search("x" * MAX_QUERY_LENGTH)  # at the limit: fine

    with pytest.raises(InvalidSemanticQueryError, match="too long"):
        semantic.search("x" * (MAX_QUERY_LENGTH + 1))


def test_query_validation_errors_do_not_echo_the_query(semantic: SemanticSearchService) -> None:
    customer_text = "jane.doe@example.com " * 60

    with pytest.raises(InvalidSemanticQueryError) as exc:
        semantic.search(customer_text)

    assert "jane.doe" not in str(exc.value)


def test_query_text_is_not_logged(
    semantic: SemanticSearchService, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        semantic.search("jane.doe@example.com cannot reset her password")

    assert "Semantic search:" in caplog.text
    assert "jane.doe" not in caplog.text


# --- top_k ---------------------------------------------------------------------------------


@pytest.mark.parametrize("top_k", [1, 3, len(ACTIVE_IDS)])
def test_top_k_limits_results(semantic: SemanticSearchService, top_k: int) -> None:
    assert len(semantic.search("order", top_k=top_k)) == top_k


def test_top_k_larger_than_index_returns_everything(semantic: SemanticSearchService) -> None:
    assert len(semantic.search("order", top_k=MAX_TOP_K)) == len(ACTIVE_IDS)


def test_top_k_returns_the_highest_ranked_prefix(semantic: SemanticSearchService) -> None:
    everything = semantic.search("order", top_k=MAX_TOP_K)

    assert ids(semantic.search("order", top_k=3)) == ids(everything)[:3]


@pytest.mark.parametrize("top_k", [0, -1, MAX_TOP_K + 1, 1000])
def test_out_of_range_top_k_is_rejected(semantic: SemanticSearchService, top_k: int) -> None:
    with pytest.raises(ValueError, match=f"between 1 and {MAX_TOP_K}"):
        semantic.search("order", top_k=top_k)


@pytest.mark.parametrize("top_k", [2.0, "3", True, None])
def test_non_integer_top_k_is_rejected(semantic: SemanticSearchService, top_k: object) -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        semantic.search("order", top_k=top_k)  # type: ignore[arg-type]


# --- Category filter -------------------------------------------------------------------------


@pytest.mark.parametrize("category", ["shipping", " Shipping ", FAQCategory.SHIPPING])
def test_category_filter(semantic: SemanticSearchService, category: str | FAQCategory) -> None:
    results = semantic.search("how long does my order take", top_k=MAX_TOP_K, category=category)

    assert sorted(ids(results)) == sorted(ACTIVE_SHIPPING_IDS)
    assert all(m.faq.category is FAQCategory.SHIPPING for m in results)


def test_category_filter_is_applied_before_top_k(semantic: SemanticSearchService) -> None:
    # The password FAQ is the overall best match; within "shipping" we still get top_k results.
    results = semantic.search("reset my password", top_k=2, category="shipping")

    assert len(results) == 2
    assert {m.faq.category for m in results} == {FAQCategory.SHIPPING}


def test_category_without_active_faqs_returns_nothing(semantic: SemanticSearchService) -> None:
    assert semantic.search("gift hamper", category="products") == []


@pytest.mark.parametrize("category", ["billing", "accounts", ""])
def test_unknown_category_is_rejected(semantic: SemanticSearchService, category: str) -> None:
    with pytest.raises(ValueError, match="Unknown FAQ category"):
        semantic.search("order", category=category)


# --- Scores and the minimum-score threshold --------------------------------------------------


def test_scores_are_finite_floats_within_cosine_range(semantic: SemanticSearchService) -> None:
    for match in semantic.search("where is my order", top_k=MAX_TOP_K):
        assert type(match.score) is float
        assert math.isfinite(match.score)
        assert -1.0 <= match.score <= 1.0


def test_scores_equal_the_cosine_similarity_formula(
    semantic: SemanticSearchService, faq_service: FAQService
) -> None:
    reference = EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader(FakeEncoder()))
    query = reference.encode_text("track my order")

    for match in semantic.search("track my order", top_k=MAX_TOP_K):
        faq_vector = reference.encode_text(match.faq.question)
        expected = np.dot(query, faq_vector) / (np.linalg.norm(query) * np.linalg.norm(faq_vector))
        assert match.score == pytest.approx(float(expected), abs=1e-6)


def test_exact_question_scores_one(semantic: SemanticSearchService) -> None:
    [top] = semantic.search(TEST_FAQ_RECORDS[6]["question"], top_k=1)

    assert top.faq.id == "faq_107"
    assert top.score == pytest.approx(1.0)


def test_scores_are_true_cosine_even_without_normalised_embeddings(
    faq_service: FAQService,
) -> None:
    faqs = faq_service.list_active_faqs()
    normalised = make_semantic(faqs).search("my order status", top_k=MAX_TOP_K)
    raw = make_semantic(faqs, normalize=False).search("my order status", top_k=MAX_TOP_K)

    assert ids(raw) == ids(normalised)
    assert [m.score for m in raw] == pytest.approx([m.score for m in normalised], abs=1e-6)


def test_min_score_filters_low_matches(semantic: SemanticSearchService) -> None:
    everything = semantic.search("reset my password", top_k=MAX_TOP_K)
    threshold = everything[2].score

    filtered = semantic.search("reset my password", top_k=MAX_TOP_K, min_score=threshold)

    assert filtered
    assert all(m.score >= threshold for m in filtered)
    assert ids(filtered) == [m.faq.id for m in everything if m.score >= threshold]


def test_min_score_of_minus_one_keeps_everything(semantic: SemanticSearchService) -> None:
    assert len(semantic.search("order", top_k=MAX_TOP_K, min_score=-1.0)) == len(ACTIVE_IDS)


def test_min_score_above_every_match_returns_empty_list(semantic: SemanticSearchService) -> None:
    assert semantic.search("xyzzy quantum banana", min_score=0.99) == []


@pytest.mark.parametrize("min_score", [1.01, -1.01, 2, 100, math.nan, math.inf, -math.inf])
def test_min_score_outside_cosine_range_is_rejected(
    semantic: SemanticSearchService, min_score: float
) -> None:
    with pytest.raises(ValueError, match="range of cosine similarity"):
        semantic.search("order", min_score=min_score)


@pytest.mark.parametrize("min_score", ["0.5", True, [0.5]])
def test_non_numeric_min_score_is_rejected(
    semantic: SemanticSearchService, min_score: object
) -> None:
    with pytest.raises(ValueError, match="must be a number"):
        semantic.search("order", min_score=min_score)  # type: ignore[arg-type]


@pytest.mark.parametrize("min_score", [0, 1, -1, 0.0])
def test_integer_and_boundary_thresholds_are_accepted(
    semantic: SemanticSearchService, min_score: float
) -> None:
    semantic.search("order", min_score=min_score)


# --- Ties ------------------------------------------------------------------------------------


def test_tied_scores_keep_knowledge_base_order() -> None:
    faqs = [
        faq("faq_201", "Where is my parcel?"),
        faq("faq_202", "Where is my parcel?"),  # identical question -> identical score
        faq("faq_203", "Where is my parcel?"),
        faq("faq_204", "Unrelated question about gift cards"),
    ]

    results = make_semantic(faqs).search("where is my parcel", top_k=4)

    assert ids(results)[:3] == ["faq_201", "faq_202", "faq_203"]
    assert results[0].score == results[1].score == results[2].score
    assert ids(make_semantic(faqs[::-1]).search("where is my parcel", top_k=4))[:3] == [
        "faq_203",
        "faq_202",
        "faq_201",
    ]


# --- Empty index and model problems ------------------------------------------------------------


def test_empty_knowledge_base_returns_nothing_without_loading_model() -> None:
    loader = CountingLoader()
    semantic = SemanticSearchService(lambda: [], EmbeddingService(FAKE_MODEL_NAME, loader=loader))

    assert semantic.search("anything") == []
    assert loader.calls == []


class BrokenQueryEncoder(FakeEncoder):
    """Embeds FAQ questions normally but returns a bad vector for one query."""

    def __init__(self, bad_vector: float) -> None:
        super().__init__()
        self.bad_vector = bad_vector

    def encode(self, sentences: list[str], **kwargs: Any) -> np.ndarray:
        if sentences == ["broken query"]:
            return np.full((1, self.dimension), self.bad_vector)
        return super().encode(sentences, **kwargs)


@pytest.mark.parametrize("bad_vector", [0.0, math.nan, math.inf])
def test_unusable_query_embedding_raises_instead_of_returning_bad_scores(
    faq_service: FAQService, bad_vector: float
) -> None:
    semantic = make_semantic(faq_service.list_active_faqs(), BrokenQueryEncoder(bad_vector))

    with pytest.raises(EmbeddingError, match="query embedding"):
        semantic.search("broken query")


def test_unusable_index_embedding_raises(faq_service: FAQService) -> None:
    class ZeroRows(FakeEncoder):
        def encode(self, sentences: list[str], **kwargs: Any) -> np.ndarray:
            vectors = super().encode(sentences, **kwargs)
            if len(sentences) > 1:
                vectors[0] = 0.0  # one FAQ embedded as a zero vector
            return vectors

    semantic = make_semantic(faq_service.list_active_faqs(), ZeroRows())

    with pytest.raises(EmbeddingError, match="indexed FAQ embedding"):
        semantic.search("order")


# --- Opt-in: real model ------------------------------------------------------------------------


@pytest.mark.embedding_model
@pytest.mark.sample_data
def test_real_model_finds_meaning_matches_keyword_search_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    from app.database.faq_repository import FAQRepository

    service = FAQService(FAQRepository.from_json_file())
    semantic = SemanticSearchService(
        service.list_active_faqs, EmbeddingService("sentence-transformers/all-MiniLM-L6-v2")
    )

    # Two of the keyword baseline's documented misses (see test_faq_service.py).
    assert semantic.search("when will my stuff arrive", top_k=1)[0].faq.id == "faq_006"
    assert semantic.search("my package is late", top_k=3)[0].faq.category is FAQCategory.SHIPPING
