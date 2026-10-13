"""Decide whether to answer a customer question with an approved FAQ, ask, or fall back.

The hybrid retriever (keyword + semantic, fused with RRF) only ORDERS candidates. Whether
an answer is shown is decided here, from evidence that actually measures relevance:

Acceptance rule (a candidate is "acceptable" if either holds)
    - exact question match: the query is the FAQ question (keyword retrieval), or
    - semantic evidence: cosine similarity >= ``min_similarity`` (the same threshold as
      the semantic search endpoint, ``SEMANTIC_MIN_SIMILARITY``; experimental 0.40).
  RRF scores, keyword points alone and rank positions are never sufficient.

Decision rules, in order
    1. No acceptable candidate                        -> no_match (fallback message)
    2. Top acceptable candidate is an exact question  -> answered
    3. Another acceptable candidate's similarity is within ``ambiguity_margin`` of the
       top candidate's, or higher (the retrievers disagree) -> ambiguous (clarifying question)
    4. Otherwise                                       -> answered (top acceptable FAQ)

  The clarifying question names only what the competing candidates contain: their
  categories when they differ, otherwise their actual questions. Nothing is guessed.

Failure policy (semantic model / embedding infrastructure unavailable)
    Keyword-only degraded retrieval is used, and an answer is given only on strong
    lexical evidence: an exact question match, or a keyword score >= ``degraded_min_keyword_score``
    (40 = the whole query appears in the FAQ question) that is strictly higher than the
    runner-up, for a query with at least ``DEGRADED_MIN_QUERY_TERMS`` meaningful
    (non-stopword) words. A single word such as "delivery" appears in many FAQs and is
    not reliable evidence on its own. Such answers are flagged ``degraded=True`` with
    ``retrieval_mode="keyword_only"``, never presented as a normal result.
    Otherwise -> retrieval_unavailable (safe message).
    The failure is logged by exception type only; the query text is never logged.
"""

import logging
import math
from dataclasses import dataclass
from enum import StrEnum

from app.models.faq import FAQ, FAQCategory, parse_category
from app.services.embedding_service import EmbeddingError
from app.services.faq_search import STOPWORDS, tokenize
from app.services.hybrid_search_service import HybridResult, HybridSearchService
from app.services.keyword_search_service import KeywordCandidate, KeywordSearchService
from app.services.semantic_search_service import meets_threshold

logger = logging.getLogger(__name__)

DEFAULT_MIN_SIMILARITY = 0.40
DEFAULT_AMBIGUITY_MARGIN = 0.05
DEFAULT_DEGRADED_MIN_KEYWORD_SCORE = 40
DEGRADED_MIN_QUERY_TERMS = 2
DEFAULT_CANDIDATES = 5
MAX_CLARIFICATION_OPTIONS = 3

NO_MATCH_MESSAGE = (
    "Sorry, I couldn't find a reliable answer to that question. "
    "Please contact our support team for assistance."
)
UNAVAILABLE_MESSAGE = (
    "Sorry, I can't search our help articles right now. "
    "Please try again in a few minutes or contact our support team for assistance."
)


class Decision(StrEnum):
    ANSWERED = "answered"
    NO_MATCH = "no_match"
    AMBIGUOUS = "ambiguous"
    RETRIEVAL_UNAVAILABLE = "retrieval_unavailable"


class RetrievalMode(StrEnum):
    HYBRID = "hybrid"
    KEYWORD_ONLY = "keyword_only"  # degraded: the semantic model was unavailable


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    """The relevance evidence for one FAQ that was considered."""

    faq: FAQ
    semantic_similarity: float | None
    keyword_score: int | None
    exact_question: bool
    rrf_score: float | None  # ordering only; never used as proof of relevance


@dataclass(frozen=True, slots=True)
class AnswerDecision:
    """The outcome for one customer question.

    ``message`` is the text to show: the approved FAQ answer verbatim when answered,
    otherwise a predefined fallback or clarifying question. ``reasons`` explain the
    decision in plain language.
    """

    decision: Decision
    message: str
    retrieval_mode: RetrievalMode
    reasons: tuple[str, ...]
    faq: FAQ | None = None
    evidence: CandidateEvidence | None = None
    options: tuple[CandidateEvidence, ...] = ()  # competing candidates when ambiguous
    retry_after_seconds: float | None = None

    @property
    def degraded(self) -> bool:
        """True when the semantic model was unavailable and keyword-only retrieval ran."""
        return self.retrieval_mode is RetrievalMode.KEYWORD_ONLY


class AnswerDecisionService:
    """Turns retrieval results into an answer, a clarifying question or a fallback."""

    def __init__(
        self,
        hybrid: HybridSearchService,
        keyword: KeywordSearchService,
        *,
        min_similarity: float = DEFAULT_MIN_SIMILARITY,
        ambiguity_margin: float = DEFAULT_AMBIGUITY_MARGIN,
        degraded_min_keyword_score: int = DEFAULT_DEGRADED_MIN_KEYWORD_SCORE,
        candidates: int = DEFAULT_CANDIDATES,
    ) -> None:
        _check_range("min_similarity", min_similarity, -1.0, 1.0)
        _check_range("ambiguity_margin", ambiguity_margin, 0.0, 2.0)
        _check_range("degraded_min_keyword_score", degraded_min_keyword_score, 1, 10_000)
        if isinstance(candidates, bool) or not isinstance(candidates, int) or candidates < 2:
            raise ValueError("candidates must be an integer >= 2")
        if candidates > hybrid.candidate_depth:
            raise ValueError("candidates must not exceed the hybrid candidate depth")
        self._hybrid = hybrid
        self._keyword = keyword
        self.min_similarity = min_similarity
        self.ambiguity_margin = ambiguity_margin
        self.degraded_min_keyword_score = degraded_min_keyword_score
        self.candidates = candidates

    def decide(self, query: str, *, category: FAQCategory | str | None = None) -> AnswerDecision:
        """Decide how to respond to ``query``.

        Raises:
            ValueError: invalid query or unknown category (not a retrieval failure).
        """
        wanted = None if category is None else parse_category(category)
        try:
            results = self._hybrid.search(query, top_k=self.candidates, category=wanted)
        except EmbeddingError as exc:
            logger.warning(
                "Semantic retrieval unavailable (%s); trying keyword-only fallback",
                type(exc).__name__,
            )
            return self._degraded(query, wanted, getattr(exc, "retry_after_seconds", None))
        return self._decide_from_hybrid([_evidence(r) for r in results])

    # --- Normal path --------------------------------------------------------------------------

    def _decide_from_hybrid(self, candidates: list[CandidateEvidence]) -> AnswerDecision:
        acceptable = [c for c in candidates if self._is_acceptable(c)]
        if not acceptable:
            return AnswerDecision(
                decision=Decision.NO_MATCH,
                message=NO_MATCH_MESSAGE,
                retrieval_mode=RetrievalMode.HYBRID,
                reasons=(
                    f"no candidate had an exact question match or semantic similarity "
                    f">= {self.min_similarity}",
                ),
                options=tuple(candidates[:MAX_CLARIFICATION_OPTIONS]),
            )

        top = acceptable[0]
        if top.exact_question:
            return self._answered(top, RetrievalMode.HYBRID, "exact question match")

        competing = self._competing(top, acceptable[1:])
        if len(competing) > 1:
            return AnswerDecision(
                decision=Decision.AMBIGUOUS,
                message=clarification_message(competing),
                retrieval_mode=RetrievalMode.HYBRID,
                reasons=(
                    f"{len(competing) - 1} other candidate(s) scored within "
                    f"{self.ambiguity_margin} of, or above, the top candidate's semantic "
                    f"similarity "
                    f"({', '.join(f'{c.faq.id} {c.semantic_similarity:.3f}' for c in competing)})",
                ),
                options=tuple(competing),
            )

        return self._answered(
            top,
            RetrievalMode.HYBRID,
            f"semantic similarity {top.semantic_similarity:.3f} >= {self.min_similarity}",
        )

    def _is_acceptable(self, candidate: CandidateEvidence) -> bool:
        if candidate.exact_question:
            return True
        similarity = candidate.semantic_similarity
        return similarity is not None and meets_threshold(similarity, self.min_similarity)

    def _competing(
        self, top: CandidateEvidence, others: list[CandidateEvidence]
    ) -> list[CandidateEvidence]:
        """The top candidate plus acceptable rivals close to (or above) its similarity.

        The top candidate here always has a similarity: it was accepted without an
        exact question match, so it passed the semantic threshold.
        """
        floor = top.semantic_similarity - self.ambiguity_margin  # type: ignore[operator]
        rivals = [
            c
            for c in others
            if c.semantic_similarity is not None and meets_threshold(c.semantic_similarity, floor)
        ]
        return [top, *rivals][:MAX_CLARIFICATION_OPTIONS]

    def _answered(
        self, evidence: CandidateEvidence, mode: RetrievalMode, reason: str
    ) -> AnswerDecision:
        return AnswerDecision(
            decision=Decision.ANSWERED,
            message=evidence.faq.answer,  # approved answer, verbatim
            retrieval_mode=mode,
            reasons=(reason,),
            faq=evidence.faq,
            evidence=evidence,
        )

    # --- Degraded path (semantic model unavailable) -------------------------------------------

    def _degraded(
        self, query: str, category: FAQCategory | None, retry_after: float | None
    ) -> AnswerDecision:
        candidates = self._keyword.search(query, top_k=2, category=category)
        top = candidates[0] if candidates else None
        runner_up_score = candidates[1].score if len(candidates) > 1 else -math.inf

        if top is not None and top.exact_question:
            reason = "semantic retrieval unavailable; exact question match (keyword only)"
        elif (
            top is not None
            and top.score >= self.degraded_min_keyword_score
            and top.score > runner_up_score
            and _meaningful_terms(query) >= DEGRADED_MIN_QUERY_TERMS
        ):
            reason = (
                f"semantic retrieval unavailable; clear keyword match "
                f"(score {top.score} >= {self.degraded_min_keyword_score}, runner-up "
                f"{'none' if runner_up_score == -math.inf else int(runner_up_score)})"
            )
        else:
            return AnswerDecision(
                decision=Decision.RETRIEVAL_UNAVAILABLE,
                message=UNAVAILABLE_MESSAGE,
                retrieval_mode=RetrievalMode.KEYWORD_ONLY,
                reasons=("semantic retrieval unavailable; no strong keyword-only evidence",),
                retry_after_seconds=retry_after,
            )

        decision = self._answered(_keyword_evidence(top), RetrievalMode.KEYWORD_ONLY, reason)
        return AnswerDecision(**{**_as_dict(decision), "retry_after_seconds": retry_after})


# --- Helpers ------------------------------------------------------------------------------------


def clarification_message(options: list[CandidateEvidence]) -> str:
    """A clarifying question built only from what the competing FAQs contain."""
    categories = list(dict.fromkeys(option.faq.category.value for option in options))
    if len(categories) > 1:
        return f"Could you clarify whether your question is about {_join_or(categories)}?"
    questions = [f'"{option.faq.question}"' for option in options]
    return f"Could you clarify which of these you mean: {_join_or(questions)}"


def _meaningful_terms(query: str) -> int:
    return len({token for token in tokenize(query) if token not in STOPWORDS})


def _join_or(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} or {items[-1]}"


def _evidence(result: HybridResult) -> CandidateEvidence:
    return CandidateEvidence(
        faq=result.faq,
        semantic_similarity=None if result.semantic is None else result.semantic.similarity,
        keyword_score=None if result.keyword is None else result.keyword.score,
        exact_question=result.keyword is not None and result.keyword.exact_question,
        rrf_score=result.rrf_score,
    )


def _keyword_evidence(candidate: KeywordCandidate) -> CandidateEvidence:
    return CandidateEvidence(
        faq=candidate.faq,
        semantic_similarity=None,
        keyword_score=candidate.score,
        exact_question=candidate.exact_question,
        rrf_score=None,
    )


def _as_dict(decision: AnswerDecision) -> dict:
    return {name: getattr(decision, name) for name in AnswerDecision.__slots__}


def _check_range(name: str, value: object, low: float, high: float) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number, not {type(value).__name__}")
    if not (math.isfinite(value) and low <= value <= high):
        raise ValueError(f"{name} must be between {low} and {high}, got {value}")
