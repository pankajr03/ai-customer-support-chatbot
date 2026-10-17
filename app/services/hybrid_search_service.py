"""Hybrid FAQ retrieval: keyword and semantic rankings combined with Reciprocal Rank Fusion.

Reciprocal Rank Fusion (RRF)
----------------------------
Each retriever returns its own ranked list (rank 1 = best). Every FAQ gets

    RRF(faq) = sum over retrievers of 1 / (k + rank(faq))

summed only over the retrievers that returned it. With the default k = 60:

    rank 1 in both lists      1/61 + 1/61 = 0.03279
    rank 1 in one list only   1/61        = 0.01639
    rank 2 + rank 3           1/62 + 1/63 = 0.03200

RRF only looks at ranks, never at the retrievers' own scores. That is why it can
combine keyword points (unbounded integers) with cosine similarities (-1..1): no score
normalisation is needed. A larger k makes lower ranks count relatively more.

RRF scores are ranking scores, NOT probabilities or confidence. Their absolute values
depend on k and on how many retrievers ran: 0.03 does not mean "3% likely", and a
cosine threshold such as 0.40 must never be applied to them. Whether a result is good
enough to show must be decided from the preserved per-retriever information (e.g. the
semantic cosine similarity or an exact keyword match), not from the RRF score.

Tie-breaking (deterministic): higher RRF score, then the better (lower) of the
individual ranks, then semantic rank, then keyword rank, then FAQ ID. Semantic rank
comes before keyword rank because semantic search had the higher top-1 accuracy in
the Stage 3 evaluation (86% vs 67%).
"""

import logging
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from app.models.faq import FAQ, FAQCategory, parse_category
from app.services.keyword_search_service import KeywordCandidate
from app.services.semantic_search_service import SemanticMatch

logger = logging.getLogger(__name__)

DEFAULT_RRF_K = 60
DEFAULT_CANDIDATE_DEPTH = 20
DEFAULT_TOP_K = 5
MAX_TOP_K = 50
MAX_CANDIDATE_DEPTH = 50  # the keyword and semantic services allow at most 50 results each

KEYWORD = "keyword"
SEMANTIC = "semantic"


# --- Retriever interfaces (satisfied by the real services and by test fakes) ------------------


class KeywordRetriever(Protocol):
    def search(
        self, query: str, *, top_k: int, category: FAQCategory | str | None
    ) -> list[KeywordCandidate]: ...


class SemanticRetriever(Protocol):
    def search(
        self,
        query: str,
        *,
        top_k: int,
        category: FAQCategory | str | None,
        min_score: float | None,
    ) -> list[SemanticMatch]: ...


# --- Results ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KeywordRank:
    """How keyword retrieval ranked an FAQ (score is in keyword points)."""

    rank: int
    score: int
    exact_question: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SemanticRank:
    """How semantic retrieval ranked an FAQ (similarity is cosine, -1..1)."""

    rank: int
    similarity: float


@dataclass(frozen=True, slots=True)
class HybridResult:
    """One fused result.

    ``rrf_score`` orders results; it is not a probability or a confidence. ``keyword``
    and ``semantic`` keep each retriever's original rank and score (None if that
    retriever did not return this FAQ).
    """

    faq_id: str
    rank: int
    rrf_score: float
    faq: FAQ
    keyword: KeywordRank | None
    semantic: SemanticRank | None

    @property
    def retrievers(self) -> tuple[str, ...]:
        """Which retrievers returned this FAQ, e.g. ("keyword", "semantic")."""
        found = ((KEYWORD, self.keyword), (SEMANTIC, self.semantic))
        return tuple(name for name, info in found if info is not None)


@dataclass(frozen=True, slots=True)
class FusedRanking:
    """Output of ``reciprocal_rank_fusion``: one entry per unique item ID."""

    item_id: str
    rrf_score: float
    ranks: Mapping[str, int]  # retriever name -> 1-based rank in that retriever


# --- Pure fusion --------------------------------------------------------------------------------


def reciprocal_rank_fusion(
    rankings: Mapping[str, Sequence[str]],
    *,
    k: float = DEFAULT_RRF_K,
    tie_break_order: Sequence[str] = (SEMANTIC, KEYWORD),
) -> list[FusedRanking]:
    """Fuse ranked ID lists with RRF. Pure function: no I/O, no models.

    Args:
        rankings: Retriever name -> item IDs, best first (position 0 is rank 1). An ID
            repeated within one list counts once, at its best rank.
        k: RRF constant (> 0).
        tie_break_order: Retrievers whose ranks break ties, in priority order.

    Returns:
        Unique items sorted by descending RRF score, ties broken deterministically.
    """
    if not (isinstance(k, int | float) and not isinstance(k, bool)) or not (
        math.isfinite(k) and k > 0
    ):
        raise ValueError(f"RRF k must be a positive number, got {k!r}")

    ranks: dict[str, dict[str, int]] = {}
    for retriever, ids in rankings.items():
        for position, item_id in enumerate(ids, start=1):
            ranks.setdefault(item_id, {}).setdefault(retriever, position)  # keep best rank

    fused = [
        FusedRanking(
            item_id=item_id,
            rrf_score=sum(1.0 / (k + rank) for rank in item_ranks.values()),
            ranks=dict(item_ranks),
        )
        for item_id, item_ranks in ranks.items()
    ]

    def sort_key(item: FusedRanking) -> tuple:
        missing = math.inf
        return (
            -item.rrf_score,
            min(item.ranks.values()),
            *(item.ranks.get(name, missing) for name in tie_break_order),
            item.item_id,
        )

    return sorted(fused, key=sort_key)


# --- Service ------------------------------------------------------------------------------------


class HybridSearchService:
    """Retrieve candidates from keyword and semantic search and fuse them with RRF."""

    def __init__(
        self,
        keyword: KeywordRetriever,
        semantic: SemanticRetriever,
        *,
        rrf_k: float = DEFAULT_RRF_K,
        candidate_depth: int = DEFAULT_CANDIDATE_DEPTH,
        semantic_candidate_min_similarity: float = -1.0,
    ) -> None:
        """
        Args:
            keyword: Keyword retriever (``KeywordSearchService``).
            semantic: Semantic retriever (``SemanticSearchService``).
            rrf_k: RRF constant k (> 0). Default 60.
            candidate_depth: How many candidates to take from each retriever (1-50).
                Must be at least the largest ``top_k`` you will request.
            semantic_candidate_min_similarity: Cosine cut-off for the semantic
                candidate list *before* fusion. Default -1 keeps the full ranking. It
                filters semantic candidates; it is never applied to RRF scores.
        """
        reciprocal_rank_fusion({}, k=rrf_k)  # validates k
        _validate_int("candidate_depth", candidate_depth, 1, MAX_CANDIDATE_DEPTH)
        if not -1.0 <= semantic_candidate_min_similarity <= 1.0:
            raise ValueError("semantic_candidate_min_similarity must be between -1 and 1")
        self._keyword = keyword
        self._semantic = semantic
        self.rrf_k = rrf_k
        self.candidate_depth = candidate_depth
        self.semantic_candidate_min_similarity = semantic_candidate_min_similarity

    def search(
        self,
        query: str,
        *,
        top_k: int = DEFAULT_TOP_K,
        category: FAQCategory | str | None = None,
    ) -> list[HybridResult]:
        """Return the ``top_k`` best fused results (empty if neither retriever finds any).

        Keyword retrieval runs first: it validates the query cheaply, so an invalid query
        never loads the embedding model.

        Raises:
            ValueError: invalid ``top_k`` (1..min(MAX_TOP_K, candidate_depth)), invalid
                query (subclasses of ValueError from either retriever) or unknown category.
            EmbeddingError: the semantic model is unavailable.
        """
        _validate_int("top_k", top_k, 1, min(MAX_TOP_K, self.candidate_depth))
        wanted = None if category is None else parse_category(category)

        started = time.perf_counter()
        keyword_candidates = self._keyword.search(
            query, top_k=self.candidate_depth, category=wanted
        )
        keyword_done = time.perf_counter()
        semantic_matches = self._semantic.search(
            query,
            top_k=self.candidate_depth,
            category=wanted,
            min_score=self.semantic_candidate_min_similarity,
        )
        logger.debug(
            "event=hybrid_search keyword_ms=%.1f semantic_ms=%.1f keyword=%d semantic=%d",
            (keyword_done - started) * 1000,
            (time.perf_counter() - keyword_done) * 1000,
            len(keyword_candidates),
            len(semantic_matches),
        )

        keyword_info = {c.faq_id: c for c in keyword_candidates}
        semantic_info = {m.faq.id: m for m in semantic_matches}
        # The keyword retriever reads the catalogue directly, so its FAQ objects win
        # over the index's copies (identical when the index matches the revision).
        faqs = {m.faq.id: m.faq for m in semantic_matches}
        faqs.update({c.faq_id: c.faq for c in keyword_candidates})

        fused = reciprocal_rank_fusion(
            {
                KEYWORD: [c.faq_id for c in keyword_candidates],
                SEMANTIC: [m.faq.id for m in semantic_matches],
            },
            k=self.rrf_k,
        )
        return [
            HybridResult(
                faq_id=item.item_id,
                rank=position,
                rrf_score=item.rrf_score,
                faq=faqs[item.item_id],
                keyword=_keyword_rank(keyword_info.get(item.item_id), item.ranks),
                semantic=_semantic_rank(semantic_info.get(item.item_id), item.ranks),
            )
            for position, item in enumerate(fused[:top_k], start=1)
        ]


def _keyword_rank(
    candidate: KeywordCandidate | None, ranks: Mapping[str, int]
) -> KeywordRank | None:
    if candidate is None:
        return None
    return KeywordRank(
        rank=ranks[KEYWORD],
        score=candidate.score,
        exact_question=candidate.exact_question,
        reasons=candidate.reasons,
    )


def _semantic_rank(match: SemanticMatch | None, ranks: Mapping[str, int]) -> SemanticRank | None:
    if match is None:
        return None
    return SemanticRank(rank=ranks[SEMANTIC], similarity=match.score)


def _validate_int(name: str, value: object, low: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, not {type(value).__name__}")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}, got {value}")
