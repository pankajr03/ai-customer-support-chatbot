"""Semantic search with hand-chosen embeddings, so every score and rank is known exactly.

``ControlledEncoder`` maps each text to a fixed vector. The FAQ vectors are chosen so
that their cosine similarity with the query vector [1, 0, 0, 0] is exactly:

    faq_101 (orders)    1.0     faq_104 (shipping)  0.0
    faq_102 (orders)    0.8     faq_106 (returns)   0.0
    faq_103 (shipping)  0.6     faq_108 (payments)  0.0
    faq_107 (account)  -1.0

Texts without a vector raise KeyError, so embedding an unexpected text (for example an
inactive FAQ's question) makes the test fail loudly.
"""

import math
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.models.semantic_search import SCORE_DECIMALS
from app.services.embedding_service import EmbeddingError, EmbeddingService
from app.services.faq_service import FAQService
from app.services.semantic_search_service import (
    MAX_TOP_K,
    SCORE_TOLERANCE,
    SemanticSearchService,
    meets_threshold,
)
from tests.fakes import CountingLoader
from tests.faq_test_data import TEST_FAQ_RECORDS

QUESTION = {record["id"]: record["question"] for record in TEST_FAQ_RECORDS}

FAQ_VECTORS = {
    "faq_101": [1.0, 0.0, 0.0, 0.0],
    "faq_102": [0.8, 0.6, 0.0, 0.0],
    "faq_103": [0.6, 0.8, 0.0, 0.0],
    "faq_104": [0.0, 1.0, 0.0, 0.0],
    "faq_106": [0.0, 0.0, 1.0, 0.0],
    "faq_107": [-1.0, 0.0, 0.0, 0.0],
    "faq_108": [0.0, 0.0, 0.0, 1.0],
}
QUERIES = {
    "order query": [1.0, 0.0, 0.0, 0.0],
    "unrelated query": [0.0, 0.0, -1.0, 0.0],  # only faq_106 is non-zero: -1.0
    "nan query": [math.nan, 0.0, 0.0, 0.0],
    "infinite query": [math.inf, 0.0, 0.0, 0.0],
}
EXPECTED_ORDER_QUERY = [
    ("faq_101", 1.0),
    ("faq_102", 0.8),
    ("faq_103", 0.6),
    ("faq_104", 0.0),  # three-way tie at 0.0 keeps knowledge-base order
    ("faq_106", 0.0),
    ("faq_108", 0.0),
    ("faq_107", -1.0),
]


class ControlledEncoder:
    """Returns pre-set vectors; never invents one."""

    def __init__(self, vectors: dict[str, list[float]], scale: float = 1.0) -> None:
        self.vectors = vectors
        self.scale = scale
        self.encoded: list[str] = []

    def get_embedding_dimension(self) -> int:
        return 4

    def encode(self, sentences: list[str], **kwargs: Any) -> np.ndarray:
        self.encoded.extend(sentences)
        rows = np.array([self.vectors[text] for text in sentences], dtype=np.float64)
        rows *= self.scale
        if kwargs.get("normalize_embeddings"):
            with np.errstate(invalid="ignore", divide="ignore"):
                rows /= np.linalg.norm(rows, axis=1, keepdims=True)
        return rows


def vectors(faq_vectors: dict[str, list[float]] | None = None) -> dict[str, list[float]]:
    faq_vectors = faq_vectors or FAQ_VECTORS
    return {**{QUESTION[i]: v for i, v in faq_vectors.items()}, **QUERIES}


def controlled_service(
    faq_service: FAQService, encoder: ControlledEncoder | None = None, **kwargs: Any
) -> SemanticSearchService:
    embeddings = EmbeddingService(
        "controlled", loader=CountingLoader(encoder or ControlledEncoder(vectors())), **kwargs
    )
    return SemanticSearchService(faq_service.list_active_faqs, embeddings)


def ranked(matches: list) -> list[tuple[str, float]]:
    return [(m.faq.id, round(m.score, 6)) for m in matches]


@pytest.fixture
def semantic(faq_service: FAQService) -> SemanticSearchService:
    return controlled_service(faq_service)


# --- Ranking and scores -----------------------------------------------------------------------


def test_exact_ranking_and_scores(semantic: SemanticSearchService) -> None:
    assert ranked(semantic.search("order query", top_k=MAX_TOP_K)) == EXPECTED_ORDER_QUERY


def test_scores_do_not_depend_on_vector_length(faq_service: FAQService) -> None:
    """Cosine ignores magnitude: scaled, unnormalised vectors give the same scores."""
    encoder = ControlledEncoder(vectors(), scale=37.5)
    semantic = controlled_service(faq_service, encoder, normalize=False)

    assert ranked(semantic.search("order query", top_k=MAX_TOP_K)) == EXPECTED_ORDER_QUERY


def test_ties_keep_knowledge_base_order_on_every_run(semantic: SemanticSearchService) -> None:
    runs = {tuple(ranked(semantic.search("order query", top_k=MAX_TOP_K))) for _ in range(20)}

    assert runs == {tuple(EXPECTED_ORDER_QUERY)}


def test_no_duplicate_results(semantic: SemanticSearchService) -> None:
    ids = [faq_id for faq_id, _ in ranked(semantic.search("order query", top_k=MAX_TOP_K))]

    assert len(ids) == len(set(ids)) == len(FAQ_VECTORS)


@pytest.mark.parametrize("top_k", [1, 2, 3, 7])
def test_top_k_returns_the_exact_prefix(semantic: SemanticSearchService, top_k: int) -> None:
    assert ranked(semantic.search("order query", top_k=top_k)) == EXPECTED_ORDER_QUERY[:top_k]


# --- Filters ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("orders", [("faq_101", 1.0), ("faq_102", 0.8)]),
        ("shipping", [("faq_103", 0.6), ("faq_104", 0.0)]),
        ("account", [("faq_107", -1.0)]),
        ("refunds", []),
    ],
)
def test_category_filter(
    semantic: SemanticSearchService, category: str, expected: list[tuple[str, float]]
) -> None:
    assert ranked(semantic.search("order query", top_k=MAX_TOP_K, category=category)) == expected


@pytest.mark.parametrize(
    ("threshold", "expected_ids"),
    [
        (1.0, ["faq_101"]),
        (0.8, ["faq_101", "faq_102"]),  # inclusive: exactly 0.8 is kept
        (0.61, ["faq_101", "faq_102"]),
        (0.6, ["faq_101", "faq_102", "faq_103"]),
        (0.0, ["faq_101", "faq_102", "faq_103", "faq_104", "faq_106", "faq_108"]),
        (-1.0, [faq_id for faq_id, _ in EXPECTED_ORDER_QUERY]),
    ],
)
def test_threshold_filter(
    semantic: SemanticSearchService, threshold: float, expected_ids: list[str]
) -> None:
    results = semantic.search("order query", top_k=MAX_TOP_K, min_score=threshold)

    assert [m.faq.id for m in results] == expected_ids


def test_filters_combine(semantic: SemanticSearchService) -> None:
    results = semantic.search("order query", top_k=1, category="shipping", min_score=0.5)

    assert ranked(results) == [("faq_103", 0.6)]


def test_nothing_above_threshold_returns_empty_list(semantic: SemanticSearchService) -> None:
    assert semantic.search("unrelated query", min_score=0.1) == []
    assert ranked(semantic.search("unrelated query", top_k=1, min_score=-1.0)) == [
        ("faq_101", 0.0)  # the "closest" FAQ exists but was correctly withheld above
    ]


# --- Index: metadata and exclusions -----------------------------------------------------------


def test_each_faq_keeps_its_own_embedding(semantic: SemanticSearchService) -> None:
    index = semantic.get_index()

    for faq_id, vector in FAQ_VECTORS.items():
        stored = index.get_embedding(faq_id)
        faq = index.get_faq(faq_id)
        assert stored is not None and faq is not None
        np.testing.assert_allclose(stored, np.array(vector) / np.linalg.norm(vector), atol=1e-7)
        assert faq.question == QUESTION[faq_id]


def test_inactive_faq_questions_are_never_embedded(faq_service: FAQService) -> None:
    # The encoder has no vectors for faq_105/faq_109: embedding them would raise KeyError.
    encoder = ControlledEncoder(vectors())
    controlled_service(faq_service, encoder).search("order query")

    assert QUESTION["faq_105"] not in encoder.encoded
    assert QUESTION["faq_109"] not in encoder.encoded
    assert sorted(encoder.encoded[: len(FAQ_VECTORS)]) == sorted(QUESTION[i] for i in FAQ_VECTORS)


# --- Invalid scores ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["nan query", "infinite query"])
def test_non_finite_query_embedding_is_rejected(
    semantic: SemanticSearchService, query: str
) -> None:
    with pytest.raises(EmbeddingError, match="query embedding"):
        semantic.search(query)


@pytest.mark.parametrize("bad_value", [math.nan, math.inf, 0.0])
def test_unusable_faq_embedding_in_index_is_rejected(
    faq_service: FAQService, bad_value: float
) -> None:
    bad = {**FAQ_VECTORS, "faq_108": [bad_value, 0.0, 0.0, 0.0]}
    semantic = controlled_service(faq_service, ControlledEncoder(vectors(bad)), normalize=False)

    with pytest.raises(EmbeddingError, match="indexed FAQ embedding"):
        semantic.search("order query")


def test_scores_never_exceed_the_cosine_range(faq_service: FAQService) -> None:
    # Nearly identical vectors can produce 1.0000000002 in floating point; it is clipped.
    near = {**FAQ_VECTORS, "faq_101": [1.0, 1e-9, 0.0, 0.0]}
    semantic = controlled_service(faq_service, ControlledEncoder(vectors(near)), normalize=False)

    scores = [m.score for m in semantic.search("order query", top_k=MAX_TOP_K)]

    assert all(-1.0 <= score <= 1.0 and math.isfinite(score) for score in scores)


# --- API with controlled embeddings -----------------------------------------------------------


@pytest.fixture
def client(faq_service: FAQService) -> Iterator[TestClient]:
    semantic = controlled_service(faq_service)
    app = create_app(
        Settings(_env_file=None), faq_service=faq_service, semantic_search_service=semantic
    )
    with TestClient(app) as test_client:
        yield test_client


def test_api_returns_exact_ranking_and_scores(client: TestClient) -> None:
    body = client.post(
        "/api/v1/semantic-search",
        json={"query": "order query", "top_k": 3, "min_similarity": -1},
    ).json()

    assert [(r["faq_id"], r["similarity_score"]) for r in body["results"]] == (
        EXPECTED_ORDER_QUERY[:3]
    )


def test_api_category_and_threshold(client: TestClient) -> None:
    body = client.post(
        "/api/v1/semantic-search",
        json={"query": "order query", "category": "shipping", "min_similarity": 0.5},
    ).json()

    assert body["min_similarity"] == 0.5
    assert [(r["faq_id"], r["similarity_score"]) for r in body["results"]] == [("faq_103", 0.6)]


def test_api_no_match_response(client: TestClient) -> None:
    response = client.post(
        "/api/v1/semantic-search", json={"query": "unrelated query", "min_similarity": 0.1}
    )

    assert response.status_code == 200
    assert response.json() == {
        "query": "unrelated query",
        "min_similarity": 0.1,
        "results": [],
        "total_results": 0,
    }


def test_api_invalid_embedding_is_503(client: TestClient) -> None:
    response = client.post("/api/v1/semantic-search", json={"query": "nan query"})

    assert response.status_code == 503
    assert "nan" not in response.text.lower()


# --- Threshold tolerance for float32 rounding --------------------------------------------------


@pytest.mark.parametrize(
    ("score", "threshold", "expected"),
    [
        (0.7999999928474427, 0.8, True),  # float32 noise below a score designed to be 0.8
        (0.8, 0.8, True),
        (0.8000001, 0.8, True),
        (0.7999995, 0.8, True),  # within 1e-6
        (0.7999985, 0.8, False),  # beyond the tolerance
        (0.7999, 0.8, False),
        (-1.0, -1.0, True),
    ],
)
def test_meets_threshold(score: float, threshold: float, expected: bool) -> None:
    assert meets_threshold(score, threshold) is expected


def test_tolerance_is_below_displayed_precision() -> None:
    # Scores are shown with 4 decimals; the tolerance must never change a visible value.
    assert SCORE_TOLERANCE < 0.5 * 10**-SCORE_DECIMALS
