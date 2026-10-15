"""Baseline security and privacy protections (Stage 5).

These tests check concrete, enforceable properties: secrets stay out of Git, request
bodies are size-limited, customer text is neither logged nor retained, the model only
sees the selected evidence, and generation is bounded. They do not make the API
production-secure: authentication, rate limiting and abuse protection are still needed
before a public deployment (see README, "Security and privacy").
"""

import json
import shutil
import subprocess
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.api.dependencies import get_grounded_answer_service
from app.core.config import PROJECT_ROOT, Settings
from app.core.middleware import REQUEST_TOO_LARGE_DETAIL
from app.main import create_app
from app.prompts.customer_support import SYSTEM_INSTRUCTIONS
from app.services.answer_decision_service import AnswerDecisionService
from app.services.context_builder import MAX_QUESTION_CHARS, ContextBuilder
from app.services.embedding_service import EmbeddingService
from app.services.faq_service import FAQService
from app.services.grounded_answer_service import GroundedAnswerService
from app.services.llm_service import LLMService
from app.services.semantic_search_service import SemanticSearchService
from app.services.usage_tracker import UsageTracker
from tests.fakes import FAKE_MODEL_NAME, FAQS, CountingLoader, FakeHybrid, FakeKeyword
from tests.fakes import result as hybrid_spec

KEY = SecretStr("sk-test-0123456789abcdefghij")
PII_QUESTION = "I forgot my password. I'm Jane Doe, jane.doe@example.com, card 4111 1111 1111 1111"


# --- Secrets stay out of Git -----------------------------------------------------------------


def test_gitignore_excludes_env_files_but_keeps_the_example() -> None:
    rules = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in rules and ".env.*" in rules and "!.env.example" in rules


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_env_is_ignored_and_untracked_in_git() -> None:
    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed arguments, no user input
            ["git", *args],  # noqa: S607 - git from PATH
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    if git("rev-parse", "--is-inside-work-tree").returncode != 0:
        pytest.skip("not a Git checkout")

    assert git("check-ignore", "-q", ".env").returncode == 0  # ignored
    assert git("ls-files", "--error-unmatch", ".env").returncode != 0  # never tracked
    assert git("ls-files", "--error-unmatch", ".env.example").returncode == 0


# --- Request size limits ---------------------------------------------------------------------


class RecordingService:
    def __init__(self) -> None:
        self.calls = 0

    def answer(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - must not run
        self.calls += 1
        raise AssertionError("an oversized request must never reach the service")


@pytest.fixture
def limited_client(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> Iterator[tuple[TestClient, RecordingService]]:
    app = create_app(make_settings(max_request_body_bytes=2048), faq_service=faq_service)
    service = RecordingService()
    app.dependency_overrides[get_grounded_answer_service] = lambda: service
    with TestClient(app) as client:
        yield client, service


@pytest.mark.parametrize(
    "path", ["/api/v1/answer", "/api/v1/hybrid-search", "/api/v1/semantic-search"]
)
def test_oversized_bodies_are_rejected_before_parsing(
    limited_client: tuple[TestClient, RecordingService], path: str
) -> None:
    client, service = limited_client
    body = json.dumps({"query": "x" * 5000})

    response = client.post(path, content=body, headers={"Content-Type": "application/json"})

    assert response.status_code == 413
    assert response.json() == {
        "detail": REQUEST_TOO_LARGE_DETAIL,
        "request_id": response.headers["X-Request-ID"],
    }
    assert service.calls == 0


def test_streamed_bodies_without_content_length_are_counted(
    limited_client: tuple[TestClient, RecordingService],
) -> None:
    client, service = limited_client

    def chunks() -> Iterator[bytes]:
        yield b'{"query": "'
        for _ in range(10):
            yield b"x" * 500
        yield b'"}'

    response = client.post(
        "/api/v1/answer", content=chunks(), headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 413
    assert response.json()["detail"] == REQUEST_TOO_LARGE_DETAIL
    assert service.calls == 0


def test_normal_requests_are_not_affected(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    semantic = SemanticSearchService(
        faq_service.list_active_faqs, EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader())
    )
    app = create_app(make_settings(), faq_service=faq_service, semantic_search_service=semantic)

    with TestClient(app) as client:
        longest = client.post("/api/v1/answer", json={"query": "x" * MAX_QUESTION_CHARS})
        too_long = client.post("/api/v1/answer", json={"query": "x" * (MAX_QUESTION_CHARS + 1)})
        listing = client.get("/api/v1/faqs")

    assert (longest.status_code, too_long.status_code, listing.status_code) == (200, 422, 200)


@pytest.mark.parametrize("value", ["1023", "1048577", "big"])
def test_request_limit_setting_is_validated(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", value)

    with pytest.raises(ValidationError, match="max_request_body_bytes"):
        Settings(_env_file=None)


# --- Customer text is neither logged nor retained ------------------------------------------


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        reply = {
            "status": "answered",
            "answer": "Choose Forgot Password on the sign-in page and follow the emailed link.",
            "source_faq_ids": ["faq_107"],
            "reason": "r",
        }
        return SimpleNamespace(
            status="completed",
            output_text=json.dumps(reply),
            output=[],
            model="gpt-5.4-mini",
            id="resp_1",
            _request_id="req_1",
            usage=SimpleNamespace(input_tokens=600, output_tokens=40, total_tokens=640),
        )

    def close(self) -> None:
        pass


@pytest.fixture
def llm_app(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> tuple[Any, FakeClient, UsageTracker, LLMService]:
    client = FakeClient()
    tracker = UsageTracker()
    llm = LLMService(
        KEY, model="gpt-5.4-mini", usage_tracker=tracker, client_factory=lambda k: client
    )
    hybrid = FakeHybrid(
        [hybrid_spec("faq_107", similarity=0.72), hybrid_spec("faq_108", similarity=0.1)]
    )
    service = GroundedAnswerService(
        AnswerDecisionService(hybrid, FakeKeyword()),  # type: ignore[arg-type]
        ContextBuilder(),
        llm,
    )
    app = create_app(make_settings(log_level="DEBUG"), faq_service=faq_service, llm_service=llm)
    app.dependency_overrides[get_grounded_answer_service] = lambda: service
    return app, client, tracker, llm


def test_customer_text_is_never_logged(
    capsys: pytest.CaptureFixture[str],  # first, so the app's log handler writes into it
    caplog: pytest.LogCaptureFixture,
    llm_app: tuple[Any, FakeClient, UsageTracker, LLMService],
) -> None:
    app, _, _, _ = llm_app

    with caplog.at_level("DEBUG"), TestClient(app) as http:
        assert http.post("/api/v1/answer", json={"query": PII_QUESTION}).status_code == 200
        http.post("/api/v1/answer", json={"query": PII_QUESTION, "bad": 1})  # 422 path too
    log = capsys.readouterr().out + caplog.text  # handler output and every log record

    assert "event=grounded_answer" in log and "event=llm_usage" in log  # logging works
    for private in ("Jane Doe", "jane.doe", "4111", "I forgot my password", "Forgot Password on"):
        assert private not in log


def test_customer_text_is_not_retained_after_the_request(
    llm_app: tuple[Any, FakeClient, UsageTracker, LLMService],
) -> None:
    app, client, tracker, llm = llm_app

    with TestClient(app) as http:
        http.post("/api/v1/answer", json={"query": PII_QUESTION})

    assert len(client.calls) == 1  # the question went to the model once, as data ...
    retained = " ".join(
        [repr(tracker.recent()), repr(tracker.summary()), repr(llm.error_counts()), repr(llm)]
    )
    for private in ("jane.doe", "Jane Doe", "4111", "forgot my password"):
        assert private not in retained  # ... and nothing about it is kept afterwards


def test_responses_are_not_stored_by_openai(
    llm_app: tuple[Any, FakeClient, UsageTracker, LLMService],
) -> None:
    app, client, _, _ = llm_app

    with TestClient(app) as http:
        http.post("/api/v1/answer", json={"query": "I forgot my password"})

    assert client.calls[0]["store"] is False


# --- The model sees only the selected evidence ------------------------------------------------


def test_model_input_is_restricted_to_the_selected_faqs(
    llm_app: tuple[Any, FakeClient, UsageTracker, LLMService],
) -> None:
    app, client, _, _ = llm_app

    with TestClient(app) as http:
        http.post("/api/v1/answer", json={"query": "I forgot my password"})

    [call] = client.calls
    assert call["instructions"] is SYSTEM_INSTRUCTIONS
    assert 'id="faq_107"' in call["input"]
    unselected = [faq for faq_id, faq in FAQS.items() if faq_id != "faq_107"]
    for faq in unselected:  # neither the unaccepted candidate nor the rest of the base
        assert f'id="{faq.id}"' not in call["input"]
        assert faq.answer not in call["input"]
    limit = ContextBuilder().max_chars + MAX_QUESTION_CHARS + 500  # evidence + question + tags
    assert len(call["input"]) <= limit


# --- Generation limits ----------------------------------------------------------------------


def test_generation_is_bounded(llm_app: tuple[Any, FakeClient, UsageTracker, LLMService]) -> None:
    app, client, _, llm = llm_app

    with TestClient(app) as http:
        http.post("/api/v1/answer", json={"query": "I forgot my password"})

    assert client.calls[0]["max_output_tokens"] == 800
    assert llm.timeout_seconds <= 300 and llm.max_retries <= 5
    with pytest.raises(ValueError):
        LLMService(KEY, model="m", max_output_tokens=4097)


def test_generation_limit_setting_has_a_reasonable_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_MAX_OUTPUT_TOKENS", "4097")

    with pytest.raises(ValidationError, match="openai_max_output_tokens"):
        Settings(_env_file=None)


# --- Size-limit middleware edge cases (raw ASGI) ---------------------------------------------


def run_asgi(
    headers: list[tuple[bytes, bytes]], messages: list[dict[str, Any]], max_bytes: int = 10
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run the middleware around an app that records what it receives."""
    import asyncio

    from app.core.middleware import RequestSizeLimitMiddleware

    seen: list[dict[str, Any]] = []
    sent: list[dict[str, Any]] = []
    incoming = list(messages)

    async def app(scope: Any, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            seen.append(message)
            if message["type"] != "http.request" or not message.get("more_body"):
                break
        seen.append(await receive())  # e.g. waiting for a disconnect after the body
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def receive() -> dict[str, Any]:
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    asyncio.run(RequestSizeLimitMiddleware(app, max_bytes)(scope, receive, send))
    return seen, sent


def test_small_streamed_body_is_replayed_unchanged() -> None:
    chunks = [
        {"type": "http.request", "body": b"abc", "more_body": True},
        {"type": "http.request", "body": b"def", "more_body": False},
    ]

    seen, sent = run_asgi([], chunks)

    assert seen[:2] == chunks
    assert seen[2] == {"type": "http.disconnect"}  # later reads reach the real receive
    assert sent[0]["status"] == 200


def test_disconnect_while_reading_is_passed_on() -> None:
    seen, sent = run_asgi([], [{"type": "http.request", "body": b"ab", "more_body": True},
                               {"type": "http.disconnect"}])  # fmt: skip

    assert {"type": "http.disconnect"} in seen
    assert sent[0]["status"] == 200


def test_malformed_content_length_is_left_to_the_framework() -> None:
    seen, sent = run_asgi(
        [(b"content-length", b"not-a-number")],
        [{"type": "http.request", "body": b"x" * 50, "more_body": False}],
    )

    assert seen[0]["body"] == b"x" * 50  # not judged here; not silently truncated either
    assert sent[0]["status"] == 200


def test_non_http_scopes_pass_through() -> None:
    import asyncio

    from app.core.middleware import RequestSizeLimitMiddleware

    called: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        called.append(scope["type"])

    asyncio.run(RequestSizeLimitMiddleware(app, 10)({"type": "lifespan"}, None, None))

    assert called == ["lifespan"]
