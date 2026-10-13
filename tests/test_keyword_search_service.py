"""KeywordSearchService: ranked keyword candidates for hybrid retrieval."""

from typing import Any

import pytest

from app.models.faq import FAQ, FAQCategory
from app.services.faq_search import EXACT_QUESTION, MatchField
from app.services.faq_service import FAQService
from app.services.keyword_search_service import (
    MAX_QUERY_LENGTH,
    MAX_TOP_K,
    InvalidKeywordQueryError,
    KeywordCandidate,
    KeywordSearchService,
)
from tests.faq_test_data import ACTIVE_IDS, ACTIVE_SHIPPING_IDS, INACTIVE_IDS, TEST_FAQ_RECORDS


def make_faq(faq_id: str, question: str, keywords: list[str], answer: str = "", **extra: Any):
    return FAQ(
        id=faq_id,
        question=question,
        answer=answer or "General information is available in our help centre pages.",
        category=extra.pop("category", "general"),
        keywords=keywords,
        **extra,
    )


def service_for(faqs: list[FAQ]) -> KeywordSearchService:
    return KeywordSearchService(lambda: faqs)


def ids(candidates: list[KeywordCandidate]) -> list[str]:
    return [c.faq_id for c in candidates]


@pytest.fixture
def keyword(faq_service: FAQService) -> KeywordSearchService:
    return KeywordSearchService(faq_service.list_active_faqs)


# --- Ranking preferences --------------------------------------------------------------------


def test_ranking_follows_the_documented_preferences() -> None:
    faqs = [
        make_faq(
            "faq_005",
            "Security tips",
            ["security"],
            answer="Never share a reset link; choose a strong password.",
        ),  # answer only
        make_faq("faq_004", "Password rules", ["rules"]),  # partial question tokens
        make_faq("faq_003", "Login trouble", ["reset password"]),  # tag
        make_faq("faq_002", "How to reset password for my account", ["account"]),  # phrase
        make_faq("faq_001", "Reset password", ["account"]),  # exact question
    ]

    candidates = service_for(faqs).search("reset password")

    assert ids(candidates) == ["faq_001", "faq_002", "faq_003", "faq_004", "faq_005"]
    assert [c.rank for c in candidates] == [1, 2, 3, 4, 5]
    assert candidates[0].exact_question is True
    assert candidates[2].matched_fields == {MatchField.KEYWORDS}
    assert candidates[4].matched_fields == {MatchField.ANSWER}


def test_exact_question_wins_even_against_higher_scores() -> None:
    keyword_heavy = make_faq(
        "faq_002",
        "Order help",
        [
            "track",
            "order",
            "my order",
            "track my order",
            "can i track",
            "i track",
            "how can",
            "how can i",
            "can i",
            "i track my",
        ],
    )
    exact = make_faq("faq_001", "How can I track my order?", ["tracking"])

    candidates = service_for([keyword_heavy, exact]).search("How can I track my order?")

    assert ids(candidates) == ["faq_001", "faq_002"]
    assert candidates[1].score > candidates[0].score >= EXACT_QUESTION


@pytest.mark.parametrize(
    "query",
    [
        "How do I reset my password?",
        "how do i reset my password",
        "  HOW do I reset  MY password!? ",
        "How do I reset my password…",
        "how, do; I reset: my password",
    ],
)
def test_case_whitespace_and_punctuation_are_normalised(
    keyword: KeywordSearchService, query: str
) -> None:
    [top, *_] = keyword.search(query)

    assert top.faq_id == "faq_107"
    assert top.exact_question is True


def test_curly_apostrophes_match_straight_ones() -> None:
    faq = make_faq("faq_001", "I can't log in", ["login"])

    assert service_for([faq]).search("I can’t log in")[0].exact_question is True


def test_matching_is_by_whole_words_not_substrings() -> None:
    faqs = [
        make_faq("faq_001", "Border crossing fees", ["borders"]),
        make_faq("faq_002", "Ordering in bulk", ["preorder"]),
        make_faq("faq_003", "Where is my order?", ["status"]),
    ]

    assert ids(service_for(faqs).search("order")) == ["faq_003"]


def test_answer_matches_rank_below_question_matches() -> None:
    faqs = [
        make_faq("faq_001", "Opening hours", ["hours"], answer="Delivery is free on large orders."),
        make_faq("faq_002", "Delivery options", ["options"]),
    ]

    candidates = service_for(faqs).search("free delivery")

    assert ids(candidates) == ["faq_002", "faq_001"]
    assert MatchField.ANSWER in candidates[1].matched_fields


def test_matched_fields_report_every_contributing_field(keyword: KeywordSearchService) -> None:
    [top, *_] = keyword.search("where is my order")

    assert top.faq_id == "faq_101"
    assert top.matched_fields == {MatchField.QUESTION, MatchField.KEYWORDS}
    assert all(reason.endswith(")") for reason in top.reasons)


# --- Filtering, de-duplication, ordering -------------------------------------------------------


def test_inactive_faqs_are_excluded_even_on_exact_question(
    faq_service: FAQService, faq_repository: Any
) -> None:
    service = KeywordSearchService(faq_repository.list_all)  # provider includes inactive FAQs
    inactive_question = next(r["question"] for r in TEST_FAQ_RECORDS if r["id"] == "faq_105")

    candidates = service.search(inactive_question, top_k=MAX_TOP_K)

    assert set(ids(candidates)).isdisjoint(INACTIVE_IDS)


@pytest.mark.parametrize("category", ["shipping", " SHIPPING ", FAQCategory.SHIPPING])
def test_category_filter(keyword: KeywordSearchService, category: str | FAQCategory) -> None:
    candidates = keyword.search("how long does delivery take international", category=category)

    assert set(ids(candidates)) <= set(ACTIVE_SHIPPING_IDS)
    assert candidates and all(c.faq.category is FAQCategory.SHIPPING for c in candidates)


def test_category_filter_applies_before_top_k(keyword: KeywordSearchService) -> None:
    # "order" matches orders FAQs best overall; filtered to shipping, the top slot is still filled.
    [only] = keyword.search("order delivery", top_k=1, category="shipping")

    assert only.faq.category is FAQCategory.SHIPPING


def test_category_without_matches_returns_empty(keyword: KeywordSearchService) -> None:
    assert keyword.search("password", category="products") == []


def test_unknown_category_is_rejected(keyword: KeywordSearchService) -> None:
    with pytest.raises(ValueError, match="Unknown FAQ category"):
        keyword.search("order", category="billing")


def test_duplicate_faqs_from_the_provider_are_returned_once() -> None:
    faq = make_faq("faq_001", "Where is my order?", ["track"])
    copy = faq.model_copy(update={"question": "A different question about orders"})

    candidates = service_for([faq, copy, faq]).search("order", top_k=MAX_TOP_K)

    assert ids(candidates) == ["faq_001"]
    assert candidates[0].faq is faq  # first occurrence wins


def test_ties_keep_knowledge_base_order() -> None:
    first = make_faq("faq_001", "Islands delivery", ["islands"])
    second = make_faq("faq_002", "Remote islands delivery", ["islands"])

    assert ids(service_for([first, second]).search("islands")) == ["faq_001", "faq_002"]
    assert ids(service_for([second, first]).search("islands")) == ["faq_002", "faq_001"]


def test_results_are_deterministic(keyword: KeywordSearchService) -> None:
    runs = {
        tuple((c.faq_id, c.rank, c.score) for c in keyword.search("order delivery refund"))
        for _ in range(20)
    }

    assert len(runs) == 1


def test_ranks_are_consecutive_and_scores_descend_after_exact(
    keyword: KeywordSearchService,
) -> None:
    candidates = keyword.search("order", top_k=MAX_TOP_K)

    assert [c.rank for c in candidates] == list(range(1, len(candidates) + 1))
    non_exact = [c.score for c in candidates if not c.exact_question]
    assert non_exact == sorted(non_exact, reverse=True)


@pytest.mark.parametrize("top_k", [1, 2, 3])
def test_top_k_limits_results(keyword: KeywordSearchService, top_k: int) -> None:
    assert len(keyword.search("order", top_k=top_k)) <= top_k


@pytest.mark.parametrize("top_k", [0, -1, MAX_TOP_K + 1, 2.0, "3", True, None])
def test_invalid_top_k_is_rejected(keyword: KeywordSearchService, top_k: object) -> None:
    with pytest.raises(ValueError, match="top_k"):
        keyword.search("order", top_k=top_k)  # type: ignore[arg-type]


# --- Query validation --------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["", "   ", "\n\t"])
def test_blank_query_is_rejected(keyword: KeywordSearchService, query: str) -> None:
    with pytest.raises(InvalidKeywordQueryError, match="empty or whitespace-only"):
        keyword.search(query)


@pytest.mark.parametrize("query", [None, 42, ["order"]])
def test_non_string_query_is_rejected(keyword: KeywordSearchService, query: object) -> None:
    with pytest.raises(InvalidKeywordQueryError, match="must be a string"):
        keyword.search(query)  # type: ignore[arg-type]


def test_overlong_query_is_rejected(keyword: KeywordSearchService) -> None:
    keyword.search("x" * MAX_QUERY_LENGTH)

    with pytest.raises(InvalidKeywordQueryError, match="too long"):
        keyword.search("x" * (MAX_QUERY_LENGTH + 1))


def test_overlong_punctuation_only_query_is_rejected(keyword: KeywordSearchService) -> None:
    with pytest.raises(InvalidKeywordQueryError, match="too long"):
        keyword.search("?" * (MAX_QUERY_LENGTH + 1))


@pytest.mark.parametrize("query", ["how do I", "the", "?!.", "xyzzy quantum"])
def test_valid_queries_without_matches_return_empty(
    keyword: KeywordSearchService, query: str
) -> None:
    assert keyword.search(query) == []


def test_errors_do_not_echo_the_query(keyword: KeywordSearchService) -> None:
    with pytest.raises(InvalidKeywordQueryError) as exc:
        keyword.search("jane.doe@example.com " * 60)

    assert "jane.doe" not in str(exc.value)


# --- Stage 2 compatibility ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "How can I track my order?",
        "where is my order",
        "order",
        "reset password",
        "free shipping",
        "do you ship abroad",
        "how long does delivery take",
    ],
)
def test_same_ranking_as_stage2_keyword_search(
    keyword: KeywordSearchService, faq_service: FAQService, query: str
) -> None:
    """The Stage 2 endpoint (FAQService.search) and this service must always agree."""
    stage2 = [(m.faq.id, m.score, m.reasons) for m in faq_service.search(query, limit=MAX_TOP_K)]
    stage4 = [(c.faq_id, c.score, c.reasons) for c in keyword.search(query, top_k=MAX_TOP_K)]

    assert stage4 == stage2


def test_stage2_endpoint_response_is_unchanged(faq_client: Any) -> None:
    body = faq_client.get("/api/v1/faqs/search", params={"q": "where is my order"}).json()

    assert set(body["results"][0]) == {"id", "question", "answer", "category", "score", "reasons"}
    assert body["results"][0]["id"] == "faq_101"


def test_only_active_test_faqs_are_ever_returned(keyword: KeywordSearchService) -> None:
    returned = {
        c.faq_id
        for q in ("order", "delivery", "password", "return", "pay")
        for c in keyword.search(q, top_k=MAX_TOP_K)
    }

    assert returned <= set(ACTIVE_IDS)
