"""Test doubles shared by several test modules."""

import threading
import zlib
from typing import Any

import numpy as np

from app.services.embedding_service import EmbeddingService, TextEncoder

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
