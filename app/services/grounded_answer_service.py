"""Generate a grounded answer with the LLM, or fall back safely to the Stage 4 message.

Flow for one question (the Stage 4 decision and its grounded context are inputs):

1. No approved evidence (empty context)  -> fallback, the model is not called.
2. LLM disabled or no key                 -> fallback, the model is not called.
3. Call the model with the trusted instructions, the escaped evidence and question,
   and a strict JSON schema (Structured Outputs).
4. Any failure -> fallback: unavailable after retries, rejected request, refusal,
   incomplete or empty output, invalid JSON, schema violations, an empty or overlong
   answer, internal markup in the answer, or source IDs that were not supplied.
5. Valid reply -> ``generated``, ``clarification`` or ``insufficient_information``.

The fallback is always the Stage 4 decision's own message, which is already safe and
approved: the matched FAQ answer verbatim, the rule-based clarifying question, or the
configured no-match / unavailable text. So a failing or misbehaving model can never
make the customer-facing answer worse than Stage 4 without an LLM.

Source IDs returned by the model are validated against the supplied context, but they
are treated as the model's claim about what it used, never as proof that the answer
is factually supported.
"""

import logging

from app.models.generation import (
    FallbackReason,
    GenerationDiagnostics,
    GenerationOutcome,
    GenerationResult,
)
from app.prompts.customer_support import (
    PROMPT_VERSION,
    InvalidReplyError,
    ReplyStatus,
    build_prompt,
    parse_reply,
)
from app.services.answer_decision_service import AnswerDecision, Decision
from app.services.context_builder import GroundedContext
from app.services.llm_service import (
    LLMDisabledError,
    LLMNotConfiguredError,
    LLMRequestError,
    LLMResponseError,
    LLMService,
    LLMUnavailableError,
)

logger = logging.getLogger(__name__)

_OUTCOMES = {
    ReplyStatus.ANSWERED: GenerationOutcome.GENERATED,
    ReplyStatus.CLARIFICATION_NEEDED: GenerationOutcome.CLARIFICATION,
    ReplyStatus.INSUFFICIENT_INFORMATION: GenerationOutcome.INSUFFICIENT_INFORMATION,
}


class GroundedAnswerService:
    """Turns a question, its Stage 4 decision and grounded context into a safe answer."""

    def __init__(self, llm: LLMService) -> None:
        self._llm = llm

    def generate(
        self,
        question: str,
        decision: AnswerDecision,
        context: GroundedContext,
        *,
        safety_identifier: str | None = None,
    ) -> GenerationResult:
        """Generate a grounded answer, or return the decision's safe message.

        Args:
            question: The customer's question (validated by the API).
            decision: The Stage 4 decision for the question; its message is the fallback.
            context: Evidence built from that decision (``ContextBuilder``).
            safety_identifier: Optional stable, non-identifying end-user ID for OpenAI.

        Raises:
            ValueError: the question is blank or too long (a caller error, not a model
                failure).
        """
        base = GenerationDiagnostics(
            context_faq_ids=context.source_ids, prompt_version=PROMPT_VERSION
        )
        if context.is_empty:
            return self._fallback(
                decision, base, FallbackReason.NO_EVIDENCE, "no approved evidence"
            )
        if not self._llm.is_available:
            reason = (
                FallbackReason.LLM_DISABLED
                if not self._llm.enabled
                else FallbackReason.LLM_NOT_CONFIGURED
            )
            return self._fallback(decision, base, reason, "LLM generation is not available")

        parts = build_prompt(question, context)  # ValueError for an invalid question
        try:
            llm_result = self._llm.generate(
                instructions=parts.instructions,
                input=parts.input,
                text_format=parts.text_format,
                safety_identifier=safety_identifier,
            )
        except LLMDisabledError:
            return self._fallback(decision, base, FallbackReason.LLM_DISABLED, "disabled")
        except LLMNotConfiguredError:
            return self._fallback(decision, base, FallbackReason.LLM_NOT_CONFIGURED, "no key")
        except LLMUnavailableError as exc:
            detail = f"unavailable (HTTP {exc.status_code})" if exc.status_code else "unavailable"
            return self._fallback(decision, base, FallbackReason.LLM_UNAVAILABLE, detail)
        except LLMRequestError as exc:
            detail = f"rejected (HTTP {exc.status_code}, {exc.code or 'no code'})"
            return self._fallback(decision, base, FallbackReason.LLM_REJECTED, detail)
        except LLMResponseError as exc:
            return self._fallback(decision, base, FallbackReason.UNUSABLE_RESPONSE, exc.reason)

        metadata = base.model_copy(
            update={
                "model": llm_result.model,
                "response_id": llm_result.response_id,
                "request_id": llm_result.request_id,
                "attempts": llm_result.attempts,
                "latency_ms": llm_result.latency_ms,
            }
        )
        try:
            reply = parse_reply(llm_result.text, parts.allowed_faq_ids)
        except InvalidReplyError as exc:
            return self._fallback(decision, metadata, FallbackReason.INVALID_OUTPUT, exc.reason)

        result = GenerationResult(
            answer=reply.answer,
            is_answerable=reply.status is ReplyStatus.ANSWERED,
            source_faq_ids=reply.source_faq_ids,
            outcome=_OUTCOMES[reply.status],
            diagnostics=metadata.model_copy(update={"model_reason": reply.reason or None}),
        )
        _log(result)
        return result

    @staticmethod
    def _fallback(
        decision: AnswerDecision,
        diagnostics: GenerationDiagnostics,
        reason: FallbackReason,
        detail: str,
    ) -> GenerationResult:
        answered = decision.decision is Decision.ANSWERED and decision.faq is not None
        result = GenerationResult(
            answer=decision.message,  # Stage 4's approved, verbatim or configured text
            is_answerable=answered,
            source_faq_ids=(decision.faq.id,) if answered else (),  # type: ignore[union-attr]
            outcome=GenerationOutcome.FALLBACK,
            diagnostics=diagnostics.model_copy(
                update={"fallback_reason": reason, "detail": detail}
            ),
        )
        _log(result)
        return result


def _log(result: GenerationResult) -> None:
    """One line per generation. IDs, enums and numbers only, never customer or model text."""
    d = result.diagnostics
    level = (
        logging.WARNING
        if d.fallback_reason not in (None, FallbackReason.NO_EVIDENCE, FallbackReason.LLM_DISABLED)
        else logging.INFO
    )
    logger.log(
        level,
        "event=grounded_generation outcome=%s fallback=%s detail=%s sources=%s context=%s "
        "model=%s attempts=%s latency_ms=%s prompt=%s",
        result.outcome.value,
        d.fallback_reason.value if d.fallback_reason else "-",
        d.detail or "-",
        ",".join(result.source_faq_ids) or "-",
        ",".join(d.context_faq_ids) or "-",
        d.model or "-",
        d.attempts if d.attempts is not None else "-",
        f"{d.latency_ms:.0f}" if d.latency_ms is not None else "-",
        d.prompt_version,
    )
