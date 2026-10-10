"""Deterministic keyword search over FAQs: the baseline for Stage 3 semantic search.

How a query is scored against one FAQ (all text is case-folded, punctuation is
ignored and whitespace collapsed before comparing):

    Rule                                         Points
    -------------------------------------------  ------
    Query is exactly the question                 100   (always ranked first)
    Question contains the whole query phrase       40
    Query is exactly one of the keywords           30   per keyword
    A keyword phrase appears in the query          15   per keyword
    The query phrase appears inside a keyword      10   per keyword
    Meaningful query word also in the question      5   per word
    Meaningful query word only in the answer        1   per word

"Meaningful" words exclude common stopwords ("how", "my", "the" ...); a query with no
meaningful words returns nothing. FAQs scoring
below MIN_SCORE are dropped, so a single common word in a long answer is not a hit.
Results are ordered by: exact question match, then score, then file order.

Known limitations, kept on purpose so Stage 3 has a fair baseline:
no stemming ("orders" does not match "order"), no synonyms, no typo tolerance.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.models.faq import FAQ

EXACT_QUESTION = 100
QUESTION_PHRASE = 40
KEYWORD_EXACT = 30
KEYWORD_IN_QUERY = 15
QUERY_IN_KEYWORD = 10
QUESTION_TERM = 5
ANSWER_TERM = 1
MIN_SCORE = 2

STOPWORDS = frozenset(
    "a an and are am as at be by can could do does did for from had has have how i if in "
    "is it its me my of on or our so than that the their them then there this to us was "
    "we were what when which who will with would you your".split()
)

# Words, keeping internal apostrophes and hyphen-free: "can't" -> "can't", "e-mail" -> "e", "mail".
_TOKEN_PATTERN = re.compile(r"\w+(?:'\w+)*")


def tokenize(text: str) -> list[str]:
    """Lower-case word tokens, ignoring punctuation and extra whitespace."""
    return _TOKEN_PATTERN.findall(text.casefold().replace("’", "'"))


def normalize(text: str) -> str:
    """Canonical form used for phrase comparisons: tokens joined by single spaces."""
    return " ".join(tokenize(text))


def _contains_phrase(text: str, phrase: str) -> bool:
    """Whole-word phrase containment on normalised text ("order" is not in "orders")."""
    return bool(phrase) and f" {phrase} " in f" {text} "


@dataclass(frozen=True, slots=True)
class FAQMatch:
    """One search hit with its score and the reasons it matched."""

    faq: FAQ
    score: int
    exact_question: bool
    reasons: tuple[str, ...]


class FAQSearcher(Protocol):
    """Interface for any FAQ search strategy (keyword now, semantic in Stage 3)."""

    def search(self, query: str, faqs: Sequence[FAQ], limit: int) -> list[FAQMatch]:
        """Return up to ``limit`` matches from ``faqs``, best first."""
        ...


class KeywordSearcher:
    """Explainable keyword/phrase scoring. See the module docstring for the rules."""

    def __init__(self, *, search_answers: bool = True) -> None:
        self.search_answers = search_answers

    def search(self, query: str, faqs: Sequence[FAQ], limit: int) -> list[FAQMatch]:
        query_tokens = tokenize(query)
        query_terms = {token for token in query_tokens if token not in STOPWORDS}
        if not query_terms:
            # Only stopwords ("how do I"): any phrase match would be noise.
            return []
        query_phrase = " ".join(query_tokens)

        ranked: list[tuple[int, int, int, FAQMatch]] = []
        for position, faq in enumerate(faqs):
            match = self._score(faq, query_phrase, query_terms)
            if match.score >= MIN_SCORE:
                ranked.append((not match.exact_question, -match.score, position, match))

        ranked.sort(key=lambda item: item[:3])
        return [match for *_, match in ranked[:limit]]

    def _score(self, faq: FAQ, query_phrase: str, query_terms: set[str]) -> FAQMatch:
        score = 0
        reasons: list[str] = []

        def add(points: int, reason: str) -> None:
            nonlocal score
            score += points
            reasons.append(f"{reason} (+{points})")

        question = normalize(faq.question)
        exact = question == query_phrase
        if exact:
            add(EXACT_QUESTION, "exact question match")
        elif _contains_phrase(question, query_phrase):
            add(QUESTION_PHRASE, "question contains query")

        for keyword in faq.keywords:
            keyword_phrase = normalize(keyword)
            if keyword_phrase == query_phrase:
                add(KEYWORD_EXACT, f"query is keyword '{keyword}'")
            elif _contains_phrase(query_phrase, keyword_phrase):
                add(KEYWORD_IN_QUERY, f"keyword '{keyword}' in query")
            elif _contains_phrase(keyword_phrase, query_phrase):
                add(QUERY_IN_KEYWORD, f"query in keyword '{keyword}'")

        question_hits = query_terms & set(tokenize(faq.question))
        if question_hits:
            add(QUESTION_TERM * len(question_hits), f"question words: {_join(question_hits)}")

        if self.search_answers:
            answer_hits = (query_terms - question_hits) & set(tokenize(faq.answer))
            if answer_hits:
                add(ANSWER_TERM * len(answer_hits), f"answer words: {_join(answer_hits)}")

        return FAQMatch(faq=faq, score=score, exact_question=exact, reasons=tuple(reasons))


def _join(words: set[str]) -> str:
    return ", ".join(sorted(words))
