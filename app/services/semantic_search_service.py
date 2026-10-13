"""In-memory semantic index of active FAQs.

Each active FAQ question is turned into an embedding (see ``embedding_service``) and
stored in one matrix, row ``i`` belonging to ``faqs[i]``. Searching (a later step)
then only embeds the *query* and compares it with this precomputed matrix, instead of
re-embedding every FAQ on every request.

Lifecycle
---------
    NOT_BUILT --build_index() / get_index()--> READY --rebuild_index()--> READY
        ^                                        |
        +----------------- reset() --------------+

- ``get_index()`` builds lazily on first use; ``build_index()`` does the same
  explicitly (e.g. at startup) and is a no-op once built.
- A failed build (e.g. the model cannot be downloaded) leaves the service NOT_BUILT,
  so the next call retries.
- ``rebuild_index()`` re-embeds the current FAQs, e.g. after the data changes.

Concurrency
-----------
- Building is guarded by a lock, so concurrent first requests build the index once.
- The index is an immutable snapshot (tuples, read-only arrays, read-only mapping)
  that is published by a single reference assignment. Readers either see the old
  snapshot or the new one, never a half-built index, and need no lock.
- All methods are synchronous and CPU-heavy on first use. Call them from FastAPI
  ``def`` (not ``async def``) routes, which run in a worker thread, or from async code
  via ``await run_in_threadpool(...)``, never directly on the event loop.

Workers and processes
---------------------
The index lives in the memory of one Python process. Running several workers
(``uvicorn --workers 4``, gunicorn, several containers) means each worker loads its
own model and builds its own index: N times the memory and N first-use builds.
A shared vector database is the usual answer once that matters.
"""

import logging
import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

import numpy as np

from app.models.faq import FAQ, FAQCategory, find_duplicate_ids, parse_category
from app.services.embedding_service import EmbeddingError, EmbeddingService, Matrix

logger = logging.getLogger(__name__)

FAQProvider = Callable[[], Sequence[FAQ]]

DEFAULT_TOP_K = 5
MAX_TOP_K = 50
MAX_QUERY_LENGTH = 1000
MIN_COSINE = -1.0
MAX_COSINE = 1.0
# Embeddings are float32 (~7 significant digits), so a score that is "exactly" 0.8 may be
# computed as 0.7999999928. Scores within this tolerance of the threshold count as
# meeting it, so a result displayed as 0.8 is never rejected by min_score=0.8.
SCORE_TOLERANCE = 1e-6


class InvalidSemanticQueryError(ValueError):
    """The search query is blank, not a string, or too long. Contains no query text."""


@dataclass(frozen=True, slots=True)
class SemanticMatch:
    """One semantic search hit.

    ``faq`` is the approved FAQ record exactly as stored: its answer is returned
    verbatim, never generated or rewritten.

    ``score`` is the cosine similarity between the query and the FAQ question, a
    finite float between -1 and 1. Higher means the texts are closer in meaning. It
    is a relative ranking signal, *not* a probability or confidence percentage:
    0.6 does not mean "60% likely correct", and useful thresholds depend on the
    model and the data, so they must be chosen by testing.
    """

    faq: FAQ
    score: float


class IndexStatus(StrEnum):
    NOT_BUILT = "not_built"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class FAQEmbeddingIndex:
    """Immutable snapshot: ``embeddings[i]`` is the question embedding of ``faqs[i]``."""

    faqs: tuple[FAQ, ...]
    embeddings: Matrix
    positions: Mapping[str, int]
    model_name: str
    dimension: int
    build_seconds: float

    def __len__(self) -> int:
        return len(self.faqs)

    @property
    def faq_ids(self) -> tuple[str, ...]:
        return tuple(faq.id for faq in self.faqs)

    def get_faq(self, faq_id: str) -> FAQ | None:
        """The indexed FAQ with this ID (case-insensitive), or None."""
        position = self.positions.get(faq_id.strip().lower())
        return None if position is None else self.faqs[position]

    def get_embedding(self, faq_id: str) -> Matrix | None:
        """The stored embedding for this FAQ ID, or None if it is not indexed."""
        position = self.positions.get(faq_id.strip().lower())
        return None if position is None else self.embeddings[position]


class SemanticSearchService:
    """Owns the lifecycle of the FAQ embedding index for one application."""

    def __init__(
        self,
        faq_provider: FAQProvider,
        embedding_service: EmbeddingService,
        *,
        default_min_score: float | None = None,
    ) -> None:
        """
        Args:
            faq_provider: Returns the FAQs to index, e.g. ``FAQService.list_active_faqs``.
                Called on every (re)build; inactive FAQs are excluded regardless.
            embedding_service: Converts text to embeddings; loaded lazily.
            default_min_score: Similarity threshold applied when ``search`` is called
                without ``min_score``. None means no threshold. The application sets it
                from ``SEMANTIC_MIN_SIMILARITY``.

        Raises:
            ValueError: ``default_min_score`` is outside the cosine range -1..1.
        """
        _validate_min_score(default_min_score)
        self.default_min_score = default_min_score
        self._faq_provider = faq_provider
        self._embeddings = embedding_service
        self._index: FAQEmbeddingIndex | None = None
        self._lock = threading.Lock()

    @property
    def status(self) -> IndexStatus:
        return IndexStatus.READY if self._index is not None else IndexStatus.NOT_BUILT

    @property
    def is_ready(self) -> bool:
        return self._index is not None

    def get_index(self) -> FAQEmbeddingIndex:
        """Return the index, building it on first use (thread-safe, built once)."""
        index = self._index  # read once: another thread may swap it concurrently
        if index is not None:
            return index
        return self.build_index()

    def build_index(self) -> FAQEmbeddingIndex:
        """Build the index if it does not exist yet; otherwise return the existing one."""
        with self._lock:
            if self._index is None:  # double-check: another thread may have built it
                self._index = self._build()
            return self._index

    def rebuild_index(self) -> FAQEmbeddingIndex:
        """Re-embed the current FAQs and replace the index atomically.

        Requests already holding the previous index keep using it safely.
        """
        with self._lock:
            self._index = self._build()
            return self._index

    def reset(self) -> None:
        """Drop the index; the next use rebuilds it."""
        with self._lock:
            self._index = None

    def resolve_min_score(self, min_score: float | None) -> float | None:
        """The threshold a search will apply: ``min_score`` if given, else the default."""
        return self.default_min_score if min_score is None else min_score

    def search(
        self,
        query: str,
        *,
        top_k: int = DEFAULT_TOP_K,
        category: FAQCategory | str | None = None,
        min_score: float | None = None,
    ) -> list[SemanticMatch]:
        """Rank active FAQs by meaning similarity to ``query``, best first.

        Steps: validate and normalise the query, embed it (one model call), compute
        the cosine similarity with every indexed FAQ question, keep the requested
        category, sort by descending score (ties keep knowledge-base order), drop
        scores below ``min_score``, return at most ``top_k`` matches. Returns an
        empty list when nothing qualifies. Answers are the stored FAQ answers only.

        Args:
            query: Customer text. Surrounding/repeated whitespace is collapsed.
            top_k: Maximum results, 1..MAX_TOP_K.
            category: Only return FAQs in this category (case-insensitive).
            min_score: Only return matches with cosine similarity >= this value,
                which must lie in the cosine range -1.0..1.0. None uses
                ``default_min_score``. If no FAQ reaches the threshold the result is
                an empty list: there is no fallback to a lower-scoring FAQ.

        Raises:
            InvalidSemanticQueryError: the query is blank, not a string or too long.
            ValueError: ``top_k``, ``category`` or ``min_score`` is invalid.
            EmbeddingError: the model failed or produced an unusable query vector.
        """
        normalized_query = _normalize_query(query)
        _validate_top_k(top_k)
        _validate_min_score(min_score)
        min_score = self.resolve_min_score(min_score)
        wanted_category = None if category is None else parse_category(category)

        index = self.get_index()
        if len(index) == 0:
            return []

        query_vector = self._embeddings.encode_text(normalized_query)
        scores = _cosine_similarities(index.embeddings, query_vector)

        candidates = [
            (position, float(scores[position]))
            for position, faq in enumerate(index.faqs)
            if wanted_category is None or faq.category is wanted_category
        ]
        # Highest score first; equal scores keep knowledge-base order (stable, deterministic).
        candidates.sort(key=lambda item: (-item[1], item[0]))

        # No duplicates are possible: the index rejects duplicate IDs at build time and
        # each FAQ occupies exactly one row, so each position (and FAQ) appears once.
        matches: list[SemanticMatch] = []
        for position, score in candidates:
            if min_score is not None and not meets_threshold(score, min_score):
                break  # sorted descending: every remaining score is lower
            matches.append(SemanticMatch(faq=index.faqs[position], score=score))
            if len(matches) == top_k:
                break

        # Counts only: the query may contain customer data.
        logger.debug(
            "Semantic search: %d candidate(s), %d returned (top_k=%d, category=%s, min_score=%s)",
            len(candidates),
            len(matches),
            top_k,
            wanted_category,
            min_score,
        )
        return matches

    def _build(self) -> FAQEmbeddingIndex:
        started = time.perf_counter()
        faqs = tuple(faq for faq in self._faq_provider() if faq.is_active)
        if duplicates := find_duplicate_ids(faqs):
            raise ValueError(f"Cannot index duplicate FAQ ids: {', '.join(duplicates)}")

        if faqs:
            embeddings = self._embeddings.encode_batch([faq.question for faq in faqs])
            dimension = int(embeddings.shape[1])
        else:
            # Nothing to embed: do not load (or download) the model at all.
            embeddings = np.empty((0, 0), dtype=np.float32)
            dimension = 0
        embeddings.setflags(write=False)  # shared between threads: make it read-only

        index = FAQEmbeddingIndex(
            faqs=faqs,
            embeddings=embeddings,
            positions=MappingProxyType({faq.id: i for i, faq in enumerate(faqs)}),
            model_name=self._embeddings.model_name,
            dimension=dimension,
            build_seconds=time.perf_counter() - started,
        )
        logger.info(
            "Built semantic FAQ index: %d FAQs, dimension %d, model %r, in %.2f s",
            len(index),
            index.dimension,
            index.model_name,
            index.build_seconds,
        )
        return index


# --- Search helpers -------------------------------------------------------------------


def _normalize_query(query: object) -> str:
    if not isinstance(query, str):
        raise InvalidSemanticQueryError(f"query must be a string, not {type(query).__name__}")
    normalized = " ".join(query.split())
    if not normalized:
        raise InvalidSemanticQueryError("query must not be empty or whitespace-only")
    if len(normalized) > MAX_QUERY_LENGTH:
        raise InvalidSemanticQueryError(
            f"query is too long ({len(normalized)} characters, maximum {MAX_QUERY_LENGTH})"
        )
    return normalized


def _validate_top_k(top_k: object) -> None:
    # bool is a subclass of int: reject True/False explicitly.
    if isinstance(top_k, bool) or not isinstance(top_k, int):
        raise ValueError(f"top_k must be an integer, not {type(top_k).__name__}")
    if not 1 <= top_k <= MAX_TOP_K:
        raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}, got {top_k}")


def _validate_min_score(min_score: object) -> None:
    if min_score is None:
        return
    if isinstance(min_score, bool) or not isinstance(min_score, int | float):
        raise ValueError(f"min_score must be a number, not {type(min_score).__name__}")
    if not math.isfinite(min_score) or not MIN_COSINE <= min_score <= MAX_COSINE:
        raise ValueError(
            f"min_score must be between {MIN_COSINE} and {MAX_COSINE} "
            f"(the range of cosine similarity), got {min_score}"
        )


def _cosine_similarities(embeddings: Matrix, query_vector: Matrix) -> np.ndarray:
    """Cosine similarity of the query with every row: (E . q) / (|E_i| * |q|).

    Computed in float64 and divided by the actual norms, so it is correct even if the
    vectors were not normalised. Results are clipped to [-1, 1] to remove tiny
    floating-point overshoot (e.g. 1.0000001).
    """
    matrix = embeddings.astype(np.float64)
    query = np.asarray(query_vector, dtype=np.float64)
    query_norm = float(np.linalg.norm(query))
    row_norms = np.linalg.norm(matrix, axis=1)
    if not math.isfinite(query_norm) or query_norm == 0.0:
        raise EmbeddingError("query embedding is zero or not finite; cannot compute similarity")
    if not np.all(np.isfinite(row_norms)) or np.any(row_norms == 0.0):
        raise EmbeddingError("an indexed FAQ embedding is zero or not finite")
    # With finite, non-zero norms the result is finite: |a . b| <= |a| * |b|.
    scores = (matrix @ query) / (row_norms * query_norm)
    return np.clip(scores, MIN_COSINE, MAX_COSINE)


def meets_threshold(score: float, min_score: float) -> bool:
    """Whether ``score`` reaches ``min_score``, allowing for float32 rounding noise."""
    return score >= min_score - SCORE_TOLERANCE
