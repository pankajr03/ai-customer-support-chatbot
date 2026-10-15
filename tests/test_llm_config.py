"""LLM configuration: optional when disabled, validated when enabled, never leaked.

No test here calls OpenAI: the suite blocks all network access, and the LLM client is
never created unless a test injects a fake one.
"""

import re
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.core.config import MIN_API_KEY_LENGTH, PROJECT_ROOT, Settings
from app.main import create_app
from app.services.embedding_service import EmbeddingService
from app.services.faq_service import FAQService
from app.services.llm_service import LLMDisabledError, LLMNotConfiguredError, LLMService
from app.services.semantic_search_service import SemanticSearchService
from tests.fakes import FAKE_MODEL_NAME, CountingLoader

API_KEY = "sk-proj-TESTKEY-0123456789abcdefghij"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"


def settings_from_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def validation_message(monkeypatch: pytest.MonkeyPatch, **env: str) -> str:
    with pytest.raises(ValidationError) as caught:
        settings_from_env(monkeypatch, **env)
    return str(caught.value)


# --- Defaults: disabled, no key required --------------------------------------------------------


def test_llm_is_disabled_by_default_and_needs_no_key(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings()

    assert (settings.llm_enabled, settings.openai_api_key, settings.llm_temperature) == (
        False,
        None,
        None,
    )


def test_a_key_may_be_set_while_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = settings_from_env(monkeypatch, OPENAI_API_KEY=API_KEY)

    assert settings.llm_enabled is False
    assert settings.openai_api_key is not None


@pytest.fixture
def no_key_app(make_settings: Callable[..., Settings], faq_service: FAQService) -> FastAPI:
    embeddings = EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader())
    semantic = SemanticSearchService(faq_service.list_active_faqs, embeddings)
    return create_app(make_settings(), faq_service=faq_service, semantic_search_service=semantic)


@pytest.fixture
def no_key_client(no_key_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(no_key_app) as client:
        yield client


def test_every_stage_1_to_4_endpoint_works_without_a_key(
    no_key_app: FastAPI, no_key_client: TestClient
) -> None:
    client = no_key_client
    checks = [
        client.get("/"),
        client.get("/api/v1/health"),
        client.get("/api/v1/faqs"),
        client.get("/api/v1/faqs/categories"),
        client.get("/api/v1/faqs/search", params={"q": "order"}),
        client.get("/api/v1/faqs/faq_101"),
        client.post("/api/v1/semantic-search", json={"query": "where is my order"}),
        client.post("/api/v1/hybrid-search", json={"query": "How do I reset my password?"}),
        client.get("/docs"),
        client.get("/openapi.json"),
    ]

    assert [response.status_code for response in checks] == [200] * len(checks)
    assert checks[7].json()["decision"] == "answered"
    llm = no_key_app.state.llm_service
    assert (llm.enabled, llm.is_configured, llm.client_created) == (False, False, False)


# --- Enabled: fail clearly on invalid configuration --------------------------------------------


def test_enabled_without_a_key_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    message = validation_message(monkeypatch, LLM_ENABLED="true")

    assert "LLM_ENABLED=true requires OPENAI_API_KEY" in message
    assert "LLM_ENABLED=false" in message  # tells the user how to opt out


@pytest.mark.parametrize("key", ["", "   "])
def test_enabled_with_an_empty_key_fails(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    assert "requires OPENAI_API_KEY" in validation_message(
        monkeypatch, LLM_ENABLED="true", OPENAI_API_KEY=key
    )


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("sk-proj-0123456789 abcdefghij", "must not contain spaces or line breaks"),
        ("sk-proj-0123456789\nabcdefghij", "must not contain spaces or line breaks"),
        ("sk-short", "looks incomplete"),
        ("changeme", "looks incomplete"),
    ],
)
def test_enabled_with_an_invalid_key_fails_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, key: str, expected: str
) -> None:
    message = validation_message(monkeypatch, LLM_ENABLED="true", OPENAI_API_KEY=key)

    assert expected in message
    for part in key.split():
        assert part not in message


def test_enabled_with_a_valid_key_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = settings_from_env(monkeypatch, LLM_ENABLED="true", OPENAI_API_KEY=f"  {API_KEY}\n")

    assert settings.llm_enabled is True
    assert settings.openai_api_key.get_secret_value() == API_KEY  # type: ignore[union-attr]
    assert len(API_KEY) >= MIN_API_KEY_LENGTH


def test_enabled_app_logs_that_generation_is_on_without_the_key(
    make_settings: Callable[..., Settings], capsys: pytest.CaptureFixture[str]
) -> None:
    # create_app() configures logging, so its startup lines go to captured stdout.
    app = create_app(make_settings(llm_enabled=True, openai_api_key=API_KEY))
    log = capsys.readouterr().out

    assert app.state.llm_service.is_available is True
    assert app.state.llm_service.client_created is False  # still lazy
    assert "LLM generation enabled: model=gpt-5.4-mini" in log
    assert "'llm_enabled': True" in log
    assert API_KEY not in log


def test_disabled_app_logs_that_generation_is_off(
    make_settings: Callable[..., Settings], capsys: pytest.CaptureFixture[str]
) -> None:
    create_app(make_settings(openai_api_key=API_KEY))
    log = capsys.readouterr().out

    assert "LLM generation disabled" in log
    assert API_KEY not in log


# --- Numeric ranges and temperature --------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OPENAI_TIMEOUT_SECONDS", "0"),
        ("OPENAI_TIMEOUT_SECONDS", "-5"),
        ("OPENAI_TIMEOUT_SECONDS", "301"),
        ("OPENAI_TIMEOUT_SECONDS", "inf"),
        ("OPENAI_MAX_RETRIES", "-1"),
        ("OPENAI_MAX_RETRIES", "6"),
        ("OPENAI_MAX_RETRIES", "two"),
        ("OPENAI_MAX_OUTPUT_TOKENS", "15"),
        ("OPENAI_MAX_OUTPUT_TOKENS", "4097"),
        ("LLM_TEMPERATURE", "-0.1"),
        ("LLM_TEMPERATURE", "2.1"),
        ("LLM_TEMPERATURE", "nan"),
        ("LLM_TEMPERATURE", "warm"),
        ("LLM_ENABLED", "maybe"),
    ],
)
def test_out_of_range_values_fail_and_name_the_setting(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    extra = {"OPENAI_REASONING_EFFORT": ""} if name == "LLM_TEMPERATURE" else {}

    message = validation_message(monkeypatch, **{name: value}, **extra)

    assert name.lower() in message


@pytest.mark.parametrize("effort", ["", "none"])
def test_temperature_is_allowed_without_reasoning(
    monkeypatch: pytest.MonkeyPatch, effort: str
) -> None:
    settings = settings_from_env(monkeypatch, LLM_TEMPERATURE="0.2", OPENAI_REASONING_EFFORT=effort)

    assert settings.llm_temperature == 0.2


@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high"])
def test_temperature_with_a_reasoning_effort_fails_clearly(
    monkeypatch: pytest.MonkeyPatch, effort: str
) -> None:
    message = validation_message(monkeypatch, LLM_TEMPERATURE="0.2", OPENAI_REASONING_EFFORT=effort)

    assert "LLM_TEMPERATURE is only supported with OPENAI_REASONING_EFFORT=none" in message


def test_empty_temperature_means_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    assert settings_from_env(monkeypatch, LLM_TEMPERATURE=" ").llm_temperature is None


# --- The key never leaks ------------------------------------------------------------------------


def test_settings_errors_never_echo_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model-level error used to print every input value, including the key."""
    message = validation_message(
        monkeypatch, OPENAI_API_KEY=API_KEY, APP_ENV="production", DEBUG="true"
    )

    assert "DEBUG must be false" in message
    assert API_KEY not in message and API_KEY[-10:] not in message


def test_field_errors_never_echo_values(monkeypatch: pytest.MonkeyPatch) -> None:
    message = validation_message(monkeypatch, OPENAI_API_KEY=API_KEY, OPENAI_MAX_RETRIES="99")

    assert "openai_max_retries" in message
    assert "99" not in message and API_KEY not in message


def test_key_is_masked_in_settings_and_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = settings_from_env(monkeypatch, OPENAI_API_KEY=API_KEY, LLM_ENABLED="true")

    for text in (
        repr(settings),
        str(settings),
        str(settings.model_dump()),
        str(settings.safe_summary()),
    ):
        assert API_KEY not in text
    assert settings.safe_summary()["openai_api_key_set"] is True
    assert "openai_api_key" not in settings.safe_summary()


def test_no_response_or_header_ever_contains_the_key(
    make_settings: Callable[..., Settings], faq_service: FAQService
) -> None:
    app = create_app(
        make_settings(llm_enabled=True, openai_api_key=API_KEY),
        faq_service=faq_service,
        semantic_search_service=SemanticSearchService(
            faq_service.list_active_faqs,
            EmbeddingService(FAKE_MODEL_NAME, loader=CountingLoader()),
        ),
    )
    with TestClient(app) as client:
        responses = [
            client.get("/"),
            client.get("/api/v1/health"),
            client.get("/openapi.json"),
            client.get("/docs"),
            client.get("/redoc"),
            client.get("/api/v1/faqs"),
            client.get("/api/v1/does-not-exist"),
            client.post("/api/v1/hybrid-search", json={"query": ""}),
            client.post("/api/v1/hybrid-search", json={"query": "order"}),
        ]

    for response in responses:
        assert API_KEY not in response.text
        assert API_KEY not in str(response.headers)
    paths = client.app.openapi()["paths"]  # type: ignore[attr-defined]
    assert not [path for path in paths if "config" in path or "settings" in path]


# --- LLMService honours LLM_ENABLED and LLM_TEMPERATURE ------------------------------------------


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

        class Responses:
            def create(inner, **kwargs: Any) -> Any:  # noqa: N805
                self.calls.append(kwargs)
                return type(
                    "R",
                    (),
                    {"status": "completed", "output_text": "ok", "output": [], "usage": None},
                )()

        self.responses = Responses()

    def close(self) -> None:
        pass


def test_disabled_service_refuses_even_with_a_key() -> None:
    created: list[str] = []
    llm = LLMService(
        SecretStr(API_KEY),
        model="gpt-5.4-mini",
        enabled=False,
        client_factory=lambda key: created.append(key) or FakeClient(),  # type: ignore[func-returns-value]
    )

    with pytest.raises(LLMDisabledError, match="LLM_ENABLED=false"):
        llm.generate(instructions="i", input="q")
    assert issubclass(LLMDisabledError, LLMNotConfiguredError)
    assert (llm.is_configured, llm.is_available, created) == (True, False, [])
    assert "enabled=False" in repr(llm)


@pytest.mark.parametrize(("temperature", "sent"), [(None, False), (0.0, True), (0.7, True)])
def test_temperature_is_sent_only_when_configured(temperature: float | None, sent: bool) -> None:
    client = FakeClient()
    llm = LLMService(
        SecretStr(API_KEY),
        model="gpt-4.1-mini",
        reasoning_effort=None,
        temperature=temperature,
        client_factory=lambda key: client,
    )

    llm.generate(instructions="i", input="q")

    assert ("temperature" in client.calls[0]) is sent
    if sent:
        assert client.calls[0]["temperature"] == temperature
    assert "reasoning" not in client.calls[0]


@pytest.mark.parametrize("temperature", [-0.1, 2.5, True, "0.5"])
def test_invalid_temperature_is_rejected_by_the_service(temperature: Any) -> None:
    with pytest.raises(ValueError, match="temperature"):
        LLMService(SecretStr(API_KEY), model="m", temperature=temperature)


def test_service_from_settings_carries_every_llm_setting(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings(
        llm_enabled=True,
        openai_api_key=API_KEY,
        openai_model="gpt-4.1-mini",
        openai_timeout_seconds=9,
        openai_max_retries=1,
        openai_max_output_tokens=256,
        openai_reasoning_effort=None,
        llm_temperature=0.3,
    )

    llm = LLMService.from_settings(settings)

    assert (
        llm.enabled,
        llm.model,
        llm.timeout_seconds,
        llm.max_retries,
        llm.max_output_tokens,
        llm.reasoning_effort,
        llm.temperature,
    ) == (True, "gpt-4.1-mini", 9, 1, 256, None, 0.3)


# --- .env.example ------------------------------------------------------------------------------


def test_env_example_is_valid_and_disables_the_llm() -> None:
    settings = Settings(_env_file=ENV_EXAMPLE)

    assert settings.llm_enabled is False
    assert settings.openai_api_key is None
    assert settings.llm_temperature is None


def test_env_example_lists_every_llm_setting_and_holds_no_real_key() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")

    for name in (
        "LLM_ENABLED",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "OPENAI_TIMEOUT_SECONDS",
        "OPENAI_MAX_OUTPUT_TOKENS",
        "OPENAI_MAX_RETRIES",
        "OPENAI_REASONING_EFFORT",
        "LLM_TEMPERATURE",
    ):
        assert re.search(rf"^{name}=", text, flags=re.M), name
    assert re.search(r"^OPENAI_API_KEY=\s*$", text, flags=re.M)  # empty placeholder
    assert not re.search(r"sk-[A-Za-z0-9_-]{16,}", text)  # nothing that looks like a key
