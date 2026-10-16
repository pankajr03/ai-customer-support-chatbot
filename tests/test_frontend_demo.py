"""Demo mode (Stage 9, Task 16): a labelled demonstration, never a pretend LLM.

The page in demo mode says that the data is fictional and how answers are produced
**right now**, as the backend's readiness check reports it. With the LLM off it must not
suggest AI-written answers; an unknown mode is shown as unknown.
"""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from frontend import resources
from frontend.api_client import BackendStatus, ChatApiClient
from frontend.components.chat import ANSWER_MODES, UNKNOWN_ANSWER_MODE
from frontend.config import FrontendSettings
from tests.test_api_workflows import RESET
from tests.test_frontend_app import APP

FAKE_KEY = "sk-test-" + "k" * 40


@pytest.fixture
def open_demo(
    monkeypatch: pytest.MonkeyPatch, make_backend: Callable[..., TestClient]
) -> Callable[..., AppTest]:
    def _open(*, demo: bool = True, client: ChatApiClient | None = None, **backend: Any) -> AppTest:
        api = client or ChatApiClient(http_client=make_backend(**backend), sleep=lambda s: None)
        status = lambda _client: BackendStatus.ONLINE  # noqa: E731
        status.clear = lambda: None  # type: ignore[attr-defined]
        monkeypatch.setattr(resources, "get_api_client", lambda: api)
        monkeypatch.setattr(resources, "backend_status", status)
        monkeypatch.setattr(resources, "answer_mode", resources.answer_mode.__wrapped__)
        monkeypatch.setattr(
            resources, "get_settings", lambda: FrontendSettings(_env_file=None, demo_mode=demo)
        )
        page = AppTest.from_file(str(APP), default_timeout=30)
        page.run()
        assert not page.exception
        return page

    return _open


def notice(page: AppTest) -> str:
    return "\n".join(info.value for info in page.info)


@pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")
def test_demo_mode_with_the_llm_off_says_answers_are_the_approved_text(
    open_demo: Callable[..., AppTest],
) -> None:
    page = open_demo()
    text = notice(page)
    assert "Demo." in text and "fictional online store" in text
    assert ANSWER_MODES["disabled"] in text
    assert "AI rewording is on" not in text
    page.chat_input[0].set_value(RESET).run()
    captions = [c.value for c in page.caption]
    assert "Answer from our help articles" in captions  # each reply labelled too
    assert "AI-written summary of our help articles" not in captions


@pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")
def test_demo_mode_with_the_llm_on_says_so_without_calling_it(
    open_demo: Callable[..., AppTest],
) -> None:
    page = open_demo(llm_enabled=True, openai_api_key=FAKE_KEY)
    assert ANSWER_MODES["enabled"] in notice(page)


def test_an_unknown_answer_mode_is_shown_as_unknown(
    open_demo: Callable[..., AppTest], monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    def no_ready(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ready"):
            return httpx.Response(
                404, json={"error": {"code": "NOT_FOUND", "message": "m", "request_id": "r"}}
            )
        return httpx.Response(
            201,
            json={
                "session_id": "3f2b8c1e-7a4d-4e9b-9c61-2d5f0a8e4b17",
                "access_token": "cst_x",
                "token_type": "Bearer",
                "status": "active",
                "created_at": "2026-10-16T09:30:00Z",
                "expires_at": None,
            },
        )

    client = ChatApiClient(
        http_client=httpx.Client(base_url="http://b.test", transport=httpx.MockTransport(no_ready))
    )
    page = open_demo(client=client)
    text = notice(page)
    assert UNKNOWN_ANSWER_MODE in text
    assert all(mode not in text for mode in ANSWER_MODES.values())


def test_no_demo_notice_outside_demo_mode(open_demo: Callable[..., AppTest]) -> None:
    page = open_demo(demo=False)
    assert "Demo." not in notice(page)


def test_demo_mode_is_off_by_default() -> None:
    assert FrontendSettings(_env_file=None).demo_mode is False
