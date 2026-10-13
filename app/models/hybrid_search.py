"""Request and response models for hybrid FAQ search with an answer decision.

The response states a decision (answered / ambiguous / no_match / retrieval_unavailable)
and carries exactly one customer-facing text: the approved FAQ answer verbatim, a
clarifying question, or a safe fallback message. The fields that do not apply are null.

``results`` is retrieval evidence for debugging and evaluation, not a set of answers:
it never contains FAQ answers. Scores are computed for each request, never fixed.
"""

from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    computed_field,
    model_validator,
)

from app.models.faq import Category, FAQCategory
from app.models.semantic_search import SCORE_DECIMALS, SIMILARITY_DESCRIPTION, SearchQueryText
from app.services.answer_decision_service import (
    AnswerDecision,
    CandidateEvidence,
    Decision,
    RetrievalMode,
)
from app.services.hybrid_search_service import DEFAULT_TOP_K, MAX_TOP_K
from app.services.semantic_search_service import MAX_COSINE, MIN_COSINE

RRF_DECIMALS = 6

# Illustrative only: real responses carry the IDs and scores computed for the actual query.
_EXAMPLE_RESULT = {
    "rank": 1,
    "faq_id": "faq_001",
    "question": "How can I track my order?",
    "category": "orders",
    "semantic_similarity": 0.6412,
    "keyword_score": 45,
    "exact_question_match": False,
    "rrf_score": 0.032787,
}


class HybridSearchRequest(BaseModel):
    """Parameters for a hybrid FAQ search."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{"query": "Where is my package?", "top_k": 3, "category": None}]
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
        description="Maximum number of retrieval results to return as evidence (at most "
        f"{MAX_TOP_K}, and at most the server's HYBRID_CANDIDATE_DEPTH). It does not "
        "change the decision.",
    )
    category: Category | None = Field(
        default=None, description="Only search FAQs in this category (case-insensitive)."
    )


class HybridSearchResult(BaseModel):
    """Retrieval evidence for one FAQ. Not an answer: the FAQ answer is never included."""

    model_config = ConfigDict(json_schema_extra={"examples": [_EXAMPLE_RESULT]})

    rank: int = Field(ge=1, description="Position in the retrieval ranking, 1 = best.")
    faq_id: str = Field(description="ID of the retrieved FAQ.")
    question: str = Field(description="The FAQ question that was retrieved.")
    category: FAQCategory
    semantic_similarity: float | None = Field(
        ge=MIN_COSINE,
        le=MAX_COSINE,
        allow_inf_nan=False,
        description=f"{SIMILARITY_DESCRIPTION} null if semantic retrieval did not return "
        "this FAQ or was unavailable.",
    )
    keyword_score: int | None = Field(
        description="Keyword relevance points (unbounded, not a percentage). null if "
        "keyword retrieval did not return this FAQ."
    )
    exact_question_match: bool = Field(
        description="True when the query is exactly this FAQ's question."
    )
    rrf_score: float | None = Field(
        ge=0,
        allow_inf_nan=False,
        description=f"Reciprocal Rank Fusion score (rounded to {RRF_DECIMALS} decimal "
        "places). Orders results only: it is not a probability or confidence and does not "
        "decide whether to answer. null when only keyword retrieval ran.",
    )

    @classmethod
    def from_evidence(cls, rank: int, evidence: CandidateEvidence) -> Self:
        similarity = evidence.semantic_similarity
        return cls(
            rank=rank,
            faq_id=evidence.faq.id,
            question=evidence.faq.question,
            category=evidence.faq.category,
            semantic_similarity=None if similarity is None else round(similarity, SCORE_DECIMALS),
            keyword_score=evidence.keyword_score,
            exact_question_match=evidence.exact_question,
            rrf_score=None
            if evidence.rrf_score is None
            else round(evidence.rrf_score, RRF_DECIMALS),
        )


class HybridSearchResponse(BaseModel):
    """The decision for a customer question, with the retrieval evidence behind it.

    Exactly one of ``answer``, ``clarification_question`` and ``fallback_message`` is
    set, depending on ``decision``; the others are null. ``matched_faq_id`` is set only
    when answered.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "query": "Where is my package?",
                    "decision": "answered",
                    "retrieval_mode": "hybrid",
                    "answer": "You can track your order from the My Orders section of your "
                    "account.",
                    "matched_faq_id": "faq_001",
                    "clarification_question": None,
                    "fallback_message": None,
                    "results": [_EXAMPLE_RESULT],
                    "total_results": 1,
                }
            ]
        }
    )

    query: str = Field(description="The query that was searched, after whitespace normalisation.")
    decision: Decision = Field(
        description="answered: an approved FAQ answer is returned. ambiguous: several FAQs "
        "fit about equally well, so a clarifying question is returned. no_match: no FAQ is "
        "relevant enough, so a fallback message is returned. retrieval_unavailable: search "
        "is temporarily unavailable, so a fallback message is returned."
    )
    retrieval_mode: RetrievalMode = Field(
        description="hybrid: keyword and semantic retrieval ran. keyword_only: the semantic "
        "model was unavailable and a degraded keyword-only search ran."
    )
    answer: str | None = Field(
        description="The approved FAQ answer, verbatim. Only set when answered."
    )
    matched_faq_id: str | None = Field(
        description="ID of the FAQ whose answer is returned. Only set when answered."
    )
    clarification_question: str | None = Field(
        description="A question asking the customer to clarify. Only set when ambiguous."
    )
    fallback_message: str | None = Field(
        description="A safe message for the customer. Only set for no_match and "
        "retrieval_unavailable."
    )
    results: list[HybridSearchResult] = Field(
        description="Retrieval evidence, best ranked first, at most top_k. The matched FAQ "
        "is not necessarily the first result, or present at all when top_k is small."
    )

    @computed_field(description="Number of results returned (always len(results)).")
    @property
    def total_results(self) -> int:
        return len(self.results)

    @model_validator(mode="after")
    def _one_customer_text(self) -> Self:
        expected = {
            Decision.ANSWERED: ("answer", "matched_faq_id"),
            Decision.AMBIGUOUS: ("clarification_question",),
            Decision.NO_MATCH: ("fallback_message",),
            Decision.RETRIEVAL_UNAVAILABLE: ("fallback_message",),
        }[self.decision]
        for name in ("answer", "matched_faq_id", "clarification_question", "fallback_message"):
            if (getattr(self, name) is not None) != (name in expected):
                raise ValueError(f"{name} does not match decision {self.decision.value}")
        return self

    @classmethod
    def from_decision(cls, query: str, decision: AnswerDecision) -> Self:
        """Build the public response; internal reasons and thresholds are not exposed."""
        outcome = decision.decision
        return cls(
            query=query,
            decision=outcome,
            retrieval_mode=decision.retrieval_mode,
            answer=decision.message if outcome is Decision.ANSWERED else None,
            matched_faq_id=decision.faq.id if decision.faq is not None else None,
            clarification_question=decision.message if outcome is Decision.AMBIGUOUS else None,
            fallback_message=(
                decision.message
                if outcome in (Decision.NO_MATCH, Decision.RETRIEVAL_UNAVAILABLE)
                else None
            ),
            results=[
                HybridSearchResult.from_evidence(rank, evidence)
                for rank, evidence in enumerate(decision.retrieved, start=1)
            ],
        )
