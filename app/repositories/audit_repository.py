"""Administrative audit log storage (Stage 11, Task 17). See ``app.models.audit``.

- ``append_audit`` adds an event **inside the caller's transaction**: the FAQ and ticket
  repositories call it in the same transaction as the change, so a committed change
  always has its audit event and a rolled-back one never does.
- ``AuditRepository.record`` writes one event in its own transaction (refused access,
  which changes nothing else). A failure there is logged and swallowed: an unwritable
  audit log must not turn a refusal into a server error.
- ``AuditRepository.list_events`` reads them, newest first, filtered and paginated.

Events are append-only: there is no update or delete method, and database triggers
refuse both (``AdminAuditEventRow``). The request ID comes from the request context.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.logging_config import request_id_ctx
from app.database.connection import Database
from app.database.models import AdminAuditEventRow
from app.models.audit import (
    MAX_DETAIL_KEYS,
    AuditAction,
    AuditActorType,
    AuditEvent,
    AuditEventPage,
    AuditOutcome,
    AuditResource,
)
from app.models.conversation import utc_now

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 200


class AuditStoreError(Exception):
    """The audit log could not be read."""


@dataclass(frozen=True)
class AuditActor:
    actor_type: AuditActorType
    actor_id: str | None = None

    @classmethod
    def staff(cls, staff_id: str) -> "AuditActor":
        return cls(AuditActorType.STAFF, staff_id)

    @classmethod
    def system(cls, service: str) -> "AuditActor":
        return cls(AuditActorType.SYSTEM, service)

    @classmethod
    def unauthenticated(cls) -> "AuditActor":
        return cls(AuditActorType.UNAUTHENTICATED)


@dataclass(frozen=True)
class AuditFilter:
    action: AuditAction | None = None
    actor_id: str | None = None
    resource_type: AuditResource | None = None
    resource_id: str | None = None
    outcome: AuditOutcome | None = None
    created_from: datetime | None = None  # inclusive
    created_before: datetime | None = None  # exclusive


def current_request_id() -> str | None:
    value = request_id_ctx.get()
    return None if value in (None, "-") else value[:64]


def append_audit(
    db: Session,
    *,
    actor: AuditActor,
    action: AuditAction,
    resource_type: AuditResource,
    resource_id: str | None,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    details: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    """Add an audit event to ``db``'s transaction (committed or rolled back with it)."""
    details = dict(details or {})
    if len(details) > MAX_DETAIL_KEYS:
        raise ValueError("audit details must stay small")
    db.add(
        AdminAuditEventRow(
            event_id=uuid.uuid4(),
            created_at=now or utc_now(),
            actor_type=actor.actor_type.value,
            actor_id=actor.actor_id,
            action=action.value,
            resource_type=resource_type.value,
            resource_id=resource_id,
            outcome=outcome.value,
            request_id=current_request_id(),
            details=details,
        )
    )


class AuditRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def record(
        self,
        *,
        actor: AuditActor,
        action: AuditAction,
        resource_type: AuditResource,
        resource_id: str | None,
        outcome: AuditOutcome,
        details: dict[str, Any] | None = None,
    ) -> bool:
        """One event in its own transaction. Returns False (and logs the error type) if
        it could not be written."""
        try:
            with self._db.write() as db:
                append_audit(
                    db,
                    actor=actor,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    outcome=outcome,
                    details=details,
                )
        except SQLAlchemyError as exc:
            logger.error("Audit event %s not recorded (%s)", action.value, type(exc).__name__)
            return False
        return True

    def list_events(
        self, filters: AuditFilter | None = None, *, offset: int = 0, limit: int = 50
    ) -> AuditEventPage:
        if offset < 0 or not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"offset must be >= 0 and limit 1..{MAX_PAGE_SIZE}")
        f = filters or AuditFilter()
        query = select(AdminAuditEventRow)
        row = AdminAuditEventRow
        for column, value in (
            (row.action, f.action.value if f.action else None),
            (row.actor_id, f.actor_id),
            (row.resource_type, f.resource_type.value if f.resource_type else None),
            (row.resource_id, f.resource_id),
            (row.outcome, f.outcome.value if f.outcome else None),
        ):
            if value is not None:
                query = query.where(column == value)
        if f.created_from is not None:
            query = query.where(row.created_at >= f.created_from)
        if f.created_before is not None:
            query = query.where(row.created_at < f.created_before)
        try:
            with self._db.read() as db:
                total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
                rows = db.scalars(
                    query.order_by(row.created_at.desc(), row.event_id).offset(offset).limit(limit)
                ).all()
                events = [_to_event(r) for r in rows]
        except SQLAlchemyError as exc:
            logger.error("Audit log read failed (%s)", type(exc).__name__)
            raise AuditStoreError("the audit log is unavailable") from exc
        return AuditEventPage(
            events=events,
            total=total,
            offset=offset,
            limit=limit,
            has_more=offset + len(events) < total,
        )


def _to_event(row: AdminAuditEventRow) -> AuditEvent:
    return AuditEvent(
        event_id=row.event_id,
        created_at=row.created_at,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        outcome=row.outcome,
        request_id=row.request_id,
        details=row.details or {},
    )
