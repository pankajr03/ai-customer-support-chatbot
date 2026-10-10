"""Chat business logic, kept independent of HTTP so it can be reused and tested directly."""

import logging
import uuid
from datetime import UTC, datetime

from app.models.chat import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)

PLACEHOLDER_REPLY = (
    "Thanks for your message. Our AI assistant is not connected yet, "
    "but your request has been received."
)


def generate_reply(request: ChatRequest) -> ChatResponse:
    """Produce a reply to a customer message.

    Stage 1 returns a fixed placeholder. A later stage replaces this body with
    an LLM call; the API route will not need to change.
    """
    session_id = request.session_id or uuid.uuid4().hex
    # Log metadata only: message content may contain customer personal data.
    logger.info("Chat message received (session=%s, chars=%d)", session_id, len(request.message))
    return ChatResponse(
        reply=PLACEHOLDER_REPLY,
        session_id=session_id,
        created_at=datetime.now(UTC),
    )
