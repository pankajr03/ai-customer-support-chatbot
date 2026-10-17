"""FAQ business logic, independent of HTTP and of how FAQs are stored or searched.

The service enforces the public rules (only active FAQs are ever returned) and
delegates storage to an ``FAQSource`` and matching to a ``FAQSearcher``. Either can be
replaced without changing this class: the source is the database-backed
``DatabaseFAQRepository`` since Stage 11 (the JSON ``FAQRepository`` before, and still in
tests), the searcher could be a semantic one.
"""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from app.models.faq import FAQ, FAQCategory, parse_category
from app.services.faq_catalog import CatalogPin, CatalogSnapshot
from app.services.faq_search import FAQMatch, FAQSearcher, KeywordSearcher

DEFAULT_SEARCH_LIMIT = 5
MAX_SEARCH_LIMIT = 50
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class FAQSource(Protocol):
    """Read access to a validated FAQ catalogue (JSON or database repository)."""

    def __len__(self) -> int: ...

    def list_all(self) -> list[FAQ]: ...

    def list_active(self) -> list[FAQ]: ...

    def get_by_id(self, faq_id: str) -> FAQ | None: ...

    def list_by_category(
        self, category: FAQCategory | str, *, active_only: bool = False
    ) -> list[FAQ]: ...


@dataclass(frozen=True, slots=True)
class FAQPage:
    """One page of FAQs plus the information needed to request other pages."""

    items: list[FAQ]
    total: int
    offset: int
    limit: int


class FAQService:
    """Public, read-only FAQ operations. Inactive FAQs are never exposed."""

    def __init__(self, repository: FAQSource, searcher: FAQSearcher | None = None) -> None:
        self._repository = repository
        self._searcher: FAQSearcher = searcher or KeywordSearcher()
        self._pin = CatalogPin(f"faq_catalog_pin_{id(self)}")
        # A repository without snapshots (the read-only JSON one) never changes.
        self._static: CatalogSnapshot | None = None

    @property
    def repository(self) -> FAQSource:
        """Where the FAQs come from (for wiring FAQ management to the same catalogue)."""
        return self._repository

    # --- The catalogue (app.services.faq_catalog.FAQCatalog) --------------------------

    def latest(self) -> CatalogSnapshot:
        """The latest committed catalogue, ignoring any pin."""
        snapshot = getattr(self._repository, "snapshot", None)
        if snapshot is not None:
            return snapshot()  # type: ignore[no-any-return]
        if self._static is None:
            self._static = CatalogSnapshot.of(self._repository.list_all())
        return self._static

    def snapshot(self) -> CatalogSnapshot:
        """The snapshot pinned by the current retrieval operation, else the latest."""
        return self._pin.get() or self.latest()

    def pinned(self, snapshot: CatalogSnapshot) -> AbstractContextManager[CatalogSnapshot]:
        """Every read below returns ``snapshot`` for the duration of the block (this
        thread or task only)."""
        return self._pin.pinned(snapshot)

    def catalog_revision(self) -> int:
        return self.snapshot().revision

    def is_current(self, faq: FAQ) -> bool:
        """Whether ``faq`` is active in the latest catalogue with exactly this content."""
        return self.latest().is_current(faq)

    # --- Reads (from the pinned or latest snapshot) -----------------------------------

    def list_active_faqs(self) -> list[FAQ]:
        """All active FAQs, in knowledge-base order."""
        return list(self.snapshot().active)

    def get_active_faq(self, faq_id: str) -> FAQ | None:
        """The FAQ with this ID, or None if it does not exist or is inactive."""
        faq = self.snapshot().get(faq_id)
        return faq if faq is not None and faq.is_active else None

    def list_categories(self) -> list[FAQCategory]:
        """Categories that currently have at least one active FAQ, sorted alphabetically."""
        return sorted({faq.category for faq in self.snapshot().active})

    def list_faqs_by_category(self, category: FAQCategory | str) -> list[FAQ]:
        """Active FAQs in one category.

        Raises:
            ValueError: ``category`` is not a known FAQ category.
        """
        wanted = parse_category(category)
        return [faq for faq in self.snapshot().active if faq.category is wanted]

    def list_faqs_page(
        self,
        category: FAQCategory | str | None = None,
        *,
        offset: int = 0,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> FAQPage:
        """One page of active FAQs (optionally in one category), in knowledge-base order.

        An ``offset`` past the end returns an empty page, not an error.

        Raises:
            ValueError: ``offset`` is negative, ``limit`` is outside 1..MAX_PAGE_SIZE,
                or ``category`` is unknown.
        """
        if offset < 0:
            raise ValueError(f"offset must not be negative, got {offset}")
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}, got {limit}")
        faqs = self.list_active_faqs() if category is None else self.list_faqs_by_category(category)
        return FAQPage(
            items=faqs[offset : offset + limit], total=len(faqs), offset=offset, limit=limit
        )

    def search(self, query: str, *, limit: int = DEFAULT_SEARCH_LIMIT) -> list[FAQMatch]:
        """Search active FAQs, best match first. A blank query returns no results.

        Raises:
            ValueError: ``limit`` is outside 1..MAX_SEARCH_LIMIT.
        """
        if not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_SEARCH_LIMIT}, got {limit}")
        if not query.strip():
            return []
        return self._searcher.search(query, self.list_active_faqs(), limit)
