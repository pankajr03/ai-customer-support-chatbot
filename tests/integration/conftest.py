"""End-to-end integration harness (Stage 9, Task 11).

Every test here runs the **real** FastAPI application (real routes, middleware, error
handlers, conversation storage in a temporary SQLite file, retrieval, decisions and
grounding validation) and talks to it the way the Streamlit frontend does: through
``frontend.api_client.ChatApiClient``, or through the page itself (``AppTest``).

Only the external AI dependencies are replaced, deterministically:

- the embedding model by ``ScriptedEncoder`` (no download, no PyTorch);
- OpenAI by ``GroundedLLM``, a client stand-in whose reply each test chooses (no paid
  call; the root conftest also blocks every non-local network connection).

``FaultInjector`` sits between the client and the app and can lose a request, lose a
reply after the backend stored it, or corrupt a reply, so reliability tests know exactly
what the backend really did.
"""

import html
import importlib
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from streamlit.testing.v1 import AppTest

from app.core.config import Settings
from app.database.connection import Database
from app.main import create_app
from app.services.embedding_service import EmbeddingService
from app.services.faq_service import FAQService
from app.services.llm_service import LLMService
from app.services.semantic_search_service import SemanticSearchService
from frontend import resources
from frontend.api_client import BackendStatus, ChatApiClient, Credentials
from frontend.config import FrontendSettings
from tests.fakes import FAKE_MODEL_NAME
from tests.test_api_workflows import ScriptedEncoder

APP_FILE = Path(__file__).resolve().parents[2] / "frontend" / "app.py"
CONVERSATIONS = "/api/v1/conversations"
FAKE_KEY = SecretStr("sk-test-" + "0123456789" * 4)

# Questions with known outcomes against tests/faq_test_data.py and ScriptedEncoder.
EXACT = "How do I reset my password?"  # faq_107's own question
PARAPHRASE = "I can't remember the password for my login"  # faq_107, other words
KEYWORDS = "reset password forgot password login"  # faq_107 by keywords
FOLLOW_UP = "How long does it take?"  # resolved from the previous turn
AMBIGUOUS = "Tell me about shipping"  # faq_103 and faq_104
OUT_OF_SCOPE = "Do you sell submarines?"  # nothing
RETURNS = "Can I send something back? return"  # faq_106

_PRIMARY = re.compile(
    r'<faq id="(faq_\d+)"[^>]*role="primary">\s*<question>.*?</question>\s*'
    r"<answer>(.*?)</answer>",
    re.S,
)

Reply = Callable[[str, str], dict[str, Any]]


def faithful(faq_id: str, answer: str) -> dict[str, Any]:
    """A grounded reply: the approved answer, citing its FAQ."""
    return {"status": "answered", "answer": answer, "source_faq_ids": [faq_id], "reason": "r"}


class GroundedLLM:
    """OpenAI client stand-in. ``reply`` turns the primary FAQ of the request's evidence
    into the model's structured reply; ``error`` is raised instead. Records requests."""

    def __init__(self, reply: Reply = faithful, error: Exception | None = None) -> None:
        self.reply, self.error = reply, error
        self.calls: list[dict[str, Any]] = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        match = _PRIMARY.search(str(kwargs["input"]))
        assert match, "the request carries approved FAQ evidence"
        body = self.reply(match.group(1), html.unescape(match.group(2)))
        return SimpleNamespace(
            status="completed",
            output_text=json.dumps(body),
            output=[],
            model="gpt-5.4-mini",
            id="resp_1",
            usage=SimpleNamespace(input_tokens=300, output_tokens=40),
            _request_id="req_1",
        )

    def prompts(self) -> str:
        """Everything sent to the model, as one string (for leak checks)."""
        return json.dumps(self.calls, default=str)

    def close(self) -> None:
        pass


class FaultInjector(httpx.BaseTransport):
    """Wraps the app's transport. ``plan`` maps a path suffix ("/chat", "/messages", ...)
    to the next steps for requests to it:

    - "refused": no connection at all (the backend is offline);
    - "lost": the request never reaches the backend (a timeout);
    - "reply_lost": the backend handles it, the reply never arrives (a timeout);
    - "malformed": the backend handles it, the reply is not valid JSON;
    - "strip_optional": the backend handles it; nullable fields and Stage 9 additions
      are removed from the reply, as an older backend would send it.
    """

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self.inner = inner
        self.plan: dict[str, list[str]] = {}
        self.seen: list[tuple[str, str]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append((request.method, request.url.path))
        path = request.url.path
        steps = next((s for suffix, s in self.plan.items() if path.endswith(suffix)), [])
        step = steps.pop(0) if steps else "ok"
        if step == "refused":
            raise httpx.ConnectError("connection refused", request=request)
        if step == "lost":
            raise httpx.ReadTimeout("lost on the way", request=request)
        response = self.inner.handle_request(request)
        if step == "ok":
            return response
        response.read()
        if step == "reply_lost":
            raise httpx.ReadTimeout("reply lost", request=request)
        library = importlib.import_module(type(request).__module__.split(".")[0])
        if step == "malformed":
            return library.Response(200, content=b'{"decision": "answ', headers=response.headers)
        if step == "strip_optional":
            body = _strip_optional(response.json())
            headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
            return library.Response(response.status_code, json=body, headers=headers)
        raise AssertionError(f"unknown step {step}")

    def count(self, method: str, suffix: str) -> int:
        return sum(1 for m, path in self.seen if m == method and path.endswith(suffix))


_STAGE_9_ADDITIONS = {"sources", "client_message_id"}


def _strip_optional(value: Any, *, top: bool = True) -> Any:
    """Drop null fields and the Stage 9 history additions (keep top-level chat fields)."""
    if isinstance(value, dict):
        return {
            key: _strip_optional(item, top=False)
            for key, item in value.items()
            if item is not None and (top or key not in _STAGE_9_ADDITIONS)
        }
    if isinstance(value, list):
        return [_strip_optional(item, top=False) for item in value]
    return value


@dataclass
class Stack:
    """One running backend and the frontend's client for it."""

    http: TestClient
    api: ChatApiClient
    database: Database
    faults: FaultInjector
    llm: GroundedLLM | None
    encoder: ScriptedEncoder
    created: list[Credentials] = field(default_factory=list)

    def start(self) -> Credentials:
        credentials = self.api.create_conversation().credentials()
        self.created.append(credentials)
        return credentials

    def raw_chat(self, credentials: Credentials | None, session_id: str, body: Any) -> Any:
        headers = credentials.authorization() if credentials else {}
        return self.http.post(f"{CONVERSATIONS}/{session_id}/chat", json=body, headers=headers)

    def sql(self, statement: str) -> None:
        from sqlalchemy import text

        with self.database.write() as session:
            session.execute(text(statement))


@pytest.fixture
def make_stack(
    make_settings: Callable[..., Settings], faq_service: FAQService, tmp_path: Path
) -> Iterator[Callable[..., Stack]]:
    stacks: list[Stack] = []

    def _make(
        *,
        llm: GroundedLLM | None = None,
        loader: Callable[[str], Any] | None = None,
        **settings: Any,
    ) -> Stack:
        encoder = ScriptedEncoder()
        semantic = SemanticSearchService(
            faq_service.list_active_faqs,
            EmbeddingService(
                FAKE_MODEL_NAME, loader=loader or (lambda name: encoder), load_retry_seconds=30
            ),
        )
        database = Database(f"sqlite:///{(tmp_path / f'stack{len(stacks)}.db').as_posix()}")
        llm_service = None
        if llm is not None:
            llm_service = LLMService(
                FAKE_KEY, model="gpt-5.4-mini", max_retries=0, client_factory=lambda key: llm
            )
        app = create_app(
            make_settings(query_rewrite_use_llm=False, **settings),
            faq_service=faq_service,
            semantic_search_service=semantic,
            database=database,
            llm_service=llm_service,
        )
        http = TestClient(app)
        http.__enter__()
        faults = FaultInjector(http._transport)
        http._transport = faults
        api = ChatApiClient(http_client=http, sleep=lambda seconds: None)
        stack = Stack(http, api, database, faults, llm, encoder)
        stacks.append(stack)
        return stack

    yield _make
    for stack in stacks:
        stack.http.__exit__(None, None, None)


@pytest.fixture
def stack(make_stack: Callable[..., Stack]) -> Stack:
    return make_stack()


@pytest.fixture
def open_page(monkeypatch: pytest.MonkeyPatch) -> Callable[..., AppTest]:
    """The Streamlit page in a new browser session, using ``stack``'s client."""

    def _open(stack: Stack, status: BackendStatus = BackendStatus.ONLINE) -> AppTest:
        backend_status = lambda _client: status  # noqa: E731
        backend_status.clear = lambda: None  # type: ignore[attr-defined]
        monkeypatch.setattr(resources, "get_api_client", lambda: stack.api)
        monkeypatch.setattr(resources, "backend_status", backend_status)
        monkeypatch.setattr(resources, "get_settings", lambda: FrontendSettings(_env_file=None))
        page = AppTest.from_file(str(APP_FILE), default_timeout=30)
        page.run()
        assert not page.exception
        return page

    return _open


def say(page: AppTest, text: str) -> AppTest:
    page.chat_input[0].set_value(text).run()
    assert not page.exception
    return page


def page_text(page: AppTest) -> str:
    """Everything visible on the page as text: markdown, captions, alerts, buttons."""
    parts: list[str] = []
    for kind in ("markdown", "caption", "error", "info", "warning", "success"):
        parts += [str(element.value) for element in getattr(page, kind)]
    parts += [button.label for button in page.button]
    return "\n".join(parts)


def unescape(markdown: str) -> str:
    """Page markdown back to plain text (the page escapes Markdown characters)."""
    return re.sub(r"\\(.)", r"\1", markdown).replace("  \n", "\n").rstrip()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # The frontend's short status-check timeouts mean nothing to the in-process TestClient.
    for item in items:
        if "tests/integration/" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.filterwarnings("ignore:You should not use the 'timeout'"))
