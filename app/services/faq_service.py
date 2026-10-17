"""FAQ business logic, independent of HTTP and of how FAQs are stored or searched.

The service enforces the public rules (only active FAQs are ever returned) and
delegates storage to an ``FAQSource`` and matching to a ``FAQSearcher``. Either can be
replaced without changing this class: the source is the database-backed
``DatabaseFAQRepository`` since Stage 11 (the JSON ``FAQRepository`` before, and still in
tests), the searcher could be a semantic one.
"""

from dataclasses import dataclass
from typing import Protocol

from app.models.faq import FAQ, FAQCategory
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

    @property
    def repository(self) -> FAQSource:
        """Where the FAQs come from (for wiring FAQ management to the same catalogue)."""
        return self._repository

    def list_active_faqs(self) -> list[FAQ]:
        """All active FAQs, in knowledge-base order."""
        return self._repository.list_active()

    def get_active_faq(self, faq_id: str) -> FAQ | None:
        """The FAQ with this ID, or None if it does not exist or is inactive."""
        faq = self._repository.get_by_id(faq_id)
        return faq if faq is not None and faq.is_active else None

    def list_categories(self) -> list[FAQCategory]:
        """Categories that currently have at least one active FAQ, sorted alphabetically."""
        return sorted({faq.category for faq in self._repository.list_active()})

    def list_faqs_by_category(self, category: FAQCategory | str) -> list[FAQ]:
        """Active FAQs in one category.

        Raises:
            ValueError: ``category`` is not a known FAQ category.
        """
        return self._repository.list_by_category(category, active_only=True)

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
        return self._searcher.search(query, self._repository.list_active(), limit)
