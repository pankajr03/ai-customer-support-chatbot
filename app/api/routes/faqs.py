"""FAQ endpoints. Handlers only translate HTTP <-> FAQService; no loading or matching here.

Route order matters: the static paths (/categories, /search) are declared before the
dynamic /{faq_id} path, otherwise FastAPI would treat "categories" as an FAQ id.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import StringConstraints

from app.models.faq import (
    Category,
    FAQCategoriesResponse,
    FAQListResponse,
    FAQResponse,
    FAQSearchResponse,
    FAQSearchResult,
)
from app.services.faq_search import FAQMatch
from app.services.faq_service import DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT, FAQService

MAX_QUERY_LENGTH = 200

router = APIRouter(prefix="/faqs", tags=["faqs"])


def get_faq_service(request: Request) -> FAQService:
    """The FAQService created at startup (see ``create_app``)."""
    return request.app.state.faq_service


FAQServiceDep = Annotated[FAQService, Depends(get_faq_service)]
SearchQuery = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUERY_LENGTH),
    Query(description="Search text, e.g. 'track order'. Must not be blank."),
]


@router.get("", response_model=FAQListResponse, summary="List active FAQs")
def list_faqs(
    service: FAQServiceDep,
    category: Annotated[Category | None, Query(description="Only return this category.")] = None,
) -> FAQListResponse:
    """Return all active FAQs, optionally filtered by category."""
    faqs = (
        service.list_active_faqs() if category is None else service.list_faqs_by_category(category)
    )
    return FAQListResponse(count=len(faqs), faqs=[FAQResponse.model_validate(f) for f in faqs])


@router.get("/categories", response_model=FAQCategoriesResponse, summary="List FAQ categories")
def list_categories(service: FAQServiceDep) -> FAQCategoriesResponse:
    """Return categories that contain at least one active FAQ."""
    return FAQCategoriesResponse(categories=service.list_categories())


@router.get("/search", response_model=FAQSearchResponse, summary="Search FAQs")
def search_faqs(
    service: FAQServiceDep,
    q: SearchQuery,
    limit: Annotated[int, Query(ge=1, le=MAX_SEARCH_LIMIT)] = DEFAULT_SEARCH_LIMIT,
) -> FAQSearchResponse:
    """Keyword search over active FAQs, best match first.

    Returns an empty ``results`` list (not an error) when nothing matches.
    """
    results = [_to_search_result(match) for match in service.search(q, limit=limit)]
    return FAQSearchResponse(query=q, count=len(results), results=results)


@router.get(
    "/{faq_id}",
    response_model=FAQResponse,
    summary="Get an FAQ",
    responses={status.HTTP_404_NOT_FOUND: {"description": "FAQ not found or not active"}},
)
def get_faq(
    service: FAQServiceDep,
    faq_id: Annotated[str, Path(max_length=64, examples=["faq_001"])],
) -> FAQResponse:
    """Return one active FAQ by id (case-insensitive)."""
    faq = service.get_active_faq(faq_id)
    if faq is None:
        # Same response for missing and inactive FAQs, so inactive ones are not revealed.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="FAQ not found")
    return FAQResponse.model_validate(faq)


def _to_search_result(match: FAQMatch) -> FAQSearchResult:
    return FAQSearchResult(
        **FAQResponse.model_validate(match.faq).model_dump(),
        score=match.score,
        reasons=list(match.reasons),
    )
