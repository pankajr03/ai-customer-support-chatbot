"""Semantic (meaning-based) FAQ search endpoint.

The handler is a plain ``def``: FastAPI runs it in a worker thread, so loading the
embedding model and running inference never block the event loop. The first call may
be slow (model load and index build); later calls reuse both.
"""

from fastapi import APIRouter, Depends, status

from app.api.dependencies import SemanticSearchServiceDep, limit_public_search
from app.api.errors import SEMANTIC_UNAVAILABLE_MESSAGE
from app.models.errors import ErrorCode, ErrorResponse, error_example
from app.models.semantic_search import (
    SemanticSearchRequest,
    SemanticSearchResponse,
    SemanticSearchResult,
)

# Token-free and compute-heavy: a low per-IP allowance shared by the three (Stage 12).
router = APIRouter(tags=["semantic search"], dependencies=[Depends(limit_public_search)])


@router.post(
    "/semantic-search",
    response_model=SemanticSearchResponse,
    summary="Semantic FAQ search",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "The embedding model or semantic index is unavailable.",
            "model": ErrorResponse,
            "content": {
                "application/json": {
                    "example": error_example(
                        ErrorCode.SERVICE_UNAVAILABLE, SEMANTIC_UNAVAILABLE_MESSAGE
                    )
                }
            },
        }
    },
)
def semantic_search(
    request: SemanticSearchRequest, service: SemanticSearchServiceDep
) -> SemanticSearchResponse:
    """Rank active FAQs by how close their question is in meaning to ``query``.

    Results are approved FAQ answers returned verbatim, ordered by `similarity_score`
    (cosine similarity, -1 to 1; not a probability). Only results scoring at least the
    similarity threshold are returned: the request's `min_similarity`, or the server
    default (`SEMANTIC_MIN_SIMILARITY`, an experimental 0.40) when omitted. The applied
    threshold is echoed in the response. If nothing qualifies, `results` is empty: no
    unrelated FAQ is substituted and no answer is generated. Inactive FAQs are never
    returned.

    The first request after startup may take a while: the embedding model is loaded
    (and downloaded on first ever use) and the FAQ index is built.
    """
    threshold = service.resolve_min_score(request.min_similarity)
    matches = service.search(
        request.query, top_k=request.top_k, category=request.category, min_score=threshold
    )
    return SemanticSearchResponse(
        query=request.query,
        min_similarity=threshold,
        results=[SemanticSearchResult.from_faq(match.faq, match.score) for match in matches],
    )
