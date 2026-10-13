"""EmbeddingService behaviour, tested with a fast fake model (no download, no PyTorch).

One opt-in test at the end loads the real model; it is skipped unless
RUN_EMBEDDING_MODEL_TESTS=1 and the model is already cached locally.
"""

import logging
import os
import sys
import threading
import types
from typing import Any

import numpy as np
import pytest

from app.services.embedding_service import (
    EmbeddingError,
    EmbeddingModelLoadError,
    EmbeddingService,
    InvalidEmbeddingInputError,
    TextEncoder,
    load_sentence_transformer,
)
from tests.fakes import FAKE_DIMENSION, FAKE_MODEL_NAME, CountingLoader, FakeEncoder

MODEL_NAME = FAKE_MODEL_NAME


@pytest.fixture
def loader() -> CountingLoader:
    return CountingLoader()


@pytest.fixture
def service(loader: CountingLoader) -> EmbeddingService:
    return EmbeddingService(MODEL_NAME, loader=loader)


# --- Lazy loading and reuse ---------------------------------------------------------


def test_model_is_not_loaded_on_construction(
    service: EmbeddingService, loader: CountingLoader
) -> None:
    assert service.is_loaded is False
    assert loader.calls == []


def test_model_is_loaded_once_and_reused(service: EmbeddingService, loader: CountingLoader) -> None:
    service.encode_text("track my order")
    service.encode_batch(["reset password", "return policy"])
    service.encode_text("delivery time")

    assert loader.calls == [MODEL_NAME]
    assert service.is_loaded is True


def test_concurrent_first_requests_load_the_model_once() -> None:
    loader = CountingLoader(delay=0.2)  # slow load widens the race window
    service = EmbeddingService(MODEL_NAME, loader=loader)
    threads = [threading.Thread(target=service.encode_text, args=("hello",)) for _ in range(8)]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert loader.calls == [MODEL_NAME]


def test_explicit_load_for_warm_up(service: EmbeddingService, loader: CountingLoader) -> None:
    service.load()
    service.load()

    assert service.is_loaded
    assert loader.calls == [MODEL_NAME]


def test_dimension_loads_the_model_lazily(
    service: EmbeddingService, loader: CountingLoader
) -> None:
    assert service.dimension == FAKE_DIMENSION
    assert loader.calls == [MODEL_NAME]


def test_each_service_instance_has_its_own_model() -> None:
    first, second = CountingLoader(), CountingLoader()

    EmbeddingService(MODEL_NAME, loader=first).encode_text("a")
    EmbeddingService(MODEL_NAME, loader=second).encode_text("a")

    assert first.calls == second.calls == [MODEL_NAME]


# --- Encoding ------------------------------------------------------------------------


def test_encode_text_returns_unit_float32_vector(service: EmbeddingService) -> None:
    vector = service.encode_text("How can I track my order?")

    assert vector.shape == (FAKE_DIMENSION,)
    assert vector.dtype == np.float32
    assert np.linalg.norm(vector) == pytest.approx(1.0, abs=1e-6)


def test_encode_batch_returns_one_row_per_text_in_a_single_model_call(
    service: EmbeddingService, loader: CountingLoader
) -> None:
    texts = ["track my order", "reset my password", "return policy"]

    vectors = service.encode_batch(texts)

    assert vectors.shape == (3, FAKE_DIMENSION)
    assert vectors.dtype == np.float32
    encoder = loader.encoder
    assert isinstance(encoder, FakeEncoder)
    assert len(encoder.encode_calls) == 1
    sentences, kwargs = encoder.encode_calls[0]
    assert sentences == texts
    assert kwargs["batch_size"] == 32
    assert kwargs["normalize_embeddings"] is True
    assert kwargs["show_progress_bar"] is False


def test_encode_batch_rows_match_single_encodings(service: EmbeddingService) -> None:
    texts = ["track my order", "reset my password"]

    batch = service.encode_batch(texts)

    for row, text in zip(batch, texts, strict=True):
        np.testing.assert_allclose(row, service.encode_text(text), rtol=1e-6)


def test_encode_batch_accepts_tuples(service: EmbeddingService) -> None:
    assert service.encode_batch(("one", "two")).shape == (2, FAKE_DIMENSION)


def test_surrounding_whitespace_is_stripped(
    service: EmbeddingService, loader: CountingLoader
) -> None:
    service.encode_text("  track my order \n")

    assert isinstance(loader.encoder, FakeEncoder)
    assert loader.encoder.encode_calls[0][0] == ["track my order"]


def test_normalization_can_be_disabled(loader: CountingLoader) -> None:
    service = EmbeddingService(MODEL_NAME, loader=loader, normalize=False)

    vector = service.encode_text("order order order")

    assert np.linalg.norm(vector) > 1.0
    assert isinstance(loader.encoder, FakeEncoder)
    assert loader.encoder.encode_calls[0][1]["normalize_embeddings"] is False


def test_custom_batch_size_is_passed_to_the_model(loader: CountingLoader) -> None:
    EmbeddingService(MODEL_NAME, loader=loader, batch_size=4).encode_batch(["a", "b"])

    assert isinstance(loader.encoder, FakeEncoder)
    assert loader.encoder.encode_calls[0][1]["batch_size"] == 4


# --- Input validation -----------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_blank_text_is_rejected_without_loading_model(
    service: EmbeddingService, loader: CountingLoader, text: str
) -> None:
    with pytest.raises(InvalidEmbeddingInputError, match="empty or whitespace-only"):
        service.encode_text(text)

    assert loader.calls == []


def test_empty_batch_is_rejected(service: EmbeddingService) -> None:
    with pytest.raises(InvalidEmbeddingInputError, match="at least one text"):
        service.encode_batch([])


def test_blank_item_in_batch_reports_its_position(service: EmbeddingService) -> None:
    with pytest.raises(InvalidEmbeddingInputError, match="index 2 is empty"):
        service.encode_batch(["ok", "fine", "   "])


@pytest.mark.parametrize("bad", [None, 42, b"bytes"])
def test_non_string_items_are_rejected(service: EmbeddingService, bad: object) -> None:
    with pytest.raises(TypeError, match="index 1 must be a string"):
        service.encode_batch(["ok", bad])  # type: ignore[list-item]


def test_a_single_string_is_not_treated_as_a_batch(service: EmbeddingService) -> None:
    # Otherwise "hello" would silently become five one-letter texts.
    with pytest.raises(TypeError, match="not a single string"):
        service.encode_batch("hello")


def test_validation_errors_do_not_contain_the_text(service: EmbeddingService) -> None:
    customer_text = "my card is 4111 1111 1111 1111"
    with pytest.raises(TypeError) as exc:
        service.encode_batch([customer_text, 7])  # type: ignore[list-item]

    assert "4111" not in str(exc.value)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [({"model_name": "  "}, "model_name"), ({"model_name": "m", "batch_size": 0}, "batch_size")],
)
def test_invalid_constructor_arguments(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        EmbeddingService(**kwargs)


# --- Consistent dimensions -------------------------------------------------------------


def test_wrong_vector_shape_from_model_is_an_error() -> None:
    class WrongDimension(FakeEncoder):
        def encode(self, sentences: list[str], **kwargs: Any) -> np.ndarray:
            return np.ones((len(sentences), self.dimension + 1))

    service = EmbeddingService(MODEL_NAME, loader=CountingLoader(WrongDimension()))

    with pytest.raises(EmbeddingError, match="returned shape"):
        service.encode_text("hello")


def test_wrong_row_count_from_model_is_an_error() -> None:
    class DropsRows(FakeEncoder):
        def encode(self, sentences: list[str], **kwargs: Any) -> np.ndarray:
            return np.ones((1, self.dimension))

    service = EmbeddingService(MODEL_NAME, loader=CountingLoader(DropsRows()))

    with pytest.raises(EmbeddingError, match="returned shape"):
        service.encode_batch(["a", "b"])


@pytest.mark.parametrize("dimension", [None, 0, -1, "384"])
def test_model_with_invalid_dimension_fails_to_load(dimension: object) -> None:
    encoder = FakeEncoder()
    encoder.get_embedding_dimension = lambda: dimension  # type: ignore[method-assign]

    service = EmbeddingService(MODEL_NAME, loader=CountingLoader(encoder))

    with pytest.raises(EmbeddingModelLoadError, match="invalid dimension"):
        service.load()


# --- Model loading failures --------------------------------------------------------------


def test_model_load_failure_raises_clear_error(caplog: pytest.LogCaptureFixture) -> None:
    def failing_loader(model_name: str) -> TextEncoder:
        raise OSError(f"{model_name} is not a valid model identifier")

    service = EmbeddingService("missing/model", loader=failing_loader)

    with caplog.at_level(logging.ERROR), pytest.raises(EmbeddingModelLoadError) as exc:
        service.encode_text("hello")

    assert str(exc.value) == "Could not load embedding model 'missing/model': OSError"
    assert isinstance(exc.value.__cause__, OSError)
    assert "Failed to load embedding model 'missing/model': OSError" in caplog.text
    assert service.is_loaded is False


def test_loading_is_retried_after_a_failure() -> None:
    attempts: list[str] = []

    def flaky_loader(model_name: str) -> TextEncoder:
        attempts.append(model_name)
        if len(attempts) == 1:
            raise ConnectionError("offline")
        return FakeEncoder()

    service = EmbeddingService(MODEL_NAME, loader=flaky_loader)

    with pytest.raises(EmbeddingModelLoadError):
        service.encode_text("hello")
    assert service.encode_text("hello").shape == (FAKE_DIMENSION,)
    assert len(attempts) == 2


# --- Logging -----------------------------------------------------------------------------


def test_logs_model_load_but_never_the_text(
    service: EmbeddingService, caplog: pytest.LogCaptureFixture
) -> None:
    customer_text = "jane.doe@example.com wants refund for order 99812"

    with caplog.at_level(logging.DEBUG, logger="app.services.embedding_service"):
        service.encode_text(customer_text)
        service.encode_batch([customer_text, customer_text])

    assert f"Loaded embedding model '{MODEL_NAME}' (dimension {FAKE_DIMENSION})" in caplog.text
    assert "Embedded 2 text(s)" in caplog.text
    assert "jane.doe" not in caplog.text
    assert "99812" not in caplog.text


# --- Cosine similarity --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ([1, 0], [1, 0], 1.0),
        ([1, 0], [0, 1], 0.0),
        ([1, 0], [-1, 0], -1.0),
        ([1, 1], [1, 0], 1 / np.sqrt(2)),
        ([3, 4], [6, 8], 1.0),  # same direction, different length
    ],
)
def test_similarity_values(a: list[float], b: list[float], expected: float) -> None:
    assert EmbeddingService.similarity(a, b) == pytest.approx(expected)


def test_similarity_matches_the_formula() -> None:
    rng = np.random.default_rng(seed=7)
    a, b = rng.normal(size=384), rng.normal(size=384)

    expected = np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

    assert EmbeddingService.similarity(a, b) == pytest.approx(expected)


def test_similarity_of_normalised_embeddings_is_their_dot_product(
    service: EmbeddingService,
) -> None:
    a = service.encode_text("track my order")
    b = service.encode_text("where is my order")

    assert EmbeddingService.similarity(a, b) == pytest.approx(float(np.dot(a, b)), abs=1e-6)


def test_similar_texts_score_higher_than_unrelated_ones(service: EmbeddingService) -> None:
    query = service.encode_text("track my order")

    related = EmbeddingService.similarity(query, service.encode_text("track order status"))
    unrelated = EmbeddingService.similarity(query, service.encode_text("reset password"))

    assert related > unrelated


@pytest.mark.parametrize(
    ("a", "b", "message"),
    [
        ([0, 0], [1, 0], "zero vector"),
        ([1, 0], [1, 0, 0], "same length"),
        ([[1, 0]], [[1, 0]], "1-D"),
    ],
)
def test_similarity_rejects_invalid_vectors(a: object, b: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        EmbeddingService.similarity(a, b)  # type: ignore[arg-type]


# --- Default loader --------------------------------------------------------------------------


def test_default_loader_imports_lazily_and_sets_no_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    class FakeSentenceTransformer:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            created.append((args, kwargs))

    fake_module = types.ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = FakeSentenceTransformer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)

    load_sentence_transformer("sentence-transformers/all-MiniLM-L6-v2")

    assert created == [(("sentence-transformers/all-MiniLM-L6-v2",), {})]


# --- Opt-in: the real model ------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("RUN_EMBEDDING_MODEL_TESTS") != "1",
    reason="loads the real model (slow); set RUN_EMBEDDING_MODEL_TESTS=1 to run",
)
def test_real_model_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requires the model in the local Hugging Face cache; never downloads (offline mode)."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")  # read by huggingface_hub when first imported
    from sentence_transformers import util

    service = EmbeddingService("sentence-transformers/all-MiniLM-L6-v2")

    faqs = service.encode_batch(["How long does delivery take?", "How do I reset my password?"])
    query = service.encode_text("my package is late")

    assert service.dimension == 384
    assert faqs.shape == (2, 384)
    assert np.linalg.norm(query) == pytest.approx(1.0, abs=1e-5)
    ours = [EmbeddingService.similarity(query, row) for row in faqs]
    theirs = util.cos_sim(query, faqs)[0].tolist()
    assert ours == pytest.approx(theirs, abs=1e-5)
    assert ours[0] > ours[1]  # delivery question beats password question
