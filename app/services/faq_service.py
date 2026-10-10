"""FAQ business logic, independent of HTTP and of how FAQs are stored or searched.

The service enforces the public rules (only active FAQs are ever returned) and
delegates storage to ``FAQRepository`` and matching to a ``FAQSearcher``. Either can
be replaced, e.g. a semantic searcher in Stage 3, without changing this class.
"""

from app.database.faq_repository import FAQRepository
from app.models.faq import FAQ, FAQCategory
from app.services.faq_search import FAQMatch, FAQSearcher, KeywordSearcher

DEFAULT_SEARCH_LIMIT = 5
MAX_SEARCH_LIMIT = 50


class FAQService:
    """Public, read-only FAQ operations. Inactive FAQs are never exposed."""

    def __init__(self, repository: FAQRepository, searcher: FAQSearcher | None = None) -> None:
        self._repository = repository
        self._searcher: FAQSearcher = searcher or KeywordSearcher()

    def list_active_faqs(self) -> list[FAQ]:
        """All active FAQs, in knowledge-base order."""
        return self._repository.list_active()

    def get_active_faq(self, faq_id: str) -> FAQ | None:
        """The FAQ with this ID, or None if it does not exist or is inactive."""
        faq = self._repository.get_by_id(faq_id)
        return faq if faq is not None and faq.is_active else None

    def list_categories(self) -> list[FAQCategory]:
        """Categories that currently have at least one active FAQ, in definition order."""
        in_use = {faq.category for faq in self._repository.list_active()}
        return [category for category in FAQCategory if category in in_use]

    def list_faqs_by_category(self, category: FAQCategory | str) -> list[FAQ]:
        """Active FAQs in one category.

        Raises:
            ValueError: ``category`` is not a known FAQ category.
        """
        return self._repository.list_by_category(category, active_only=True)

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
