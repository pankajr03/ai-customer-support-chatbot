"""Liveness and readiness checks.

Both are cheap enough to poll every few seconds: they never load the embedding model,
call OpenAI or read conversation rows, and they are exempt from the request-rate limit.
"""

import logging
from typing import Literal

from fastapi import APIRouter, Request, Response, status

from app.database.connection import Database, DatabaseError
from app.models.errors import ErrorBody, ErrorCode, current_request_id
from app.models.health import HealthResponse, ReadinessChecks, ReadinessResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

NOT_READY_MESSAGE = "The service cannot answer chat requests right now. Please try again later."


@router.get("/health", response_model=HealthResponse, summary="Health check")
def health(request: Request) -> HealthResponse:
    """Report that the service is running.

    Used by load balancers, uptime monitors and deploy scripts. It deliberately
    does no expensive work, so it stays fast and cheap to call often.
    """
    return HealthResponse(status="healthy", service=request.app.state.settings.service_name)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    response_model_exclude_none=True,
    summary="Readiness check",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ReadinessResponse,
            "description": "Not ready: the knowledge base or conversation storage is "
            "unavailable. The body says which (the 200 shape plus the standard `error` "
            "object).",
        }
    },
)
def ready(request: Request, response: Response) -> ReadinessResponse:
    """Report whether chat requests can be answered now.

    `200` when the FAQ knowledge base and conversation storage are usable, otherwise
    `503` with the same checks plus the standard `error` object (`SERVICE_UNAVAILABLE`).
    `semantic_search` and `llm` are informational: chat works
    without them (with keyword-only retrieval or approved FAQ answers), so they never
    make the service "not ready". Nothing is loaded, downloaded or sent to OpenAI.
    """
    state = request.app.state
    semantic = state.semantic_search_service
    llm = state.llm_service
    checks = ReadinessChecks(
        knowledge_base="ok" if state.faq_service.list_active_faqs() else "unavailable",
        conversation_storage=_storage_status(state.database),
        semantic_search=(
            "ready"
            if semantic.is_ready
            else "unavailable"
            if semantic.model_load_failed_recently
            else "not_loaded"
        ),
        llm=(
            "disabled"
            if not llm.is_available
            else "paused"
            if llm.circuit_breaker.is_open
            else "enabled"
        ),
    )
    is_ready = checks.knowledge_base == "ok" and checks.conversation_storage == "ok"
    response.headers["Cache-Control"] = "no-store"
    error = None
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        error = ErrorBody(
            code=ErrorCode.SERVICE_UNAVAILABLE,
            message=NOT_READY_MESSAGE,
            request_id=current_request_id(),
        )
    return ReadinessResponse(
        status="ready" if is_ready else "not_ready",
        service=state.settings.service_name,
        checks=checks,
        error=error,
    )


def _storage_status(database: Database) -> Literal["ok", "unavailable"]:
    try:
        database.check()
    except DatabaseError as exc:
        logger.warning("Readiness: conversation storage unavailable (%s)", exc)
        return "unavailable"
    return "ok"
