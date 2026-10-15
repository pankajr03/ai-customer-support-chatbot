"""Application entry point. Run with: uvicorn app.main:app --reload"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from app.api.errors import register_exception_handlers
from app.api.routes import answer, faqs, health, hybrid_search, semantic_search
from app.core.config import Settings, get_settings
from app.core.logging_config import configure_logging
from app.core.middleware import RequestContextMiddleware, RequestSizeLimitMiddleware
from app.database.faq_repository import FAQDataError, FAQRepository
from app.models.root import RootResponse
from app.services.context_builder import ContextBuilder
from app.services.embedding_service import EmbeddingError, EmbeddingService
from app.services.faq_service import FAQService
from app.services.grounded_answer_service import GroundedAnswerService
from app.services.llm_service import LLMService
from app.services.search_pipeline import build_search_services
from app.services.semantic_search_service import SemanticSearchService
from app.services.usage_tracker import UsageTracker

logger = logging.getLogger(__name__)

DESCRIPTION = "Backend API for an AI-powered customer support chatbot."


def create_app(
    settings: Settings | None = None,
    *,
    faq_service: FAQService | None = None,
    semantic_search_service: SemanticSearchService | None = None,
    llm_service: LLMService | None = None,
) -> FastAPI:
    """Build and configure a FastAPI application.

    Using a factory instead of only a module-level app lets tests create
    isolated instances with their own settings (e.g. production mode).

    Shared services are built here, once per application, and stored on
    ``app.state``; route handlers receive them through ``app.api.dependencies``.

    Args:
        settings: Configuration to use. Defaults to values from the environment.
        faq_service: A ready-made FAQ service (e.g. an in-memory one in tests).
            Defaults to one backed by the JSON file at ``settings.faq_data_path``.
        semantic_search_service: A ready-made semantic index service (e.g. with a
            fake embedding model in tests). Defaults to one indexing the active FAQs
            with ``settings.embedding_model_name``. Creating it loads nothing; the
            model and index are built on first use or by the startup preload.
        llm_service: A ready-made LLM service (e.g. with a fake client in tests).
            Defaults to one configured from the ``OPENAI_*`` settings. Creating it
            makes no network call; the OpenAI client is created on first use.

    Returns:
        A fully configured FastAPI application.

    Raises:
        FAQDataError: the FAQ knowledge base is missing or invalid. The app refuses
            to start rather than serve incomplete answers.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    if faq_service is None:
        faq_service = _load_faq_service(settings)
    if semantic_search_service is None:
        semantic_search_service = SemanticSearchService(
            faq_service.list_active_faqs,
            EmbeddingService(
                settings.embedding_model_name,
                load_retry_seconds=settings.embedding_load_retry_seconds,
            ),
            default_min_score=settings.semantic_min_similarity,
        )

    app = FastAPI(
        lifespan=_lifespan,
        title=settings.app_name,
        version=settings.app_version,
        description=DESCRIPTION,
        debug=settings.debug,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    app.state.faq_service = faq_service
    app.state.semantic_search_service = semantic_search_service
    # Cheap to create: no model is loaded until a semantic or hybrid search runs.
    search = build_search_services(settings, faq_service.list_active_faqs, semantic_search_service)
    app.state.keyword_search_service = search.keyword
    app.state.hybrid_search_service = search.hybrid
    app.state.answer_decision_service = search.decision
    app.state.context_builder = ContextBuilder.from_settings(settings)
    # Token usage and cost estimates (only with configured prices) for monitoring.
    app.state.usage_tracker = UsageTracker.from_settings(settings)
    app.state.llm_service = llm_service or LLMService.from_settings(
        settings, usage_tracker=app.state.usage_tracker
    )
    app.state.grounded_answer_service = GroundedAnswerService(
        search.decision, app.state.context_builder, app.state.llm_service
    )
    if app.state.llm_service.is_available:
        logger.info("LLM generation enabled: model=%s", app.state.llm_service.model)
    else:
        logger.info("LLM generation disabled; Stage 1-4 endpoints are unaffected")

    # Middleware added last runs first: the request context wraps CORS and the size
    # limit, so 413 responses also get a request ID and an access-log line.
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
            expose_headers=["X-Request-ID"],
        )
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    app.include_router(health.router, prefix=settings.api_v1_prefix)
    app.include_router(faqs.router, prefix=settings.api_v1_prefix)
    app.include_router(semantic_search.router, prefix=settings.api_v1_prefix)
    app.include_router(hybrid_search.router, prefix=settings.api_v1_prefix)
    app.include_router(answer.router, prefix=settings.api_v1_prefix)

    @app.get("/", response_model=RootResponse, tags=["root"], summary="API information")
    def root() -> RootResponse:
        """Return the API name and version."""
        return RootResponse(message=settings.app_name, version=settings.app_version)

    # Log only the allow-listed summary: never the Settings object, which may hold secrets.
    logger.info(
        "%s v%s started %s", settings.app_name, settings.app_version, settings.safe_summary()
    )
    return app


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup/shutdown hook. Optionally builds the semantic index before serving, and
    closes the LLM client at shutdown.

    The build is CPU-heavy (model load + embedding), so it runs in a worker thread
    instead of blocking the event loop. A failure is logged and the app still starts:
    keyword search and the other endpoints keep working, and the index is retried
    lazily on first semantic use.
    """
    if app.state.settings.semantic_index_preload:
        try:
            await run_in_threadpool(app.state.semantic_search_service.build_index)
        except EmbeddingError as exc:
            logger.error(
                "Semantic index preload failed (%s); it will be retried on first use. %s",
                type(exc).__name__,
                exc,
            )
    yield
    app.state.llm_service.close()  # release the OpenAI connection pool, if one was opened


def _load_faq_service(settings: Settings) -> FAQService:
    """Load and validate the FAQ knowledge base once, at startup."""
    try:
        repository = FAQRepository.from_json_file(settings.faq_data_path)
    except FAQDataError as exc:
        logger.critical(
            "Cannot start: FAQ knowledge base failed to load (%s). %s", type(exc).__name__, exc
        )
        raise
    return FAQService(repository)


app = create_app()
