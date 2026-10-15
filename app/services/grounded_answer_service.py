"""Grounded answer generation: Stage 4 retrieval and decision first, the LLM second.

``answer(question)`` orchestrates one customer question:

1. Stage 4 runs unchanged: hybrid retrieval (keyword + semantic, RRF) and the answer
   decision (``AnswerDecisionService.decide``), including its keyword-only degraded mode.
2. The decision's approved FAQ evidence is selected and bounded (``ContextBuilder``).
3. The decision rules below choose between an LLM-written answer and a Stage 4 message.

Decision rules
--------------
    Stage 4 decision             Customer gets                                LLM called
    ---------------------------  -------------------------------------------  ----------
    answered (hybrid)            LLM rewrite of the approved answer, if valid  yes
    answered (keyword-only)      the matched FAQ answer, verbatim              no
    ambiguous                    Stage 4's clarifying question                 no
    no_match                     the configured no-match message               no
    retrieval_unavailable        the configured unavailable message            no

- The LLM is only called when Stage 4 accepted an answer from full (hybrid) evidence
  and the matched FAQ is in the context. It rewrites or summarises that approved answer
  and must keep its conditions and limits (see the prompt).
- Ambiguous questions are never handed to the LLM to guess: the clarifying question is
  Stage 4's, built from the competing FAQs that retrieval found.
- In degraded mode (semantic model down) the answer rests on keyword evidence alone, so
  it is returned verbatim rather than rewritten, and nothing unsupported is generated.
- The LLM cannot override Stage 4. Its reply is used only if it is valid, says
  "answered", cites the FAQ Stage 4 matched and passes the grounding checks
  (``grounding_validator``: no unsupported numbers, dates, fees, promises or dropped
  restrictions). If it declines (asks to clarify or calls
  the FAQ insufficient), cites only other FAQs, returns invalid output or fails, the
  customer gets the matched FAQ answer verbatim: Stage 4 accepted it, so it is still the
  best supported answer. Only if that FAQ is no longer active is the no-match message
  used instead.

So the LLM can only change the wording of an answer Stage 4 already approved; it can
never turn "no answer" into an answer, or one FAQ's answer into another's.
"""

import logging
from dataclasses import dataclass

from app.models.faq import FAQCategory
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
from app.services.answer_decision_service import (
    AnswerDecision,
    AnswerDecisionService,
    Decision,
)
from app.services.context_builder import ContextBuilder, GroundedContext
from app.services.grounding_validator import check_grounding
from app.services.llm_service import (
    LLMDisabledError,
    LLMNotConfiguredError,
    LLMRequestError,
    LLMResponseError,
    LLMService,
    LLMUnavailableError,
)

logger = logging.getLogger(__name__)

_NO_LLM_CALL = {
    Decision.AMBIGUOUS: (GenerationOutcome.CLARIFICATION, FallbackReason.STAGE4_AMBIGUOUS),
    Decision.NO_MATCH: (GenerationOutcome.NO_MATCH, FallbackReason.STAGE4_NO_MATCH),
    Decision.RETRIEVAL_UNAVAILABLE: (
        GenerationOutcome.RETRIEVAL_UNAVAILABLE,
        FallbackReason.STAGE4_RETRIEVAL_UNAVAILABLE,
    ),
}


@dataclass(frozen=True, slots=True)
class GroundedAnswer:
    """The answer plus the Stage 4 decision and context it came from (for the API)."""

    result: GenerationResult
    decision: AnswerDecision
    context: GroundedContext


class GroundedAnswerService:
    """Orchestrates Stage 4 retrieval, evidence selection and grounded generation."""

    def __init__(
        self,
        decision_service: AnswerDecisionService,
        context_builder: ContextBuilder,
        llm: LLMService,
    ) -> None:
        self._decisions = decision_service
        self._contexts = context_builder
        self._llm = llm

    def answer(
        self,
        question: str,
        *,
        category: FAQCategory | str | None = None,
        safety_identifier: str | None = None,
    ) -> GroundedAnswer:
        """Answer one customer question.

        Raises:
            ValueError: invalid question or unknown category (a caller error).
        """
        decision = self._decisions.decide(question, category=category)
        context = self._contexts.build_for_decision(
            decision, is_acceptable=self._decisions.is_acceptable, category=category
        )
        result = self.generate(question, decision, context, safety_identifier=safety_identifier)
        return GroundedAnswer(result=result, decision=decision, context=context)

    def generate(
        self,
        question: str,
        decision: AnswerDecision,
        context: GroundedContext,
        *,
        safety_identifier: str | None = None,
    ) -> GenerationResult:
        """Apply the decision rules for an existing Stage 4 decision and its context."""
        base = GenerationDiagnostics(
            stage4_decision=decision.decision.value,
            retrieval_mode=decision.retrieval_mode.value,
            context_faq_ids=context.source_ids,
            prompt_version=PROMPT_VERSION,
        )

        if decision.decision in _NO_LLM_CALL:
            outcome, reason = _NO_LLM_CALL[decision.decision]
            sources = context.source_ids if outcome is GenerationOutcome.CLARIFICATION else ()
            return _finish(
                GenerationResult(
                    answer=decision.message,  # Stage 4's clarifying question or safe message
                    is_answerable=False,
                    source_faq_ids=sources,
                    outcome=outcome,
                    diagnostics=base.model_copy(update={"fallback_reason": reason}),
                )
            )

        # Stage 4 answered.
        if decision.degraded:
            return self._approved(decision, base, FallbackReason.DEGRADED_RETRIEVAL)
        if decision.faq is None or decision.faq.id not in context.source_ids:
            return self._approved(decision, base, FallbackReason.PRIMARY_NOT_IN_CONTEXT)
        if not self._llm.is_available:
            reason = (
                FallbackReason.LLM_DISABLED
                if not self._llm.enabled
                else FallbackReason.LLM_NOT_CONFIGURED
            )
            return self._approved(decision, base, reason)

        parts = build_prompt(question, context)  # ValueError for an invalid question
        called = base.model_copy(update={"llm_called": True})
        try:
            llm_result = self._llm.generate(
                instructions=parts.instructions,
                input=parts.input,
                text_format=parts.text_format,
                safety_identifier=safety_identifier,
            )
        except LLMDisabledError:
            return self._approved(decision, called, FallbackReason.LLM_DISABLED)
        except LLMNotConfiguredError:
            return self._approved(decision, called, FallbackReason.LLM_NOT_CONFIGURED)
        except LLMUnavailableError as exc:
            detail = f"HTTP {exc.status_code}" if exc.status_code else None
            return self._approved(decision, called, FallbackReason.LLM_UNAVAILABLE, detail)
        except LLMRequestError as exc:
            detail = f"HTTP {exc.status_code}, {exc.code or 'no code'}"
            return self._approved(decision, called, FallbackReason.LLM_REJECTED, detail)
        except LLMResponseError as exc:
            return self._approved(decision, called, FallbackReason.UNUSABLE_RESPONSE, exc.reason)

        metadata = called.model_copy(
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
            return self._approved(decision, metadata, FallbackReason.INVALID_OUTPUT, exc.reason)

        metadata = metadata.model_copy(
            update={"model_status": reply.status.value, "model_reason": reply.reason or None}
        )
        if reply.status is not ReplyStatus.ANSWERED:
            return self._approved(decision, metadata, FallbackReason.MODEL_DECLINED)
        if decision.faq.id not in reply.source_faq_ids:
            return self._approved(decision, metadata, FallbackReason.PRIMARY_NOT_CITED)

        # Source IDs only prove which FAQs were supplied. Check the text itself against
        # the cited FAQs; when in doubt, return the approved answer verbatim.
        cited = [record for record in context.records if record.faq_id in reply.source_faq_ids]
        report = check_grounding(
            reply.answer,
            evidence=[text for record in cited for text in (record.question, record.answer)],
            primary=decision.faq.answer,
        )
        if not report.passed:
            return self._approved(
                decision,
                metadata.model_copy(
                    update={
                        "grounding_issues": tuple(
                            f"{issue.kind.value}: {issue.detail}" for issue in report.issues
                        )
                    }
                ),
                FallbackReason.GROUNDING_CHECK_FAILED,
                ",".join(report.kinds),
            )

        return _finish(
            GenerationResult(
                answer=reply.answer,
                is_answerable=True,
                source_faq_ids=reply.source_faq_ids,
                outcome=GenerationOutcome.GENERATED,
                diagnostics=metadata,
            )
        )

    def _approved(
        self,
        decision: AnswerDecision,
        diagnostics: GenerationDiagnostics,
        reason: FallbackReason,
        detail: str | None = None,
    ) -> GenerationResult:
        """The matched FAQ answer verbatim, or the no-match message if it is not valid."""
        faq = decision.faq
        diagnostics = diagnostics.model_copy(update={"fallback_reason": reason, "detail": detail})
        if faq is not None and faq.is_active:
            result = GenerationResult(
                answer=faq.answer,
                is_answerable=True,
                source_faq_ids=(faq.id,),
                outcome=GenerationOutcome.APPROVED_ANSWER,
                diagnostics=diagnostics,
            )
        else:
            result = GenerationResult(
                answer=self._decisions.messages.no_match,
                is_answerable=False,
                source_faq_ids=(),
                outcome=GenerationOutcome.NO_MATCH,
                diagnostics=diagnostics,
            )
        return _finish(result)


_EXPECTED = {
    None,
    FallbackReason.STAGE4_AMBIGUOUS,
    FallbackReason.STAGE4_NO_MATCH,
    FallbackReason.STAGE4_RETRIEVAL_UNAVAILABLE,
    FallbackReason.DEGRADED_RETRIEVAL,
    FallbackReason.LLM_DISABLED,
}


def _finish(result: GenerationResult) -> GenerationResult:
    """Log one line per answer: IDs, enums and numbers only, never customer or model text.

    Routine outcomes log at INFO; an LLM that failed or was overruled logs at WARNING.
    """
    d = result.diagnostics
    logger.log(
        logging.INFO if d.fallback_reason in _EXPECTED else logging.WARNING,
        "event=grounded_answer stage4=%s mode=%s outcome=%s llm_called=%s fallback=%s "
        "detail=%s model_status=%s sources=%s context=%s model=%s attempts=%s "
        "latency_ms=%s prompt=%s",
        d.stage4_decision or "-",
        d.retrieval_mode or "-",
        result.outcome.value,
        d.llm_called,
        d.fallback_reason.value if d.fallback_reason else "-",
        d.detail or "-",
        d.model_status or "-",
        ",".join(result.source_faq_ids) or "-",
        ",".join(d.context_faq_ids) or "-",
        d.model or "-",
        d.attempts if d.attempts is not None else "-",
        f"{d.latency_ms:.0f}" if d.latency_ms is not None else "-",
        d.prompt_version,
    )
    return result
