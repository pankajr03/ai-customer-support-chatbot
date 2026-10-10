"""KeywordSearcher: normalisation, scoring rules, ordering and determinism."""

import re
from typing import Any

import pytest

from app.models.faq import FAQ
from app.services.faq_search import (
    ANSWER_TERM,
    EXACT_QUESTION,
    KEYWORD_EXACT,
    MIN_SCORE,
    QUESTION_PHRASE,
    FAQMatch,
    FAQSearcher,
    KeywordSearcher,
    normalize,
    tokenize,
)


def make_faq(faq_id: str, question: str, keywords: list[str], answer: str = "") -> FAQ:
    return FAQ(
        id=faq_id,
        question=question,
        answer=answer or "Please see our help pages for more information on this topic.",
        category="general",
        keywords=keywords,
    )


TRACK = make_faq("faq_001", "How can I track my order?", ["track", "order status"])
CANCEL = make_faq("faq_002", "Can I cancel my order?", ["cancel", "change order"])
PASSWORD = make_faq("faq_003", "How do I reset my password?", ["reset password", "login"])
FAQS = [TRACK, CANCEL, PASSWORD]


@pytest.fixture
def searcher() -> KeywordSearcher:
    return KeywordSearcher()


def ids(matches: list[FAQMatch]) -> list[str]:
    return [match.faq.id for match in matches]


# --- Normalisation ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("How can I Track my ORDER?", "how can i track my order"),
        ("  lots   of\n\tspace  ", "lots of space"),
        ("Can't log in!!!", "can't log in"),
        ("Can’t log in", "can't log in"),
        ("e-mail, 2FA & café", "e mail 2fa café"),
        ("?!.", ""),
    ],
)
def test_normalize(text: str, expected: str) -> None:
    assert normalize(text) == expected


def test_tokenize_returns_words() -> None:
    assert tokenize("Where is MY order?") == ["where", "is", "my", "order"]


# --- Matching rules ----------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    ["How can I track my order?", "how can i track my order", "  HOW  can I\ttrack my ORDER!! "],
)
def test_exact_question_match_ignores_case_whitespace_and_punctuation(
    searcher: KeywordSearcher, query: str
) -> None:
    [top, *_] = searcher.search(query, FAQS, limit=5)

    assert top.faq is TRACK
    assert top.exact_question is True
    assert top.score >= EXACT_QUESTION


def test_exact_question_beats_higher_scoring_keyword_matches(searcher: KeywordSearcher) -> None:
    keyword_heavy = make_faq(
        "faq_009",
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

    matches = searcher.search("How can I track my order?", [keyword_heavy, TRACK], limit=5)

    assert ids(matches) == ["faq_001", "faq_009"]
    assert matches[1].score > matches[0].score  # ranked by exactness first, not raw score


def test_question_phrase_match(searcher: KeywordSearcher) -> None:
    [top] = searcher.search("reset my password", FAQS, limit=5)

    assert top.faq is PASSWORD
    assert any("question contains query" in reason for reason in top.reasons)
    assert top.score >= QUESTION_PHRASE


def test_exact_keyword_match(searcher: KeywordSearcher) -> None:
    [top, *_] = searcher.search("Order Status", FAQS, limit=5)

    assert top.faq is TRACK
    assert f"query is keyword 'order status' (+{KEYWORD_EXACT})" in top.reasons


def test_keyword_inside_longer_query(searcher: KeywordSearcher) -> None:
    [top] = searcher.search("I need to cancel please", FAQS, limit=5)

    assert top.faq is CANCEL
    assert "keyword 'cancel' in query (+15)" in top.reasons


def test_matching_uses_whole_words(searcher: KeywordSearcher) -> None:
    # No stemming in the baseline: "orders"/"tracking" are different words.
    assert searcher.search("orders tracking", FAQS, limit=5) == []


@pytest.mark.parametrize("query", ["", "   ", "?!", "how do I", "the my your"])
def test_empty_or_stopword_only_queries_return_nothing(
    searcher: KeywordSearcher, query: str
) -> None:
    assert searcher.search(query, FAQS, limit=5) == []


def test_unrelated_query_returns_nothing(searcher: KeywordSearcher) -> None:
    assert searcher.search("xyzzy quantum banana", FAQS, limit=5) == []


# --- Answer matching and the minimum score ----------------------------------------


def test_single_answer_word_is_below_minimum_score(searcher: KeywordSearcher) -> None:
    faq = make_faq("faq_010", "Opening hours", ["hours"], answer="We open at nine every weekday.")

    assert ANSWER_TERM < MIN_SCORE
    assert searcher.search("weekday", [faq], limit=5) == []


def test_several_answer_words_can_match(searcher: KeywordSearcher) -> None:
    faq = make_faq("faq_010", "Opening hours", ["hours"], answer="We open at nine every weekday.")

    [match] = searcher.search("weekday nine", [faq], limit=5)

    assert match.score == 2 * ANSWER_TERM
    assert match.reasons == ("answer words: nine, weekday (+2)",)


def test_answer_search_can_be_disabled() -> None:
    faq = make_faq("faq_010", "Opening hours", ["hours"], answer="We open at nine every weekday.")

    assert KeywordSearcher(search_answers=False).search("weekday nine", [faq], limit=5) == []


def test_answer_words_already_in_question_are_not_double_counted(
    searcher: KeywordSearcher,
) -> None:
    faq = make_faq("faq_011", "Gift wrapping", ["wrap"], answer="Gift wrapping is available.")

    [match] = searcher.search("gift", [faq], limit=5)

    assert not any(reason.startswith("answer words") for reason in match.reasons)


# --- Ordering, limits and explainability --------------------------------------------


def test_ties_keep_knowledge_base_order(searcher: KeywordSearcher) -> None:
    first = make_faq("faq_020", "Delivery to islands", ["islands"])
    second = make_faq("faq_021", "Delivery to remote islands", ["islands"])

    assert ids(searcher.search("islands", [first, second], limit=5)) == ["faq_020", "faq_021"]
    assert ids(searcher.search("islands", [second, first], limit=5)) == ["faq_021", "faq_020"]


def test_results_are_deterministic(searcher: KeywordSearcher) -> None:
    runs = {tuple(ids(searcher.search("order", FAQS, limit=5))) for _ in range(20)}

    assert len(runs) == 1


def test_limit_is_respected(searcher: KeywordSearcher) -> None:
    assert len(searcher.search("order", FAQS, limit=1)) == 1


def test_reasons_add_up_to_the_score(searcher: KeywordSearcher) -> None:
    for match in searcher.search("How can I track my order status?", FAQS, limit=5):
        points = sum(int(n) for n in re.findall(r"\(\+(\d+)\)", " ".join(match.reasons)))
        assert points == match.score


def test_keyword_searcher_implements_the_searcher_interface() -> None:
    searcher: FAQSearcher = KeywordSearcher()  # type-checked against the Protocol

    assert callable(searcher.search)


def test_match_is_immutable(searcher: KeywordSearcher) -> None:
    [match, *_] = searcher.search("track", FAQS, limit=5)
    attributes: Any = match

    with pytest.raises(AttributeError):
        attributes.score = 0
