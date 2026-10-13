"""Test doubles shared by several test modules."""

import threading
import zlib
from typing import Any

import numpy as np

from app.models.faq import FAQ
from app.services.embedding_service import EmbeddingService, TextEncoder
from app.services.faq_search import MatchField
from app.services.hybrid_search_service import HybridResult, KeywordRank, SemanticRank
from app.services.keyword_search_service import KeywordCandidate
from tests.faq_test_data import TEST_FAQ_RECORDS

FAKE_DIMENSION = 16
FAKE_MODEL_NAME = "fake/test-model"


class FakeEncoder:
    """Deterministic bag-of-words embeddings: texts sharing words get similar vectors.

    Fast, needs no download and no PyTorch. Records every ``encode`` call.
    """

    def __init__(self, dimension: int = FAKE_DIMENSION, delay: float = 0.0) -> None:
        self.dimension = dimension
        self.delay = delay
        self.encode_calls: list[tuple[list[str], dict[str, Any]]] = []
        self._calls_lock = threading.Lock()

    def get_embedding_dimension(self) -> int:
        return self.dimension

    def encode(self, sentences: list[str], **kwargs: Any) -> np.ndarray:
        with self._calls_lock:
            self.encode_calls.append((list(sentences), kwargs))
        if self.delay:
            threading.Event().wait(self.delay)
        vectors = np.zeros((len(sentences), self.dimension), dtype=np.float64)
        for row, sentence in enumerate(sentences):
            for word in sentence.lower().split():
                vectors[row, zlib.crc32(word.encode()) % self.dimension] += 1.0
        if kwargs.get("normalize_embeddings"):
            vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors


class CountingLoader:
    """Model loader that records how often the model is loaded."""

    def __init__(self, encoder: TextEncoder | None = None, delay: float = 0.0) -> None:
        self.encoder = encoder or FakeEncoder()
        self.calls: list[str] = []
        self.delay = delay

    def __call__(self, model_name: str) -> TextEncoder:
        self.calls.append(model_name)
        if self.delay:
            threading.Event().wait(self.delay)
        return self.encoder


def make_fake_embedding_service(
    encoder: FakeEncoder | None = None,
) -> tuple[EmbeddingService, FakeEncoder, CountingLoader]:
    """An EmbeddingService backed by a FakeEncoder, plus handles to inspect it."""
    encoder = encoder or FakeEncoder()
    loader = CountingLoader(encoder)
    return EmbeddingService(FAKE_MODEL_NAME, loader=loader), encoder, loader


# --- Deterministic retrieval results for decision tests ---------------------------------------

FAQS = {r["id"]: FAQ.model_validate(r) for r in TEST_FAQ_RECORDS}


def result(
    faq_id: str,
    *,
    similarity: float | None = None,
    keyword_score: int | None = None,
    exact: bool = False,
    rrf: float = 0.03,
) -> tuple:
    """Spec for one hybrid result: (faq_id, similarity, keyword_score, exact, rrf)."""
    return (faq_id, similarity, keyword_score, exact, rrf)


class FakeHybrid:
    def __init__(self, specs: list[tuple], error: Exception | None = None) -> None:
        self.specs = specs
        self.error = error
        self.candidate_depth = 20
        self.calls: list[dict[str, Any]] = []

    def search(self, query: str, *, top_k: int, category: Any) -> list[HybridResult]:
        self.calls.append({"query": query, "top_k": top_k, "category": category})
        if self.error:
            raise self.error
        results = []
        for rank, (faq_id, similarity, kw_score, exact, rrf) in enumerate(self.specs, start=1):
            keyword = (
                None
                if kw_score is None and not exact
                else KeywordRank(rank=rank, score=kw_score or 100, exact_question=exact, reasons=())
            )
            semantic = (
                None if similarity is None else SemanticRank(rank=rank, similarity=similarity)
            )
            results.append(HybridResult(faq_id, rank, rrf, FAQS[faq_id], keyword, semantic))
        return results[:top_k]


class FakeKeyword:
    def __init__(self, specs: list[tuple[str, int, bool]] | None = None) -> None:
        self.specs = specs or []
        self.calls: list[dict[str, Any]] = []

    def search(self, query: str, *, top_k: int, category: Any) -> list[KeywordCandidate]:
        self.calls.append({"query": query, "top_k": top_k, "category": category})
        return [
            KeywordCandidate(
                faq_id, rank, score, exact, frozenset({MatchField.QUESTION}), (), FAQS[faq_id]
            )
            for rank, (faq_id, score, exact) in enumerate(self.specs[:top_k], start=1)
        ]
