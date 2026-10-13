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
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

import numpy as np

from app.models.faq import FAQ, find_duplicate_ids
from app.services.embedding_service import EmbeddingService, Matrix

logger = logging.getLogger(__name__)

FAQProvider = Callable[[], Sequence[FAQ]]


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

    def __init__(self, faq_provider: FAQProvider, embedding_service: EmbeddingService) -> None:
        """
        Args:
            faq_provider: Returns the FAQs to index, e.g. ``FAQService.list_active_faqs``.
                Called on every (re)build; inactive FAQs are excluded regardless.
            embedding_service: Converts text to embeddings; loaded lazily.
        """
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
