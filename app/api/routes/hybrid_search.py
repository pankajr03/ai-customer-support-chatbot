"""Hybrid FAQ search endpoint: keyword + semantic retrieval and an answer decision.

The handler is a plain ``def``: FastAPI runs it in a worker thread, so loading the
embedding model and running inference never block the event loop.

An unavailable embedding model is not an HTTP error here: the decision service falls
back to keyword-only retrieval and the response says so (``retrieval_mode``), with a
safe ``fallback_message`` when no reliable answer can be given.
"""

import math

from fastapi import APIRouter, Response
from fastapi.exceptions import RequestValidationError

from app.api.dependencies import AnswerDecisionServiceDep
from app.models.hybrid_search import HybridSearchRequest, HybridSearchResponse

router = APIRouter(tags=["hybrid search"])


@router.post("/hybrid-search", response_model=HybridSearchResponse, summary="Hybrid FAQ search")
def hybrid_search(
    request: HybridSearchRequest, service: AnswerDecisionServiceDep, response: Response
) -> HybridSearchResponse:
    """Find the approved FAQ answer for ``query``, or say why none is given.

    Keyword and semantic rankings are fused with Reciprocal Rank Fusion, then a decision
    is made from the relevance evidence (semantic similarity or an exact question match;
    RRF scores only order results):

    - `answered`: `answer` is the approved FAQ answer, verbatim; `matched_faq_id` names it.
    - `ambiguous`: several FAQs fit about equally well; `clarification_question` asks
      the customer to choose.
    - `no_match`: no FAQ is relevant enough; `fallback_message` is a safe message. No
      unrelated FAQ is substituted and no answer is generated.
    - `retrieval_unavailable`: the semantic model is unavailable and keyword-only
      retrieval found no strong match; `fallback_message` is a safe message and a
      `Retry-After` header may be set.

    `results` is retrieval evidence (no answers), at most `top_k`; `top_k` does not
    change the decision. Inactive FAQs are never returned.
    """
    if request.top_k > service.max_top_k:
        raise RequestValidationError(
            [
                {
                    "type": "less_than_equal",
                    "loc": ("body", "top_k"),
                    "msg": f"Input should be less than or equal to {service.max_top_k}",
                    "input": request.top_k,
                    "ctx": {"le": service.max_top_k},
                }
            ]
        )

    decision = service.decide(request.query, category=request.category, top_k=request.top_k)
    if decision.retry_after_seconds:
        response.headers["Retry-After"] = str(math.ceil(decision.retry_after_seconds))
    return HybridSearchResponse.from_decision(request.query, decision)
