"""HTTP behaviour of the /api/v1/faqs endpoints."""

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
from app.models.faq import FAQ_EXAMPLE
from app.services.faq_service import DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT, FAQService

FAQS_URL = "/api/v1/faqs"
PUBLIC_FIELDS = {"id", "question", "answer", "category"}
INACTIVE_ID = "faq_012"


# --- GET /api/v1/faqs ---------------------------------------------------------------


def test_list_faqs_returns_active_faqs(client: TestClient) -> None:
    response = client.get(FAQS_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == len(body["faqs"]) == 29
    assert INACTIVE_ID not in {faq["id"] for faq in body["faqs"]}
    assert body["faqs"][0]["id"] == "faq_001"


def test_list_faqs_exposes_only_public_fields(client: TestClient) -> None:
    faqs = client.get(FAQS_URL).json()["faqs"]

    assert all(set(faq) == PUBLIC_FIELDS for faq in faqs)


@pytest.mark.parametrize("category", ["shipping", "Shipping", " SHIPPING "])
def test_list_faqs_filtered_by_category(client: TestClient, category: str) -> None:
    body = client.get(FAQS_URL, params={"category": category}).json()

    assert body["count"] == 6
    assert {faq["category"] for faq in body["faqs"]} == {"shipping"}


def test_list_faqs_for_category_without_faqs_is_empty(client: TestClient) -> None:
    response = client.get(FAQS_URL, params={"category": "technical"})

    assert response.status_code == 200
    assert response.json() == {"count": 0, "faqs": []}


@pytest.mark.parametrize("category", ["billing", "accounts", ""])
def test_list_faqs_with_invalid_category_is_422(client: TestClient, category: str) -> None:
    response = client.get(FAQS_URL, params={"category": category})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "category"]


# --- GET /api/v1/faqs/categories ----------------------------------------------------


def test_list_categories(client: TestClient) -> None:
    response = client.get(f"{FAQS_URL}/categories")

    assert response.status_code == 200
    assert response.json() == {
        "categories": ["account", "orders", "payments", "refunds", "returns", "shipping"]
    }


# --- GET /api/v1/faqs/search --------------------------------------------------------


def test_search_returns_scored_results(client: TestClient) -> None:
    response = client.get(f"{FAQS_URL}/search", params={"q": "track order"})

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "track order"
    assert body["count"] == len(body["results"]) >= 1
    top = body["results"][0]
    assert top["id"] == "faq_001"
    assert set(top) == PUBLIC_FIELDS | {"score", "reasons"}
    assert top["score"] > 0
    assert top["reasons"]


def test_search_results_are_ordered_by_score(client: TestClient) -> None:
    results = client.get(f"{FAQS_URL}/search", params={"q": "refund"}).json()["results"]

    scores = [result["score"] for result in results]
    assert scores == sorted(scores, reverse=True)


def test_search_with_plus_encoded_spaces(client: TestClient) -> None:
    body = client.get(f"{FAQS_URL}/search?q=track+order").json()

    assert body["query"] == "track order"
    assert body["results"][0]["id"] == "faq_001"


def test_search_exact_question_ranks_first(client: TestClient) -> None:
    body = client.get(f"{FAQS_URL}/search", params={"q": "how do I reset my password?"}).json()

    assert body["results"][0]["id"] == "faq_027"
    assert "exact question match (+100)" in body["results"][0]["reasons"]


@pytest.mark.parametrize("query", ["xyzzy", "how do I", "the"])
def test_search_with_no_matches_returns_empty_results(client: TestClient, query: str) -> None:
    response = client.get(f"{FAQS_URL}/search", params={"q": query})

    assert response.status_code == 200
    assert response.json() == {"query": query, "count": 0, "results": []}


def test_search_never_returns_inactive_faqs(client: TestClient) -> None:
    query = "Is there free shipping during the holiday sale?"
    params = {"q": query, "limit": MAX_SEARCH_LIMIT}

    results = client.get(f"{FAQS_URL}/search", params=params).json()["results"]

    assert INACTIVE_ID not in {result["id"] for result in results}


@pytest.mark.parametrize(
    "params",
    [{}, {"q": ""}, {"q": "   "}, {"q": "\t\n"}, {"q": "x" * (MAX_QUERY_LENGTH + 1)}],
    ids=["missing", "empty", "spaces", "whitespace", "too-long"],
)
def test_search_with_empty_or_invalid_query_is_422(client: TestClient, params: dict) -> None:
    response = client.get(f"{FAQS_URL}/search", params=params)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "q"]


def test_search_query_is_trimmed(client: TestClient) -> None:
    assert client.get(f"{FAQS_URL}/search", params={"q": "  track  "}).json()["query"] == "track"


def test_search_default_and_custom_limit(client: TestClient) -> None:
    default = client.get(f"{FAQS_URL}/search", params={"q": "order"}).json()
    custom = client.get(f"{FAQS_URL}/search", params={"q": "order", "limit": 2}).json()

    assert default["count"] == DEFAULT_SEARCH_LIMIT
    assert custom["count"] == 2


@pytest.mark.parametrize("limit", [0, -1, MAX_SEARCH_LIMIT + 1, "ten"])
def test_search_with_invalid_limit_is_422(client: TestClient, limit: object) -> None:
    response = client.get(f"{FAQS_URL}/search", params={"q": "order", "limit": limit})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "limit"]


# --- GET /api/v1/faqs/{faq_id} ------------------------------------------------------


@pytest.mark.parametrize("faq_id", ["faq_001", "FAQ_001"])
def test_get_faq(client: TestClient, faq_id: str) -> None:
    response = client.get(f"{FAQS_URL}/{faq_id}")

    assert response.status_code == 200
    assert response.json() == {
        "id": "faq_001",
        "question": "How can I track my order?",
        "answer": "You can track your order from the My Orders section of your account. "
        "Once your order has shipped, you will also receive an email with a tracking link. "
        "Tracking details can take up to 24 hours to appear after dispatch.",
        "category": "orders",
    }


@pytest.mark.parametrize("faq_id", [INACTIVE_ID, "faq_999", "not-an-id", "x" * 64])
def test_get_missing_or_inactive_faq_is_404(client: TestClient, faq_id: str) -> None:
    response = client.get(f"{FAQS_URL}/{faq_id}")

    assert response.status_code == 404
    assert response.json() == {"detail": "FAQ not found"}


def test_get_faq_with_overlong_id_is_422(client: TestClient) -> None:
    assert client.get(f"{FAQS_URL}/{'x' * 65}").status_code == 422


# --- Routing ------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["categories", "search"])
def test_static_routes_are_not_treated_as_faq_ids(client: TestClient, path: str) -> None:
    response = client.get(f"{FAQS_URL}/{path}", params={"q": "track"})

    assert response.status_code == 200
    assert "detail" not in response.json()


def test_static_routes_are_registered_before_dynamic_route() -> None:
    # Inspect the router itself: FastAPI matches routes in this declaration order.
    paths = [route.path for route in faqs_router.routes if isinstance(route, APIRoute)]

    dynamic = paths.index("/faqs/{faq_id}")
    assert paths.index("/faqs/categories") < dynamic
    assert paths.index("/faqs/search") < dynamic


@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_faqs_are_read_only(client: TestClient, method: str) -> None:
    assert client.request(method.upper(), FAQS_URL).status_code == 405


def test_openapi_documents_faq_endpoints(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    assert {
        FAQS_URL,
        f"{FAQS_URL}/categories",
        f"{FAQS_URL}/search",
        f"{FAQS_URL}/{{faq_id}}",
    } <= schema["paths"].keys()
    assert "404" in schema["paths"][f"{FAQS_URL}/{{faq_id}}"]["get"]["responses"]
    assert {"FAQResponse", "FAQListResponse", "FAQSearchResponse"} <= schema["components"][
        "schemas"
    ].keys()


# --- Startup and configuration ------------------------------------------------------


def test_custom_faq_file_is_used(make_settings: Callable[..., Settings], tmp_path: Path) -> None:
    faq_file = tmp_path / "client_faqs.json"
    faq_file.write_text(json.dumps([FAQ_EXAMPLE]), encoding="utf-8")

    client = TestClient(create_app(make_settings(faq_data_path=faq_file)))

    assert client.get(FAQS_URL).json()["count"] == 1


def test_app_refuses_to_start_without_faq_file(
    make_settings: Callable[..., Settings], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(FAQFileNotFoundError):
        create_app(make_settings(faq_data_path=tmp_path / "missing.json"))

    assert "Cannot start: FAQ knowledge base failed to load" in capsys.readouterr().out


def test_app_refuses_to_start_with_invalid_faq_file(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    faq_file = tmp_path / "bad.json"
    faq_file.write_text(json.dumps([{**FAQ_EXAMPLE, "answer": " "}]), encoding="utf-8")

    with pytest.raises(InvalidFAQRecordError):
        create_app(make_settings(faq_data_path=faq_file))


def test_faq_service_can_be_overridden(app: FastAPI) -> None:
    empty_service = FAQService(FAQRepository([]))
    app.dependency_overrides[get_faq_service] = lambda: empty_service

    with TestClient(app) as client:
        assert client.get(FAQS_URL).json() == {"count": 0, "faqs": []}
