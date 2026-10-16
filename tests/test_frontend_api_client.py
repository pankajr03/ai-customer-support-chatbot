"""The Streamlit frontend's API client (Stage 8, Task 4).

Two kinds of tests:

- against the real backend, in-process: the client is given FastAPI's ``TestClient``
  (an ``httpx.Client``) as its transport, so every request goes through the real
  routes, validation and storage. Only the embedding model is scripted.
- against ``httpx.MockTransport``, for what the backend rarely or never does on demand:
  timeouts, broken connections, 403, 502, malformed replies, retries.
"""

import logging
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from frontend.api_client import (
    ApiError,
    BackendStatus,
    ChatApiClient,
    ConversationStatus,
    Credentials,
    Decision,
    ErrorKind,
)
from tests.test_api_workflows import RESET, SHIPPING, UNKNOWN

TOKEN = "cst_" + "s3cr3t" * 7  # noqa: S105 - a fake token
SESSION = uuid.UUID("3f2b8c1e-7a4d-4e9b-9c61-2d5f0a8e4b17")
CREDENTIALS = Credentials(session_id=SESSION, access_token=TOKEN)  # type: ignore[arg-type]
MESSAGE_ID = uuid.UUID("4c981eb1-7847-41c0-bf6b-0a0e1b9d6d91")


# --- Against the real backend ---------------------------------------------------------


@pytest.fixture
def api(make_backend: Callable[..., TestClient]) -> ChatApiClient:
    return ChatApiClient(http_client=make_backend(), sleep=_no_sleep)


def _no_sleep(seconds: float) -> None:
    raise AssertionError(f"unexpected sleep of {seconds} s")


# The health check's short timeout means nothing to the in-process TestClient.
@pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")
def test_health_check(api: ChatApiClient) -> None:
    assert api.check_health().status == "healthy"


def test_a_whole_conversation(api: ChatApiClient) -> None:
    created = api.create_conversation()
    assert created.status is ConversationStatus.ACTIVE
    credentials = created.credentials()

    answered = api.send_message(credentials, RESET, MESSAGE_ID)
    assert answered.decision is Decision.ANSWERED
    assert answered.client_message_id == MESSAGE_ID
    assert [source.faq_id for source in answered.sources] == ["faq_107"]
    assert not answered.replayed

    ambiguous = api.send_message(credentials, SHIPPING, uuid.uuid4())
    assert ambiguous.decision is Decision.AMBIGUOUS
    assert {source.faq_id for source in ambiguous.sources} == {"faq_103", "faq_104"}

    no_match = api.send_message(credentials, UNKNOWN, uuid.uuid4())
    assert no_match.decision is Decision.NO_MATCH
    assert no_match.sources == []

    history = api.get_history(credentials)
    assert history.complete and history.status is ConversationStatus.ACTIVE
    assert [m.role for m in history.messages] == ["user", "assistant"] * 3
    assert history.messages[0].content == RESET
    assert history.messages[1].message_id == answered.message_id

    closed = api.close_conversation(credentials)
    assert closed.status is ConversationStatus.CLOSED
    with pytest.raises(ApiError) as caught:
        api.send_message(credentials, RESET, uuid.uuid4())
    assert caught.value.kind is ErrorKind.SESSION_CLOSED
    assert api.get_history(credentials).status is ConversationStatus.CLOSED  # still readable

    api.delete_conversation(credentials)
    with pytest.raises(ApiError) as caught:
        api.get_history(credentials)
    assert caught.value.kind is ErrorKind.SESSION_NOT_FOUND
    assert caught.value.ends_conversation


def test_resending_a_message_returns_the_stored_reply(api: ChatApiClient) -> None:
    credentials = api.create_conversation().credentials()
    first = api.send_message(credentials, RESET, MESSAGE_ID)
    again = api.send_message(credentials, RESET, MESSAGE_ID)
    assert again.replayed
    assert again.message_id == first.message_id
    assert len(api.get_history(credentials).messages) == 2  # answered once


def test_a_wrong_token_looks_like_an_unknown_conversation(api: ChatApiClient) -> None:
    created = api.create_conversation()
    wrong = Credentials(
        session_id=created.session_id,
        access_token="cst_wrong",  # type: ignore[arg-type]  # noqa: S106 - a fake token
    )
    with pytest.raises(ApiError) as caught:
        api.send_message(wrong, RESET, MESSAGE_ID)
    assert caught.value.kind is ErrorKind.SESSION_NOT_FOUND
    assert caught.value.request_id  # the backend's request ID, for support


def test_a_message_that_is_too_long(make_backend: Callable[..., TestClient]) -> None:
    api = ChatApiClient(http_client=make_backend(max_customer_message_characters=100))
    credentials = api.create_conversation().credentials()
    with pytest.raises(ApiError) as caught:
        api.send_message(credentials, "a" * 101, MESSAGE_ID)
    error = caught.value
    assert error.kind is ErrorKind.INVALID_MESSAGE and not error.retryable
    assert error.validation_types == ("string_too_long",)
    assert error.max_length == 100


def test_reusing_an_id_for_another_message_is_a_conflict(api: ChatApiClient) -> None:
    credentials = api.create_conversation().credentials()
    api.send_message(credentials, RESET, MESSAGE_ID)
    with pytest.raises(ApiError) as caught:
        api.send_message(credentials, UNKNOWN, MESSAGE_ID)
    assert caught.value.kind is ErrorKind.MESSAGE_CONFLICT


def test_long_histories_are_read_page_by_page(api: ChatApiClient) -> None:
    credentials = api.create_conversation().credentials()
    for _ in range(3):
        api.send_message(credentials, UNKNOWN, uuid.uuid4())
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("frontend.api_client.HISTORY_PAGE_SIZE", 4)
        history = api.get_history(credentials)
        latest = api.get_history(credentials, max_messages=4)
    assert len(history.messages) == 6 and history.complete
    assert history.session_id == credentials.session_id and history.total_messages == 6
    assert [m.role for m in history.messages] == ["user", "assistant"] * 3
    times = [m.created_at for m in history.messages]
    assert times == sorted(times)  # oldest first
    # Over the limit, the newest messages are kept, still oldest first.
    assert latest.omitted == 2 and not latest.complete
    assert [m.message_id for m in latest.messages] == [m.message_id for m in history.messages[2:]]


# --- Against a scripted transport -----------------------------------------------------

Handler = Callable[[httpx.Request], httpx.Response]


def scripted(*handlers: Handler | httpx.Response) -> tuple[httpx.Client, list[httpx.Request]]:
    """An httpx client answering each request with the next handler or response."""
    requests: list[httpx.Request] = []
    queue = list(handlers)

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        step = queue.pop(0)
        return step(request) if callable(step) else step

    client = httpx.Client(base_url="http://backend.test", transport=httpx.MockTransport(handle))
    return client, requests


def error(status: int, code: str, *, retry_after: str | None = None) -> httpx.Response:
    headers = {"X-Request-ID": "req123"}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    body = {"error": {"code": code, "message": "A safe message.", "request_id": "req123"}}
    return httpx.Response(status, json=body, headers=headers)


def reply_body(**overrides: Any) -> dict[str, Any]:
    return {
        "session_id": str(SESSION),
        "message_id": str(uuid.uuid4()),
        "client_message_id": str(MESSAGE_ID),
        "request_id": "req123",
        "decision": "answered",
        "answer": "Select Forgot Password.",
        "generation_method": "approved_faq",
        "sources": [{"faq_id": "faq_027", "question": "How do I reset my password?"}],
        "usage": None,
        "created_at": "2026-10-15T09:31:06Z",
        "user_message": {},
        "assistant_message": {},
    } | overrides


def raise_(exc: Exception) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


class Sleeps(list[float]):
    def __call__(self, seconds: float) -> None:
        self.append(seconds)


def test_the_token_goes_only_in_the_authorization_header() -> None:
    http, requests = scripted(httpx.Response(200, json=reply_body()))
    ChatApiClient(http_client=http).send_message(CREDENTIALS, "Hello", MESSAGE_ID)
    (request,) = requests
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(request.url)
    assert TOKEN not in request.content.decode()
    assert request.url.path == f"/api/v1/conversations/{SESSION}/chat"
    assert request.read() == (f'{{"message":"Hello","client_message_id":"{MESSAGE_ID}"}}'.encode())


def test_tokens_are_masked_wherever_they_are_printed() -> None:
    assert TOKEN not in repr(CREDENTIALS) and TOKEN not in str(CREDENTIALS)
    assert TOKEN not in CREDENTIALS.model_dump_json()


@pytest.mark.parametrize(
    "failure",
    [
        error(503, "SERVICE_UNAVAILABLE", retry_after="1"),
        error(409, "REQUEST_IN_PROGRESS", retry_after="2"),
        error(429, "RATE_LIMITED", retry_after="1"),
        raise_(httpx.ConnectError("refused")),
        raise_(httpx.ReadError("reset")),
    ],
)
def test_temporary_failures_are_retried_with_the_same_message_id(failure: Any) -> None:
    http, requests = scripted(failure, httpx.Response(200, json=reply_body()))
    sleeps = Sleeps()
    reply = ChatApiClient(http_client=http, sleep=sleeps).send_message(
        CREDENTIALS, "Hello", MESSAGE_ID
    )
    assert reply.decision is Decision.ANSWERED
    assert len(requests) == 2 and requests[0].content == requests[1].content
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 5


def test_retry_after_is_honoured() -> None:
    http, _ = scripted(error(409, "REQUEST_IN_PROGRESS", retry_after="3"), reply_body_response())
    sleeps = Sleeps()
    ChatApiClient(http_client=http, sleep=sleeps).send_message(CREDENTIALS, "Hi", MESSAGE_ID)
    assert sleeps == [3.0]


def reply_body_response() -> httpx.Response:
    return httpx.Response(200, json=reply_body())


def test_a_long_retry_after_is_reported_instead_of_waited_for() -> None:
    http, requests = scripted(error(429, "RATE_LIMITED", retry_after="30"))
    sleeps = Sleeps()
    with pytest.raises(ApiError) as caught:
        ChatApiClient(http_client=http, sleep=sleeps).send_message(CREDENTIALS, "Hi", MESSAGE_ID)
    assert caught.value.kind is ErrorKind.RATE_LIMITED and caught.value.retry_after == 30
    assert sleeps == [] and len(requests) == 1


def test_retries_stop_after_max_retries() -> None:
    http, requests = scripted(*[error(503, "SERVICE_UNAVAILABLE")] * 3)
    sleeps = Sleeps()
    with pytest.raises(ApiError) as caught:
        ChatApiClient(http_client=http, max_retries=2, sleep=sleeps).send_message(
            CREDENTIALS, "Hi", MESSAGE_ID
        )
    assert caught.value.kind is ErrorKind.SERVICE_UNAVAILABLE and caught.value.retryable
    assert len(requests) == 3 and sleeps == [0.5, 1.0]


def test_a_timeout_is_not_retried_automatically() -> None:
    http, requests = scripted(raise_(httpx.ReadTimeout("slow")))
    with pytest.raises(ApiError) as caught:
        ChatApiClient(http_client=http, sleep=_no_sleep).send_message(CREDENTIALS, "Hi", MESSAGE_ID)
    assert caught.value.kind is ErrorKind.TIMEOUT and caught.value.retryable
    assert len(requests) == 1


def test_a_backend_that_is_down() -> None:
    http, _ = scripted(raise_(httpx.ConnectTimeout("no route")))
    with pytest.raises(ApiError) as caught:
        ChatApiClient(http_client=http).check_health()
    assert caught.value.kind is ErrorKind.BACKEND_UNREACHABLE


def test_creating_a_conversation_is_not_retried() -> None:
    http, requests = scripted(error(503, "SERVICE_UNAVAILABLE", retry_after="1"))
    with pytest.raises(ApiError):
        ChatApiClient(http_client=http, sleep=_no_sleep).create_conversation()
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (error(400, "VALIDATION_ERROR"), ErrorKind.CLIENT_ERROR),
        (error(401, "UNAUTHORIZED_SESSION"), ErrorKind.UNAUTHORIZED),
        (httpx.Response(403, text="Forbidden"), ErrorKind.FORBIDDEN),
        (error(404, "SESSION_NOT_FOUND"), ErrorKind.SESSION_NOT_FOUND),
        (error(404, "NOT_FOUND"), ErrorKind.CLIENT_ERROR),
        (error(409, "SESSION_CLOSED"), ErrorKind.SESSION_CLOSED),
        (error(410, "SESSION_EXPIRED"), ErrorKind.SESSION_EXPIRED),
        (error(413, "PAYLOAD_TOO_LARGE"), ErrorKind.INVALID_MESSAGE),
        (error(422, "VALIDATION_ERROR"), ErrorKind.INVALID_MESSAGE),
        (error(422, "DUPLICATE_MESSAGE_CONFLICT"), ErrorKind.MESSAGE_CONFLICT),
        (error(500, "INTERNAL_ERROR"), ErrorKind.SERVER_ERROR),
        # From a proxy: the backend may have answered, so the outcome is unknown.
        (httpx.Response(502, text="<html>Bad gateway</html>"), ErrorKind.NETWORK_ERROR),
        (httpx.Response(504, text="<html>Gateway timeout</html>"), ErrorKind.TIMEOUT),
        (error(503, "SERVICE_UNAVAILABLE"), ErrorKind.SERVICE_UNAVAILABLE),
        (httpx.Response(307, headers={"Location": "http://elsewhere"}), ErrorKind.INVALID_RESPONSE),
    ],
)
def test_error_responses_map_to_kinds(response: httpx.Response, kind: ErrorKind) -> None:
    http, requests = scripted(response)
    with pytest.raises(ApiError) as caught:
        ChatApiClient(http_client=http, max_retries=0).send_message(CREDENTIALS, "Hi", MESSAGE_ID)
    assert caught.value.kind is kind
    assert caught.value.status_code == response.status_code
    assert len(requests) == 1  # never retried, never redirected


def test_the_request_id_comes_from_the_envelope_or_the_header() -> None:
    http, _ = scripted(httpx.Response(500, text="oops", headers={"X-Request-ID": "hdr1"}))
    with pytest.raises(ApiError) as caught:
        ChatApiClient(http_client=http).get_conversation(CREDENTIALS)
    assert caught.value.request_id == "hdr1"


@pytest.mark.parametrize(
    "body",
    [
        {"unexpected": True},
        reply_body(decision="something_new"),
        reply_body(answer=""),
        reply_body(session_id=str(uuid.uuid4())),  # another conversation's reply
        reply_body(client_message_id=str(uuid.uuid4())),  # another message's reply
    ],
)
def test_replies_that_break_the_contract_are_rejected(body: dict[str, Any]) -> None:
    http, _ = scripted(httpx.Response(200, json=body))
    with pytest.raises(ApiError) as caught:
        ChatApiClient(http_client=http).send_message(CREDENTIALS, "Hi", MESSAGE_ID)
    assert caught.value.kind is ErrorKind.INVALID_RESPONSE


def test_nothing_secret_or_private_is_logged_or_raised(caplog: pytest.LogCaptureFixture) -> None:
    private = "my card number is 4111 1111 1111 1111"
    http, _ = scripted(
        raise_(httpx.ConnectError("refused")),
        error(422, "VALIDATION_ERROR"),
    )
    caplog.set_level(logging.DEBUG, logger="frontend")
    with pytest.raises(ApiError) as caught:
        ChatApiClient(http_client=http, sleep=lambda s: None).send_message(
            CREDENTIALS, private, MESSAGE_ID
        )
    for text in (caplog.text, str(caught.value), repr(caught.value)):
        assert TOKEN not in text and private not in text
    assert "chat" in caplog.text  # the route is logged, not the URL or body


def test_cookies_are_never_stored_or_sent() -> None:
    def set_cookie(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "healthy", "service": "x"},
                              headers={"Set-Cookie": "sid=abc; Path=/"})  # fmt: skip

    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return set_cookie(request)

    client = ChatApiClient("http://backend.test")
    client._http._transport = httpx.MockTransport(handle)  # the client's own cookie policy
    client.check_health()
    client.check_health()
    assert "cookie" not in requests[1].headers
    assert not client._http.cookies
    client.close()


def test_the_api_prefix_is_configurable() -> None:
    http, requests = scripted(httpx.Response(200, json={"status": "healthy", "service": "x"}))
    ChatApiClient(http_client=http, api_prefix="/support/v1").check_health()
    assert requests[0].url.path == "/support/v1/health"


def history_page(session: uuid.UUID, ids: list[uuid.UUID], *, total: int, more: bool) -> Any:
    messages = [
        {
            "message_id": str(message_id),
            "role": "user",
            "content": "Hi",
            "created_at": "2026-10-15T09:31:00Z",
            "decision": None,
            "source_faq_ids": [],
        }
        for message_id in ids
    ]
    return httpx.Response(
        200,
        json={
            "session_id": str(session),
            "status": "active",
            "total_messages": total,
            "offset": 0,
            "limit": 100,
            "has_more": more,
            "messages": messages,
        },
    )


def test_a_history_page_of_another_conversation_is_rejected() -> None:
    http, _ = scripted(history_page(uuid.uuid4(), [uuid.uuid4()], total=1, more=False))
    with pytest.raises(ApiError) as caught:
        ChatApiClient(http_client=http).get_history(CREDENTIALS)
    assert caught.value.kind is ErrorKind.INVALID_RESPONSE


def test_a_message_seen_on_two_pages_is_kept_once() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    http, requests = scripted(
        history_page(SESSION, [a, b], total=3, more=True),
        history_page(SESSION, [b, c], total=3, more=False),  # b again: the pages shifted
    )
    history = ChatApiClient(http_client=http).get_history(CREDENTIALS)
    assert [m.message_id for m in history.messages] == [a, b, c]
    assert [r.url.params["offset"] for r in requests] == ["0", "2"]
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in requests)


def created_body() -> dict[str, Any]:
    return {
        "session_id": str(SESSION),
        "access_token": TOKEN,
        "token_type": "Bearer",
        "status": "active",
        "created_at": "2026-10-15T09:30:00Z",
        "expires_at": "2026-10-15T10:30:00Z",
    }


def test_creating_a_conversation_sends_no_token_and_reads_the_one_returned() -> None:
    http, requests = scripted(httpx.Response(201, json=created_body()))
    created = ChatApiClient(http_client=http).create_conversation()
    (request,) = requests
    assert request.method == "POST" and request.url.path == "/api/v1/conversations"
    assert "authorization" not in request.headers and request.content == b""
    credentials = created.credentials()
    assert credentials.session_id == SESSION
    assert credentials.access_token.get_secret_value() == TOKEN


def test_a_create_reply_without_201_or_a_token_is_rejected() -> None:
    for response in (
        httpx.Response(200, json=created_body()),
        httpx.Response(201, json=created_body() | {"access_token": ""}),
    ):
        http, _ = scripted(response)
        with pytest.raises(ApiError) as caught:
            ChatApiClient(http_client=http).create_conversation()
        assert caught.value.kind is ErrorKind.INVALID_RESPONSE


def test_deleting_sends_delete_with_the_token() -> None:
    http, requests = scripted(httpx.Response(204))
    ChatApiClient(http_client=http).delete_conversation(CREDENTIALS)
    (request,) = requests
    assert request.method == "DELETE"
    assert request.url.path == f"/api/v1/conversations/{SESSION}"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"


def test_a_delete_that_is_not_confirmed_with_204_is_not_trusted() -> None:
    http, _ = scripted(httpx.Response(200, json={}))
    with pytest.raises(ApiError) as caught:
        ChatApiClient(http_client=http).delete_conversation(CREDENTIALS)
    assert caught.value.kind is ErrorKind.INVALID_RESPONSE


# --- Readiness (Stage 9) --------------------------------------------------------------

READY_BODY = {
    "status": "ready",
    "service": "x",
    "checks": {
        "knowledge_base": "ok",
        "conversation_storage": "ok",
        "semantic_search": "ready",
        "llm": "disabled",
    },
}


@pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")
def test_backend_status_follows_readiness(api: ChatApiClient) -> None:
    # A fresh backend loads its search model on the first message.
    assert api.backend_status() is BackendStatus.WARMING_UP
    api.send_message(api.create_conversation().credentials(), RESET, uuid.uuid4())
    assert api.backend_status() is BackendStatus.ONLINE


def test_a_backend_that_is_not_ready_is_unavailable() -> None:
    not_ready = {**READY_BODY, "status": "not_ready"}
    http, requests = scripted(httpx.Response(503, json=not_ready))
    client = ChatApiClient(http_client=http)
    assert client.backend_status() is BackendStatus.UNAVAILABLE
    assert len(requests) == 1  # a 503 here is an answer, not an error to retry


def test_a_backend_without_a_readiness_check_falls_back_to_health() -> None:
    http, requests = scripted(
        error(404, "NOT_FOUND"), httpx.Response(200, json={"status": "healthy", "service": "x"})
    )
    assert ChatApiClient(http_client=http).backend_status() is BackendStatus.ONLINE
    assert [r.url.path for r in requests] == ["/api/v1/ready", "/api/v1/health"]


@pytest.mark.parametrize(
    "response",
    [raise_(httpx.ConnectError("refused")), error(500, "INTERNAL_ERROR")],
)
def test_a_backend_status_check_never_raises(response: Any) -> None:
    http, _ = scripted(response)
    assert ChatApiClient(http_client=http).backend_status() is BackendStatus.UNAVAILABLE
