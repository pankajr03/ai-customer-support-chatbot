"""FastAPI dependencies that hand application-wide objects to route handlers.

Strategy: expensive objects (like the FAQ service, which loads and validates the
JSON knowledge base) are built once in ``create_app()`` and stored on
``app.state``. These functions only look them up, so no request ever reloads data.

Tests can replace an object in two ways:
- build the app with ``create_app(settings, faq_service=...)``, or
- set ``app.dependency_overrides[get_faq_service] = lambda: fake_service``.
"""

from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.rate_limit import RateLimits, session_key
from app.services.answer_decision_service import AnswerDecisionService
from app.services.context_builder import ContextBuilder
from app.services.conversation_service import ConversationService
from app.services.customer_chat_service import CustomerChatService
from app.services.faq_service import FAQService
from app.services.grounded_answer_service import GroundedAnswerService
from app.services.hybrid_search_service import HybridSearchService
from app.services.llm_service import LLMService
from app.services.semantic_search_service import SemanticSearchService
from app.services.usage_tracker import UsageTracker


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


def get_grounded_answer_service(request: Request) -> GroundedAnswerService:
    """Return the application's grounded answer generator (LLM with safe fallback)."""
    service = getattr(request.app.state, "grounded_answer_service", None)
    if service is None:
        raise RuntimeError(
            "GroundedAnswerService is not initialised; build the app with create_app()."
        )
    return service


GroundedAnswerServiceDep = Annotated[GroundedAnswerService, Depends(get_grounded_answer_service)]


def get_usage_tracker(request: Request) -> UsageTracker:
    """Return the application's LLM usage tracker (token counts and cost estimates)."""
    tracker = getattr(request.app.state, "usage_tracker", None)
    if tracker is None:
        raise RuntimeError("UsageTracker is not initialised; build the app with create_app().")
    return tracker


UsageTrackerDep = Annotated[UsageTracker, Depends(get_usage_tracker)]


def get_conversation_service(request: Request) -> ConversationService:
    """Return the application's conversation session service."""
    service = getattr(request.app.state, "conversation_service", None)
    if service is None:
        raise RuntimeError(
            "ConversationService is not initialised; build the app with create_app()."
        )
    return service


ConversationServiceDep = Annotated[ConversationService, Depends(get_conversation_service)]


def get_customer_chat_service(request: Request) -> CustomerChatService:
    """Return the application's customer chat orchestration service."""
    service = getattr(request.app.state, "customer_chat_service", None)
    if service is None:
        raise RuntimeError(
            "CustomerChatService is not initialised; build the app with create_app()."
        )
    return service


CustomerChatServiceDep = Annotated[CustomerChatService, Depends(get_customer_chat_service)]


# --- Conversation access token --------------------------------------------------------

conversation_token_scheme = HTTPBearer(
    scheme_name="ConversationToken",
    description="The `access_token` returned once by `POST /api/v1/conversations`, sent as "
    "`Authorization: Bearer <token>`. It grants access to that one conversation only. "
    "Never send it in a URL.",
    auto_error=False,
)
# Query parameters that would put a token in a URL (logs, browser history, Referer).
_TOKEN_QUERY_PARAMETERS = ("access_token", "token", "authorization")


class AccessTokenMissingError(Exception):
    """No ``Authorization: Bearer`` header (401, see ``app.api.errors``)."""


class AccessTokenInURLError(Exception):
    """A token was sent as a query parameter (400, see ``app.api.errors``)."""


def get_access_token(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Security(conversation_token_scheme)
    ],
) -> str:
    """The bearer token from the Authorization header.

    401 when it is missing (checked before any lookup, so it reveals nothing about the
    session); 400 when a token is sent in the URL instead. Whether the token is valid
    for the session is checked by the conversation service (404 if not).
    """
    if any(name in request.query_params for name in _TOKEN_QUERY_PARAMETERS):
        raise AccessTokenInURLError()
    if credentials is None or not credentials.credentials:
        raise AccessTokenMissingError()
    return credentials.credentials


AccessTokenDep = Annotated[str, Depends(get_access_token)]


# --- Rate limits (process-local; see app.core.rate_limit) -----------------------------


def _limits(request: Request) -> RateLimits | None:
    return getattr(request.app.state, "rate_limits", None)


def limit_new_conversations(request: Request) -> None:
    """Count a new conversation for the client's IP address (429 when over the limit)."""
    limits = _limits(request)
    if limits is not None and limits.new_conversations is not None:
        client = request.client.host if request.client else "unknown"
        limits.new_conversations.hit(client)


def limit_chat_messages(request: Request, token: AccessTokenDep) -> None:
    """Count a chat message for the conversation and presented token (after the 401
    check, before any lookup)."""
    limits = _limits(request)
    if limits is not None and limits.chat_messages is not None:
        session_id = str(request.path_params.get("session_id", ""))
        limits.chat_messages.hit(session_key(session_id, token))
