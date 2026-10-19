"""Liveness, readiness and public status checks.

- ``/health`` (liveness): the process answers HTTP. Nothing else is checked.
- ``/ready`` (readiness): chat requests can be answered now. Essential dependencies
  decide it; optional ones (semantic search, the LLM) are reported beside them. For the
  private networks: Docker's health check and the two pages.
- ``/status`` (public status): the readiness verdict alone, for the public site and
  uptime monitors. It names no dependency, so the internet learns nothing about the
  deployment (whether an LLM is configured or paused, whether the model loaded).

All three are cheap enough to poll every few seconds: they never load the embedding
model, call OpenAI or read conversation rows, they are exempt from the request-rate
limit, and they answer HEAD as well as GET (many uptime monitors send HEAD).
"""

import logging
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from starlette.datastructures import State

from app.database.connection import Database, DatabaseError
from app.models.errors import ErrorBody, ErrorCode, current_request_id
from app.models.health import (
    HealthResponse,
    PublicStatusResponse,
    ReadinessChecks,
    ReadinessResponse,
)
from app.repositories.faq_repository import FAQStoreError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

NOT_READY_MESSAGE = "The service cannot answer chat requests right now. Please try again later."
NOT_READY_RESPONSE = "Not ready: the knowledge base or conversation storage is unavailable."


@router.get("/health", response_model=HealthResponse, summary="Liveness check")
def health(request: Request, response: Response) -> HealthResponse:
    """Report that the process is running and answering HTTP.

    Checks nothing else, so it stays fast and never fails because of a dependency: use
    it to decide whether the process must be restarted, not whether it can serve chat
    (that is `/ready`).
    """
    response.headers["Cache-Control"] = "no-store"
    return HealthResponse(status="healthy", service=request.app.state.settings.service_name)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    response_model_exclude_none=True,
    summary="Readiness check",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ReadinessResponse,
            "description": f"{NOT_READY_RESPONSE} The body says which (the 200 shape plus "
            "the standard `error` object).",
        }
    },
)
def ready(request: Request, response: Response) -> ReadinessResponse:
    """Report whether chat requests can be answered now.

    `200` when the FAQ knowledge base and conversation storage are usable, otherwise
    `503` with the same checks plus the standard `error` object (`SERVICE_UNAVAILABLE`).
    `semantic_search` and `llm` are optional dependencies, reported only: chat works
    without them (with keyword-only retrieval or approved FAQ answers), so they never
    make the service "not ready". Nothing is loaded, downloaded or sent to OpenAI.

    Not on the public site (it describes the deployment); there, use `/status`.
    """
    checks = _readiness_checks(request.app.state)
    is_ready = _is_ready(checks)
    response.headers["Cache-Control"] = "no-store"
    return ReadinessResponse(
        status="ready" if is_ready else "not_ready",
        service=request.app.state.settings.service_name,
        checks=checks,
        error=_not_ready(response) if not is_ready else None,
    )


@router.get(
    "/status",
    response_model=PublicStatusResponse,
    response_model_exclude_none=True,
    summary="Public service status",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": PublicStatusResponse,
            "description": f"{NOT_READY_RESPONSE} The body does not say which.",
        }
    },
)
def public_status(request: Request, response: Response) -> PublicStatusResponse:
    """Report whether chat requests can be answered now, and nothing else.

    The same verdict and status code as `/ready` (`200` ready, `503` not ready with the
    standard `error` object), without the checks: safe to publish to uptime monitors.
    """
    is_ready = _is_ready(_readiness_checks(request.app.state))
    response.headers["Cache-Control"] = "no-store"
    return PublicStatusResponse(
        status="ready" if is_ready else "not_ready",
        error=_not_ready(response) if not is_ready else None,
    )


# HEAD for every check, kept out of the schema (it mirrors GET without a body).
for _path, _endpoint in (("/health", health), ("/ready", ready), ("/status", public_status)):
    router.add_api_route(_path, _endpoint, methods=["HEAD"], include_in_schema=False)


def _readiness_checks(state: State) -> ReadinessChecks:
    semantic = state.semantic_search_service
    llm = state.llm_service
    knowledge_base: Literal["ok", "unavailable"]
    try:
        knowledge_base = "ok" if state.faq_service.list_active_faqs() else "unavailable"
    except FAQStoreError:  # unverified after a failed reload: not ready (fail closed)
        knowledge_base = "unavailable"
    return ReadinessChecks(
        knowledge_base=knowledge_base,
        conversation_storage=_storage_status(state.database),
        semantic_search=(
            "ready"
            if semantic.is_ready
            else "unavailable"
            if semantic.model_load_failed_recently or semantic.model_files_missing
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


def _is_ready(checks: ReadinessChecks) -> bool:
    """Only the essential dependencies: without either, no chat request can succeed."""
    return checks.knowledge_base == "ok" and checks.conversation_storage == "ok"


def _not_ready(response: Response) -> ErrorBody:
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ErrorBody(
        code=ErrorCode.SERVICE_UNAVAILABLE,
        message=NOT_READY_MESSAGE,
        request_id=current_request_id(),
    )


def _storage_status(database: Database) -> Literal["ok", "unavailable"]:
    try:
        database.check()
    except DatabaseError as exc:
        logger.warning("Readiness: conversation storage unavailable (%s)", exc)
        return "unavailable"
    return "ok"
