"""Keyword retrieval that produces ranked candidates for hybrid search.

Scoring is the deterministic, explainable rule set of ``KeywordSearcher``
(``faq_search.py``), the same one behind the Stage 2 endpoint
``GET /api/v1/faqs/search``, so both always agree. This service adds what a hybrid
retriever needs on top: query validation, category filtering, de-duplication,
active-only filtering and an explicit 1-based rank for every candidate.

Matching (all case-insensitive, punctuation ignored, whitespace collapsed, whole
words only; "order" never matches "orders" or "border"):

    Priority  Rule                                        Points
    1         query is exactly the FAQ question            100 (always ranked first)
    2         question contains the whole query phrase      40
    3         FAQ tags (the ``keywords`` field):
                query is a tag                              30 per tag
                a tag phrase appears in the query           15 per tag
                the query phrase appears inside a tag       10 per tag
    4         meaningful query word in the question          5 per word
    5         meaningful query word only in the answer       1 per word

Points add up. Candidates need at least 2 points, and queries made only of stopwords
("how do I") return nothing. Ordering: exact question first, then score, then
knowledge-base order, so ties are always broken the same way.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.models.faq import FAQ, FAQCategory, parse_category
from app.services.faq_search import FAQMatch, KeywordSearcher, MatchField

FAQProvider = Callable[[], Sequence[FAQ]]

DEFAULT_TOP_K = 10
MAX_TOP_K = 50
MAX_QUERY_LENGTH = 1000


class InvalidKeywordQueryError(ValueError):
    """The query is not a string, is blank, or is too long. Contains no query text."""


@dataclass(frozen=True, slots=True)
class KeywordCandidate:
    """One ranked keyword-retrieval result.

    ``score`` is in keyword points (unbounded; see the module docstring), not on the
    same scale as semantic cosine similarity. Use ``rank`` when combining the two.
    """

    faq_id: str
    rank: int
    score: int
    exact_question: bool
    matched_fields: frozenset[MatchField]
    reasons: tuple[str, ...]
    faq: FAQ


class KeywordSearchService:
    """Rank active FAQs by keyword match for a query."""

    def __init__(self, faq_provider: FAQProvider, searcher: KeywordSearcher | None = None) -> None:
        """
        Args:
            faq_provider: Returns the FAQs to search, e.g. ``FAQService.list_active_faqs``.
                Called on every search; inactive FAQs are excluded regardless.
            searcher: Scoring rules; defaults to ``KeywordSearcher()`` (questions,
                tags and answers).
        """
        self._faq_provider = faq_provider
        self._searcher = searcher or KeywordSearcher()

    def search(
        self,
        query: str,
        *,
        top_k: int = DEFAULT_TOP_K,
        category: FAQCategory | str | None = None,
    ) -> list[KeywordCandidate]:
        """Return up to ``top_k`` ranked candidates, best first (empty if none match).

        Raises:
            InvalidKeywordQueryError: the query is not a string, blank or too long.
            ValueError: ``top_k`` is not an integer in 1..MAX_TOP_K, or the category
                is unknown.
        """
        _validate_query(query)
        _validate_top_k(top_k)
        wanted = None if category is None else parse_category(category)

        faqs = _unique_active(self._faq_provider())
        if wanted is not None:
            faqs = [faq for faq in faqs if faq.category is wanted]

        matches = self._searcher.search(query, faqs, top_k)
        return [_to_candidate(match, rank) for rank, match in enumerate(matches, start=1)]


def _unique_active(faqs: Sequence[FAQ]) -> list[FAQ]:
    """Active FAQs in their original order, keeping only the first FAQ per ID."""
    seen: set[str] = set()
    unique = []
    for faq in faqs:
        if faq.is_active and faq.id not in seen:
            seen.add(faq.id)
            unique.append(faq)
    return unique


def _to_candidate(match: FAQMatch, rank: int) -> KeywordCandidate:
    return KeywordCandidate(
        faq_id=match.faq.id,
        rank=rank,
        score=match.score,
        exact_question=match.exact_question,
        matched_fields=match.matched_fields,
        reasons=match.reasons,
        faq=match.faq,
    )


def _validate_query(query: object) -> None:
    if not isinstance(query, str):
        raise InvalidKeywordQueryError(f"query must be a string, not {type(query).__name__}")
    if not query.strip():
        raise InvalidKeywordQueryError("query must not be empty or whitespace-only")
    if len(" ".join(query.split())) > MAX_QUERY_LENGTH:  # same rule as semantic search
        raise InvalidKeywordQueryError(f"query is too long (maximum {MAX_QUERY_LENGTH} characters)")


def _validate_top_k(top_k: object) -> None:
    if isinstance(top_k, bool) or not isinstance(top_k, int):
        raise ValueError(f"top_k must be an integer, not {type(top_k).__name__}")
    if not 1 <= top_k <= MAX_TOP_K:
        raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}, got {top_k}")
