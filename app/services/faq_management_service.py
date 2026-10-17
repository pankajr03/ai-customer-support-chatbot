"""FAQ management for staff (Stage 11): create, list, search, edit, (de)activate.

Rules:

- **Only admins.** Every method takes the authenticated ``StaffMember`` and checks the
  ``manage_faqs`` permission itself, in addition to the API's check, so no future caller
  can skip it.
- **Validation** happens in the input models (``FAQDraft``, ``FAQChanges``): empty
  questions and answers, over-long text, unknown categories, invalid or too many
  keywords are refused; keywords (tags) are normalised the same way as in the JSON file.
- **No lost updates.** An edit names the version it was based on; if someone changed
  the FAQ meanwhile, it is refused with ``FAQVersionConflictError`` (the caller reloads
  and decides), never silently overwritten.
- **Deactivate, never delete.** Deactivated FAQs disappear from customer answers and
  search but stay stored, so stored chat replies that cite them keep their meaning and
  IDs are never reused. Permanent deletion is deliberately not offered.
- At least one FAQ must stay active (``LastActiveFAQError``).
- **History.** Every creation and change is recorded as a revision in the same
  transaction (``list_revisions``): who, when, and the changed fields before and after.

After each committed change the retrieval snapshot is already current (the repository
reloads it) and the ``on_change`` callbacks run, for example to drop the semantic index
so it is rebuilt from the new catalogue on next use. A failing callback is logged and
does not undo the change.

Logs name the FAQ ID, the operation and the staff ID, never FAQ text.
"""

import logging
from collections.abc import Callable, Iterable
from datetime import datetime

from app.core.staff_auth import StaffMember, StaffPermission
from app.models.faq import FAQCategory
from app.models.faq_admin import (
    FAQCatalogSummary,
    FAQChanges,
    FAQDraft,
    FAQFilter,
    FAQRecordPage,
    FAQRevisionPage,
    ManagedFAQ,
)
from app.repositories.faq_repository import DatabaseFAQRepository

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 20

ChangeListener = Callable[[], None]


class FAQManagementService:
    """Staff operations on the database FAQ catalogue."""

    def __init__(
        self, repository: DatabaseFAQRepository, *, on_change: Iterable[ChangeListener] = ()
    ) -> None:
        self._repository = repository
        self._listeners = tuple(on_change)

    def list_faqs(
        self,
        actor: StaffMember,
        *,
        search: str | None = None,
        category: FAQCategory | None = None,
        is_active: bool | None = None,
        offset: int = 0,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> FAQRecordPage:
        """One page of FAQs (active and inactive), in ID order.

        Raises:
            StaffPermissionError; ValueError (invalid page or search text).
        """
        actor.require(StaffPermission.MANAGE_FAQS)
        filters = FAQFilter(
            search=search if search and search.strip() else None,
            category=category,
            is_active=is_active,
        )
        return self._repository.list_records(filters, offset=offset, limit=limit)

    def get_faq(self, actor: StaffMember, faq_id: str) -> ManagedFAQ:
        """Raises StaffPermissionError, FAQNotFoundError."""
        actor.require(StaffPermission.MANAGE_FAQS)
        return self._repository.get_record(faq_id)

    def create_faq(
        self, actor: StaffMember, draft: FAQDraft, *, now: datetime | None = None
    ) -> ManagedFAQ:
        """Store a new FAQ under the next free ID.

        Raises:
            StaffPermissionError, FAQIdsExhaustedError, FAQStoreError.
        """
        actor.require(StaffPermission.MANAGE_FAQS)
        created = self._repository.create(draft, actor_id=actor.staff_id, now=now)
        logger.info(
            "event=faq_created faq_id=%s actor=staff:%s active=%s",
            created.id,
            actor.staff_id,
            created.is_active,
        )
        self._changed()
        return created

    def update_faq(
        self,
        actor: StaffMember,
        faq_id: str,
        changes: FAQChanges,
        *,
        now: datetime | None = None,
    ) -> ManagedFAQ:
        """Apply an edit based on ``changes.expected_version``.

        Raises:
            StaffPermissionError, FAQNotFoundError, FAQVersionConflictError,
            LastActiveFAQError, FAQStoreError.
        """
        actor.require(StaffPermission.MANAGE_FAQS)
        updated = self._repository.update(faq_id, changes, actor_id=actor.staff_id, now=now)
        if updated.version != changes.expected_version:  # something was written
            logger.info(
                "event=faq_updated faq_id=%s actor=staff:%s version=%d fields=%s",
                updated.id,
                actor.staff_id,
                updated.version,
                ",".join(sorted(changes.changed_fields())),
            )
            self._changed()
        return updated

    def deactivate_faq(
        self,
        actor: StaffMember,
        faq_id: str,
        expected_version: int,
        *,
        now: datetime | None = None,
    ) -> ManagedFAQ:
        """Take an FAQ out of customer answers and search (it stays stored)."""
        return self.update_faq(
            actor, faq_id, FAQChanges(expected_version=expected_version, is_active=False), now=now
        )

    def activate_faq(
        self,
        actor: StaffMember,
        faq_id: str,
        expected_version: int,
        *,
        now: datetime | None = None,
    ) -> ManagedFAQ:
        """Make a deactivated FAQ available to customers again."""
        return self.update_faq(
            actor, faq_id, FAQChanges(expected_version=expected_version, is_active=True), now=now
        )

    def list_revisions(
        self, actor: StaffMember, faq_id: str, *, offset: int = 0, limit: int = DEFAULT_PAGE_SIZE
    ) -> FAQRevisionPage:
        """An FAQ's change history, newest first (admins only).

        Raises:
            StaffPermissionError, FAQNotFoundError, ValueError (invalid page).
        """
        actor.require(StaffPermission.MANAGE_FAQS)
        return self._repository.list_revisions(faq_id, offset=offset, limit=limit)

    def summary(self, actor: StaffMember) -> FAQCatalogSummary:
        """Catalogue counts (analytics: needs ``view_analytics``)."""
        actor.require(StaffPermission.VIEW_ANALYTICS)
        return self._repository.summary()

    def _changed(self) -> None:
        for listener in self._listeners:
            try:
                listener()
            except Exception as exc:  # noqa: BLE001 - the change is committed regardless
                logger.error("FAQ change listener failed (%s)", type(exc).__name__)
