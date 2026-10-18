"""Grounded answer endpoint: Stage 4 retrieval and decision, then an optional LLM rewrite.

The handler is a plain ``def``: FastAPI runs it in a worker thread, so retrieval, model
inference and the LLM call never block the event loop. Each request is independent;
there is no conversation history in Stage 5.

Every decision is a normal 200 response with a safe customer-facing answer, including
when the LLM is disabled, fails or is overruled (the approved FAQ answer is returned
instead) and when search is degraded. Errors: 422 for invalid input, 503 when the FAQ
knowledge base is unavailable, 500 for anything unexpected. No error contains prompts,
credentials or internal exception details.
"""

import math

from fastapi import APIRouter, Depends, Response, status

from app.api.dependencies import GroundedAnswerServiceDep, limit_public_search
from app.api.errors import FAQ_UNAVAILABLE_MESSAGE
from app.core.middleware import INTERNAL_ERROR_MESSAGE
from app.models.answer import AnswerRequest, AnswerResponse
from app.models.errors import ErrorCode, ErrorResponse, error_example

# Token-free and compute-heavy: a low per-IP allowance shared by the three (Stage 12).
router = APIRouter(tags=["grounded answers"], dependencies=[Depends(limit_public_search)])


@router.post(
    "/answer",
    response_model=AnswerResponse,
    summary="Answer a customer question from approved FAQs",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "The FAQ knowledge base is temporarily unavailable.",
            "model": ErrorResponse,
            "content": {
                "application/json": {
                    "example": error_example(ErrorCode.SERVICE_UNAVAILABLE, FAQ_UNAVAILABLE_MESSAGE)
                }
            },
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "Unexpected error. The body never contains internal details.",
            "model": ErrorResponse,
            "content": {
                "application/json": {
                    "example": error_example(ErrorCode.INTERNAL_ERROR, INTERNAL_ERROR_MESSAGE)
                }
            },
        },
    },
)
def answer(
    request: AnswerRequest, service: GroundedAnswerServiceDep, response: Response
) -> AnswerResponse:
    """Answer one customer question using only approved FAQ content.

    1. Stage 4 hybrid search finds candidate FAQs and decides: `answered`, `ambiguous`,
       `no_match` or `retrieval_unavailable`.
    2. Only for `answered` (with full hybrid search) may the LLM rewrite the approved FAQ
       answer, using only the retrieved FAQ evidence. Its reply is validated (structure,
       sources, grounding checks for numbers, fees, promises and restrictions).

    `generation_method` says how the answer was produced:

    - `llm_grounded`: the LLM's validated rewrite of the approved answer.
    - `approved_faq`: the approved FAQ answer, verbatim (LLM disabled, unavailable,
      failed validation, or search ran in keyword-only mode).
    - `clarification`: a clarifying question built from the competing FAQs.
    - `fallback`: a safe message because no FAQ matched or search is unavailable
      (with a `Retry-After` header while the search model is cooling down).

    `usage` reports the actual token counts from the LLM API when an LLM response was
    received, and `estimated_cost_usd` only when a USD price is configured. Answers
    come from approved FAQs; the LLM cannot invent an answer when none was approved.
    """
    grounded = service.answer(request.query, category=request.category)
    retry_after = grounded.decision.retry_after_seconds
    if retry_after:
        response.headers["Retry-After"] = str(math.ceil(retry_after))
    return AnswerResponse.from_result(request.query, grounded.decision.decision, grounded.result)
