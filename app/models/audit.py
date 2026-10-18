"""Administrative audit events (Stage 11, Task 17).

An append-only record of who did what to which resource: FAQ and ticket changes made
through the staff API, imports, and refused staff access. It is separate from the
application log (which is for diagnostics and may be rotated away) and from the FAQ
revision history and ticket events (which record *content* and *status* history).

What an event holds: the actor, the action, the resource type and ID, the outcome, the
request ID (to find the matching log lines) and a few non-personal details such as
``{"from": "open", "to": "assigned"}`` or the names of changed fields. Never tokens,
passwords, customer contact details, ticket text, note text or FAQ text.

Actors are never invented:

- ``staff`` with the staff ID behind the authenticated personal token;
- ``system`` with a fixed, documented service name (``faq-import`` for the import
  script and the start-up seeding, ``retention`` for anonymization runs);
- ``unauthenticated`` with no ID, for refused requests whose token identified nobody.
"""

import uuid
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class AuditActorType(StrEnum):
    STAFF = "staff"
    SYSTEM = "system"
    UNAUTHENTICATED = "unauthenticated"


class AuditAction(StrEnum):
    FAQ_CREATED = "faq.created"
    FAQ_UPDATED = "faq.updated"
    FAQ_ACTIVATED = "faq.activated"
    FAQ_DEACTIVATED = "faq.deactivated"
    FAQ_IMPORTED = "faq.imported"
    TICKET_STATUS_CHANGED = "ticket.status_changed"
    TICKET_ASSIGNMENT_CHANGED = "ticket.assignment_changed"
    TICKET_NOTE_ADDED = "ticket.note_added"
    TICKET_ANONYMIZED = "ticket.anonymized"
    ACCESS_DENIED = "auth.access_denied"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    DENIED = "denied"


class AuditResource(StrEnum):
    FAQ = "faq"
    FAQ_CATALOG = "faq_catalog"
    TICKET = "ticket"
    ENDPOINT = "endpoint"  # a refused request: the resource is the path


# Documented service actors (``system``): never a person.
IMPORT_ACTOR = "faq-import"
RETENTION_ACTOR = "retention"

MAX_DETAIL_KEYS = 8


class AuditEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: uuid.UUID
    created_at: AwareDatetime
    actor_type: AuditActorType
    actor_id: str | None = Field(description="Staff ID or service name; null if unknown.")
    action: AuditAction
    resource_type: AuditResource
    resource_id: str | None
    outcome: AuditOutcome
    request_id: str | None = Field(description="Matches X-Request-ID and the log lines.")
    details: dict[str, Any] = Field(description="Non-personal context, e.g. from/to status.")


class AuditEventPage(BaseModel):
    """One page of audit events, newest first."""

    model_config = ConfigDict(frozen=True)

    events: list[AuditEvent]
    total: int
    offset: int
    limit: int
    has_more: bool
