"""Application entry point. Run with: uvicorn app.main:app --reload"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import faqs, health
from app.core.config import Settings, get_settings
from app.core.logging_config import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.database.faq_repository import FAQDataError, FAQRepository
from app.models.root import RootResponse
from app.services.faq_service import FAQService

logger = logging.getLogger(__name__)

DESCRIPTION = "Backend API for an AI-powered customer support chatbot."


def create_app(
    settings: Settings | None = None, *, faq_service: FAQService | None = None
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

    app = FastAPI(
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

    # Middleware added last runs first, so the request context wraps CORS too.
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
            expose_headers=["X-Request-ID"],
        )
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router, prefix=settings.api_v1_prefix)
    app.include_router(faqs.router, prefix=settings.api_v1_prefix)

    @app.get("/", response_model=RootResponse, tags=["root"], summary="API information")
    def root() -> RootResponse:
        """Return the API name and version."""
        return RootResponse(message=settings.app_name, version=settings.app_version)

    # Log only the allow-listed summary: never the Settings object, which may hold secrets.
    logger.info(
        "%s v%s started %s", settings.app_name, settings.app_version, settings.safe_summary()
    )
    return app


def _load_faq_service(settings: Settings) -> FAQService:
    """Load and validate the FAQ knowledge base once, at startup."""
    try:
        repository = FAQRepository.from_json_file(settings.faq_data_path)
    except FAQDataError as exc:
        logger.critical("Cannot start: FAQ knowledge base failed to load. %s", exc)
        raise
    return FAQService(repository)


app = create_app()
