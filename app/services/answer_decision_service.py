"""Decide whether to answer a customer question with an approved FAQ, ask, or fall back.

Three separate responsibilities, each with its own configuration:

1. Retrieval ranking (``HybridSearchService``: ``rrf_k``, ``candidate_depth``)
   ORDERS candidates. Ranking scores (RRF) never decide whether to answer.
2. Answer acceptance (``AcceptancePolicy``) decides whether a candidate is relevant
   ENOUGH to show, from evidence that measures relevance.
3. Ambiguity detection (``AmbiguityPolicy``) decides whether several acceptable
   candidates are too close to choose between, so the customer should be asked.

The fallback texts (``FallbackMessages``) are presentation, not decision rules.

All numeric defaults are EXPERIMENTAL starting values. They are not universally
reliable: calibrate them for each model and knowledge base with a labelled evaluation
dataset (see README, "Decision thresholds").

Acceptance rule (a candidate is "acceptable" if either holds)
    - ``accept_exact_question`` and the query is exactly the FAQ question, or
    - semantic cosine similarity >= ``min_similarity``.
  RRF scores, rank positions and keyword points alone are never sufficient.

Decision rules, in order
    1. No acceptable candidate                        -> no_match (fallback message)
    2. Top acceptable candidate is an exact question  -> answered
    3. Another acceptable candidate's similarity is within ``margin`` of the top
       candidate's, or higher (the retrievers disagree) -> ambiguous (clarifying question)
    4. Otherwise                                       -> answered (top acceptable FAQ)

  The clarifying question names only what the competing candidates contain: their
  categories when they differ, otherwise their actual questions. Nothing is guessed.

Failure policy (semantic model / embedding infrastructure unavailable)
    Keyword-only degraded retrieval is used. An answer is given only on strong lexical
    evidence: an exact question match (if ``accept_exact_question``), or a keyword score
    >= ``degraded_min_keyword_score`` (40 = the whole query appears in the FAQ question)
    that is strictly higher than the runner-up, for a query with at least
    ``degraded_min_query_terms`` meaningful (non-stopword) words. A single word such as
    "delivery" appears in many FAQs and is not reliable evidence on its own. Such answers
    are flagged ``degraded=True`` with ``retrieval_mode="keyword_only"``, never presented
    as a normal result. Otherwise -> retrieval_unavailable (safe message).
    The failure is logged by exception type only; the query text is never logged.

Consistency with an editable catalogue (Stage 11, with ``catalog``)
    Each decision pins one FAQ catalogue snapshot (``app.services.faq_catalog``), so
    keyword search, the semantic index and the chosen evidence all come from the same
    FAQ versions. If the catalogue changed while deciding (staff edited an FAQ), the
    decision is made again on the new snapshot; if it changed again, the result is
    ``retrieval_unavailable`` (the safe message) rather than a decision on outdated FAQs.

Logging
    Every decision is logged once at INFO as ``event=answer_decision`` with key=value
    fields (decision, retrieval mode, matched FAQ ID, category, candidate counts, top
    similarity, duration). The same fields are attached to the record as ``extra``
    attributes for structured (e.g. JSON) handlers. The query, answers and clarifying
    texts are never logged: they may contain customer data.
"""

import dataclasses
import logging
import math
import time
from dataclasses import dataclass
from enum import StrEnum

from app.models.faq import FAQ, FAQCategory, parse_category
from app.services.embedding_service import EmbeddingError
from app.services.faq_catalog import FAQCatalog
from app.services.faq_search import STOPWORDS, tokenize
from app.services.hybrid_search_service import MAX_TOP_K, HybridResult, HybridSearchService
from app.services.keyword_search_service import KeywordCandidate, KeywordSearchService
from app.services.semantic_search_service import meets_threshold

logger = logging.getLogger(__name__)

DEFAULT_CANDIDATES = 5
DEGRADED_CANDIDATES = 2  # keyword-only path: the top candidate and its runner-up
# Decisions attempted on successive catalogue snapshots before giving up (Stage 11).
CATALOG_ATTEMPTS = 2
CATALOG_RETRY_AFTER_SECONDS = 1.0
MAX_MESSAGE_LENGTH = 500

NO_MATCH_MESSAGE = (
    "Sorry, I couldn't find a reliable answer to that question. "
    "Please contact our support team for assistance."
)
UNAVAILABLE_MESSAGE = (
    "Sorry, I can't search our help articles right now. "
    "Please try again in a few minutes or contact our support team for assistance."
)


# --- Policies (one per responsibility) -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AcceptancePolicy:
    """When is a candidate relevant enough to show? (Experimental defaults.)"""

    min_similarity: float = 0.40
    accept_exact_question: bool = True
    degraded_min_keyword_score: int = 40
    degraded_min_query_terms: int = 2

    def __post_init__(self) -> None:
        _check_number("min_similarity", self.min_similarity, -1.0, 1.0)
        if not isinstance(self.accept_exact_question, bool):
            raise ValueError("accept_exact_question must be a boolean")
        _check_int("degraded_min_keyword_score", self.degraded_min_keyword_score, 1, 1000)
        _check_int("degraded_min_query_terms", self.degraded_min_query_terms, 1, 20)


@dataclass(frozen=True, slots=True)
class AmbiguityPolicy:
    """When are acceptable candidates too close to choose between? (Experimental defaults.)"""

    margin: float = 0.05
    max_options: int = 3

    def __post_init__(self) -> None:
        _check_number("margin", self.margin, 0.0, 1.0)
        _check_int("max_options", self.max_options, 2, 10)


@dataclass(frozen=True, slots=True)
class FallbackMessages:
    """Customer-facing texts used when no FAQ answer is given."""

    no_match: str = NO_MATCH_MESSAGE
    unavailable: str = UNAVAILABLE_MESSAGE

    def __post_init__(self) -> None:
        for name in ("no_match", "unavailable"):
            text = getattr(self, name)
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"{name} message must be non-empty text")
            if len(text) > MAX_MESSAGE_LENGTH:
                raise ValueError(f"{name} message must be at most {MAX_MESSAGE_LENGTH} characters")


# --- Results -------------------------------------------------------------------------------------


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
    otherwise a configured fallback or a clarifying question. ``reasons`` explain the
    decision in plain language. ``retrieved`` is the ranked retrieval evidence (hybrid,
    or keyword-only when degraded), for debugging and evaluation; it is not an answer.
    """

    decision: Decision
    message: str
    retrieval_mode: RetrievalMode
    reasons: tuple[str, ...]
    faq: FAQ | None = None
    evidence: CandidateEvidence | None = None
    options: tuple[CandidateEvidence, ...] = ()  # competing candidates when ambiguous
    retry_after_seconds: float | None = None
    retrieved: tuple[CandidateEvidence, ...] = ()

    @property
    def degraded(self) -> bool:
        """True when the semantic model was unavailable and keyword-only retrieval ran."""
        return self.retrieval_mode is RetrievalMode.KEYWORD_ONLY


# --- Service -------------------------------------------------------------------------------------


class AnswerDecisionService:
    """Turns retrieval results into an answer, a clarifying question or a fallback."""

    def __init__(
        self,
        hybrid: HybridSearchService,
        keyword: KeywordSearchService,
        *,
        acceptance: AcceptancePolicy | None = None,
        ambiguity: AmbiguityPolicy | None = None,
        messages: FallbackMessages | None = None,
        candidates: int = DEFAULT_CANDIDATES,
        catalog: FAQCatalog | None = None,
    ) -> None:
        """
        Args:
            hybrid: Retrieval ranking (keyword + semantic, RRF).
            keyword: Keyword retrieval, used alone when the semantic model fails.
            acceptance: Answer-acceptance rules.
            ambiguity: Ambiguity-detection rules.
            messages: Fallback texts.
            candidates: How many ranked candidates to evaluate (2..hybrid candidate depth).
            catalog: The FAQ catalogue whose snapshot each decision pins (the same one
                the retrievers read). None: FAQs never change while deciding.
        """
        _check_int("candidates", candidates, 2, hybrid.candidate_depth)
        self._hybrid = hybrid
        self._keyword = keyword
        self.acceptance = acceptance or AcceptancePolicy()
        self.ambiguity = ambiguity or AmbiguityPolicy()
        self.messages = messages or FallbackMessages()
        self.candidates = candidates
        self._catalog = catalog

    @property
    def max_top_k(self) -> int:
        """The largest ``top_k`` that ``decide`` accepts (bounded by the hybrid depth)."""
        return min(MAX_TOP_K, self._hybrid.candidate_depth)

    def decide(
        self,
        query: str,
        *,
        category: FAQCategory | str | None = None,
        top_k: int | None = None,
    ) -> AnswerDecision:
        """Decide how to respond to ``query``.

        Args:
            query: The customer question.
            category: Only consider FAQs in this category.
            top_k: How many ranked candidates to return in ``retrieved`` (1..max_top_k).
                Defaults to ``candidates``. It never changes the decision: that is always
                made from the first ``candidates`` results of the same ranking.

        Raises:
            ValueError: invalid query, ``top_k`` or unknown category (not a retrieval
                failure).
        """
        wanted = None if category is None else parse_category(category)
        shown = self.candidates if top_k is None else top_k
        _check_int("top_k", shown, 1, self.max_top_k)
        started = time.perf_counter()
        if self._catalog is None:
            decision = self._decide(query, wanted, shown)
        else:
            decision = self._decide_consistently(self._catalog, query, wanted, shown)
        _log_decision(decision, wanted, time.perf_counter() - started)
        return decision

    def _decide_consistently(
        self, catalog: FAQCatalog, query: str, category: FAQCategory | None, shown: int
    ) -> AnswerDecision:
        """Decide on one pinned snapshot; again on a newer one if it changed meanwhile."""
        for _ in range(CATALOG_ATTEMPTS):
            snapshot = catalog.latest()
            with catalog.pinned(snapshot):
                decision = self._decide(query, category, shown)
            if catalog.latest().revision == snapshot.revision:
                return decision
            logger.info(
                "event=faq_catalog_changed_during_decision revision=%d latest=%d",
                snapshot.revision,
                catalog.latest().revision,
            )
        return AnswerDecision(
            decision=Decision.RETRIEVAL_UNAVAILABLE,
            message=self.messages.unavailable,
            retrieval_mode=RetrievalMode.HYBRID,
            reasons=("the FAQ knowledge base changed during retrieval; try again",),
            retry_after_seconds=CATALOG_RETRY_AFTER_SECONDS,
        )

    def _decide(self, query: str, wanted: FAQCategory | None, shown: int) -> AnswerDecision:
        try:
            results = self._hybrid.search(query, top_k=max(self.candidates, shown), category=wanted)
        except EmbeddingError as exc:
            logger.warning(
                "Semantic retrieval unavailable (%s); trying keyword-only fallback",
                type(exc).__name__,
            )
            decision = self._degraded(
                query, wanted, getattr(exc, "retry_after_seconds", None), shown
            )
        else:
            ranked = [_evidence(r) for r in results]
            decision = dataclasses.replace(
                self._decide_from_hybrid(ranked[: self.candidates]),
                retrieved=tuple(ranked[:shown]),
            )
        return decision

    # --- Normal path --------------------------------------------------------------------------

    def _decide_from_hybrid(self, candidates: list[CandidateEvidence]) -> AnswerDecision:
        acceptable = [c for c in candidates if self.is_acceptable(c)]
        if not acceptable:
            exact = "an exact question match or " if self.acceptance.accept_exact_question else ""
            return AnswerDecision(
                decision=Decision.NO_MATCH,
                message=self.messages.no_match,
                retrieval_mode=RetrievalMode.HYBRID,
                reasons=(
                    f"no candidate had {exact}semantic similarity "
                    f">= {self.acceptance.min_similarity}",
                ),
                options=tuple(candidates[: self.ambiguity.max_options]),
            )

        top = acceptable[0]
        if top.exact_question and self.acceptance.accept_exact_question:
            return self._answered(top, RetrievalMode.HYBRID, "exact question match")

        competing = self._competing(top, acceptable[1:])
        if len(competing) > 1:
            listed = ", ".join(f"{c.faq.id} {c.semantic_similarity:.3f}" for c in competing)
            return AnswerDecision(
                decision=Decision.AMBIGUOUS,
                message=clarification_message(competing),
                retrieval_mode=RetrievalMode.HYBRID,
                reasons=(
                    f"{len(competing) - 1} other candidate(s) scored within "
                    f"{self.ambiguity.margin} of, or above, the top candidate's semantic "
                    f"similarity ({listed})",
                ),
                options=tuple(competing),
            )

        return self._answered(
            top,
            RetrievalMode.HYBRID,
            f"semantic similarity {top.semantic_similarity:.3f} >= "
            f"{self.acceptance.min_similarity}",
        )

    def is_acceptable(self, candidate: CandidateEvidence) -> bool:
        """Whether this candidate has enough relevance evidence to be shown (acceptance rule)."""
        if candidate.exact_question and self.acceptance.accept_exact_question:
            return True
        similarity = candidate.semantic_similarity
        return similarity is not None and meets_threshold(
            similarity, self.acceptance.min_similarity
        )

    def _competing(
        self, top: CandidateEvidence, others: list[CandidateEvidence]
    ) -> list[CandidateEvidence]:
        """The top candidate plus acceptable rivals close to (or above) its similarity.

        The top candidate here always has a similarity: it was accepted without an
        exact question match, so it passed the semantic threshold.
        """
        floor = top.semantic_similarity - self.ambiguity.margin  # type: ignore[operator]
        rivals = [
            c
            for c in others
            if c.semantic_similarity is not None and meets_threshold(c.semantic_similarity, floor)
        ]
        return [top, *rivals][: self.ambiguity.max_options]

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
        self, query: str, category: FAQCategory | None, retry_after: float | None, shown: int
    ) -> AnswerDecision:
        policy = self.acceptance
        ranked = self._keyword.search(
            query, top_k=max(DEGRADED_CANDIDATES, shown), category=category
        )
        retrieved = tuple(_keyword_evidence(c) for c in ranked[:shown])
        candidates = ranked[:DEGRADED_CANDIDATES]
        top = candidates[0] if candidates else None
        runner_up_score = candidates[1].score if len(candidates) > 1 else -math.inf

        if top is not None and top.exact_question and policy.accept_exact_question:
            reason = "semantic retrieval unavailable; exact question match (keyword only)"
        elif (
            top is not None
            and top.score >= policy.degraded_min_keyword_score
            and top.score > runner_up_score
            and _meaningful_terms(query) >= policy.degraded_min_query_terms
        ):
            reason = (
                f"semantic retrieval unavailable; clear keyword match "
                f"(score {top.score} >= {policy.degraded_min_keyword_score}, runner-up "
                f"{'none' if runner_up_score == -math.inf else int(runner_up_score)})"
            )
        else:
            return AnswerDecision(
                decision=Decision.RETRIEVAL_UNAVAILABLE,
                message=self.messages.unavailable,
                retrieval_mode=RetrievalMode.KEYWORD_ONLY,
                reasons=("semantic retrieval unavailable; no strong keyword-only evidence",),
                retry_after_seconds=retry_after,
                retrieved=retrieved,
            )

        decision = self._answered(_keyword_evidence(top), RetrievalMode.KEYWORD_ONLY, reason)
        return dataclasses.replace(decision, retry_after_seconds=retry_after, retrieved=retrieved)


# --- Helpers ------------------------------------------------------------------------------------


def _log_decision(decision: AnswerDecision, category: FAQCategory | None, seconds: float) -> None:
    """One structured line per decision. Identifiers and numbers only, never customer text."""
    similarities = [
        c.semantic_similarity for c in decision.retrieved if c.semantic_similarity is not None
    ]
    fields = {
        "event": "answer_decision",
        "decision": decision.decision.value,
        "retrieval_mode": decision.retrieval_mode.value,
        "faq_id": decision.faq.id if decision.faq is not None else None,
        "category": category.value if category is not None else None,
        "options": len(decision.options),
        "retrieved": len(decision.retrieved),
        "top_similarity": round(max(similarities), 4) if similarities else None,
        "duration_ms": round(seconds * 1000, 1),
    }
    logger.info(
        " ".join(f"{name}=%s" for name in fields),
        *("-" if value is None else value for value in fields.values()),
        extra={"decision_fields": fields},
    )


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


def _check_number(name: str, value: object, low: float, high: float) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number, not {type(value).__name__}")
    if not (math.isfinite(value) and low <= value <= high):
        raise ValueError(f"{name} must be between {low} and {high}, got {value}")


def _check_int(name: str, value: object, low: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, not {type(value).__name__}")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}, got {value}")
