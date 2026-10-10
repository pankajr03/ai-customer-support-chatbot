"""HTTP behaviour of the /api/v1/faqs endpoints.

Every test runs against the fixed test dataset in tests/faq_test_data.py, written to
a temporary file and loaded through FAQ_DATA_PATH (the ``faq_client`` fixture), so
the results do not depend on the production data/faqs.json.
"""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api.dependencies import get_faq_service
from app.api.routes.faqs import MAX_QUERY_LENGTH
from app.api.routes.faqs import router as faqs_router
from app.core.config import Settings
from app.database.faq_repository import FAQFileNotFoundError, FAQRepository, InvalidFAQRecordError
from app.main import create_app
from app.models.faq import FAQ
from app.services.faq_service import DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT, FAQService
from tests.faq_test_data import (
    ACTIVE_IDS,
    ACTIVE_SHIPPING_IDS,
    AVAILABLE_CATEGORIES,
    INACTIVE_IDS,
    PUBLIC_FIELDS,
    TEST_FAQ_RECORDS,
)

FAQS_URL = "/api/v1/faqs"
SEARCH_URL = f"{FAQS_URL}/search"


# --- GET /api/v1/faqs ---------------------------------------------------------------


def test_list_faqs_returns_active_faqs_in_order(faq_client: TestClient) -> None:
    response = faq_client.get(FAQS_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == len(body["faqs"]) == len(ACTIVE_IDS)
    assert [faq["id"] for faq in body["faqs"]] == ACTIVE_IDS


def test_list_faqs_excludes_inactive(faq_client: TestClient) -> None:
    ids = {faq["id"] for faq in faq_client.get(FAQS_URL).json()["faqs"]}

    assert ids.isdisjoint(INACTIVE_IDS)


def test_list_faqs_exposes_only_public_fields(faq_client: TestClient) -> None:
    faqs = faq_client.get(FAQS_URL).json()["faqs"]

    assert all(set(faq) == PUBLIC_FIELDS for faq in faqs)


@pytest.mark.parametrize("category", ["shipping", "Shipping", " SHIPPING "])
def test_filter_by_category(faq_client: TestClient, category: str) -> None:
    body = faq_client.get(FAQS_URL, params={"category": category}).json()

    assert [faq["id"] for faq in body["faqs"]] == ACTIVE_SHIPPING_IDS
    assert body["count"] == len(ACTIVE_SHIPPING_IDS)


@pytest.mark.parametrize("category", ["technical", "products"])
def test_filter_by_category_without_active_faqs_is_empty(
    faq_client: TestClient, category: str
) -> None:
    response = faq_client.get(FAQS_URL, params={"category": category})

    assert response.status_code == 200
    assert response.json() == {"count": 0, "faqs": []}


@pytest.mark.parametrize("category", ["billing", "accounts", ""])
def test_filter_by_invalid_category_is_422(faq_client: TestClient, category: str) -> None:
    response = faq_client.get(FAQS_URL, params={"category": category})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "category"]


# --- GET /api/v1/faqs/categories ----------------------------------------------------


def test_list_categories(faq_client: TestClient) -> None:
    response = faq_client.get(f"{FAQS_URL}/categories")

    assert response.status_code == 200
    assert response.json() == {"categories": AVAILABLE_CATEGORIES}


# --- GET /api/v1/faqs/search --------------------------------------------------------


def test_search_returns_scored_results(faq_client: TestClient) -> None:
    response = faq_client.get(SEARCH_URL, params={"q": "track order"})

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "track order"
    assert body["count"] == len(body["results"]) >= 1
    top = body["results"][0]
    assert top["id"] == "faq_101"
    assert set(top) == PUBLIC_FIELDS | {"score", "reasons"}
    assert top["score"] > 0
    assert top["reasons"]


def test_search_with_plus_encoded_spaces(faq_client: TestClient) -> None:
    body = faq_client.get(f"{SEARCH_URL}?q=track+order").json()

    assert body["query"] == "track order"
    assert body["results"][0]["id"] == "faq_101"


def test_search_exact_question_ranks_first(faq_client: TestClient) -> None:
    top = faq_client.get(SEARCH_URL, params={"q": "how do I reset my password?"}).json()["results"][
        0
    ]

    assert top["id"] == "faq_107"
    assert "exact question match (+100)" in top["reasons"]


def test_search_results_are_ordered_by_score(faq_client: TestClient) -> None:
    results = faq_client.get(SEARCH_URL, params={"q": "order"}).json()["results"]

    scores = [result["score"] for result in results]
    assert len(scores) >= 2
    assert scores == sorted(scores, reverse=True)


@pytest.mark.parametrize("query", ["xyzzy", "how do I", "the"])
def test_search_with_no_matches_returns_empty_results(faq_client: TestClient, query: str) -> None:
    response = faq_client.get(SEARCH_URL, params={"q": query})

    assert response.status_code == 200
    assert response.json() == {"query": query, "count": 0, "results": []}


def test_search_never_returns_inactive_faqs(faq_client: TestClient) -> None:
    params = {"q": "Is there free shipping during the holiday sale?", "limit": MAX_SEARCH_LIMIT}

    results = faq_client.get(SEARCH_URL, params=params).json()["results"]

    assert {result["id"] for result in results}.isdisjoint(INACTIVE_IDS)


@pytest.mark.parametrize(
    "params",
    [{}, {"q": ""}, {"q": "   "}, {"q": "\t\n"}, {"q": "x" * (MAX_QUERY_LENGTH + 1)}],
    ids=["missing", "empty", "spaces", "whitespace", "too-long"],
)
def test_search_with_empty_or_invalid_query_is_422(faq_client: TestClient, params: dict) -> None:
    response = faq_client.get(SEARCH_URL, params=params)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "q"]


def test_search_query_is_trimmed(faq_client: TestClient) -> None:
    assert faq_client.get(SEARCH_URL, params={"q": "  track  "}).json()["query"] == "track"


def test_search_default_and_custom_limit(make_faq_client: Callable[..., TestClient]) -> None:
    many = [
        FAQ(
            id=f"faq_{n:03d}",
            question=f"Widget question number {n}?",
            answer="An answer about widgets.",
            category="products",
            keywords=["widget"],
        )
        for n in range(1, DEFAULT_SEARCH_LIMIT + 4)
    ]
    client = make_faq_client(many)

    default = client.get(SEARCH_URL, params={"q": "widget"}).json()
    custom = client.get(SEARCH_URL, params={"q": "widget", "limit": 2}).json()

    assert default["count"] == DEFAULT_SEARCH_LIMIT
    assert custom["count"] == 2


@pytest.mark.parametrize("limit", [0, -1, MAX_SEARCH_LIMIT + 1, "ten"])
def test_search_with_invalid_limit_is_422(faq_client: TestClient, limit: object) -> None:
    response = faq_client.get(SEARCH_URL, params={"q": "order", "limit": limit})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "limit"]


# --- GET /api/v1/faqs/{faq_id} ------------------------------------------------------


@pytest.mark.parametrize("faq_id", ["faq_101", "FAQ_101"])
def test_get_faq_by_id(faq_client: TestClient, faq_id: str) -> None:
    response = faq_client.get(f"{FAQS_URL}/{faq_id}")

    assert response.status_code == 200
    expected = {key: TEST_FAQ_RECORDS[0][key] for key in PUBLIC_FIELDS}
    assert response.json() == expected


@pytest.mark.parametrize("faq_id", ["faq_999", "not-an-id", "x" * 64, *INACTIVE_IDS])
def test_unknown_or_inactive_faq_is_404(faq_client: TestClient, faq_id: str) -> None:
    response = faq_client.get(f"{FAQS_URL}/{faq_id}")

    assert response.status_code == 404
    assert response.json() == {"detail": "FAQ not found"}


def test_get_faq_with_overlong_id_is_422(faq_client: TestClient) -> None:
    assert faq_client.get(f"{FAQS_URL}/{'x' * 65}").status_code == 422


# --- Routing ------------------------------------------------------------------------


def test_categories_route_is_not_intercepted_by_faq_id_route(faq_client: TestClient) -> None:
    response = faq_client.get(f"{FAQS_URL}/categories")

    assert response.status_code == 200
    assert response.json() == {"categories": AVAILABLE_CATEGORIES}


def test_search_route_is_not_intercepted_by_faq_id_route(faq_client: TestClient) -> None:
    # Without a query, /search must fail validation (422), not return "FAQ not found" (404).
    assert faq_client.get(SEARCH_URL).status_code == 422
    assert faq_client.get(SEARCH_URL, params={"q": "track"}).json()["query"] == "track"


def test_static_routes_are_declared_before_dynamic_route() -> None:
    # FastAPI matches routes in declaration order, so this order is what prevents interception.
    paths = [route.path for route in faqs_router.routes if isinstance(route, APIRoute)]

    dynamic = paths.index("/faqs/{faq_id}")
    assert paths.index("/faqs/categories") < dynamic
    assert paths.index("/faqs/search") < dynamic


@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_faqs_are_read_only(faq_client: TestClient, method: str) -> None:
    assert faq_client.request(method.upper(), FAQS_URL).status_code == 405


def test_openapi_documents_faq_endpoints(faq_client: TestClient) -> None:
    schema = faq_client.get("/openapi.json").json()

    expected_paths = {FAQS_URL, f"{FAQS_URL}/categories", SEARCH_URL, f"{FAQS_URL}/{{faq_id}}"}
    assert expected_paths <= schema["paths"].keys()
    assert "404" in schema["paths"][f"{FAQS_URL}/{{faq_id}}"]["get"]["responses"]
    schemas = schema["components"]["schemas"].keys()
    assert {"FAQResponse", "FAQListResponse", "FAQSearchResponse"} <= schemas


# --- Startup and configuration ------------------------------------------------------


def test_app_serves_the_configured_faq_file(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    faq_file = tmp_path / "client_faqs.json"
    faq_file.write_text(json.dumps(TEST_FAQ_RECORDS[:1]), encoding="utf-8")

    client = TestClient(create_app(make_settings(faq_data_path=faq_file)))

    assert client.get(FAQS_URL).json()["count"] == 1


def test_app_refuses_to_start_without_faq_file(
    make_settings: Callable[..., Settings], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(FAQFileNotFoundError):
        create_app(make_settings(faq_data_path=tmp_path / "missing.json"))

    assert "Cannot start: FAQ knowledge base failed to load" in capsys.readouterr().out


def test_app_refuses_to_start_with_invalid_faq_file(
    make_settings: Callable[..., Settings], faq_records: list[dict], tmp_path: Path
) -> None:
    faq_records[2]["answer"] = " "
    faq_file = tmp_path / "bad.json"
    faq_file.write_text(json.dumps(faq_records), encoding="utf-8")

    with pytest.raises(InvalidFAQRecordError):
        create_app(make_settings(faq_data_path=faq_file))


def test_faq_service_can_be_overridden(app: FastAPI) -> None:
    app.dependency_overrides[get_faq_service] = lambda: FAQService(FAQRepository([]))

    with TestClient(app) as client:
        assert client.get(FAQS_URL).json() == {"count": 0, "faqs": []}
