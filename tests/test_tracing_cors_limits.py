"""Request IDs and safe logging, CORS, and basic abuse protection (Stage 7, Tasks 9-11)."""

import logging
import re
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.core.rate_limit as rate_limit_module
from app.core.config import Settings
from app.core.logging_config import WITHHELD, configure_logging, safe_traceback
from app.core.rate_limit import RateLimiter, RateLimitExceededError, RateLimits, session_key
from app.services.faq_service import FAQService
from tests.test_conversations_api import BASE, QUESTION, build_app, create

ORIGIN = "http://localhost:8501"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --- Task 9: request IDs ----------------------------------------------------------------


@pytest.fixture
def api(make_settings: Callable[..., Settings], faq_service: FAQService) -> Iterator[TestClient]:
    with TestClient(build_app(make_settings, faq_service)) as client:
        yield client


def test_every_response_has_a_fresh_server_generated_request_id(api: TestClient) -> None:
    sid, headers = create(api)
    spoofed = {**headers, "X-Request-ID": "client-chosen"}

    first = api.post(f"{BASE}/{sid}/chat", json={"message": QUESTION}, headers=spoofed)
    second = api.get(f"{BASE}/{sid}/messages", headers=headers)
    error = api.get(f"{BASE}/{sid}")  # 401

    ids = [r.headers["X-Request-ID"] for r in (first, second, error)]
    assert all(re.fullmatch(r"[0-9a-f]{32}", i) for i in ids)
    assert len(set(ids)) == 3 and "client-chosen" not in ids  # never taken from the client
    assert first.json()["request_id"] == ids[0]  # in the chat response
    assert error.json()["error"]["request_id"] == ids[2]  # and in error responses


def test_logs_carry_the_request_id_and_no_customer_data_or_secrets(
    make_settings: Callable[..., Settings],
    faq_service: FAQService,
    capsys: pytest.CaptureFixture[str],
) -> None:
    key = "sk-test-abcdefghijklmnopqrstuvwxyz012345"
    app = build_app(make_settings, faq_service, openai_api_key=key)
    message = "My email is jane.doe@example.com and my order 4111-1111 is late. Return policy?"
    with TestClient(app) as client:
        sid, headers = create(client)
        token = headers["Authorization"].removeprefix("Bearer ")
        capsys.readouterr()  # discard startup output
        response = client.post(f"{BASE}/{sid}/chat", json={"message": message}, headers=headers)
        client.get(f"{BASE}/{sid}?access_token={token}")  # a token in the URL (refused)

    out = capsys.readouterr().out
    request_id = response.headers["X-Request-ID"]
    lines = [line for line in out.splitlines() if f"req={request_id}" in line]
    assert any("event=conversation_turn" in line for line in lines)
    assert any(f"POST {BASE}/{{session_id}}/chat -> 200" in line for line in lines)
    for secret in (token, key, "jane.doe", "4111", "late. Return", "access_token="):
        assert secret not in out, secret


def test_tracebacks_keep_frames_but_not_exception_messages(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    try:
        try:
            raise ValueError("customer wrote: my address is 1 Main Street")
        except ValueError as exc:
            raise RuntimeError("could not store it") from exc
    except RuntimeError:
        logging.getLogger("test").exception("Unhandled error")

    out = capsys.readouterr().out
    # The source line of the raise is shown (it is code); the exception message is not.
    assert "ValueError: customer wrote" not in out
    assert "RuntimeError: could not store it" not in out
    assert f"builtins.ValueError: {WITHHELD}" in out
    assert f"builtins.RuntimeError: {WITHHELD}" in out
    assert "test_tracebacks_keep_frames_but_not_exception_messages" in out  # frames kept


def test_debug_mode_keeps_exception_messages(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", exception_messages=True)
    try:
        raise ValueError("visible in development")
    except ValueError:
        logging.getLogger("test").exception("boom")
    configure_logging("INFO")

    assert "ValueError: visible in development" in capsys.readouterr().out


def test_safe_traceback_handles_cycles() -> None:
    a, b = ValueError("a"), ValueError("b")
    a.__context__, b.__context__ = b, a

    text = safe_traceback(a)

    assert text.count(WITHHELD) == 2


def test_conversation_tokens_are_redacted_without_bearer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    logging.getLogger("test").info("token was cst_" + "A1b2_" * 9)

    assert "cst_A1b2" not in capsys.readouterr().out


# --- Task 10: CORS ----------------------------------------------------------------------


@pytest.fixture
def cors_api(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> Iterator[TestClient]:
    app = build_app(make_settings, faq_service, cors_origins=[ORIGIN, "http://localhost:5173"])
    with TestClient(app) as client:
        yield client


def preflight(client: TestClient, origin: str, method: str, headers: str) -> Any:
    return client.options(
        f"{BASE}/x/chat",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        },
    )


def test_preflight_allows_the_frontend_headers_and_methods(cors_api: TestClient) -> None:
    response = preflight(cors_api, ORIGIN, "POST", "authorization,content-type,idempotency-key")

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    allowed = {
        h.strip().lower() for h in response.headers["access-control-allow-headers"].split(",")
    }
    assert {"authorization", "content-type", "idempotency-key"} <= allowed
    assert set(response.headers["access-control-allow-methods"].split(", ")) == {
        "GET",
        "POST",
        "DELETE",
    }
    assert "access-control-allow-credentials" not in response.headers  # bearer header, no cookies


@pytest.mark.parametrize(
    ("origin", "method", "headers"),
    [
        ("https://evil.example", "POST", "authorization"),  # unlisted origin
        (ORIGIN, "PUT", "content-type"),  # a method the API does not use
        (ORIGIN, "POST", "x-custom"),  # an unlisted header
    ],
)
def test_preflight_refuses_anything_else(
    cors_api: TestClient, origin: str, method: str, headers: str
) -> None:
    response = preflight(cors_api, origin, method, headers)

    assert response.status_code == 400


def test_frontends_can_read_the_headers_they_need(cors_api: TestClient) -> None:
    response = cors_api.post(BASE, headers={"Origin": ORIGIN})

    assert response.status_code == 201
    exposed = {
        h.strip().lower() for h in response.headers["access-control-expose-headers"].split(",")
    }
    assert {"x-request-id", "retry-after", "idempotent-replayed", "location"} <= exposed


def test_cors_is_not_authentication(cors_api: TestClient) -> None:
    sid, _ = create(cors_api)

    response = cors_api.get(f"{BASE}/{sid}", headers={"Origin": ORIGIN})  # allowed origin

    assert response.status_code == 401  # still needs the conversation's token


def test_no_cross_origin_access_by_default(api: TestClient) -> None:
    response = preflight(api, ORIGIN, "POST", "authorization")

    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("origins", [["http://shop.example.com"], ["http://localhost:8501"]])
def test_production_requires_https_origins(origins: list[str]) -> None:
    with pytest.raises(ValidationError, match="https://"):
        Settings(_env_file=None, app_env="production", cors_origins=origins)

    ok = Settings(
        _env_file=None,
        app_env="production",
        database_url="sqlite:///data/check.db",  # validated only; production needs a file
        cors_origins=["https://shop.example.com"],
    )
    assert ok.cors_origins == ["https://shop.example.com"]


# --- Task 11: rate limits (unit) ----------------------------------------------------------


def test_a_bucket_allows_a_burst_then_one_request_per_interval() -> None:
    clock = Clock()
    limiter = RateLimiter("test", 3, clock=clock)

    for _ in range(3):
        limiter.hit("a")
    with pytest.raises(RateLimitExceededError) as refused:
        limiter.hit("a")
    assert refused.value.retry_after_seconds == pytest.approx(20.0)  # 60 s / 3
    limiter.hit("b")  # other keys are independent

    clock.now += 20
    limiter.hit("a")
    with pytest.raises(RateLimitExceededError):
        limiter.hit("a")
    clock.now += 600  # refills up to the burst size, not beyond
    for _ in range(3):
        limiter.hit("a")
    with pytest.raises(RateLimitExceededError):
        limiter.hit("a")


def test_tracked_keys_are_bounded() -> None:
    limiter = RateLimiter("test", 1, max_keys=3, clock=Clock())
    for key in "abcd":
        limiter.hit(key)

    assert len(limiter) == 3
    limiter.hit("a")  # "a" was forgotten (least recently used): its count was reset


def test_the_limiter_is_thread_safe() -> None:
    limiter = RateLimiter("test", 50, clock=Clock())
    allowed: list[int] = []

    def worker() -> None:
        for _ in range(20):
            try:
                limiter.hit("same")
                allowed.append(1)
            except RateLimitExceededError:
                pass

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(allowed) == 50  # exactly the burst, never more


def test_invalid_limits_are_rejected() -> None:
    with pytest.raises(ValueError):
        RateLimiter("x", 0)
    with pytest.raises(ValueError):
        RateLimiter("x", 1, max_keys=0)


def test_session_keys_never_contain_the_token() -> None:
    key = session_key("sid", "cst_secret")

    assert "cst_secret" not in key and key.startswith("sid:")
    assert key != session_key("sid", "cst_other")


def test_limits_can_be_switched_off(make_settings: Callable[..., Settings]) -> None:
    limits = RateLimits.from_settings(make_settings(rate_limit_enabled=False))

    assert (limits.requests, limits.new_conversations, limits.chat_messages) == (None, None, None)


@pytest.mark.parametrize(
    "setting",
    [
        "rate_limit_requests_per_minute",
        "rate_limit_new_conversations_per_minute",
        "rate_limit_chat_messages_per_minute",
    ],
)
def test_limit_settings_are_validated(setting: str) -> None:
    with pytest.raises(ValidationError, match=setting):
        Settings(_env_file=None, **{setting: 0})


# --- Task 11: rate limits (HTTP) --------------------------------------------------------


def limited_app(
    make_settings: Callable[..., Settings], faq_service: FAQService, **limits: int
) -> Any:
    return TestClient(build_app(make_settings, faq_service, **limits))


def assert_rate_limited(response: Any) -> None:
    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == "RATE_LIMITED"
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert int(response.headers["Retry-After"]) >= 1


def test_per_ip_request_limit_spares_health_checks(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    with limited_app(make_settings, faq_service, rate_limit_requests_per_minute=3) as client:
        for _ in range(3):
            assert client.get("/api/v1/faqs").status_code == 200
        assert_rate_limited(client.get("/api/v1/faqs"))
        assert_rate_limited(client.post("/api/v1/answer", json={"query": "hi"}))
        assert client.get("/api/v1/health").status_code == 200  # exempt


def test_conversation_creation_limit(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    with limited_app(make_settings, faq_service, rate_limit_new_conversations_per_minute=2) as c:
        assert c.post(BASE).status_code == c.post(BASE).status_code == 201
        assert_rate_limited(c.post(BASE))


def test_chat_limit_is_per_conversation_and_token(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    with limited_app(make_settings, faq_service, rate_limit_chat_messages_per_minute=2) as c:
        sid, headers = create(c)
        other_sid, other_headers = create(c)
        wrong = {"Authorization": "Bearer cst_" + "x" * 43}
        url = f"{BASE}/{sid}/chat"

        for _ in range(3):  # someone guessing tokens for this session ...
            assert c.post(url, json={"message": QUESTION}, headers=wrong).status_code in {404, 429}
        # ... does not use up the owner's allowance
        assert c.post(url, json={"message": QUESTION}, headers=headers).status_code == 200
        assert c.post(url, json={"message": QUESTION}, headers=headers).status_code == 200
        refused = c.post(url, json={"message": QUESTION}, headers=headers)
        assert_rate_limited(refused)
        assert refused.headers["Cache-Control"] == "no-store"
        other = c.post(
            f"{BASE}/{other_sid}/chat", json={"message": QUESTION}, headers=other_headers
        )
        assert other.status_code == 200  # other conversations are unaffected
        assert c.get(f"{BASE}/{sid}/messages", headers=headers).json()["total_messages"] == 4


def test_a_missing_token_is_401_before_the_chat_limit(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    with limited_app(make_settings, faq_service, rate_limit_chat_messages_per_minute=1) as c:
        sid, _ = create(c)
        for _ in range(3):
            assert c.post(f"{BASE}/{sid}/chat", json={"message": QUESTION}).status_code == 401


def test_clients_cannot_supply_usage_or_token_counts(api: TestClient) -> None:
    sid, headers = create(api)
    body = {"message": QUESTION, "usage": {"input_tokens": 0}, "max_output_tokens": 100000}

    response = api.post(f"{BASE}/{sid}/chat", json=body, headers=headers)

    assert response.status_code == 422
    fields = {tuple(d["loc"]) for d in response.json()["error"]["details"]}
    assert fields == {("body", "usage"), ("body", "max_output_tokens")}


def test_default_limits_are_on_and_documented_as_process_local() -> None:
    settings = Settings(_env_file=None)
    limits = RateLimits.from_settings(settings)

    assert settings.rate_limit_enabled
    assert limits.requests is not None and limits.requests.capacity == 120
    assert limits.new_conversations is not None and limits.new_conversations.capacity == 20
    assert limits.chat_messages is not None and limits.chat_messages.capacity == 20
    assert "Process-local and in memory" in (rate_limit_module.__doc__ or "")
