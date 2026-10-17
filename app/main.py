"""Application entry point. Run from the project root with:

uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
"""

import logging
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError
from pydantic_settings import SettingsError
from starlette.concurrency import run_in_threadpool

from app.api.errors import VALIDATION_RESPONSES, register_exception_handlers
from app.api.routes import (
    admin,
    answer,
    conversations,
    faqs,
    health,
    hybrid_search,
    semantic_search,
    support,
)
from app.core.config import (
    Settings,
    describe_settings_error,
    describe_settings_source_error,
    get_settings,
)
from app.core.logging_config import configure_logging
from app.core.middleware import (
    RequestContextMiddleware,
    RequestRateLimitMiddleware,
    RequestSizeLimitMiddleware,
)
from app.core.rate_limit import RateLimits
from app.core.staff_auth import StaffAuthenticator
from app.database.connection import Database, DatabaseError
from app.database.faq_repository import FAQDataError, FAQRepository
from app.models.root import RootResponse
from app.repositories.conversation_repository import (
    ConversationRepository,
    ConversationStoreError,
)
from app.repositories.faq_repository import DatabaseFAQRepository, FAQStoreError
from app.repositories.ticket_repository import TicketRepository
from app.services.admin_dashboard_service import AdminDashboardService
from app.services.context_builder import ContextBuilder
from app.services.conversation_history_service import ConversationHistoryService
from app.services.conversation_service import ConversationService
from app.services.customer_chat_service import CustomerChatService
from app.services.embedding_service import EmbeddingError, EmbeddingService
from app.services.escalation_service import EscalationService
from app.services.faq_management_service import FAQManagementService
from app.services.faq_service import FAQService
from app.services.grounded_answer_service import GroundedAnswerService
from app.services.llm_service import LLMService
from app.services.query_rewrite_service import QueryRewriteService
from app.services.search_pipeline import build_search_services
from app.services.semantic_search_service import SemanticSearchService
from app.services.ticket_integrations import NullTicketEventSink
from app.services.ticket_workflow import TicketWorkflow
from app.services.usage_tracker import UsageTracker

logger = logging.getLogger(__name__)

# CORS: only what the API uses. Methods: reads, chat/close/create, delete.
CORS_METHODS = ["GET", "POST", "DELETE"]
# Request headers a frontend sends: JSON bodies, the bearer token, the retry key.
CORS_REQUEST_HEADERS = ["Content-Type", "Authorization", "Idempotency-Key"]
# Response headers a frontend needs to read (browsers hide others from scripts).
CORS_EXPOSED_HEADERS = ["X-Request-ID", "Retry-After", "Idempotent-Replayed", "Location"]
CORS_MAX_AGE_SECONDS = 600  # browsers may cache a preflight for 10 minutes

DESCRIPTION = "Backend API for an AI-powered customer support chatbot."


def create_app(
    settings: Settings | None = None,
    *,
    faq_service: FAQService | None = None,
    semantic_search_service: SemanticSearchService | None = None,
    llm_service: LLMService | None = None,
    database: Database | None = None,
) -> FastAPI:
    """Build and configure a FastAPI application.

    Using a factory instead of only a module-level app lets tests create
    isolated instances with their own settings (e.g. production mode).

    Shared services are built here, once per application, and stored on
    ``app.state``; route handlers receive them through ``app.api.dependencies``.

    Args:
        settings: Configuration to use. Defaults to values from the environment.
        faq_service: A ready-made FAQ service (e.g. an in-memory one in tests).
            Defaults to one backed by the ``faqs`` table of ``database`` (Stage 11),
            seeded from the JSON file at ``settings.faq_data_path`` if the table is
            empty. FAQ management (``/admin/faqs``) is available only when the FAQ
            service reads from a ``DatabaseFAQRepository``.
        semantic_search_service: A ready-made semantic index service (e.g. with a
            fake embedding model in tests). Defaults to one indexing the active FAQs
            with ``settings.embedding_model_name``. Creating it loads nothing; the
            model and index are built on first use or by the startup preload.
        llm_service: A ready-made LLM service (e.g. with a fake client in tests).
            Defaults to one configured from the ``OPENAI_*`` settings. Creating it
            makes no network call; the OpenAI client is created on first use.
        database: Conversation, ticket and FAQ storage (e.g. an in-memory one in
            tests). Defaults to ``settings.database_url``. Without ``faq_service`` the
            schema is created here, because the FAQs are loaded from it; otherwise
            nothing is opened until startup (lifespan).

    Returns:
        A fully configured FastAPI application.

    Raises:
        FAQDataError: the FAQ table is empty and the JSON file to seed it is missing or
            invalid. The app refuses to start rather than serve incomplete answers.
        DatabaseError, FAQStoreError: the FAQ table cannot be created or read.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level, exception_messages=settings.debug)

    database = database or Database(settings.database_url)
    if faq_service is None:
        faq_service = _load_faq_service(settings, database)
    if semantic_search_service is None:
        semantic_search_service = SemanticSearchService(
            faq_service.list_active_faqs,
            EmbeddingService(
                settings.embedding_model_name,
                load_retry_seconds=settings.embedding_load_retry_seconds,
            ),
            default_min_score=settings.semantic_min_similarity,
            # Rebuilt (only changed questions re-embedded) whenever the FAQ catalogue
            # revision differs from the one it was built from (Stage 11).
            catalog_revision=faq_service.catalog_revision,
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
    search = build_search_services(
        settings, faq_service.list_active_faqs, semantic_search_service, catalog=faq_service
    )
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
        search.decision,
        app.state.context_builder,
        app.state.llm_service,
        # Evidence edited or deactivated while an answer was produced is never served.
        current_faq=lambda faq_id: faq_service.latest().get(faq_id),
    )
    # Conversations (Stage 6): sessions, history, query rewriting around the same pipeline.
    app.state.database = database
    repository = ConversationRepository(app.state.database)
    app.state.conversation_service = ConversationService.from_settings(settings, repository)
    # Customer chat (Stage 7): one turn, orchestrated over the services above.
    app.state.customer_chat_service = CustomerChatService(
        app.state.conversation_service,
        ConversationHistoryService.from_settings(
            settings, app.state.conversation_service, repository
        ),
        QueryRewriteService.from_settings(settings, app.state.llm_service),
        app.state.grounded_answer_service,
        faq_lookup=faq_service.get_active_faq,
        max_message_chars=settings.max_customer_message_characters,
        fallback_message=settings.fallback_no_match_message,
    )
    # Support tickets (Stage 10): handoff to staff and sales leads, in the same database.
    app.state.ticket_repository = TicketRepository(app.state.database)
    app.state.staff_authenticator = StaffAuthenticator.from_settings(settings)
    # No integration yet (email, CRM, helpdesk): committed changes go nowhere, and
    # nothing claims a notification. See app.services.ticket_integrations.
    app.state.ticket_events = NullTicketEventSink()
    app.state.escalation_service = EscalationService.from_settings(
        settings,
        app.state.conversation_service,
        repository,
        app.state.ticket_repository,
        events=app.state.ticket_events,
    )
    app.state.ticket_workflow = TicketWorkflow(
        app.state.ticket_repository,
        app.state.staff_authenticator,
        app.state.conversation_service,
        events=app.state.ticket_events,
    )
    # FAQ management and the dashboard overview (Stage 11). FAQ edits publish a new
    # catalogue snapshot at once; the semantic index is then brought up to date (only
    # changed questions re-embedded), and searches never use an index for another
    # revision (see app.services.faq_catalog).
    faq_store = faq_service.repository
    database_faqs = faq_store if isinstance(faq_store, DatabaseFAQRepository) else None
    app.state.faq_management_service = (
        FAQManagementService(database_faqs, on_change=[semantic_search_service.refresh])
        if database_faqs is not None
        else None
    )
    app.state.admin_dashboard_service = AdminDashboardService(
        database_faqs, app.state.ticket_repository
    )
    if not app.state.staff_authenticator.staff_ids:
        logger.info("No staff configured (STAFF_TOKEN_HASHES): staff endpoints are disabled")
    if app.state.llm_service.is_available:
        logger.info("LLM generation enabled: model=%s", app.state.llm_service.model)
    else:
        logger.info("LLM generation disabled; Stage 1-4 endpoints are unaffected")

    # Middleware added last runs first: the request context wraps CORS and the size
    # limit, so 413 responses also get a request ID and an access-log line.
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    # Basic abuse protection (process-local, in memory): per-IP request allowance here,
    # plus per-IP conversation creation and per-session chat limits on those routes.
    app.state.rate_limits = RateLimits.from_settings(settings)
    if app.state.rate_limits.requests is not None:
        app.add_middleware(
            RequestRateLimitMiddleware,
            limiter=app.state.rate_limits.requests,
            prefix=settings.api_v1_prefix,
            exempt=(f"{settings.api_v1_prefix}/health", f"{settings.api_v1_prefix}/ready"),
        )
    if settings.cors_origins:
        # Lets the listed browser origins (a Streamlit or React frontend) call the API.
        # CORS only tells browsers which pages may read responses; it is not access
        # control. Requests are authorised by the Authorization bearer token, which is
        # sent as a header (not a cookie), so credentials mode is off and wildcards are
        # never used.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,
            allow_methods=CORS_METHODS,
            allow_headers=CORS_REQUEST_HEADERS,
            expose_headers=CORS_EXPOSED_HEADERS,
            max_age=CORS_MAX_AGE_SECONDS,
        )
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    app.include_router(health.router, prefix=settings.api_v1_prefix, responses=VALIDATION_RESPONSES)
    app.include_router(faqs.router, prefix=settings.api_v1_prefix, responses=VALIDATION_RESPONSES)
    app.include_router(
        semantic_search.router, prefix=settings.api_v1_prefix, responses=VALIDATION_RESPONSES
    )
    app.include_router(
        hybrid_search.router, prefix=settings.api_v1_prefix, responses=VALIDATION_RESPONSES
    )
    app.include_router(answer.router, prefix=settings.api_v1_prefix, responses=VALIDATION_RESPONSES)
    app.include_router(
        conversations.router, prefix=settings.api_v1_prefix, responses=VALIDATION_RESPONSES
    )
    app.include_router(
        support.router, prefix=settings.api_v1_prefix, responses=VALIDATION_RESPONSES
    )
    app.include_router(admin.router, prefix=settings.api_v1_prefix, responses=VALIDATION_RESPONSES)

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
    """Startup/shutdown hook. Creates missing conversation tables, deletes expired
    conversations (``CONVERSATION_CLEANUP_ON_STARTUP``), optionally builds the semantic
    index (``SEMANTIC_INDEX_PRELOAD``: before serving, or in the background while
    serving), and closes the LLM client and database at shutdown.

    A conversation database that cannot be initialised is logged and the app still
    starts: FAQ, search and answer endpoints do not need it, and conversation endpoints
    answer 503 until the problem is fixed.

    The build is CPU-heavy (model load + embedding), so it runs in a worker thread
    instead of blocking the event loop. A failure is logged and the app still starts:
    keyword search and the other endpoints keep working, and the index is retried
    lazily on first semantic use.
    """
    try:
        await run_in_threadpool(app.state.database.init_schema)
        if app.state.settings.conversation_cleanup_on_startup:
            await run_in_threadpool(app.state.conversation_service.cleanup_expired)
    except (DatabaseError, ConversationStoreError) as exc:
        logger.critical(
            "Conversation storage unavailable; conversation endpoints will answer 503. %s", exc
        )
    preload = app.state.settings.semantic_index_preload
    if preload == "background":
        threading.Thread(
            target=_build_semantic_index,
            args=(app.state.semantic_search_service,),
            name="semantic-index-preload",
            daemon=True,  # never delays shutdown
        ).start()
    elif preload:
        await run_in_threadpool(_build_semantic_index, app.state.semantic_search_service)
    yield
    app.state.llm_service.close()  # release the OpenAI connection pool, if one was opened
    app.state.database.dispose()


def _build_semantic_index(service: SemanticSearchService) -> None:
    """Load the embedding model and build the FAQ index ahead of the first search.

    Built at most once: a search arriving meanwhile waits for this build (the service
    locks it). A failure is logged; the index is then retried on first use.
    """
    try:
        service.build_index()
    except EmbeddingError as exc:
        logger.error(
            "Semantic index preload failed (%s); it will be retried on first use. %s",
            type(exc).__name__,
            exc,
        )


def _load_faq_service(settings: Settings, database: Database) -> FAQService:
    """The FAQ knowledge base from the database, loaded once at startup.

    An empty ``faqs`` table (a new database, or one from before Stage 11) is seeded from
    ``FAQ_DATA_PATH`` once, in one transaction. A table with any row is never re-imported
    or overwritten here: after seeding, staff edits are the source of truth, and the
    JSON file is only read again by ``scripts/import_faqs.py``.
    """
    repository = DatabaseFAQRepository(database)
    try:
        database.init_schema()
        if repository.count() == 0:
            faqs = FAQRepository.from_json_file(settings.faq_data_path).list_all()
            report = repository.import_faqs(faqs)
            logger.info(
                "FAQ table was empty: imported %d FAQs from %s",
                len(report.created),
                settings.faq_data_path.name,
            )
        repository.reload()
    except FAQDataError as exc:
        logger.critical(
            "Cannot start: FAQ knowledge base failed to load (%s). %s", type(exc).__name__, exc
        )
        raise
    except (DatabaseError, FAQStoreError) as exc:
        logger.critical("Cannot start: FAQ storage unavailable (%s)", type(exc).__name__)
        raise
    logger.info(
        "Loaded %d FAQs (%d active) from the database",
        len(repository),
        len(repository.list_active()),
    )
    return FAQService(repository)


def _create_default_app() -> FastAPI:
    """The application uvicorn serves, built from the environment and ``.env``.

    A configuration problem stops the process with a short message naming what to fix
    (never a setting's value) instead of a traceback.
    """
    try:
        return create_app()
    except ValidationError as exc:
        raise SystemExit(describe_settings_error(exc)) from None
    except SettingsError as exc:
        raise SystemExit(describe_settings_source_error(exc)) from None
    except FAQDataError as exc:
        raise SystemExit(
            f"Cannot start: the FAQ knowledge base failed to load ({type(exc).__name__}). "
            f"{exc}\nCheck FAQ_DATA_PATH (default: data/faqs.json)."
        ) from None
    except (DatabaseError, FAQStoreError) as exc:
        raise SystemExit(
            f"Cannot start: the FAQ storage could not be opened ({type(exc).__name__}). "
            f"{exc}\nCheck DATABASE_URL (default: sqlite:///data/conversations.db)."
        ) from None


app = _create_default_app()
