"""Chat business logic, kept independent of HTTP so it can be reused and tested directly."""

import logging
import uuid
from datetime import UTC, datetime

from app.schemas.chat import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)

PLACEHOLDER_REPLY = (
    "Thanks for your message. Our AI assistant is not connected yet, "
    "but your request has been received."
)


class ChatService:
    """Produces replies to customer messages.

    Stage 1 returns a fixed placeholder. A later stage replaces the body of
    `reply` with an LLM call; the API layer will not need to change.
    """

    def reply(self, request: ChatRequest) -> ChatResponse:
        session_id = request.session_id or uuid.uuid4().hex
        # Log metadata only: message content may contain customer personal data.
        logger.info(
            "Chat message received (session=%s, chars=%d)", session_id, len(request.message)
        )
        return ChatResponse(
            reply=PLACEHOLDER_REPLY,
            session_id=session_id,
            created_at=datetime.now(UTC),
        )
