"""Answer decision layer: answered / no_match / ambiguous / retrieval_unavailable."""

import logging
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.main import create_app
from app.models.faq import FAQ, FAQCategory
from app.services.answer_decision_service import (
    NO_MATCH_MESSAGE,
    UNAVAILABLE_MESSAGE,
    AnswerDecisionService,
    Decision,
    RetrievalMode,
)
from app.services.embedding_service import (
    EmbeddingError,
    EmbeddingModelLoadError,
    EmbeddingService,
)
from app.services.faq_search import MatchField
from app.services.faq_service import FAQService
from app.services.hybrid_search_service import (
    HybridResult,
    HybridSearchService,
    KeywordRank,
    SemanticRank,
)
from app.services.keyword_search_service import (
    InvalidKeywordQueryError,
    KeywordCandidate,
    KeywordSearchService,
)
from app.services.semantic_search_service import SemanticSearchService
from tests.fakes import FAKE_MODEL_NAME, CountingLoader
from tests.faq_test_data import TEST_FAQ_RECORDS

FAQS = {r["id"]: FAQ.model_validate(r) for r in TEST_FAQ_RECORDS}


def result(
    faq_id: str,
    *,
    similarity: float | None = None,
    keyword_score: int | None = None,
    exact: bool = False,
    rrf: float = 0.03,
) -> tuple:
    """Spec for one hybrid result: (faq_id, similarity, keyword_score, exact, rrf)."""
    return (faq_id, similarity, keyword_score, exact, rrf)


class FakeHybrid:
    def __init__(self, specs: list[tuple], error: Exception | None = None) -> None:
        self.specs = specs
        self.error = error
        self.candidate_depth = 20
        self.calls: list[dict[str, Any]] = []

    def search(self, query: str, *, top_k: int, category: Any) -> list[HybridResult]:
        self.calls.append({"query": query, "top_k": top_k, "category": category})
        if self.error:
            raise self.error
        results = []
        for rank, (faq_id, similarity, kw_score, exact, rrf) in enumerate(self.specs, start=1):
            keyword = (
                None
                if kw_score is None and not exact
                else KeywordRank(rank=rank, score=kw_score or 100, exact_question=exact, reasons=())
            )
            semantic = (
                None if similarity is None else SemanticRank(rank=rank, similarity=similarity)
            )
            results.append(HybridResult(faq_id, rank, rrf, FAQS[faq_id], keyword, semantic))
        return results[:top_k]


class FakeKeyword:
    def __init__(self, specs: list[tuple[str, int, bool]] | None = None) -> None:
        self.specs = specs or []
        self.calls: list[dict[str, Any]] = []

    def search(self, query: str, *, top_k: int, category: Any) -> list[KeywordCandidate]:
        self.calls.append({"query": query, "top_k": top_k, "category": category})
        return [
            KeywordCandidate(
                faq_id, rank, score, exact, frozenset({MatchField.QUESTION}), (), FAQS[faq_id]
            )
            for rank, (faq_id, score, exact) in enumerate(self.specs[:top_k], start=1)
        ]


def decider(specs: list[tuple], keyword: FakeKeyword | None = None, **kwargs: Any):
    hybrid = FakeHybrid(specs, kwargs.pop("error", None))
    keyword = keyword or FakeKeyword()
    return AnswerDecisionService(hybrid, keyword, **kwargs), hybrid, keyword  # type: ignore[arg-type]


# --- Answered ------------------------------------------------------------------------------------


def test_answers_with_the_approved_answer_when_similarity_meets_threshold() -> None:
    service, _, _ = decider(
        [result("faq_107", similarity=0.72), result("faq_101", similarity=0.31)]
    )

    decision = service.decide("I forgot my password")

    assert decision.decision is Decision.ANSWERED
    assert decision.faq is FAQS["faq_107"]
    assert decision.message == FAQS["faq_107"].answer  # verbatim
    assert decision.retrieval_mode is RetrievalMode.HYBRID and not decision.degraded
    assert decision.evidence.semantic_similarity == 0.72
    assert decision.reasons == ("semantic similarity 0.720 >= 0.4",)


def test_exact_question_match_is_answered_even_with_low_similarity() -> None:
    service, _, _ = decider([result("faq_101", similarity=0.10, exact=True)])

    decision = service.decide("How can I track my order?")

    assert decision.decision is Decision.ANSWERED
    assert decision.reasons == ("exact question match",)


def test_exact_question_match_is_never_ambiguous() -> None:
    service, _, _ = decider(
        [result("faq_101", similarity=0.70, exact=True), result("faq_102", similarity=0.70)]
    )

    assert service.decide("q").decision is Decision.ANSWERED


def test_threshold_is_inclusive() -> None:
    service, _, _ = decider([result("faq_107", similarity=0.39999995)], min_similarity=0.4)

    assert service.decide("q").decision is Decision.ANSWERED


def test_rival_below_threshold_does_not_cause_ambiguity() -> None:
    service, _, _ = decider(
        [result("faq_107", similarity=0.42), result("faq_101", similarity=0.39)]
    )

    assert service.decide("q").decision is Decision.ANSWERED


def test_clear_winner_beyond_margin_is_answered() -> None:
    service, _, _ = decider(
        [result("faq_107", similarity=0.80), result("faq_101", similarity=0.74)]
    )

    assert service.decide("q").decision is Decision.ANSWERED


# --- No match -------------------------------------------------------------------------------------


def test_no_candidates_is_no_match() -> None:
    service, _, _ = decider([])

    decision = service.decide("xyzzy")

    assert decision.decision is Decision.NO_MATCH
    assert decision.message == NO_MATCH_MESSAGE
    assert decision.faq is None


def test_low_similarity_is_no_match() -> None:
    service, _, _ = decider(
        [result("faq_101", similarity=0.30), result("faq_102", similarity=0.20)]
    )

    decision = service.decide("q")

    assert decision.decision is Decision.NO_MATCH
    assert "semantic similarity >= 0.4" in decision.reasons[0]
    assert [o.faq.id for o in decision.options] == ["faq_101", "faq_102"]  # for diagnostics


def test_high_rrf_alone_is_not_enough() -> None:
    # Top RRF score with strong keyword points but weak semantic evidence and no exact match.
    service, _, _ = decider([result("faq_101", similarity=0.20, keyword_score=95, rrf=0.0328)])

    assert service.decide("q").decision is Decision.NO_MATCH


def test_keyword_only_candidate_without_exact_match_is_not_enough() -> None:
    service, _, _ = decider([result("faq_101", keyword_score=60)])

    assert service.decide("q").decision is Decision.NO_MATCH


# --- Ambiguous ------------------------------------------------------------------------------------


def test_close_candidates_in_different_categories_ask_about_categories() -> None:
    service, _, _ = decider(
        [
            result("faq_101", similarity=0.62),  # orders
            result("faq_103", similarity=0.60),  # shipping
            result("faq_106", similarity=0.59),  # returns
        ]
    )

    decision = service.decide("my order hasn't arrived")

    assert decision.decision is Decision.AMBIGUOUS
    assert decision.message == (
        "Could you clarify whether your question is about orders, shipping or returns?"
    )
    assert [o.faq.id for o in decision.options] == ["faq_101", "faq_103", "faq_106"]
    assert decision.faq is None


def test_close_candidates_in_one_category_ask_with_their_questions() -> None:
    service, _, _ = decider(
        [result("faq_103", similarity=0.66), result("faq_104", similarity=0.64)]
    )

    decision = service.decide("shipping question")

    assert decision.decision is Decision.AMBIGUOUS
    assert decision.message == (
        'Could you clarify which of these you mean: "How long does delivery take?" or '
        '"Do you ship internationally?"'
    )


def test_clarification_only_mentions_competing_candidates() -> None:
    """No category or question is invented: only the close candidates are offered."""
    service, _, _ = decider(
        [
            result("faq_101", similarity=0.62),  # orders
            result("faq_103", similarity=0.60),  # shipping (close)
            result("faq_107", similarity=0.45),  # account (acceptable but not close)
        ]
    )

    decision = service.decide("q")

    assert (
        decision.message == "Could you clarify whether your question is about orders or shipping?"
    )
    assert "account" not in decision.message


def test_rival_scoring_above_the_top_candidate_is_ambiguous() -> None:
    # RRF put faq_101 first, but semantic similarity strongly favours faq_107.
    service, _, _ = decider(
        [result("faq_101", similarity=0.45), result("faq_107", similarity=0.80)]
    )

    decision = service.decide("q")

    assert decision.decision is Decision.AMBIGUOUS
    assert "above" in decision.reasons[0]


def test_clarification_offers_at_most_three_options() -> None:
    specs = [result(i, similarity=0.6) for i in ("faq_101", "faq_102", "faq_103", "faq_104")]
    service, _, _ = decider(specs)

    assert len(service.decide("q").options) == 3


def test_ambiguity_margin_is_configurable() -> None:
    specs = [result("faq_107", similarity=0.70), result("faq_101", similarity=0.62)]

    assert decider(specs)[0].decide("q").decision is Decision.ANSWERED  # gap 0.08 > 0.05
    wide = decider(specs, ambiguity_margin=0.10)[0]
    assert wide.decide("q").decision is Decision.AMBIGUOUS


# --- Retrieval unavailable (degraded mode) --------------------------------------------------------


def test_model_failure_with_exact_keyword_match_answers_but_is_flagged_degraded() -> None:
    keyword = FakeKeyword([("faq_107", 115, True), ("faq_101", 5, False)])
    error = EmbeddingModelLoadError("offline", retry_after_seconds=30)
    service, _, _ = decider([], keyword, error=error)

    decision = service.decide("How do I reset my password?")

    assert decision.decision is Decision.ANSWERED
    assert decision.degraded is True
    assert decision.retrieval_mode is RetrievalMode.KEYWORD_ONLY
    assert decision.message == FAQS["faq_107"].answer
    assert "semantic retrieval unavailable" in decision.reasons[0]
    assert decision.evidence.semantic_similarity is None
    assert decision.retry_after_seconds == 30


def test_model_failure_single_word_query_is_not_strong_enough() -> None:
    keyword = FakeKeyword([("faq_103", 55, False)])
    service, _, _ = decider([], keyword, error=EmbeddingError("bad vectors"))

    decision = service.decide("delivery")

    assert decision.decision is Decision.RETRIEVAL_UNAVAILABLE


def test_model_failure_with_clear_strong_keyword_match_answers_degraded() -> None:
    keyword = FakeKeyword([("faq_107", 50, False), ("faq_101", 10, False)])
    service, _, _ = decider([], keyword, error=EmbeddingError("bad vectors"))

    decision = service.decide("reset my password")

    assert decision.decision is Decision.ANSWERED and decision.degraded


@pytest.mark.parametrize(
    "specs",
    [
        [],  # nothing found
        [("faq_107", 39, False)],  # below the degraded keyword threshold
        [("faq_107", 50, False), ("faq_101", 50, False)],  # tie: no clear winner
    ],
    ids=["no-results", "weak", "tie"],
)
def test_model_failure_without_strong_lexical_evidence_is_unavailable(specs: list) -> None:
    error = EmbeddingModelLoadError("offline", retry_after_seconds=12)
    service, _, _ = decider([], FakeKeyword(specs), error=error)

    decision = service.decide("query")

    assert decision.decision is Decision.RETRIEVAL_UNAVAILABLE
    assert decision.message == UNAVAILABLE_MESSAGE
    assert decision.faq is None
    assert decision.degraded is True
    assert decision.retry_after_seconds == 12


def test_failure_is_logged_without_the_query(caplog: pytest.LogCaptureFixture) -> None:
    service, _, _ = decider([], FakeKeyword(), error=EmbeddingModelLoadError("C:/models missing"))

    with caplog.at_level(logging.WARNING):
        service.decide("jane.doe@example.com order 99812 problem")

    assert "Semantic retrieval unavailable (EmbeddingModelLoadError)" in caplog.text
    assert "jane.doe" not in caplog.text and "99812" not in caplog.text


def test_category_is_used_in_degraded_mode_too() -> None:
    keyword = FakeKeyword()
    service, _, _ = decider([], keyword, error=EmbeddingError("x"))

    service.decide("q", category="Shipping")

    assert keyword.calls[0]["category"] is FAQCategory.SHIPPING


# --- Inputs and configuration --------------------------------------------------------------------


def test_category_and_candidate_count_are_passed_to_hybrid_search() -> None:
    service, hybrid, _ = decider([], candidates=4)

    service.decide("q", category=" orders ")

    assert hybrid.calls == [{"query": "q", "top_k": 4, "category": FAQCategory.ORDERS}]


def test_unknown_category_is_rejected() -> None:
    service, hybrid, _ = decider([])

    with pytest.raises(ValueError, match="Unknown FAQ category"):
        service.decide("q", category="billing")
    assert hybrid.calls == []


def test_invalid_query_is_an_error_not_a_degraded_answer() -> None:
    hybrid = FakeHybrid([], error=InvalidKeywordQueryError("query must not be empty"))
    service = AnswerDecisionService(hybrid, FakeKeyword())  # type: ignore[arg-type]

    with pytest.raises(InvalidKeywordQueryError):
        service.decide("   ")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_similarity": 1.5},
        {"ambiguity_margin": -0.1},
        {"degraded_min_keyword_score": 0},
        {"candidates": 1},
        {"candidates": 21},
        {"min_similarity": "0.4"},
    ],
)
def test_invalid_configuration_is_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        AnswerDecisionService(FakeHybrid([]), FakeKeyword(), **kwargs)  # type: ignore[arg-type]


def test_settings_defaults_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None)
    assert (settings.answer_ambiguity_margin, settings.degraded_min_keyword_score) == (0.05, 40)

    for name, value in (
        ("ANSWER_AMBIGUITY_MARGIN", "1.5"),
        ("DEGRADED_MIN_KEYWORD_SCORE", "0"),
        ("HYBRID_CANDIDATE_DEPTH", "1"),
    ):
        monkeypatch.setenv(name, value)
        with pytest.raises(ValidationError):
            Settings(_env_file=None)
        monkeypatch.delenv(name)


def test_app_wires_the_decision_service(make_settings: Callable[..., Settings]) -> None:
    app = create_app(
        make_settings(
            semantic_min_similarity=0.55,
            answer_ambiguity_margin=0.1,
            degraded_min_keyword_score=45,
            hybrid_candidate_depth=3,
        )
    )
    service = app.state.answer_decision_service

    assert isinstance(service, AnswerDecisionService)
    assert (service.min_similarity, service.ambiguity_margin) == (0.55, 0.1)
    assert (service.degraded_min_keyword_score, service.candidates) == (45, 3)


def test_dependency_provides_the_app_service(make_settings: Callable[..., Settings]) -> None:
    from fastapi.testclient import TestClient

    from app.api.dependencies import AnswerDecisionServiceDep

    app = create_app(make_settings())

    @app.get("/probe")
    def probe(service: AnswerDecisionServiceDep) -> dict[str, bool]:
        return {"same": service is app.state.answer_decision_service}

    assert TestClient(app).get("/probe").json() == {"same": True}
    del app.state.answer_decision_service
    assert TestClient(app).get("/probe").status_code == 500


# --- End to end with real services (fake embedding model) ---------------------------------------


def real_decider(faq_service: FAQService, loader: Any = None) -> AnswerDecisionService:
    keyword = KeywordSearchService(faq_service.list_active_faqs)
    semantic = SemanticSearchService(
        faq_service.list_active_faqs,
        EmbeddingService(FAKE_MODEL_NAME, loader=loader or CountingLoader()),
    )
    return AnswerDecisionService(HybridSearchService(keyword, semantic), keyword)


def test_end_to_end_exact_question_is_answered(faq_service: FAQService) -> None:
    decision = real_decider(faq_service).decide("How do I reset my password?")

    assert decision.decision is Decision.ANSWERED
    assert decision.faq.id == "faq_107"
    assert decision.reasons == ("exact question match",)


def test_end_to_end_unrelated_question_is_no_match(faq_service: FAQService) -> None:
    # Hand-chosen vectors (see test_semantic_controlled.py) make "unrelated" exact; the
    # 16-dimension bag-of-words fake can give unrelated words similar vectors by chance.
    from tests.test_semantic_controlled import ControlledEncoder, vectors

    loader = CountingLoader(ControlledEncoder(vectors()))
    decision = real_decider(faq_service, loader=loader).decide("unrelated query")

    assert decision.decision is Decision.NO_MATCH


def test_end_to_end_model_down_uses_keyword_only(faq_service: FAQService) -> None:
    def broken_loader(name: str) -> Any:
        raise OSError("no network")

    service = real_decider(faq_service, loader=broken_loader)

    exact = service.decide("How do I reset my password?")
    one_word = service.decide("delivery")  # 55 keyword points, but a single word
    phrase = service.decide("how long does delivery take international")  # no clear phrase

    assert (exact.decision, exact.degraded) == (Decision.ANSWERED, True)
    assert one_word.decision is Decision.RETRIEVAL_UNAVAILABLE
    assert phrase.decision is Decision.RETRIEVAL_UNAVAILABLE
