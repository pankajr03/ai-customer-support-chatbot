"""Semantic (meaning-based) FAQ search endpoint.

The handler is a plain ``def``: FastAPI runs it in a worker thread, so loading the
embedding model and running inference never block the event loop. The first call may
be slow (model load and index build); later calls reuse both.
"""

from fastapi import APIRouter, status

from app.api.dependencies import SemanticSearchServiceDep
from app.api.errors import SEMANTIC_UNAVAILABLE_DETAIL
from app.models.semantic_search import (
    SemanticSearchRequest,
    SemanticSearchResponse,
    SemanticSearchResult,
)

router = APIRouter(tags=["semantic search"])


@router.post(
    "/semantic-search",
    response_model=SemanticSearchResponse,
    summary="Semantic FAQ search",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "The embedding model or semantic index is unavailable.",
            "content": {
                "application/json": {
                    "example": {"detail": SEMANTIC_UNAVAILABLE_DETAIL, "request_id": "<id>"}
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
    (cosine similarity, -1 to 1; not a probability). Returns an empty `results` list
    when nothing matches the filters. Inactive FAQs are never returned.

    The first request after startup may take a while: the embedding model is loaded
    (and downloaded on first ever use) and the FAQ index is built.
    """
    matches = service.search(
        request.query,
        top_k=request.top_k,
        category=request.category,
        min_score=request.min_similarity,
    )
    return SemanticSearchResponse(
        query=request.query,
        results=[SemanticSearchResult.from_faq(match.faq, match.score) for match in matches],
    )
