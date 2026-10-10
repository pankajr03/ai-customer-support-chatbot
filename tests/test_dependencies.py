"""How the FAQ service is created, shared between requests and replaced in tests."""

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import get_faq_service
from app.core.config import Settings
from app.database.faq_repository import DEFAULT_FAQ_PATH, FAQRepository
from app.main import create_app
from app.models.faq import FAQ
from app.services.faq_service import FAQService
from tests.faq_test_data import ACTIVE_IDS

FAQS_URL = "/api/v1/faqs"
TEST_FAQ = FAQ(
    id="faq_900",
    question="Is this a temporary test FAQ?",
    answer="Yes. It only exists in memory for this test.",
    category="general",
    keywords=["temporary", "test"],
)


@pytest.fixture
def load_counter(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every call to FAQRepository.from_json_file (still loading for real)."""
    calls: list[Path] = []
    original = FAQRepository.from_json_file.__func__  # type: ignore[attr-defined]

    def counting(cls: type[FAQRepository], path: str | Path = DEFAULT_FAQ_PATH) -> FAQRepository:
        calls.append(Path(path))
        return original(cls, path)

    monkeypatch.setattr(FAQRepository, "from_json_file", classmethod(counting))
    return calls


# --- Loaded once, shared by every request -------------------------------------------


def test_json_is_loaded_once_per_app_not_per_request(
    load_counter: list[Path], make_settings: Callable[..., Settings], faq_file: Path
) -> None:
    client = TestClient(create_app(make_settings(faq_data_path=faq_file)))

    for path in ("", "/categories", "/search?q=track", "/faq_101", "?category=orders"):
        assert client.get(f"{FAQS_URL}{path}").status_code == 200

    assert len(load_counter) == 1


def test_no_file_is_read_while_serving_requests(
    make_settings: Callable[..., Settings], faq_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(create_app(make_settings(faq_data_path=faq_file)))

    def fail(*args: object, **kwargs: object) -> str:
        raise AssertionError("a file was read during a request")

    monkeypatch.setattr(Path, "read_text", fail)

    assert client.get(f"{FAQS_URL}/search", params={"q": "refund"}).status_code == 200
    assert client.get(FAQS_URL).status_code == 200


def test_every_request_gets_the_same_service_instance(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    handled_by: list[FAQService] = []
    original = FAQService.list_active_faqs

    def recording(self: FAQService) -> list[FAQ]:
        handled_by.append(self)
        return original(self)

    monkeypatch.setattr(FAQService, "list_active_faqs", recording)
    client = TestClient(app)
    client.get(FAQS_URL)
    client.get(FAQS_URL)

    assert len(handled_by) == 2
    assert all(service is app.state.faq_service for service in handled_by)


def test_dependency_can_still_be_overridden(app: FastAPI) -> None:
    app.dependency_overrides[get_faq_service] = lambda: FAQService(FAQRepository([TEST_FAQ]))

    assert TestClient(app).get(FAQS_URL).json()["count"] == 1


def test_each_app_has_its_own_service(
    make_faq_client: Callable[..., TestClient], faq_client: TestClient
) -> None:
    temporary = make_faq_client([TEST_FAQ])

    assert temporary.get(FAQS_URL).json()["count"] == 1
    assert faq_client.get(FAQS_URL).json()["count"] == len(ACTIVE_IDS)


# --- Injecting temporary FAQ data in tests --------------------------------------------


def test_injected_service_skips_json_loading(
    load_counter: list[Path], make_faq_client: Callable[..., TestClient]
) -> None:
    client = make_faq_client([TEST_FAQ])

    assert client.get(f"{FAQS_URL}/faq_900").json()["question"] == TEST_FAQ.question
    assert load_counter == []


def test_injected_faqs_are_searchable(make_faq_client: Callable[..., TestClient]) -> None:
    client = make_faq_client([TEST_FAQ])

    results = client.get(f"{FAQS_URL}/search", params={"q": "temporary"}).json()["results"]

    assert [result["id"] for result in results] == ["faq_900"]


def test_empty_knowledge_base(make_faq_client: Callable[..., TestClient]) -> None:
    client = make_faq_client([])

    assert client.get(FAQS_URL).json() == {"count": 0, "faqs": []}
    assert client.get(f"{FAQS_URL}/categories").json() == {"categories": []}
    assert client.get(f"{FAQS_URL}/search", params={"q": "order"}).json()["results"] == []


def test_tests_do_not_modify_production_data(make_faq_client: Callable[..., TestClient]) -> None:
    before = hashlib.sha256(DEFAULT_FAQ_PATH.read_bytes()).hexdigest()

    make_faq_client([TEST_FAQ]).get(FAQS_URL)
    make_faq_client([]).get(f"{FAQS_URL}/search", params={"q": "order"})

    assert hashlib.sha256(DEFAULT_FAQ_PATH.read_bytes()).hexdigest() == before


# --- Misconfiguration -----------------------------------------------------------------


def test_missing_service_fails_loudly_with_safe_500(app: FastAPI) -> None:
    del app.state.faq_service

    response = TestClient(app).get(FAQS_URL)

    assert response.status_code == 500
    assert response.json()["detail"] == "Internal server error"
    assert "FAQService" not in response.text  # internal details stay in the logs
