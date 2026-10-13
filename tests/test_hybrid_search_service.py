"""Hybrid retrieval with Reciprocal Rank Fusion. No embedding model is ever loaded here."""

import math
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.main import create_app
from app.models.faq import FAQ, FAQCategory
from app.services.embedding_service import EmbeddingModelLoadError, EmbeddingService
from app.services.faq_search import MatchField
from app.services.faq_service import FAQService
from app.services.hybrid_search_service import (
    DEFAULT_RRF_K,
    KEYWORD,
    SEMANTIC,
    HybridSearchService,
    reciprocal_rank_fusion,
)
from app.services.keyword_search_service import (
    InvalidKeywordQueryError,
    KeywordCandidate,
    KeywordSearchService,
)
from app.services.semantic_search_service import SemanticMatch, SemanticSearchService
from tests.fakes import FAKE_MODEL_NAME, CountingLoader
from tests.faq_test_data import ACTIVE_IDS, INACTIVE_IDS, TEST_FAQ_RECORDS

FAQS = {record["id"]: FAQ.model_validate(record) for record in TEST_FAQ_RECORDS}


def rrf(*ranks: int, k: float = 60) -> float:
    return sum(1 / (k + rank) for rank in ranks)


# --- Pure fusion -----------------------------------------------------------------------------


def test_rrf_formula_with_ranks_starting_at_one() -> None:
    fused = reciprocal_rank_fusion({KEYWORD: ["a", "b"], SEMANTIC: ["b", "c"]})
    scores = {item.item_id: item.rrf_score for item in fused}

    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)  # keyword rank 2 + semantic rank 1
    assert scores["a"] == pytest.approx(1 / 61)  # keyword rank 1 only
    assert scores["c"] == pytest.approx(1 / 62)  # semantic rank 2 only
    assert [item.item_id for item in fused] == ["b", "a", "c"]


def test_fused_items_keep_their_original_ranks() -> None:
    [b, *_] = reciprocal_rank_fusion({KEYWORD: ["a", "b"], SEMANTIC: ["b", "c"]})

    assert dict(b.ranks) == {KEYWORD: 2, SEMANTIC: 1}


def test_ids_are_deduplicated_and_keep_their_best_rank() -> None:
    fused = reciprocal_rank_fusion({KEYWORD: ["a", "b", "a"], SEMANTIC: ["a"]})

    assert [item.item_id for item in fused] == ["a", "b"]
    assert dict(fused[0].ranks) == {KEYWORD: 1, SEMANTIC: 1}
    assert fused[0].rrf_score == pytest.approx(rrf(1, 1))


def test_agreement_beats_a_single_first_place() -> None:
    # Rank 3 in both lists outscores rank 1 in only one list (k=60).
    fused = reciprocal_rank_fusion({KEYWORD: ["solo", "x", "both"], SEMANTIC: ["y", "z", "both"]})

    assert fused[0].item_id == "both"
    assert fused[0].rrf_score == pytest.approx(rrf(3, 3))


def test_k_changes_how_much_lower_ranks_count() -> None:
    rankings = {KEYWORD: ["a", "x", "y", "b"], SEMANTIC: ["p", "q", "r", "b"]}

    assert reciprocal_rank_fusion(rankings, k=60)[0].item_id == "b"  # 2/64 > 1/61
    sharp = [item.item_id for item in reciprocal_rank_fusion(rankings, k=1)]
    # With k=1 a single first place (1/2) beats rank 4 in both lists (2/5). "a" and "p"
    # tie at 1/2; the tie-break prefers the better semantic rank, so "p" comes first.
    assert sharp[:2] == ["p", "a"]
    assert sharp.index("b") > 1


def test_tie_broken_by_best_individual_rank() -> None:
    # c: keyword 1 + semantic 4; d: keyword 2 + semantic 3 -> close but not tied; use exact tie:
    # e: keyword 1 only (1/61) vs f: semantic 1 only (1/61): tied; best rank both 1;
    # then semantic rank decides -> f first.
    fused = reciprocal_rank_fusion({KEYWORD: ["e"], SEMANTIC: ["f"]})

    assert fused[0].rrf_score == fused[1].rrf_score
    assert [item.item_id for item in fused] == ["f", "e"]


def test_tie_with_same_rank_pair_prefers_better_semantic_rank() -> None:
    # a: keyword 2 + semantic 3; b: keyword 3 + semantic 2 -> identical RRF and best rank 2.
    fused = reciprocal_rank_fusion({KEYWORD: ["x", "a", "b"], SEMANTIC: ["y", "b", "a"]})
    tied = [item.item_id for item in fused if item.item_id in {"a", "b"}]

    assert tied == ["b", "a"]


def test_keyword_rank_breaks_ties_before_the_id() -> None:
    fused = reciprocal_rank_fusion({KEYWORD: ["b2"], "other": ["a1"]})

    assert [item.item_id for item in fused] == ["b2", "a1"]


def test_final_tie_break_is_the_id() -> None:
    # Neither retriever is in the tie-break order, so only the ID separates them.
    fused = reciprocal_rank_fusion({"x": ["b2"], "y": ["a1"]})

    assert [item.item_id for item in fused] == ["a1", "b2"]


def test_fusion_is_deterministic_regardless_of_input_order() -> None:
    forward = reciprocal_rank_fusion({KEYWORD: ["a", "b"], SEMANTIC: ["c", "a"]})
    swapped = reciprocal_rank_fusion({SEMANTIC: ["c", "a"], KEYWORD: ["a", "b"]})

    assert [i.item_id for i in forward] == [i.item_id for i in swapped]


def test_empty_rankings() -> None:
    assert reciprocal_rank_fusion({}) == []
    assert reciprocal_rank_fusion({KEYWORD: [], SEMANTIC: []}) == []


@pytest.mark.parametrize("k", [0, -1, math.nan, math.inf, True, "60", None])
def test_invalid_k_is_rejected(k: object) -> None:
    with pytest.raises(ValueError, match="RRF k"):
        reciprocal_rank_fusion({KEYWORD: ["a"]}, k=k)  # type: ignore[arg-type]


def test_rrf_scores_are_small_rank_scores_not_probabilities() -> None:
    """With two retrievers the maximum possible RRF score is 2 / (k + 1), about 0.033."""
    [best] = reciprocal_rank_fusion({KEYWORD: ["a"], SEMANTIC: ["a"]})

    assert best.rrf_score == pytest.approx(2 / (DEFAULT_RRF_K + 1))
    assert best.rrf_score < 0.04  # a perfect agreement is nowhere near "1.0" or "100%"


# --- Service with fake retrievers -------------------------------------------------------------


class FakeKeyword:
    def __init__(self, ranked_ids: list[str], error: Exception | None = None) -> None:
        self.ranked_ids = ranked_ids
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def search(self, query: str, *, top_k: int, category: Any) -> list[KeywordCandidate]:
        self.calls.append({"query": query, "top_k": top_k, "category": category})
        if self.error:
            raise self.error
        return [
            KeywordCandidate(
                faq_id=faq_id,
                rank=rank,
                score=100 - rank,
                exact_question=rank == 1,
                matched_fields=frozenset({MatchField.QUESTION}),
                reasons=(f"fake reason {rank}",),
                faq=FAQS[faq_id],
            )
            for rank, faq_id in enumerate(self.ranked_ids[:top_k], start=1)
        ]


class FakeSemantic:
    def __init__(self, ranked: list[tuple[str, float]], error: Exception | None = None) -> None:
        self.ranked = ranked
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def search(
        self, query: str, *, top_k: int, category: Any, min_score: float | None
    ) -> list[SemanticMatch]:
        self.calls.append(
            {"query": query, "top_k": top_k, "category": category, "min_score": min_score}
        )
        if self.error:
            raise self.error
        return [SemanticMatch(faq=FAQS[i], score=s) for i, s in self.ranked[:top_k]]


def hybrid(keyword_ids: list[str], semantic: list[tuple[str, float]], **kwargs: Any):
    keyword, sem = FakeKeyword(keyword_ids), FakeSemantic(semantic)
    return HybridSearchService(keyword, sem, **kwargs), keyword, sem


def test_results_are_fused_deduplicated_and_ranked() -> None:
    service, _, _ = hybrid(
        ["faq_101", "faq_102", "faq_103"], [("faq_102", 0.81), ("faq_104", 0.62)]
    )

    results = service.search("query", top_k=5)

    assert [r.faq_id for r in results] == ["faq_102", "faq_101", "faq_104", "faq_103"]
    assert [r.rank for r in results] == [1, 2, 3, 4]
    assert results[0].rrf_score == pytest.approx(rrf(2, 1))
    scores = [r.rrf_score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_original_ranking_information_is_preserved() -> None:
    service, _, _ = hybrid(["faq_101", "faq_102"], [("faq_102", 0.8123), ("faq_104", 0.5)])

    by_id = {r.faq_id: r for r in service.search("query")}

    both = by_id["faq_102"]
    assert both.retrievers == (KEYWORD, SEMANTIC)
    assert (both.keyword.rank, both.keyword.score, both.keyword.exact_question) == (2, 98, False)
    assert both.keyword.reasons == ("fake reason 2",)
    assert (both.semantic.rank, both.semantic.similarity) == (1, 0.8123)
    assert by_id["faq_101"].semantic is None and by_id["faq_101"].retrievers == (KEYWORD,)
    assert by_id["faq_104"].keyword is None and by_id["faq_104"].retrievers == (SEMANTIC,)
    assert by_id["faq_104"].faq is FAQS["faq_104"]


def test_top_k_limits_results() -> None:
    service, _, _ = hybrid(["faq_101", "faq_102", "faq_103"], [("faq_104", 0.5)])

    assert len(service.search("query", top_k=2)) == 2


def test_no_candidates_returns_empty_list() -> None:
    service, _, _ = hybrid([], [])

    assert service.search("query") == []


def test_candidate_depth_and_full_semantic_ranking_are_requested() -> None:
    service, keyword, semantic = hybrid([], [], candidate_depth=7)

    service.search("query", top_k=3)

    assert keyword.calls[0]["top_k"] == semantic.calls[0]["top_k"] == 7
    assert semantic.calls[0]["min_score"] == -1.0  # no cosine cut-off before fusion by default


def test_semantic_candidate_cutoff_is_configurable() -> None:
    service, _, semantic = hybrid([], [], semantic_candidate_min_similarity=0.25)

    service.search("query")

    assert semantic.calls[0]["min_score"] == 0.25


@pytest.mark.parametrize("category", ["shipping", " Shipping ", FAQCategory.SHIPPING])
def test_category_is_passed_to_both_retrievers(category: Any) -> None:
    service, keyword, semantic = hybrid([], [])

    service.search("query", category=category)

    assert keyword.calls[0]["category"] is FAQCategory.SHIPPING
    assert semantic.calls[0]["category"] is FAQCategory.SHIPPING


def test_unknown_category_is_rejected_before_any_retrieval() -> None:
    service, keyword, semantic = hybrid([], [])

    with pytest.raises(ValueError, match="Unknown FAQ category"):
        service.search("query", category="billing")
    assert keyword.calls == semantic.calls == []


@pytest.mark.parametrize("top_k", [0, -1, 21, 2.0, "3", True])
def test_invalid_top_k_is_rejected(top_k: Any) -> None:
    service, keyword, _ = hybrid([], [], candidate_depth=20)

    with pytest.raises(ValueError, match="top_k"):
        service.search("query", top_k=top_k)
    assert keyword.calls == []


def test_invalid_query_never_reaches_the_semantic_model(faq_service: FAQService) -> None:
    semantic = FakeSemantic([])
    service = HybridSearchService(KeywordSearchService(faq_service.list_active_faqs), semantic)

    with pytest.raises(InvalidKeywordQueryError):
        service.search("   ")
    assert semantic.calls == []


def test_semantic_model_failure_propagates() -> None:
    service = HybridSearchService(
        FakeKeyword(["faq_101"]), FakeSemantic([], error=EmbeddingModelLoadError("offline"))
    )

    with pytest.raises(EmbeddingModelLoadError):
        service.search("query")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rrf_k": 0},
        {"rrf_k": -5},
        {"candidate_depth": 0},
        {"candidate_depth": 51},
        {"semantic_candidate_min_similarity": 1.5},
    ],
)
def test_invalid_configuration_is_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        HybridSearchService(FakeKeyword([]), FakeSemantic([]), **kwargs)


def test_rrf_k_changes_the_fused_order() -> None:
    keyword_ids = ["faq_101", "faq_102", "faq_103", "faq_104"]
    semantic = [("faq_106", 0.9), ("faq_107", 0.8), ("faq_108", 0.7), ("faq_104", 0.6)]

    default = hybrid(keyword_ids, semantic)[0].search("query", top_k=1)
    sharp = hybrid(keyword_ids, semantic, rrf_k=1)[0].search("query", top_k=1)

    assert default[0].faq_id == "faq_104"  # rank 4 in both lists
    assert sharp[0].faq_id == "faq_106"  # k=1: first places win; semantic breaks the tie


# --- End to end with the real services (fake embedding model) ----------------------------------


@pytest.fixture
def real_hybrid(faq_service: FAQService) -> HybridSearchService:
    semantic = SemanticSearchService(
        faq_service.list_active_faqs, EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader())
    )
    return HybridSearchService(KeywordSearchService(faq_service.list_active_faqs), semantic)


def test_exact_question_is_first_and_found_by_both(real_hybrid: HybridSearchService) -> None:
    [top, *_] = real_hybrid.search("How do I reset my password?")

    assert top.faq_id == "faq_107"
    assert top.keyword.rank == 1 and top.keyword.exact_question
    assert top.semantic.rank == 1 and top.semantic.similarity == pytest.approx(1.0)
    assert top.rrf_score == pytest.approx(rrf(1, 1))


def test_inactive_faqs_never_appear(real_hybrid: HybridSearchService) -> None:
    inactive_question = next(r["question"] for r in TEST_FAQ_RECORDS if r["id"] == "faq_105")

    ids = {r.faq_id for r in real_hybrid.search(inactive_question, top_k=20)}

    assert ids.isdisjoint(INACTIVE_IDS)
    assert ids <= set(ACTIVE_IDS)


def test_category_filter_end_to_end(real_hybrid: HybridSearchService) -> None:
    results = real_hybrid.search("order delivery password", top_k=10, category="shipping")

    assert results and all(r.faq.category is FAQCategory.SHIPPING for r in results)


def test_semantic_only_results_are_included(real_hybrid: HybridSearchService) -> None:
    # A query with no keyword matches still gets semantic candidates.
    results = real_hybrid.search("xyzzy quantum", top_k=3)

    assert results and all(r.retrievers == (SEMANTIC,) for r in results)


# --- Configuration and wiring ------------------------------------------------------------------


def test_settings_defaults_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None)
    assert (settings.hybrid_rrf_k, settings.hybrid_candidate_depth) == (60, 20)
    assert settings.safe_summary()["hybrid_rrf_k"] == 60

    for name, value in (
        ("HYBRID_RRF_K", "0"),
        ("HYBRID_RRF_K", "1001"),
        ("HYBRID_CANDIDATE_DEPTH", "0"),
        ("HYBRID_CANDIDATE_DEPTH", "51"),
    ):
        monkeypatch.setenv(name, value)
        with pytest.raises(ValidationError):
            Settings(_env_file=None)
        monkeypatch.delenv(name)


def test_app_wires_hybrid_service_from_settings(make_settings: Callable[..., Settings]) -> None:
    app = create_app(make_settings(hybrid_rrf_k=30, hybrid_candidate_depth=12))
    service = app.state.hybrid_search_service

    assert isinstance(service, HybridSearchService)
    assert (service.rrf_k, service.candidate_depth) == (30, 12)
    assert app.state.semantic_search_service.is_ready is False  # nothing loaded at startup


def test_dependency_provides_the_app_hybrid_service(make_settings: Callable[..., Settings]) -> None:
    from fastapi.testclient import TestClient

    from app.api.dependencies import HybridSearchServiceDep

    app = create_app(make_settings())

    @app.get("/probe")
    def probe(service: HybridSearchServiceDep) -> dict[str, bool]:
        return {"same": service is app.state.hybrid_search_service}

    assert TestClient(app).get("/probe").json() == {"same": True}
    del app.state.hybrid_search_service
    assert TestClient(app).get("/probe").status_code == 500
