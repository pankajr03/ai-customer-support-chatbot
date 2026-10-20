"""Turn text into sentence embeddings for semantic search.

An embedding is a fixed-length list of numbers (a vector) that represents the meaning
of a text. Texts with similar meanings get vectors that point in similar directions,
even when they share no words ("my package is late" vs "How long does delivery take?").

Cosine similarity
-----------------
Cosine similarity measures the angle between two vectors, ignoring their length:

    cosine(a, b) = (a . b) / (|a| * |b|)

where ``a . b`` is the dot product (sum of element-wise products) and ``|a|`` is the
vector's length (Euclidean norm). The result is between -1 and 1:

    1.0   same direction       -> same meaning
    ~0.0  perpendicular        -> unrelated
    -1.0  opposite direction   -> rarely seen with sentence embeddings

This service normalises embeddings to length 1 by default. For unit vectors the
denominator is 1, so cosine similarity is just the dot product: cheap to compute for
many FAQs at once (one matrix multiplication).

Loading
-------
The model is loaded lazily on first use, once per service instance, and reused. The
heavy libraries (sentence-transformers, PyTorch) are imported only inside the loader,
never at module import time. Create one service per application, not per request.
"""

import logging
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import numpy.typing as npt

from app.core.config import PINNED_EMBEDDING_MODEL_REVISIONS
from app.services.model_files import MANIFEST_NAME, verify_model_folder

logger = logging.getLogger(__name__)

Vector = npt.NDArray[np.float32]
Matrix = npt.NDArray[np.float32]

DEFAULT_BATCH_SIZE = 32


# --- Exceptions -----------------------------------------------------------------


class EmbeddingError(Exception):
    """Base class for embedding failures."""


class EmbeddingModelLoadError(EmbeddingError):
    """The embedding model could not be loaded (not found, offline, corrupt...).

    ``retry_after_seconds`` is set when the service will not try again before then
    (see ``load_retry_seconds``); HTTP handlers can expose it as ``Retry-After``.
    """

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class InvalidEmbeddingInputError(ValueError):
    """Text passed for embedding is missing or blank. Contains no text content."""


# --- The model interface ----------------------------------------------------------


class TextEncoder(Protocol):
    """The part of ``SentenceTransformer`` this service relies on.

    Any object with these methods can be used, which is how tests supply a fast fake.
    """

    def encode(self, sentences: list[str], **kwargs: Any) -> Any:
        """Return one embedding row per sentence."""
        ...

    def get_embedding_dimension(self) -> int | None:
        """Length of each embedding vector."""
        ...


ModelLoader = Callable[[str], TextEncoder]


def load_sentence_transformer(
    model_name: str,
    factory: Callable[..., TextEncoder] | None = None,
    *,
    revision: str | None = None,
) -> TextEncoder:
    """Load a sentence-transformers model from a local folder or the local cache.

    - **A local folder** (the Docker deployment): its files are first checked against the
      folder's ``model-manifest.json`` (``app.services.model_files``), so a corrupt or
      incomplete copy is reported by file name instead of failing obscurely. Nothing is
      ever downloaded for a folder.
    - **A Hub ID** (local development): loaded at ``revision`` (``EMBEDDING_MODEL_REVISION``,
      the commit every evaluation used) from the Hugging Face cache. A plain
      ``SentenceTransformer(name)`` asks the Hub for the latest files even when the model is
      cached (dozens of requests, each waiting for a timeout when there is no internet);
      ``local_files_only=True`` first makes no network calls, and the Hub is contacted only
      when that revision is not cached yet. ``HF_HUB_OFFLINE=1`` forbids even that.

    The import lives here so that importing this module stays fast and never loads
    PyTorch. No device is specified: sentence-transformers uses a GPU if one is
    available and the CPU otherwise.

    Args:
        model_name: Hugging Face model name or local path.
        factory: Builds the model (``SentenceTransformer``); replaced in tests.
        revision: The Hub commit to load (ignored for a local folder). Default: the
            pinned commit for a known model (``PINNED_EMBEDDING_MODEL_REVISIONS``).

    Raises:
        ModelFilesError: a local folder's files are missing or corrupt.
    """
    if factory is None:
        from sentence_transformers import SentenceTransformer

        factory = SentenceTransformer
    folder = Path(model_name)
    if folder.is_dir():
        manifest = verify_model_folder(folder)
        if manifest is None:
            logger.warning(
                "Embedding model folder %s has no %s: loading it unverified", folder, MANIFEST_NAME
            )
        else:
            logger.info(
                "Embedding model files verified: %s at revision %s (%d files, %.1f MB)",
                manifest.repo,
                manifest.revision,
                len(manifest.files),
                manifest.total_bytes / 1e6,
            )
        return factory(str(folder), local_files_only=True)
    revision = revision or PINNED_EMBEDDING_MODEL_REVISIONS.get(model_name)
    pinned = {"revision": revision} if revision else {}
    try:
        return factory(model_name, local_files_only=True, **pinned)
    except OSError:  # not in the local cache
        logger.info(
            "Embedding model %r (revision %s) is not cached locally; downloading it",
            model_name,
            revision or "latest",
        )
        return factory(model_name, **pinned)


# --- Service ----------------------------------------------------------------------


class EmbeddingService:
    """Lazily loads one embedding model and converts text into vectors."""

    def __init__(
        self,
        model_name: str,
        *,
        loader: ModelLoader = load_sentence_transformer,
        batch_size: int = DEFAULT_BATCH_SIZE,
        normalize: bool = True,
        load_retry_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Args:
            model_name: Hugging Face model ID or local folder.
            loader: Loads the model; replaced by a fake in tests.
            batch_size: Texts per model call inside ``encode_batch``.
            normalize: Return unit-length vectors.
            load_retry_seconds: After a failed load, fail fast for this many seconds
                instead of retrying on every call. Prevents queued requests from each
                repeating a slow failing download. 0 retries immediately.
            clock: Monotonic time source (injectable for tests).
        """
        if not model_name.strip():
            raise ValueError("model_name must not be blank")
        if batch_size < 1:
            raise ValueError(f"batch_size must be at least 1, got {batch_size}")
        if load_retry_seconds < 0:
            raise ValueError(f"load_retry_seconds must not be negative, got {load_retry_seconds}")
        self.model_name = model_name.strip()
        self.batch_size = batch_size
        self.normalize = normalize
        self.load_retry_seconds = load_retry_seconds
        self._loader = loader
        self._clock = clock
        self._model: TextEncoder | None = None
        self._dimension: int | None = None
        self._failed_at: float | None = None
        self._lock = threading.Lock()

    @property
    def is_loaded(self) -> bool:
        """Whether the model has been loaded (it is not loaded until first needed)."""
        return self._model is not None

    @property
    def load_failed_recently(self) -> bool:
        """Whether the last load failed less than ``load_retry_seconds`` ago, so calls
        fail fast instead of loading. Read-only: never loads or downloads anything."""
        failed_at = self._failed_at
        if failed_at is None or self._model is not None:
            return False
        return self._clock() - failed_at < self.load_retry_seconds

    @property
    def model_files_missing(self) -> bool:
        """Whether the model is configured as a local folder that has no model in it, so
        it can never load (in the deployment: model-init has not been run). A Hub ID is
        never reported missing: telling would need the network. Read-only: one stat."""
        if self._model is not None:
            return False
        folder = Path(self.model_name)
        return folder.is_absolute() and not (folder / "config.json").is_file()

    @property
    def dimension(self) -> int:
        """Length of every embedding this service returns (loads the model if needed)."""
        self._get_model()
        if self._dimension is None:  # pragma: no cover - always set together with the model
            raise EmbeddingError("embedding dimension unavailable")
        return self._dimension

    def load(self) -> None:
        """Load the model now, e.g. to warm up at startup instead of on first request."""
        self._get_model()

    def encode_text(self, text: str) -> Vector:
        """Embed one text. Returns a 1-D float32 vector of length ``dimension``.

        Raises:
            InvalidEmbeddingInputError: ``text`` is blank.
            EmbeddingModelLoadError: the model could not be loaded.
        """
        if isinstance(text, str) and not text.strip():
            raise InvalidEmbeddingInputError("text is empty or whitespace-only")
        return self.encode_batch([text])[0]

    def encode_batch(self, texts: Sequence[str]) -> Matrix:
        """Embed several texts in one model call. Returns shape (len(texts), dimension).

        Batching is much faster than embedding texts one by one.

        Raises:
            InvalidEmbeddingInputError: ``texts`` is empty or contains a blank text.
            EmbeddingModelLoadError: the model could not be loaded.
            EmbeddingError: the model failed while encoding, or returned vectors of an
                unexpected shape.
        """
        cleaned = _validate_texts(texts)
        model = self._get_model()

        started = time.perf_counter()
        try:
            raw = model.encode(
                cleaned,
                batch_size=self.batch_size,
                normalize_embeddings=self.normalize,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            vectors = np.asarray(raw, dtype=np.float32)
        except Exception as exc:  # noqa: BLE001 - any model failure means "no embeddings"
            # The type only: a message could quote the texts (customer data).
            logger.error(
                "Embedding model %r failed to encode (%s)", self.model_name, type(exc).__name__
            )
            raise EmbeddingError(
                f"Embedding model {self.model_name!r} failed to encode ({type(exc).__name__})"
            ) from exc

        expected_shape = (len(cleaned), self._dimension)
        if vectors.shape != expected_shape:
            raise EmbeddingError(
                f"Embedding model {self.model_name!r} returned shape {vectors.shape}, "
                f"expected {expected_shape}"
            )
        # Counts and timings only: the texts may contain customer data.
        logger.debug(
            "Embedded %d text(s) in %.1f ms", len(cleaned), (time.perf_counter() - started) * 1000
        )
        return vectors

    @staticmethod
    def similarity(vector_a: npt.ArrayLike, vector_b: npt.ArrayLike) -> float:
        """Cosine similarity of two vectors, between -1 and 1 (see module docstring).

        Works for normalised and unnormalised vectors alike.

        Raises:
            ValueError: the vectors are not 1-D, differ in length, or one is all zeros.
        """
        a = np.asarray(vector_a, dtype=np.float64)
        b = np.asarray(vector_b, dtype=np.float64)
        if a.ndim != 1 or b.ndim != 1 or a.shape != b.shape:
            raise ValueError(
                f"vectors must be 1-D and the same length, got {a.shape} and {b.shape}"
            )
        norm_product = np.linalg.norm(a) * np.linalg.norm(b)
        if norm_product == 0:
            raise ValueError("cosine similarity is undefined for a zero vector")
        return float(np.clip(np.dot(a, b) / norm_product, -1.0, 1.0))

    def _get_model(self) -> TextEncoder:
        """Return the model, loading it exactly once even under concurrent requests.

        After a failed load, calls within ``load_retry_seconds`` fail immediately
        instead of queueing behind the lock and each repeating the failure.
        """
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is None:  # another thread may have loaded it while we waited
                self._raise_if_cooling_down()
                try:
                    self._model, self._dimension = self._load_model()
                except EmbeddingModelLoadError as exc:
                    self._failed_at = self._clock()
                    exc.retry_after_seconds = self.load_retry_seconds or None
                    raise
                self._failed_at = None
        return self._model

    def _raise_if_cooling_down(self) -> None:
        if self._failed_at is None or not self.load_retry_seconds:
            return
        remaining = self.load_retry_seconds - (self._clock() - self._failed_at)
        if remaining > 0:
            logger.debug("Embedding model load skipped: retry in %.0f s", remaining)
            raise EmbeddingModelLoadError(
                f"Embedding model {self.model_name!r} failed to load recently; "
                f"next attempt in {remaining:.0f} s",
                retry_after_seconds=remaining,
            )

    def _load_model(self) -> tuple[TextEncoder, int]:
        logger.info("Loading embedding model %r (first use)", self.model_name)
        started = time.perf_counter()
        try:
            model = self._loader(self.model_name)
            dimension = model.get_embedding_dimension()
        except Exception as exc:
            logger.error(
                "Failed to load embedding model %r: %s: %s",
                self.model_name,
                type(exc).__name__,
                exc,
            )
            raise EmbeddingModelLoadError(
                f"Could not load embedding model {self.model_name!r}: {type(exc).__name__}"
            ) from exc
        if not isinstance(dimension, int) or dimension < 1:
            raise EmbeddingModelLoadError(
                f"Embedding model {self.model_name!r} reported an invalid dimension: {dimension!r}"
            )
        logger.info(
            "Loaded embedding model %r (dimension %d) in %.1f s",
            self.model_name,
            dimension,
            time.perf_counter() - started,
        )
        return model, dimension


def _validate_texts(texts: Sequence[str]) -> list[str]:
    """Strip surrounding whitespace and reject blank or non-string inputs.

    Error messages give positions, never the text itself.
    """
    if isinstance(texts, str) or not isinstance(texts, Sequence):
        raise TypeError("texts must be a sequence of strings, not a single string")
    if not texts:
        raise InvalidEmbeddingInputError("at least one text is required")
    cleaned = []
    for index, text in enumerate(texts):
        if not isinstance(text, str):
            raise TypeError(f"text at index {index} must be a string, not {type(text).__name__}")
        if not text.strip():
            raise InvalidEmbeddingInputError(f"text at index {index} is empty or whitespace-only")
        cleaned.append(text.strip())
    return cleaned
