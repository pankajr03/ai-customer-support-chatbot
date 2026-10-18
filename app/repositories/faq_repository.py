"""FAQ storage in SQLite (Stage 11): the editable source of truth for the knowledge base.

``DatabaseFAQRepository`` has two faces:

- **Retrieval** (``snapshot``, ``list_all``, ``list_active``, ``get_by_id``,
  ``list_by_category``, ``len``): the same read interface as the JSON ``FAQRepository``,
  so ``FAQService`` and every search service work unchanged. These read an in-memory
  ``CatalogSnapshot`` of validated ``FAQ`` records, never the database, so a chat turn
  costs no extra queries. The snapshot is loaded on first use and replaced after every
  committed change made through this repository, with its ``revision`` increased by one
  whenever the content differs (see ``app.services.faq_catalog``). It is immutable and
  swapped by one assignment: readers see the old catalogue or the new one, never a mix.
- **Management** (``get_record``, ``list_records``, ``create``, ``update``,
  ``import_faqs``, ``list_revisions``, ``summary``): ``ManagedFAQ`` records with their
  version and timestamps, each operation one short transaction (writes take SQLite's
  write lock up front, see ``app.database.connection``).

History: every creation and change writes an ``faq_revisions`` row **in the same
transaction** as the change, so the history can never miss a committed change or record
one that was rolled back. A revision holds only the fields that changed (the full
content for a creation) and the staff ID.

Order: FAQs are listed by ID number (``faq_002`` before ``faq_010`` before
``faq_1000``), which matches the order of ``data/faqs.json``.

Concurrency: every update names the version it was based on (``expected_version``);
a different stored version raises ``FAQVersionConflictError`` and nothing is written.
New IDs are allocated inside the write transaction, so two staff members creating FAQs
at once get different IDs.

Several processes: the snapshot belongs to one process. A change made by another
process (``scripts/import_faqs.py`` while the API runs, a second API worker) is seen
after ``reload()`` or a restart. The admin dashboard changes FAQs through the API, so
the API's own snapshot is always current.

Errors: database failures become ``FAQStoreError`` with the operation name only (never
SQL parameters, which hold FAQ text).
"""

import logging
import re
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import Select, String, cast, func, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.database.connection import Database
from app.database.models import FAQRevisionRow, FAQRow
from app.models.faq import FAQ, FAQ_ID_PATTERN, FAQCategory, parse_category
from app.models.faq_admin import (
    FAQ_CONTENT_FIELDS,
    CategoryCount,
    FAQCatalogSummary,
    FAQChanges,
    FAQDraft,
    FAQFilter,
    FAQImportReport,
    FAQRecordPage,
    FAQRevision,
    FAQRevisionAction,
    FAQRevisionPage,
    ManagedFAQ,
)
from app.services.faq_catalog import CatalogSnapshot

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100
MAX_ID_NUMBER = 999_999  # faq_ + up to 6 digits
_ID_NUMBER = re.compile(FAQ_ID_PATTERN)
_LIKE_ESCAPE = "\\"


class FAQRepositoryError(Exception):
    """Base class for FAQ storage errors."""


class FAQNotFoundError(FAQRepositoryError):
    def __init__(self) -> None:
        super().__init__("FAQ not found")


class FAQVersionConflictError(FAQRepositoryError):
    """The FAQ changed since it was read: reload it and apply the edit again."""

    def __init__(self, expected: int, current: int) -> None:
        super().__init__(f"FAQ is at version {current}, the change was based on {expected}")
        self.expected = expected
        self.current = current


class DuplicateFAQQuestionError(FAQRepositoryError):
    """Another FAQ (active or not) already has this question (ignoring case)."""

    def __init__(self, faq_id: str) -> None:
        super().__init__(f"{faq_id} already has this question")
        self.faq_id = faq_id


class LastActiveFAQError(FAQRepositoryError):
    """Deactivating this FAQ would leave customers with no active FAQ at all."""

    def __init__(self) -> None:
        super().__init__("at least one FAQ must stay active")


class FAQIdsExhaustedError(FAQRepositoryError):
    def __init__(self) -> None:
        super().__init__(f"no FAQ ID left (faq_{MAX_ID_NUMBER} is the highest)")


class FAQStoreError(FAQRepositoryError):
    """The FAQ table could not be read or written (or holds an invalid row)."""


class FAQStoreBusyError(FAQStoreError):
    """Another writer held the database lock for longer than the busy timeout."""


class _DryRunFinished(Exception):
    """Raised inside an import transaction to roll it back on purpose."""


def _check_page(offset: int, limit: int) -> None:
    if offset < 0:
        raise ValueError(f"offset must not be negative, got {offset}")
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}, got {limit}")


def normalise_faq_id(faq_id: str) -> str | None:
    """The canonical form of an FAQ ID (``" FAQ_001 "`` -> ``faq_001``), or None if it
    cannot be one (then it cannot exist either)."""
    candidate = faq_id.strip().lower()
    return candidate if _ID_NUMBER.fullmatch(candidate) else None


def _id_order() -> tuple[ColumnElement[int], ColumnElement[str]]:
    # By number: shorter IDs first, then alphabetically (equal length = numeric order).
    return func.length(FAQRow.id), FAQRow.id


class DatabaseFAQRepository:
    """FAQs in the database, with a validated in-memory snapshot for retrieval."""

    def __init__(self, database: Database) -> None:
        self._db = database
        self._snapshot: CatalogSnapshot | None = None

    # --- Retrieval (same interface as the JSON FAQRepository) -------------------------

    def reload(self) -> int:
        """Load the catalogue from the database into a new snapshot. The revision only
        increases when the content differs from the previous snapshot.

        Returns:
            The number of FAQs (active or not).

        Raises:
            FAQStoreError: the table cannot be read or holds an invalid row (a database
                edited by hand, for example). The previous snapshot is kept.
        """
        with self._read("reload") as db:
            rows = db.scalars(select(FAQRow).order_by(*_id_order())).all()
            faqs = tuple(_to_record(row).to_faq() for row in rows)
        previous = self._snapshot
        if previous is None or previous.faqs != faqs:
            revision = previous.revision + 1 if previous is not None else 1
            self._snapshot = CatalogSnapshot(revision=revision, faqs=faqs)
            logger.info(
                "event=faq_catalog_reloaded revision=%d faqs=%d active=%d",
                revision,
                len(faqs),
                len(self._snapshot.active),
            )
        return len(faqs)

    def snapshot(self) -> CatalogSnapshot:
        """The latest catalogue snapshot (loaded on first use)."""
        if self._snapshot is None:
            self.reload()
        assert self._snapshot is not None  # noqa: S101 - set by reload or it raised
        return self._snapshot

    def _faqs(self) -> tuple[FAQ, ...]:
        return self.snapshot().faqs

    def __len__(self) -> int:
        return len(self._faqs())

    def list_all(self) -> list[FAQ]:
        """Every FAQ, active or not, in ID order."""
        return list(self._faqs())

    def list_active(self) -> list[FAQ]:
        """FAQs available for retrieval, in ID order."""
        return [faq for faq in self._faqs() if faq.is_active]

    def get_by_id(self, faq_id: str) -> FAQ | None:
        """The FAQ with this ID (case-insensitive), or None."""
        return self.snapshot().get(faq_id)

    def list_by_category(
        self, category: FAQCategory | str, *, active_only: bool = False
    ) -> list[FAQ]:
        """FAQs in a category, in ID order.

        Raises:
            ValueError: ``category`` is not a known FAQ category.
        """
        wanted = parse_category(category)
        return [
            faq
            for faq in self._faqs()
            if faq.category is wanted and (faq.is_active or not active_only)
        ]

    # --- Management -----------------------------------------------------------------

    def count(self) -> int:
        """How many FAQs are stored (read from the database, not the snapshot)."""
        with self._read("count") as db:
            return db.scalar(select(func.count()).select_from(FAQRow)) or 0

    def get_record(self, faq_id: str) -> ManagedFAQ:
        """Raises ``FAQNotFoundError`` for an unknown (or impossible) ID."""
        canonical = normalise_faq_id(faq_id)
        if canonical is None:
            raise FAQNotFoundError()
        with self._read("get_record") as db:
            row = db.get(FAQRow, canonical)
            if row is None:
                raise FAQNotFoundError()
            return _to_record(row)

    def list_records(
        self, filters: FAQFilter | None = None, *, offset: int = 0, limit: int = 20
    ) -> FAQRecordPage:
        """One page of matching FAQs in ID order.

        ``search`` matches the ID exactly, or the question, answer or keywords as a
        substring (case-insensitive for ASCII letters, SQLite's ``LIKE``).
        """
        _check_page(offset, limit)
        query = _filtered(select(FAQRow), filters or FAQFilter())
        with self._read("list_records") as db:
            total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
            rows = db.scalars(query.order_by(*_id_order()).offset(offset).limit(limit)).all()
            return FAQRecordPage(
                items=tuple(_to_record(row) for row in rows),
                total=total,
                offset=offset,
                limit=limit,
            )

    def create(self, draft: FAQDraft, *, actor_id: str, now: datetime | None = None) -> ManagedFAQ:
        """Store a new FAQ under the next free ID, as version 1.

        Raises:
            FAQIdsExhaustedError, FAQStoreError.
        """
        now = now or datetime.now(UTC)
        with self._write("create") as db:
            _refuse_duplicate_question(db, draft.question, None)
            faq_id = _next_id(db)
            record = ManagedFAQ(
                id=faq_id,
                **draft.model_dump(),
                version=1,
                created_at=now,
                updated_at=now,
                created_by=actor_id,
                updated_by=actor_id,
            )
            db.add(_to_row(record))
            db.flush()  # the revision references the new row
            db.add(_revision_row(None, record, actor_id, now))
        self.reload()
        return record

    def update(
        self, faq_id: str, changes: FAQChanges, *, actor_id: str, now: datetime | None = None
    ) -> ManagedFAQ:
        """Apply ``changes`` if the FAQ is still at ``changes.expected_version``.

        An edit that changes nothing (every given field already has that value) keeps
        the version and writes nothing. Deactivating the last active FAQ is refused: the
        assistant would have nothing left to answer with.

        Raises:
            FAQNotFoundError, FAQVersionConflictError, LastActiveFAQError, FAQStoreError.
        """
        canonical = normalise_faq_id(faq_id)
        if canonical is None:
            raise FAQNotFoundError()
        now = now or datetime.now(UTC)
        with self._write("update") as db:
            row = db.get(FAQRow, canonical)
            if row is None:
                raise FAQNotFoundError()
            current = _to_record(row)
            if current.version != changes.expected_version:
                raise FAQVersionConflictError(changes.expected_version, current.version)
            fields = changes.changed_fields()
            if all(getattr(current, name) == value for name, value in fields.items()):
                return current
            if "question" in fields:
                _refuse_duplicate_question(db, str(fields["question"]), canonical)
            if fields.get("is_active") is False and current.is_active:
                others = db.scalar(
                    select(func.count())
                    .select_from(FAQRow)
                    .where(FAQRow.is_active.is_(True), FAQRow.id != canonical)
                )
                if not others:
                    raise LastActiveFAQError()
            updated = ManagedFAQ.model_validate(
                {
                    **current.model_dump(),
                    **fields,
                    "version": current.version + 1,
                    "updated_at": max(now, current.created_at),
                    "updated_by": actor_id,
                }
            )
            _copy_to_row(updated, row)
            db.add(_revision_row(current, updated, actor_id, now))
        self.reload()
        return updated

    def import_faqs(
        self, faqs: Sequence[FAQ], *, now: datetime | None = None, dry_run: bool = False
    ) -> FAQImportReport:
        """Add the FAQs that are not stored yet, in one transaction.

        Existing IDs are never overwritten: identical content is ``unchanged``; different
        content (for example edited by staff since) keeps the database version and is
        reported. Running it again therefore changes nothing. ``faqs`` must already be
        validated, with unique IDs (``FAQRepository.from_json_file`` does both).

        With ``dry_run``, the report is computed and the transaction rolled back.

        Raises:
            ValueError: ``faqs`` repeats an ID. FAQStoreError: nothing was written.
        """
        ids = [faq.id for faq in faqs]
        if len(set(ids)) != len(ids):
            raise ValueError("the FAQs to import repeat an ID")
        now = now or datetime.now(UTC)
        created: list[str] = []
        unchanged: list[str] = []
        kept: list[str] = []
        try:
            self._import(faqs, now, created, unchanged, kept, dry_run=dry_run)
        except _DryRunFinished:
            pass
        if created and not dry_run:
            self.reload()
        return FAQImportReport(
            created=tuple(created), unchanged=tuple(unchanged), kept_database_version=tuple(kept)
        )

    def _import(
        self,
        faqs: Sequence[FAQ],
        now: datetime,
        created: list[str],
        unchanged: list[str],
        kept: list[str],
        *,
        dry_run: bool,
    ) -> None:
        with self._write("import") as db:
            for faq in faqs:
                row = db.get(FAQRow, faq.id)
                if row is None:
                    record = ManagedFAQ(
                        **faq.model_dump(),
                        version=1,
                        created_at=now,
                        updated_at=now,
                        created_by=None,
                        updated_by=None,
                    )
                    db.add(_to_row(record))
                    db.flush()
                    db.add(_revision_row(None, record, None, now))
                    created.append(faq.id)
                elif _to_record(row).content_equals(faq):
                    unchanged.append(faq.id)
                else:
                    kept.append(faq.id)
            if dry_run:
                db.flush()  # constraint violations surface in a dry run too
                raise _DryRunFinished()  # leaving the block with it rolls back

    def list_revisions(self, faq_id: str, *, offset: int = 0, limit: int = 20) -> FAQRevisionPage:
        """One FAQ's history, newest first.

        FAQs imported before Stage 11's history existed start their history at their
        first later change.

        Raises:
            FAQNotFoundError, FAQStoreError, ValueError (invalid page).
        """
        _check_page(offset, limit)
        canonical = normalise_faq_id(faq_id)
        if canonical is None:
            raise FAQNotFoundError()
        with self._read("list_revisions") as db:
            if db.get(FAQRow, canonical) is None:
                raise FAQNotFoundError()
            where = FAQRevisionRow.faq_id == canonical
            total = db.scalar(select(func.count()).select_from(FAQRevisionRow).where(where)) or 0
            rows = db.scalars(
                select(FAQRevisionRow)
                .where(where)
                .order_by(FAQRevisionRow.version.desc())
                .offset(offset)
                .limit(limit)
            ).all()
            revisions = [_to_revision(row) for row in rows]
        return FAQRevisionPage(
            faq_id=canonical,
            revisions=revisions,
            total=total,
            offset=offset,
            limit=limit,
            has_more=offset + len(revisions) < total,
        )

    def summary(self) -> FAQCatalogSummary:
        """Counts per category and status, and when anything last changed."""
        with self._read("summary") as db:
            rows = db.execute(
                select(FAQRow.category, FAQRow.is_active, func.count()).group_by(
                    FAQRow.category, FAQRow.is_active
                )
            ).all()
            last = db.scalar(select(func.max(FAQRow.updated_at)))
        counts: dict[FAQCategory, list[int]] = {}
        for category, is_active, number in rows:
            pair = counts.setdefault(FAQCategory(category), [0, 0])
            pair[0 if is_active else 1] += number
        active = sum(pair[0] for pair in counts.values())
        inactive = sum(pair[1] for pair in counts.values())
        return FAQCatalogSummary(
            total=active + inactive,
            active=active,
            inactive=inactive,
            by_category=tuple(
                CategoryCount(category=category, active=pair[0], inactive=pair[1])
                for category, pair in sorted(counts.items())
            ),
            last_updated_at=last.replace(tzinfo=UTC) if last is not None else None,
        )

    @contextmanager
    def _read(self, operation: str) -> Iterator[Session]:
        with _store_errors(operation), self._db.read() as db:
            yield db

    @contextmanager
    def _write(self, operation: str) -> Iterator[Session]:
        with _store_errors(operation), self._db.write() as db:
            yield db


def _refuse_duplicate_question(db: Session, question: str, own_id: str | None) -> None:
    """Raise if another FAQ has the same question, ignoring case: the usual result of a
    form submitted twice, or of re-adding an FAQ that was deactivated."""
    wanted = question.casefold()
    for faq_id, stored in db.execute(select(FAQRow.id, FAQRow.question)):
        if faq_id != own_id and stored.casefold() == wanted:
            raise DuplicateFAQQuestionError(faq_id)


def _next_id(db: Session) -> str:
    """One more than the highest ID number in use (never a gap, never reused)."""
    ids: Iterable[str] = db.scalars(select(FAQRow.id)).all()
    highest = max((int(faq_id[4:]) for faq_id in ids), default=0)
    if highest >= MAX_ID_NUMBER:
        raise FAQIdsExhaustedError()
    return f"faq_{highest + 1:03d}"


def _like(text: str) -> str:
    escaped = text.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2).replace("%", r"\%").replace("_", r"\_")
    return f"%{escaped}%"


def _filtered(query: Select[tuple[FAQRow]], f: FAQFilter) -> Select[tuple[FAQRow]]:
    if f.category is not None:
        query = query.where(FAQRow.category == f.category.value)
    if f.is_active is not None:
        query = query.where(FAQRow.is_active.is_(f.is_active))
    if f.search is not None:
        pattern = _like(f.search)
        query = query.where(
            or_(
                FAQRow.id == f.search.lower(),
                FAQRow.question.like(pattern, escape=_LIKE_ESCAPE),
                FAQRow.answer.like(pattern, escape=_LIKE_ESCAPE),
                cast(FAQRow.keywords, String).like(pattern, escape=_LIKE_ESCAPE),
            )
        )
    return query


@contextmanager
def _store_errors(operation: str) -> Iterator[None]:
    """Database failures and invalid stored rows become ``FAQStoreError``; exception
    text (which can hold FAQ content) is never logged."""
    try:
        yield
    except IntegrityError as exc:
        logger.error("FAQ store %s refused a write (%s)", operation, type(exc).__name__)
        raise FAQStoreError(f"FAQ storage refused the change ({operation})") from exc
    except SQLAlchemyError as exc:
        logger.error("FAQ store %s failed (%s)", operation, type(exc).__name__)
        if isinstance(exc, OperationalError) and "locked" in str(exc.orig).lower():
            raise FAQStoreBusyError(f"FAQ storage is busy ({operation})") from exc
        raise FAQStoreError(f"FAQ storage is unavailable ({operation})") from exc
    except ValidationError as exc:
        logger.error("Invalid FAQ data in %s (%d errors)", operation, exc.error_count())
        raise FAQStoreError("stored FAQ data is invalid") from None


# --- Conversions (rows are re-validated on the way out) -------------------------------


def _to_record(row: FAQRow) -> ManagedFAQ:
    return ManagedFAQ(
        id=row.id,
        question=row.question,
        answer=row.answer,
        category=row.category,
        keywords=row.keywords,
        is_active=bool(row.is_active),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
        updated_by=row.updated_by,
    )


def _content(record: ManagedFAQ) -> dict[str, object]:
    """The record's content fields as JSON-ready values (the category as its string)."""
    data = record.model_dump(mode="json", include=set(FAQ_CONTENT_FIELDS))
    return {name: data[name] for name in FAQ_CONTENT_FIELDS}


def _revision_row(
    before: ManagedFAQ | None, after: ManagedFAQ, actor_id: str | None, now: datetime
) -> FAQRevisionRow:
    """The history row for a change: only the fields that differ (all for a creation)."""
    new = _content(after)
    if before is None:
        action, fields, previous = FAQRevisionAction.CREATED, list(new), None
    else:
        old = _content(before)
        fields = [name for name in FAQ_CONTENT_FIELDS if old[name] != new[name]]
        previous = {name: old[name] for name in fields}
        if fields == ["is_active"]:
            action = (
                FAQRevisionAction.ACTIVATED if after.is_active else FAQRevisionAction.DEACTIVATED
            )
        else:
            action = FAQRevisionAction.UPDATED
    return FAQRevisionRow(
        revision_id=uuid.uuid4(),
        faq_id=after.id,
        version=after.version,
        action=action.value,
        changed_fields=fields,
        previous_values=previous,
        new_values={name: new[name] for name in fields},
        changed_by=actor_id,
        changed_at=max(now, after.created_at),
    )


def _to_revision(row: FAQRevisionRow) -> FAQRevision:
    return FAQRevision(
        revision_id=row.revision_id,
        faq_id=row.faq_id,
        version=row.version,
        action=row.action,
        changed_fields=row.changed_fields,
        previous_values=row.previous_values,
        new_values=row.new_values,
        changed_by=row.changed_by,
        changed_at=row.changed_at,
    )


def _to_row(record: ManagedFAQ) -> FAQRow:
    row = FAQRow(id=record.id)
    _copy_to_row(record, row)
    return row


def _copy_to_row(record: ManagedFAQ, row: FAQRow) -> None:
    row.question = record.question
    row.answer = record.answer
    row.category = record.category.value
    row.keywords = list(record.keywords)
    row.is_active = record.is_active
    row.version = record.version
    row.created_at = record.created_at
    row.updated_at = record.updated_at
    row.created_by = record.created_by
    row.updated_by = record.updated_by
