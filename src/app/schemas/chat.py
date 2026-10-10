"""Request/response contracts for the chat endpoint."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, StringConstraints

MAX_MESSAGE_LENGTH = 2000

# Leading/trailing whitespace is stripped before length checks, so "   " is rejected.
Message = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_LENGTH),
]
# Restricting the character set keeps session IDs safe to log and to use as storage keys later.
SessionId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class ChatRequest(BaseModel):
    message: Message
    session_id: SessionId | None = None


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    created_at: datetime
