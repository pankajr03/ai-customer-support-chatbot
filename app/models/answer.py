"""Request and response models for grounded answers (POST /api/v1/answer).

Each request is independent: there is no conversation history in Stage 5.
The response carries the customer-facing answer, the Stage 4 decision, the FAQs the
answer is based on, how the answer was produced, and the actual token usage of the LLM
call, if one was made. Prompts, model reasons and other diagnostics are never included.
"""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from app.models.faq import Category
from app.models.generation import GenerationOutcome, GenerationResult
from app.models.semantic_search import SearchQueryText
from app.services.answer_decision_service import Decision


class GenerationMethod(StrEnum):
    LLM_GROUNDED = "llm_grounded"  # the LLM rewrote the approved answer (validated)
    APPROVED_FAQ = "approved_faq"  # the matched FAQ answer, verbatim
    CLARIFICATION = "clarification"  # a clarifying question built from retrieved FAQs
    FALLBACK = "fallback"  # a safe message: nothing matched, or search is unavailable


_METHODS = {
    GenerationOutcome.GENERATED: GenerationMethod.LLM_GROUNDED,
    GenerationOutcome.APPROVED_ANSWER: GenerationMethod.APPROVED_FAQ,
    GenerationOutcome.CLARIFICATION: GenerationMethod.CLARIFICATION,
    GenerationOutcome.NO_MATCH: GenerationMethod.FALLBACK,
    GenerationOutcome.RETRIEVAL_UNAVAILABLE: GenerationMethod.FALLBACK,
}

_EXAMPLE_REQUEST = {"query": "How do I reset my password?", "category": None}
# Illustrative only: real responses carry the actual answer, FAQ IDs and usage.
_EXAMPLE_RESPONSE = {
    "query": "How do I reset my password?",
    "decision": "answered",
    "answer": "Select Sign In, then Forgot Password, and enter the email address on your "
    "account. We'll email you a link to set a new password; it expires after 1 hour.",
    "source_faq_ids": ["faq_027"],
    "generation_method": "llm_grounded",
    "usage": {
        "input_tokens": 850,
        "output_tokens": 70,
        "total_tokens": 920,
        "estimated_cost_usd": None,
    },
}


class AnswerRequest(BaseModel):
    """One customer question. Each request is answered independently (no history)."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [_EXAMPLE_REQUEST]})

    query: SearchQueryText = Field(
        description="The customer's question. Repeated and surrounding whitespace is "
        "removed; must not be blank (1-1000 characters)."
    )
    category: Category | None = Field(
        default=None, description="Only use FAQs in this category (case-insensitive)."
    )


class UsageSummary(BaseModel):
    """Actual token usage reported by the LLM API for this request."""

    input_tokens: int | None = Field(ge=0, description="Input tokens reported by the API.")
    output_tokens: int | None = Field(ge=0, description="Output tokens reported by the API.")
    total_tokens: int | None = Field(ge=0, description="Total tokens reported by the API.")
    estimated_cost_usd: float | None = Field(
        ge=0,
        description="ESTIMATED cost in US dollars from configured prices (LLM_PRICING with "
        "LLM_PRICING_CURRENCY=USD); null when no USD price is configured for the model. "
        "Not a bill.",
    )


class AnswerResponse(BaseModel):
    """A grounded answer and how it was produced."""

    model_config = ConfigDict(json_schema_extra={"examples": [_EXAMPLE_RESPONSE]})

    query: str = Field(description="The question as answered, after whitespace normalisation.")
    decision: Decision = Field(
        description="The Stage 4 decision: answered, ambiguous, no_match or "
        "retrieval_unavailable. The LLM can never change it."
    )
    answer: str = Field(description="The text to show the customer.")
    source_faq_ids: list[str] = Field(
        description="FAQs the answer is based on (for a clarification: the FAQs it offers; "
        "empty for fallbacks). For llm_grounded answers these are the FAQs the model "
        "reported using, checked against the supplied evidence; they are not proof that "
        "every statement is supported."
    )
    generation_method: GenerationMethod = Field(
        description="llm_grounded: the LLM rewrote the approved FAQ answer and passed "
        "validation. approved_faq: the approved FAQ answer, verbatim (LLM disabled, failed "
        "or overruled, or search degraded). clarification: a clarifying question. "
        "fallback: a safe message because nothing matched or search is unavailable."
    )
    usage: UsageSummary | None = Field(
        description="Actual LLM token usage, when an LLM response was received (also when "
        "it was then rejected); null when no LLM call produced a response."
    )

    @classmethod
    def from_result(cls, query: str, decision: Decision, result: GenerationResult) -> Self:
        return cls(
            query=query,
            decision=decision,
            answer=result.answer,
            source_faq_ids=list(result.source_faq_ids),
            generation_method=generation_method(result),
            usage=usage_summary(result),
        )


def generation_method(result: GenerationResult) -> GenerationMethod:
    """How a generation result was produced, as reported to API clients."""
    return _METHODS[result.outcome]


def usage_summary(result: GenerationResult) -> UsageSummary | None:
    """Token usage of the LLM call behind a result; None when no response was received."""
    d = result.diagnostics
    if d.model is None:  # set only when the API returned a response
        return None
    return UsageSummary(
        input_tokens=d.input_tokens,
        output_tokens=d.output_tokens,
        total_tokens=d.total_tokens,
        estimated_cost_usd=d.estimated_cost if d.cost_currency == "USD" else None,
    )
