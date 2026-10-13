"""The structured result of grounded answer generation.

``GenerationResult`` is what the application uses. Its customer-facing fields are
``answer``, ``is_answerable``, ``source_faq_ids`` and ``outcome``. Everything internal
(the model's own reason, why a fallback was used, model and request metadata) lives in
``diagnostics``, which is excluded from serialisation: ``model_dump()`` and
``model_dump_json()`` never include it, so it cannot reach a customer by accident.

``source_faq_ids`` names the FAQs the answer is based on. For a generated answer these
are the FAQs the model *claims* to have used; they are guaranteed to be FAQs supplied
for this question, but that is not proof that the answer is factually supported by
them. For a fallback they are the FAQ whose approved answer is returned verbatim.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class GenerationOutcome(StrEnum):
    GENERATED = "generated"  # the model answered from the supplied FAQs
    CLARIFICATION = "clarification"  # the model asked a clarifying question
    INSUFFICIENT_INFORMATION = "insufficient_information"  # the FAQs lack the answer
    FALLBACK = "fallback"  # a safe Stage 4 message was used instead of model output


class FallbackReason(StrEnum):
    NO_EVIDENCE = "no_evidence"  # no approved FAQ evidence: the model was not called
    LLM_DISABLED = "llm_disabled"  # LLM_ENABLED=false
    LLM_NOT_CONFIGURED = "llm_not_configured"  # no API key
    LLM_UNAVAILABLE = "llm_unavailable"  # timeouts / 429 / 5xx after retries
    LLM_REJECTED = "llm_rejected"  # 4xx: invalid key, unknown model, bad request, quota
    UNUSABLE_RESPONSE = "unusable_response"  # empty, refused, incomplete or failed
    INVALID_OUTPUT = "invalid_output"  # not valid JSON / schema / unknown source IDs


class GenerationDiagnostics(BaseModel):
    """Internal details for logs, evaluation and support staff. Never shown to customers.

    Contains no customer text: ``detail`` is a fixed description from this application
    and ``model_reason`` is the model's short note about its choice of status.
    """

    model_config = ConfigDict(frozen=True)

    fallback_reason: FallbackReason | None = None
    detail: str | None = None
    model_reason: str | None = None
    context_faq_ids: tuple[str, ...] = ()
    prompt_version: str | None = None
    model: str | None = None
    response_id: str | None = None
    request_id: str | None = None
    attempts: int | None = None
    latency_ms: float | None = None


class GenerationResult(BaseModel):
    """A customer-facing answer plus excluded internal diagnostics."""

    model_config = ConfigDict(frozen=True)

    answer: str = Field(min_length=1, description="Text to show the customer.")
    is_answerable: bool = Field(
        description="True when the answer addresses the question from approved FAQs; "
        "false for clarifying questions, missing information and no-match fallbacks."
    )
    source_faq_ids: tuple[str, ...] = Field(
        description="FAQs the answer is based on (claimed by the model for generated "
        "answers; not proof of factual support)."
    )
    outcome: GenerationOutcome
    diagnostics: GenerationDiagnostics = Field(default_factory=GenerationDiagnostics, exclude=True)

    @property
    def used_fallback(self) -> bool:
        return self.outcome is GenerationOutcome.FALLBACK
