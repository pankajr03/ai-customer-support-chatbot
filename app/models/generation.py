"""The structured result of grounded answer generation.

``GenerationResult`` is what the application uses. Its customer-facing fields are
``answer``, ``is_answerable``, ``source_faq_ids`` and ``outcome``. Everything internal
(the Stage 4 decision, why the LLM was skipped or its text not used, the model's own
reason, model and request metadata) lives in ``diagnostics``, which is excluded from
serialisation: ``model_dump()`` and ``model_dump_json()`` never include it, so it cannot
reach a customer by accident.

``source_faq_ids`` names the FAQs the answer is based on. For a generated answer these
are the FAQs the model *claims* to have used; they are guaranteed to be FAQs supplied
for this question (always including the FAQ Stage 4 matched), but that is not proof
that the answer is factually supported by them. For an approved answer it is the FAQ
returned verbatim; for a clarifying question, the FAQs it offers.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class GenerationOutcome(StrEnum):
    GENERATED = "generated"  # the LLM rewrote the approved answer (validated)
    APPROVED_ANSWER = "approved_answer"  # the matched FAQ answer, verbatim, no LLM text
    CLARIFICATION = "clarification"  # Stage 4's clarifying question (ambiguous)
    NO_MATCH = "no_match"  # the configured no-match message
    RETRIEVAL_UNAVAILABLE = "retrieval_unavailable"  # the configured unavailable message


class FallbackReason(StrEnum):
    """Why the answer is not LLM-generated. None when it is."""

    # The decision rules did not allow an LLM call.
    STAGE4_AMBIGUOUS = "stage4_ambiguous"
    STAGE4_NO_MATCH = "stage4_no_match"
    STAGE4_RETRIEVAL_UNAVAILABLE = "stage4_retrieval_unavailable"
    DEGRADED_RETRIEVAL = "degraded_retrieval"  # answered on keyword evidence only
    PRIMARY_NOT_IN_CONTEXT = "primary_not_in_context"  # matched FAQ did not fit the budget
    # The LLM could not be used.
    LLM_DISABLED = "llm_disabled"  # LLM_ENABLED=false
    LLM_NOT_CONFIGURED = "llm_not_configured"  # no API key
    LLM_UNAVAILABLE = "llm_unavailable"  # timeouts / 429 / 5xx after retries
    LLM_REJECTED = "llm_rejected"  # 4xx: invalid key, unknown model, bad request, quota
    UNUSABLE_RESPONSE = "unusable_response"  # empty, refused, incomplete or failed
    # The LLM replied, but its text was not used.
    INVALID_OUTPUT = "invalid_output"  # not valid JSON / schema / unknown source IDs
    PRIMARY_NOT_CITED = "primary_not_cited"  # did not base its answer on the matched FAQ
    MODEL_DECLINED = "model_declined"  # asked to clarify or said the FAQ is insufficient
    GROUNDING_CHECK_FAILED = "grounding_check_failed"  # unsupported numbers, terms, promises


class GenerationDiagnostics(BaseModel):
    """Internal details for logs, evaluation and support staff. Never shown to customers.

    Contains no customer text: ``detail`` is a fixed description from this application,
    and ``model_reason`` is the model's short note about its choice of status.
    """

    model_config = ConfigDict(frozen=True)

    stage4_decision: str | None = None
    retrieval_mode: str | None = None
    llm_called: bool = False
    fallback_reason: FallbackReason | None = None
    # ErrorCategory of a failed LLM call (e.g. "authentication", "timeout", "circuit_open"),
    # or "malformed_output" when the reply was not a valid structured response.
    llm_error_category: str | None = None
    detail: str | None = None
    model_status: str | None = None  # the status the model returned, if any
    model_reason: str | None = None
    # Grounding issues as "kind: detail" (details come from the model's answer, so they
    # stay in diagnostics; logs carry only the kinds, via ``detail``).
    grounding_issues: tuple[str, ...] = ()
    context_faq_ids: tuple[str, ...] = ()
    prompt_version: str | None = None
    model: str | None = None
    response_id: str | None = None
    request_id: str | None = None
    attempts: int | None = None
    latency_ms: float | None = None
    # Token usage as reported by the API (None = not reported) and the cost estimate,
    # which is only set when a price is configured (LLM_PRICING). Not a bill.
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    estimated_cost: float | None = None
    cost_currency: str | None = None


class GenerationResult(BaseModel):
    """A customer-facing answer plus excluded internal diagnostics."""

    model_config = ConfigDict(frozen=True)

    answer: str = Field(min_length=1, description="Text to show the customer.")
    is_answerable: bool = Field(
        description="True when the answer addresses the question from approved FAQs "
        "(generated or verbatim); false for clarifying questions and safe fallbacks."
    )
    source_faq_ids: tuple[str, ...] = Field(
        description="FAQs the answer is based on (claimed by the model for generated "
        "answers; not proof of factual support)."
    )
    outcome: GenerationOutcome
    diagnostics: GenerationDiagnostics = Field(default_factory=GenerationDiagnostics, exclude=True)

    @property
    def is_generated(self) -> bool:
        return self.outcome is GenerationOutcome.GENERATED
