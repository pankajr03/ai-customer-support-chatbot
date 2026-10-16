"""Support request bodies for the Stage 10 ticket tests."""

import uuid
from typing import Any

from app.models.support import SupportRequestCreate


def form_body(**changes: Any) -> dict[str, Any]:
    """A valid support request body (JSON-ready); ``...`` removes a field."""
    body: dict[str, Any] = {
        "client_request_id": str(uuid.uuid4()),
        "ticket_type": "support",
        "name": "Alex Rivera",
        "email": "alex@example.com",
        "subject": "Order arrived damaged",
        "message": "The lid was cracked. Can you replace it?",
        "consent_given": True,
        "confirmed": True,
    }
    body.update(changes)
    return {key: value for key, value in body.items() if value is not ...}


def form(**changes: Any) -> SupportRequestCreate:
    """A validated support request (a new client_request_id unless one is given)."""
    return SupportRequestCreate.model_validate(form_body(**changes))
