"""FastAPI dependencies that hand application-wide objects to route handlers.

Strategy: expensive objects (like the FAQ service, which loads and validates the
JSON knowledge base) are built once in ``create_app()`` and stored on
``app.state``. These functions only look them up, so no request ever reloads data.

Tests can replace an object in two ways:
- build the app with ``create_app(settings, faq_service=...)``, or
- set ``app.dependency_overrides[get_faq_service] = lambda: fake_service``.
"""

from typing import Annotated

from fastapi import Depends, Request

from app.services.answer_decision_service import AnswerDecisionService
from app.services.context_builder import ContextBuilder
from app.services.faq_service import FAQService
from app.services.hybrid_search_service import HybridSearchService
from app.services.llm_service import LLMService
from app.services.semantic_search_service import SemanticSearchService


def get_faq_service(request: Request) -> FAQService:
    """Return the FAQService created at startup for this application."""
    service = getattr(request.app.state, "faq_service", None)
    if service is None:
        # Only happens if an app is assembled without create_app(); fail loudly.
        raise RuntimeError("FAQService is not initialised; build the app with create_app().")
    return service


FAQServiceDep = Annotated[FAQService, Depends(get_faq_service)]


def get_semantic_search_service(request: Request) -> SemanticSearchService:
    """Return the application's semantic index service (index built lazily)."""
    service = getattr(request.app.state, "semantic_search_service", None)
    if service is None:
        raise RuntimeError(
            "SemanticSearchService is not initialised; build the app with create_app()."
        )
    return service


SemanticSearchServiceDep = Annotated[SemanticSearchService, Depends(get_semantic_search_service)]


def get_hybrid_search_service(request: Request) -> HybridSearchService:
    """Return the application's hybrid (keyword + semantic) search service."""
    service = getattr(request.app.state, "hybrid_search_service", None)
    if service is None:
        raise RuntimeError(
            "HybridSearchService is not initialised; build the app with create_app()."
        )
    return service


HybridSearchServiceDep = Annotated[HybridSearchService, Depends(get_hybrid_search_service)]


def get_answer_decision_service(request: Request) -> AnswerDecisionService:
    """Return the application's answer decision service."""
    service = getattr(request.app.state, "answer_decision_service", None)
    if service is None:
        raise RuntimeError(
            "AnswerDecisionService is not initialised; build the app with create_app()."
        )
    return service


AnswerDecisionServiceDep = Annotated[AnswerDecisionService, Depends(get_answer_decision_service)]


def get_llm_service(request: Request) -> LLMService:
    """Return the application's LLM service (its OpenAI client is created on first use)."""
    service = getattr(request.app.state, "llm_service", None)
    if service is None:
        raise RuntimeError("LLMService is not initialised; build the app with create_app().")
    return service


LLMServiceDep = Annotated[LLMService, Depends(get_llm_service)]


def get_context_builder(request: Request) -> ContextBuilder:
    """Return the application's grounded context builder."""
    builder = getattr(request.app.state, "context_builder", None)
    if builder is None:
        raise RuntimeError("ContextBuilder is not initialised; build the app with create_app().")
    return builder


ContextBuilderDep = Annotated[ContextBuilder, Depends(get_context_builder)]
