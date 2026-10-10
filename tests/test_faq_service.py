"""FAQService: active-only rules, categories, search behaviour and baseline quality.

Rule tests use the fixed test dataset (``faq_service`` fixture, loaded from a
temporary file). The baseline-relevance section at the end deliberately uses the
bundled sample knowledge base and is marked ``sample_data``.
"""

from collections.abc import Sequence

import pytest

from app.database.faq_repository import FAQRepository
from app.models.faq import FAQ, FAQCategory
from app.services.faq_search import FAQMatch
from app.services.faq_service import (
    DEFAULT_SEARCH_LIMIT,
    MAX_PAGE_SIZE,
    MAX_SEARCH_LIMIT,
    FAQService,
)
from tests.faq_test_data import (
    ACTIVE_IDS,
    ACTIVE_SHIPPING_IDS,
    AVAILABLE_CATEGORIES,
    INACTIVE_IDS,
    TEST_FAQ_RECORDS,
)

INACTIVE_QUESTION = next(r["question"] for r in TEST_FAQ_RECORDS if r["id"] == "faq_105")


def ids(matches: list[FAQMatch]) -> list[str]:
    return [match.faq.id for match in matches]


class RecordingSearcher:
    """Fake searcher that records its inputs and returns a fixed answer."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[FAQ], int]] = []

    def search(self, query: str, faqs: Sequence[FAQ], limit: int) -> list[FAQMatch]:
        self.calls.append((query, list(faqs), limit))
        return [FAQMatch(faq=faqs[0], score=1, exact_question=False, reasons=("fake",))]


# --- Retrieving active FAQs -----------------------------------------------------------


def test_retrieve_active_faqs(faq_service: FAQService) -> None:
    faqs = faq_service.list_active_faqs()

    assert [faq.id for faq in faqs] == ACTIVE_IDS
    assert all(faq.is_active for faq in faqs)


def test_inactive_faqs_are_excluded_everywhere(faq_service: FAQService) -> None:
    listed = {faq.id for faq in faq_service.list_active_faqs()}
    by_category = {faq.id for faq in faq_service.list_faqs_by_category("shipping")}

    for inactive_id in INACTIVE_IDS:
        assert inactive_id not in listed
        assert inactive_id not in by_category
        assert faq_service.get_active_faq(inactive_id) is None


@pytest.mark.parametrize("faq_id", ["faq_101", "FAQ_101", " faq_101 "])
def test_get_active_faq(faq_service: FAQService, faq_id: str) -> None:
    faq = faq_service.get_active_faq(faq_id)

    assert faq is not None
    assert faq.id == "faq_101"


def test_get_active_faq_hides_inactive_but_existing(
    faq_service: FAQService, faq_repository: FAQRepository
) -> None:
    assert faq_repository.get_by_id("faq_105") is not None  # it exists...
    assert faq_service.get_active_faq("faq_105") is None  # ...but is not public


def test_get_active_faq_missing(faq_service: FAQService) -> None:
    assert faq_service.get_active_faq("faq_999") is None


# --- Categories --------------------------------------------------------------------


def test_list_categories_only_includes_categories_with_active_faqs(
    faq_service: FAQService,
) -> None:
    categories = faq_service.list_categories()

    assert [category.value for category in categories] == AVAILABLE_CATEGORIES
    assert FAQCategory.PRODUCTS not in categories  # only has an inactive FAQ


@pytest.mark.parametrize("category", [FAQCategory.SHIPPING, "shipping", "SHIPPING "])
def test_filter_by_category_returns_active_only(
    faq_service: FAQService, category: FAQCategory | str
) -> None:
    assert [faq.id for faq in faq_service.list_faqs_by_category(category)] == ACTIVE_SHIPPING_IDS


def test_filter_by_unused_category_is_empty(faq_service: FAQService) -> None:
    assert faq_service.list_faqs_by_category("technical") == []


def test_filter_by_unknown_category_raises(faq_service: FAQService) -> None:
    with pytest.raises(ValueError, match="Unknown FAQ category"):
        faq_service.list_faqs_by_category("billing")


# --- Search behaviour ----------------------------------------------------------------


def test_search_matches_exact_question(faq_service: FAQService) -> None:
    [top, *_] = faq_service.search("How do I reset my password?")

    assert top.faq.id == "faq_107"
    assert top.exact_question is True


def test_search_matches_keywords(faq_service: FAQService) -> None:
    [top, *_] = faq_service.search("where is my order")

    assert top.faq.id == "faq_101"
    assert any("where is my order" in reason for reason in top.reasons)


@pytest.mark.parametrize(
    "query", ["HOW DO I RESET MY PASSWORD?", "how do i reset my password", "How Do I Reset"]
)
def test_search_is_case_insensitive(faq_service: FAQService, query: str) -> None:
    assert faq_service.search(query)[0].faq.id == "faq_107"


@pytest.mark.parametrize(
    "query", ["   reset    password   ", "\treset\npassword", "  How  do I   reset my password? "]
)
def test_search_ignores_extra_whitespace(faq_service: FAQService, query: str) -> None:
    assert faq_service.search(query)[0].faq.id == "faq_107"


def test_search_whitespace_variants_give_identical_results(faq_service: FAQService) -> None:
    assert ids(faq_service.search("track my order")) == ids(
        faq_service.search("  TRACK   my\torder  ")
    )


@pytest.mark.parametrize("query", ["xyzzy", "quantum banana smoothie", "the", "how do I"])
def test_search_returns_nothing_for_unrelated_queries(faq_service: FAQService, query: str) -> None:
    assert faq_service.search(query) == []


def test_search_ordering_is_deterministic(faq_service: FAQService) -> None:
    runs = {tuple(ids(faq_service.search("order", limit=MAX_SEARCH_LIMIT))) for _ in range(25)}

    assert len(runs) == 1


def test_search_orders_by_score_then_knowledge_base_order(faq_service: FAQService) -> None:
    results = faq_service.search("order", limit=MAX_SEARCH_LIMIT)

    keys = [(not m.exact_question, -m.score, ACTIVE_IDS.index(m.faq.id)) for m in results]
    assert keys == sorted(keys)


def test_search_never_returns_inactive_faq_even_on_exact_question(
    faq_service: FAQService,
) -> None:
    results = faq_service.search(INACTIVE_QUESTION, limit=MAX_SEARCH_LIMIT)

    assert set(ids(results)).isdisjoint(INACTIVE_IDS)


@pytest.mark.parametrize("query", ["", "   ", "\n\t"])
def test_blank_query_returns_nothing_without_searching(
    faq_repository: FAQRepository, query: str
) -> None:
    searcher = RecordingSearcher()

    assert FAQService(faq_repository, searcher).search(query) == []
    assert searcher.calls == []


@pytest.mark.parametrize("limit", [0, -1, MAX_SEARCH_LIMIT + 1])
def test_invalid_limit_raises(faq_service: FAQService, limit: int) -> None:
    with pytest.raises(ValueError, match="limit must be between"):
        faq_service.search("order", limit=limit)


def test_limit_caps_results(faq_service: FAQService) -> None:
    assert len(faq_service.search("order", limit=1)) == 1
    assert DEFAULT_SEARCH_LIMIT == 5


def test_searcher_is_replaceable_and_only_sees_active_faqs(
    faq_repository: FAQRepository,
) -> None:
    searcher = RecordingSearcher()

    results = FAQService(faq_repository, searcher).search("anything", limit=3)

    [(query, faqs_seen, limit)] = searcher.calls
    assert (query, limit) == ("anything", 3)
    assert [faq.id for faq in faqs_seen] == ACTIVE_IDS
    assert results[0].reasons == ("fake",)


# --- Baseline relevance on the bundled sample knowledge base -------------------------
# Expected top results for realistic queries against data/faqs.json. Stage 3 semantic
# search can be compared against these tables. Marked sample_data: deselect with
# `pytest -m "not sample_data"` after replacing data/faqs.json with client content.


@pytest.fixture(scope="module")
def sample_service() -> FAQService:
    return FAQService(FAQRepository.from_json_file())


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


@pytest.mark.sample_data
@pytest.mark.parametrize(("query", "expected_id"), BASELINE_TOP_RESULT)
def test_baseline_top_result(sample_service: FAQService, query: str, expected_id: str) -> None:
    results = sample_service.search(query)

    assert results, f"no results for {query!r}"
    assert results[0].faq.id == expected_id, [(m.faq.id, m.score) for m in results]


@pytest.mark.sample_data
@pytest.mark.parametrize(("query", "expected_id", "reason"), KNOWN_MISSES)
def test_known_misses(
    sample_service: FAQService, query: str, expected_id: str, reason: str
) -> None:
    """Pin current weaknesses so Stage 3 improvements are measurable.

    If one of these starts passing, update the table: the baseline has changed.
    """
    results = sample_service.search(query)

    assert not results or results[0].faq.id != expected_id, reason


# --- Pagination --------------------------------------------------------------------


def test_page_slices_active_faqs(faq_service: FAQService) -> None:
    page = faq_service.list_faqs_page(offset=1, limit=2)

    assert [faq.id for faq in page.items] == ACTIVE_IDS[1:3]
    assert (page.total, page.offset, page.limit) == (len(ACTIVE_IDS), 1, 2)


def test_page_by_category_counts_only_that_category(faq_service: FAQService) -> None:
    page = faq_service.list_faqs_page("shipping", limit=1)

    assert page.total == len(ACTIVE_SHIPPING_IDS)
    assert [faq.id for faq in page.items] == ACTIVE_SHIPPING_IDS[:1]


def test_page_past_the_end_is_empty(faq_service: FAQService) -> None:
    assert faq_service.list_faqs_page(offset=999).items == []


@pytest.mark.parametrize(
    ("offset", "limit", "message"),
    [(-1, 10, "offset"), (0, 0, "limit"), (0, MAX_PAGE_SIZE + 1, "limit")],
)
def test_invalid_page_arguments_raise(
    faq_service: FAQService, offset: int, limit: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        faq_service.list_faqs_page(offset=offset, limit=limit)
