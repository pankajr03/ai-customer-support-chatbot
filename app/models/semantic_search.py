"""Request and response models for semantic (meaning-based) FAQ search.

Scores are cosine similarities computed by the embedding model at request time,
never fixed values. Cosine similarity ranges from -1 to 1; it is a relative ranking
signal, not a probability or confidence percentage.
"""

from typing import Annotated

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    computed_field,
)

from app.models.faq import FAQ, Category, FAQCategory
from app.services.semantic_search_service import (
    DEFAULT_TOP_K,
    MAX_COSINE,
    MAX_QUERY_LENGTH,
    MAX_TOP_K,
    MIN_COSINE,
)

SCORE_DECIMALS = 4

SIMILARITY_DESCRIPTION = (
    "Cosine similarity between the query and the FAQ question, from -1 to 1 "
    f"(rounded to {SCORE_DECIMALS} decimal places). Higher means closer in meaning. "
    "It is a relative ranking signal computed by the embedding model for this request, "
    "not a probability or a confidence percentage."
)

_EXAMPLE_FAQ = {
    "faq_id": "faq_001",
    "question": "How can I track my order?",
    "answer": (
        "You can track your order from the My Orders section of your account. Once your "
        "order has shipped, you will also receive an email with a tracking link. Tracking "
        "details can take up to 24 hours to appear after dispatch."
    ),
    "category": "orders",
}
# Illustrative only: real responses carry the score computed for the actual query.
_EXAMPLE_SCORE = 0.6412


def _collapse_whitespace(value: object) -> object:
    return " ".join(value.split()) if isinstance(value, str) else value


SearchQueryText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_QUERY_LENGTH),
    BeforeValidator(_collapse_whitespace),
]


class SemanticSearchRequest(BaseModel):
    """Parameters for a semantic FAQ search."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "query": "Where is my package?",
                    "top_k": 3,
                    "category": None,
                    "min_similarity": 0.4,
                }
            ]
        },
    )

    query: SearchQueryText = Field(
        description="Customer question. Repeated and surrounding whitespace is removed; "
        "must not be blank."
    )
    top_k: StrictInt = Field(
        default=DEFAULT_TOP_K,
        ge=1,
        le=MAX_TOP_K,
        description=f"Maximum number of results (1-{MAX_TOP_K}).",
    )
    category: Category | None = Field(
        default=None, description="Only search FAQs in this category (case-insensitive)."
    )
    min_similarity: float | None = Field(
        default=None,
        ge=MIN_COSINE,
        le=MAX_COSINE,
        allow_inf_nan=False,
        description="Only return results whose similarity_score is at least this value "
        "(cosine range -1 to 1). Omit or send null to use the server default "
        "(SEMANTIC_MIN_SIMILARITY, initially 0.40, an experimental value). Send -1 to "
        "disable the threshold. If no FAQ qualifies, results is empty; no lower-scoring "
        "FAQ is substituted. Suitable values depend on the model and data and should be "
        "chosen by testing with real questions.",
    )


class SemanticSearchResult(BaseModel):
    """One approved FAQ and how similar its question is to the query."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{**_EXAMPLE_FAQ, "similarity_score": _EXAMPLE_SCORE}]}
    )

    faq_id: str = Field(description="ID of the matched FAQ.")
    question: str = Field(description="The FAQ question that was matched.")
    answer: str = Field(description="The approved FAQ answer, returned verbatim.")
    category: FAQCategory
    similarity_score: float = Field(
        ge=MIN_COSINE, le=MAX_COSINE, allow_inf_nan=False, description=SIMILARITY_DESCRIPTION
    )

    @classmethod
    def from_faq(cls, faq: FAQ, similarity: float) -> "SemanticSearchResult":
        """Build a result from a stored FAQ and the similarity computed for it."""
        return cls(
            faq_id=faq.id,
            question=faq.question,
            answer=faq.answer,
            category=faq.category,
            similarity_score=round(similarity, SCORE_DECIMALS),
        )


class SemanticSearchResponse(BaseModel):
    """Semantic search results, most similar first.

    ``results`` is empty when no FAQ reaches the similarity threshold; no fallback FAQ
    or generated answer is substituted.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "query": "Where is my package?",
                    "min_similarity": 0.4,
                    "results": [{**_EXAMPLE_FAQ, "similarity_score": _EXAMPLE_SCORE}],
                    "total_results": 1,
                }
            ]
        }
    )

    query: str = Field(description="The query that was searched, after whitespace normalisation.")
    min_similarity: float | None = Field(
        ge=MIN_COSINE,
        le=MAX_COSINE,
        description="The similarity threshold actually applied: the request value, or the "
        "server default when the request omitted it. null means no threshold.",
    )
    results: list[SemanticSearchResult] = Field(
        description="Matches ordered by similarity_score, highest first; ties keep "
        "knowledge-base order."
    )

    @computed_field(description="Number of results returned (always len(results)).")
    @property
    def total_results(self) -> int:
        return len(self.results)
