"""Answer decision layer: answered / no_match / ambiguous / retrieval_unavailable."""

import logging
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.main import create_app
from app.models.faq import FAQCategory
from app.services.answer_decision_service import (
    NO_MATCH_MESSAGE,
    UNAVAILABLE_MESSAGE,
    AcceptancePolicy,
    AmbiguityPolicy,
    AnswerDecisionService,
    Decision,
    FallbackMessages,
    RetrievalMode,
    clarification_message,
)
from app.services.embedding_service import (
    EmbeddingError,
    EmbeddingModelLoadError,
    EmbeddingService,
)
from app.services.faq_service import FAQService
from app.services.hybrid_search_service import (
    HybridSearchService,
)
from app.services.keyword_search_service import (
    InvalidKeywordQueryError,
    KeywordSearchService,
)
from app.services.semantic_search_service import SemanticSearchService
from tests.fakes import FAKE_MODEL_NAME, FAQS, CountingLoader, FakeHybrid, FakeKeyword, result


def decider(specs: list[tuple], keyword: FakeKeyword | None = None, **kwargs: Any):
    hybrid = FakeHybrid(specs, kwargs.pop("error", None))
    keyword = keyword or FakeKeyword()
    service = AnswerDecisionService(hybrid, keyword, **kwargs)  # type: ignore[arg-type]
    return service, hybrid, keyword


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
    service, _, _ = decider(
        [result("faq_107", similarity=0.39999995)],
        acceptance=AcceptancePolicy(min_similarity=0.4),
    )

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
    wide = decider(specs, ambiguity=AmbiguityPolicy(margin=0.10))[0]
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


# --- Safety properties -------------------------------------------------------------------------

SCENARIOS = {  # name: (hybrid specs, keyword specs if the model is down, expected decision)
    "answered": (
        [result("faq_107", similarity=0.72), result("faq_101", similarity=0.31)],
        None,
        Decision.ANSWERED,
    ),
    "exact": ([result("faq_101", similarity=0.1, exact=True)], None, Decision.ANSWERED),
    "ambiguous_categories": (
        [result("faq_101", similarity=0.62), result("faq_103", similarity=0.6)],
        None,
        Decision.AMBIGUOUS,
    ),
    "ambiguous_questions": (
        [result("faq_103", similarity=0.66), result("faq_104", similarity=0.64)],
        None,
        Decision.AMBIGUOUS,
    ),
    "no_match": ([result("faq_101", similarity=0.2, keyword_score=90)], None, Decision.NO_MATCH),
    "empty": ([], None, Decision.NO_MATCH),
    "degraded_answer": ([], [("faq_107", 115, True)], Decision.ANSWERED),
    "degraded_unavailable": (
        [],
        [("faq_103", 55, False), ("faq_104", 55, False)],  # tied: no clear winner
        Decision.RETRIEVAL_UNAVAILABLE,
    ),
}


@pytest.mark.parametrize("name", SCENARIOS)
def test_messages_are_only_approved_answers_fallbacks_or_clarifications(name: str) -> None:
    """No hallucination: every message comes verbatim from the FAQs or the configured texts."""
    specs, keyword_specs, expected_decision = SCENARIOS[name]
    messages = FallbackMessages(no_match="No match text.", unavailable="Unavailable text.")
    error = EmbeddingError("down") if keyword_specs is not None else None
    service, _, _ = decider(specs, FakeKeyword(keyword_specs), messages=messages, error=error)
    retrieved_ids = {spec[0] for spec in specs} | {spec[0] for spec in keyword_specs or []}

    decision = service.decide("customer question")

    assert decision.decision is expected_decision
    if decision.decision is Decision.ANSWERED:
        assert decision.faq is FAQS[decision.faq.id]
        assert decision.faq.id in retrieved_ids
        assert decision.message == decision.faq.answer
        return
    assert decision.faq is None
    assert not any(faq.answer in decision.message for faq in FAQS.values())
    if decision.decision is Decision.AMBIGUOUS:
        assert {o.faq.id for o in decision.options} <= retrieved_ids
        assert decision.message == clarification_message(list(decision.options))
    else:
        assert (
            decision.message
            == {
                Decision.NO_MATCH: "No match text.",
                Decision.RETRIEVAL_UNAVAILABLE: "Unavailable text.",
            }[decision.decision]
        )


@pytest.mark.parametrize("rrf", [1e-9, 0.0164, 0.0328, 0.5, 1.0, 1000.0])
def test_rrf_values_never_change_the_decision(rrf: float) -> None:
    """RRF orders candidates; its size is not confidence and is never thresholded."""
    cases = [
        [result("faq_107", similarity=0.72, rrf=rrf), result("faq_101", similarity=0.31, rrf=rrf)],
        [result("faq_101", similarity=0.2, keyword_score=90, rrf=rrf)],
        [result("faq_103", similarity=0.66, rrf=rrf), result("faq_104", similarity=0.64, rrf=rrf)],
    ]
    baseline = [decider(specs)[0].decide("q") for specs in cases]

    for specs, expected in zip(cases, baseline, strict=True):
        decision = decider(specs)[0].decide("q")
        assert (decision.decision, decision.faq, decision.message) == (
            expected.decision,
            expected.faq,
            expected.message,
        )
    assert [d.decision for d in baseline] == [
        Decision.ANSWERED,
        Decision.NO_MATCH,
        Decision.AMBIGUOUS,
    ]
    assert all("rrf" not in reason.lower() for d in baseline for reason in d.reasons)


def test_top_rrf_candidate_without_relevance_is_skipped_for_a_relevant_one() -> None:
    service, _, _ = decider(
        [
            result("faq_101", similarity=0.15, keyword_score=95, rrf=0.0328),  # best ranked
            result("faq_107", similarity=0.71, rrf=0.0161),
        ]
    )

    decision = service.decide("q")

    assert decision.decision is Decision.ANSWERED
    assert decision.faq is FAQS["faq_107"]


# --- Inputs and configuration --------------------------------------------------------------------


def test_category_and_candidate_count_are_passed_to_hybrid_search() -> None:
    service, hybrid, _ = decider([], candidates=4)

    service.decide("q", category=" orders ")

    assert hybrid.calls == [{"query": "q", "top_k": 4, "category": FAQCategory.ORDERS}]


def test_top_k_widens_the_evidence_but_not_the_decision() -> None:
    specs = [result("faq_107", similarity=0.72), result("faq_101", similarity=0.31)]
    specs += [result("faq_103", similarity=0.75), result("faq_104", similarity=0.2)]
    service, hybrid, _ = decider(specs, candidates=2)

    narrow = service.decide("q", top_k=1)
    wide = service.decide("q", top_k=4)

    assert [call["top_k"] for call in hybrid.calls] == [2, 4]
    assert [e.faq.id for e in narrow.retrieved] == ["faq_107"]
    assert [e.faq.id for e in wide.retrieved] == ["faq_107", "faq_101", "faq_103", "faq_104"]
    # faq_103 is beyond the 2 evaluated candidates, so it cannot make the decision ambiguous.
    assert narrow.decision is wide.decision is Decision.ANSWERED
    assert narrow.faq is wide.faq is FAQS["faq_107"]


def test_retrieved_defaults_to_the_evaluated_candidates() -> None:
    specs = [result(faq_id, similarity=0.1) for faq_id in ("faq_101", "faq_102", "faq_103")]
    service, _, _ = decider(specs, candidates=2)

    assert [e.faq.id for e in service.decide("q").retrieved] == ["faq_101", "faq_102"]


def test_degraded_mode_returns_keyword_evidence() -> None:
    keyword = FakeKeyword([("faq_107", 115, True), ("faq_101", 5, False), ("faq_102", 3, False)])
    service, _, _ = decider([], keyword, error=EmbeddingError("x"))

    decision = service.decide("How do I reset my password?", top_k=3)

    assert keyword.calls[0]["top_k"] == 3
    assert [e.faq.id for e in decision.retrieved] == ["faq_107", "faq_101", "faq_102"]
    assert all(e.rrf_score is None and e.semantic_similarity is None for e in decision.retrieved)
    assert decision.faq is FAQS["faq_107"]


@pytest.mark.parametrize("top_k", [0, 21, True, "3"])
def test_invalid_top_k_is_rejected(top_k: Any) -> None:
    service, hybrid, _ = decider([])

    assert service.max_top_k == hybrid.candidate_depth == 20
    with pytest.raises(ValueError, match="top_k"):
        service.decide("q", top_k=top_k)
    assert hybrid.calls == []


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
    ("factory", "kwargs"),
    [
        (AcceptancePolicy, {"min_similarity": 1.5}),
        (AcceptancePolicy, {"min_similarity": "0.4"}),
        (AcceptancePolicy, {"accept_exact_question": "yes"}),
        (AcceptancePolicy, {"degraded_min_keyword_score": 0}),
        (AcceptancePolicy, {"degraded_min_query_terms": 0}),
        (AmbiguityPolicy, {"margin": -0.1}),
        (AmbiguityPolicy, {"margin": float("nan")}),
        (AmbiguityPolicy, {"max_options": 1}),
        (FallbackMessages, {"no_match": "   "}),
        (FallbackMessages, {"unavailable": "x" * 501}),
    ],
)
def test_invalid_policies_are_rejected(factory: Any, kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        factory(**kwargs)


@pytest.mark.parametrize("candidates", [1, 21, True, 2.5])
def test_invalid_candidate_count_is_rejected(candidates: Any) -> None:
    with pytest.raises(ValueError, match="candidates"):
        AnswerDecisionService(FakeHybrid([]), FakeKeyword(), candidates=candidates)  # type: ignore[arg-type]


# --- Configurable rules ---------------------------------------------------------------------------


def test_exact_question_acceptance_can_be_disabled() -> None:
    specs = [result("faq_101", similarity=0.10, exact=True)]
    strict = decider(specs, acceptance=AcceptancePolicy(accept_exact_question=False))[0]

    decision = strict.decide("How can I track my order?")

    assert decision.decision is Decision.NO_MATCH
    assert decision.reasons == ("no candidate had semantic similarity >= 0.4",)


def test_exact_question_disabled_also_applies_in_degraded_mode() -> None:
    keyword = FakeKeyword([("faq_107", 115, True)])
    service, _, _ = decider(
        [],
        keyword,
        error=EmbeddingError("x"),
        acceptance=AcceptancePolicy(accept_exact_question=False, degraded_min_keyword_score=500),
    )

    assert service.decide("How do I reset my password?").decision is (
        Decision.RETRIEVAL_UNAVAILABLE
    )


def test_degraded_min_query_terms_is_configurable() -> None:
    keyword = FakeKeyword([("faq_103", 55, False)])
    lenient = decider(
        [],
        keyword,
        error=EmbeddingError("x"),
        acceptance=AcceptancePolicy(degraded_min_query_terms=1),
    )[0]

    assert lenient.decide("delivery").decision is Decision.ANSWERED


def test_max_clarification_options_is_configurable() -> None:
    specs = [result(i, similarity=0.6) for i in ("faq_101", "faq_102", "faq_103", "faq_104")]
    two = decider(specs, ambiguity=AmbiguityPolicy(max_options=2))[0]

    assert len(two.decide("q").options) == 2


def test_fallback_messages_are_configurable() -> None:
    messages = FallbackMessages(no_match="No answer found.", unavailable="Search is down.")
    no_match = decider([], messages=messages)[0]
    unavailable = decider([], FakeKeyword(), error=EmbeddingError("x"), messages=messages)[0]

    assert no_match.decide("q").message == "No answer found."
    assert unavailable.decide("q").message == "Search is down."


def test_default_policies_are_used_when_none_are_given() -> None:
    service, _, _ = decider([])

    assert service.acceptance == AcceptancePolicy()
    assert service.ambiguity == AmbiguityPolicy()
    assert service.messages == FallbackMessages()
    assert (service.acceptance.min_similarity, service.ambiguity.margin) == (0.40, 0.05)


# --- Settings --------------------------------------------------------------------------------


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None)

    assert (settings.hybrid_rrf_k, settings.hybrid_candidate_depth, settings.answer_candidates) == (
        60,
        20,
        5,
    )
    assert (settings.answer_min_similarity, settings.answer_accept_exact_question) == (0.40, True)
    assert (settings.degraded_min_keyword_score, settings.degraded_min_query_terms) == (40, 2)
    assert (settings.answer_ambiguity_margin, settings.answer_max_clarification_options) == (
        0.05,
        3,
    )
    assert settings.fallback_no_match_message == NO_MATCH_MESSAGE
    assert settings.fallback_unavailable_message == UNAVAILABLE_MESSAGE


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ANSWER_MIN_SIMILARITY", "1.5"),
        ("ANSWER_MIN_SIMILARITY", "nan"),
        ("ANSWER_ACCEPT_EXACT_QUESTION", "maybe"),
        ("ANSWER_CANDIDATES", "1"),
        ("ANSWER_CANDIDATES", "21"),  # > HYBRID_CANDIDATE_DEPTH (20)
        ("ANSWER_AMBIGUITY_MARGIN", "1.5"),
        ("ANSWER_MAX_CLARIFICATION_OPTIONS", "1"),
        ("DEGRADED_MIN_KEYWORD_SCORE", "0"),
        ("DEGRADED_MIN_QUERY_TERMS", "0"),
        ("HYBRID_CANDIDATE_DEPTH", "1"),
        ("FALLBACK_NO_MATCH_MESSAGE", "   "),
        ("FALLBACK_UNAVAILABLE_MESSAGE", "x" * 501),
    ],
)
def test_invalid_settings_fail_fast(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_fallback_message_setting_is_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FALLBACK_NO_MATCH_MESSAGE", "  Please email help@example.com.  ")

    assert Settings(_env_file=None).fallback_no_match_message == "Please email help@example.com."


def test_app_wires_each_responsibility_from_its_own_settings(
    make_settings: Callable[..., Settings],
) -> None:
    app = create_app(
        make_settings(
            hybrid_rrf_k=30,
            hybrid_candidate_depth=8,
            answer_candidates=4,
            answer_min_similarity=0.55,
            answer_accept_exact_question=False,
            degraded_min_keyword_score=45,
            degraded_min_query_terms=3,
            answer_ambiguity_margin=0.1,
            answer_max_clarification_options=2,
            fallback_no_match_message="No match.",
            fallback_unavailable_message="Unavailable.",
        )
    )
    hybrid = app.state.hybrid_search_service
    service = app.state.answer_decision_service

    assert (hybrid.rrf_k, hybrid.candidate_depth) == (30, 8)  # retrieval ranking
    assert service.candidates == 4
    assert service.acceptance == AcceptancePolicy(0.55, False, 45, 3)  # answer acceptance
    assert service.ambiguity == AmbiguityPolicy(0.1, 2)  # ambiguity detection
    assert service.messages == FallbackMessages("No match.", "Unavailable.")


def test_semantic_endpoint_threshold_does_not_change_answer_acceptance(
    make_settings: Callable[..., Settings],
) -> None:
    """SEMANTIC_MIN_SIMILARITY filters the semantic endpoint only; acceptance is separate."""
    app = create_app(make_settings(semantic_min_similarity=0.9, answer_min_similarity=0.3))

    assert app.state.semantic_search_service.default_min_score == 0.9
    assert app.state.answer_decision_service.acceptance.min_similarity == 0.3


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
