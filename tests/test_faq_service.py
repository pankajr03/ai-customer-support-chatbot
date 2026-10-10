"""FAQService: active-only rules, categories, search delegation and baseline quality."""

from collections.abc import Sequence

import pytest

from app.database.faq_repository import FAQRepository
from app.models.faq import FAQ, FAQCategory
from app.services.faq_search import FAQMatch
from app.services.faq_service import DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT, FAQService

INACTIVE_ID = "faq_012"
INACTIVE_QUESTION = "Is there free shipping during the holiday sale?"


@pytest.fixture(scope="module")
def repository() -> FAQRepository:
    return FAQRepository.from_json_file()


@pytest.fixture(scope="module")
def service(repository: FAQRepository) -> FAQService:
    return FAQService(repository)


class RecordingSearcher:
    """Fake searcher that records its inputs and returns a fixed answer."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[FAQ], int]] = []

    def search(self, query: str, faqs: Sequence[FAQ], limit: int) -> list[FAQMatch]:
        self.calls.append((query, list(faqs), limit))
        return [FAQMatch(faq=faqs[0], score=1, exact_question=False, reasons=("fake",))]


# --- Active FAQs and lookups -------------------------------------------------------


def test_list_active_faqs_excludes_inactive(service: FAQService) -> None:
    faqs = service.list_active_faqs()

    assert len(faqs) == 29
    assert all(faq.is_active for faq in faqs)
    assert INACTIVE_ID not in {faq.id for faq in faqs}


@pytest.mark.parametrize("faq_id", ["faq_001", "FAQ_001", " faq_001 "])
def test_get_active_faq(service: FAQService, faq_id: str) -> None:
    faq = service.get_active_faq(faq_id)

    assert faq is not None
    assert faq.id == "faq_001"


def test_get_active_faq_hides_inactive(service: FAQService, repository: FAQRepository) -> None:
    assert repository.get_by_id(INACTIVE_ID) is not None  # it exists...
    assert service.get_active_faq(INACTIVE_ID) is None  # ...but is not public


def test_get_active_faq_missing(service: FAQService) -> None:
    assert service.get_active_faq("faq_999") is None


# --- Categories --------------------------------------------------------------------


def test_list_categories(service: FAQService) -> None:
    assert service.list_categories() == [
        FAQCategory.ACCOUNT,
        FAQCategory.ORDERS,
        FAQCategory.PAYMENTS,
        FAQCategory.REFUNDS,
        FAQCategory.RETURNS,
        FAQCategory.SHIPPING,
    ]


def test_categories_with_only_inactive_faqs_are_not_listed() -> None:
    hidden = FAQ(
        id="faq_100",
        question="Do you sell gadgets?",
        answer="Not at the moment, but we plan to.",
        category="products",
        keywords=["gadgets"],
        is_active=False,
    )
    visible = hidden.model_copy(update={"id": "faq_101", "category": "general", "is_active": True})

    service = FAQService(FAQRepository([hidden, visible]))

    assert service.list_categories() == [FAQCategory.GENERAL]


@pytest.mark.parametrize("category", [FAQCategory.SHIPPING, "shipping", "SHIPPING "])
def test_list_faqs_by_category_returns_active_only(
    service: FAQService, category: FAQCategory | str
) -> None:
    faqs = service.list_faqs_by_category(category)

    assert len(faqs) == 6
    assert all(faq.is_active and faq.category is FAQCategory.SHIPPING for faq in faqs)


def test_list_faqs_by_unused_category_is_empty(service: FAQService) -> None:
    assert service.list_faqs_by_category("technical") == []


def test_list_faqs_by_unknown_category_raises(service: FAQService) -> None:
    with pytest.raises(ValueError, match="Unknown FAQ category"):
        service.list_faqs_by_category("billing")


# --- Search rules ------------------------------------------------------------------


def test_inactive_faq_is_never_returned_even_on_exact_question(service: FAQService) -> None:
    results = service.search(INACTIVE_QUESTION, limit=MAX_SEARCH_LIMIT)

    assert INACTIVE_ID not in {match.faq.id for match in results}


@pytest.mark.parametrize("query", ["", "   ", "\n\t"])
def test_blank_query_returns_nothing_without_searching(query: str) -> None:
    searcher = RecordingSearcher()
    service = FAQService(FAQRepository.from_json_file(), searcher)

    assert service.search(query) == []
    assert searcher.calls == []


@pytest.mark.parametrize("limit", [0, -1, MAX_SEARCH_LIMIT + 1])
def test_invalid_limit_raises(service: FAQService, limit: int) -> None:
    with pytest.raises(ValueError, match="limit must be between"):
        service.search("order", limit=limit)


def test_default_limit(service: FAQService) -> None:
    assert len(service.search("order")) == DEFAULT_SEARCH_LIMIT


def test_searcher_is_replaceable_and_only_sees_active_faqs() -> None:
    searcher = RecordingSearcher()
    service = FAQService(FAQRepository.from_json_file(), searcher)

    results = service.search("anything", limit=3)

    [(query, faqs_seen, limit)] = searcher.calls
    assert (query, limit) == ("anything", 3)
    assert len(faqs_seen) == 29
    assert all(faq.is_active for faq in faqs_seen)
    assert results[0].reasons == ("fake",)


# --- Baseline relevance on the sample knowledge base -------------------------------
# Expected top results for realistic queries. Stage 3 semantic search can be compared
# against this table; the "known misses" document what keyword matching cannot do.

BASELINE_TOP_RESULT = [
    ("How can I track my order?", "faq_001"),
    ("  how CAN i track my order  ", "faq_001"),
    ("where is my order", "faq_001"),
    ("track", "faq_001"),
    ("reset password", "faq_027"),
    ("I forgot my password", "faq_027"),
    ("promo code", "faq_017"),
    ("charged twice", "faq_016"),
    ("how long does a refund take", "faq_023"),
    ("can I return a gift", "faq_022"),
    ("do you ship to other countries", "faq_008"),
    ("delete my account", "faq_030"),
    ("payment declined", "faq_014"),
]

KNOWN_MISSES = [
    # (query, FAQ a human would expect, why keyword search misses it)
    ("orders", "faq_001", "no stemming: 'orders' does not match 'order'"),
    ("when will my stuff arrive", "faq_006", "no synonyms: 'arrive' vs 'delivery'"),
    ("my package is late", "faq_006", "no synonyms: 'package' vs 'parcel'/'delivery'"),
    ("send it back", "faq_019", "extra word breaks the 'send back' keyword phrase"),
]


@pytest.mark.parametrize(("query", "expected_id"), BASELINE_TOP_RESULT)
def test_baseline_top_result(service: FAQService, query: str, expected_id: str) -> None:
    results = service.search(query)

    assert results, f"no results for {query!r}"
    assert results[0].faq.id == expected_id, [(m.faq.id, m.score) for m in results]


@pytest.mark.parametrize(("query", "expected_id", "reason"), KNOWN_MISSES)
def test_known_misses(service: FAQService, query: str, expected_id: str, reason: str) -> None:
    """Pin current weaknesses so Stage 3 improvements are measurable.

    If one of these starts passing, update the table: the baseline has changed.
    """
    results = service.search(query)

    assert not results or results[0].faq.id != expected_id, reason
