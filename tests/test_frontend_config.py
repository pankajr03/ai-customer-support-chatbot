"""Frontend settings and package boundaries (Stage 8, Task 2)."""

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from frontend.config import FrontendSettings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_env_example_holds_the_defaults() -> None:
    example = FrontendSettings(_env_file=PROJECT_ROOT / ".env.example")
    assert example == FrontendSettings(_env_file=None)
    assert example.base_url == "http://127.0.0.1:8000"
    assert example.support_contact_url is None  # empty means unset


def test_environment_variables_configure_the_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKEND_API_URL", "https://support.example.com/chat/")
    monkeypatch.setenv("BACKEND_API_PREFIX", "/v2")
    monkeypatch.setenv("BACKEND_READ_TIMEOUT_SECONDS", "90")
    settings = FrontendSettings(_env_file=None)
    assert settings.base_url == "https://support.example.com/chat"
    assert settings.backend_api_prefix == "/v2"
    assert settings.backend_read_timeout_seconds == 90


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("BACKEND_API_URL", "localhost:8000"),
        ("BACKEND_API_URL", "ftp://example.com"),
        ("BACKEND_API_PREFIX", "api/v1"),
        ("BACKEND_API_PREFIX", "/api/v1/"),
        ("BACKEND_READ_TIMEOUT_SECONDS", "0"),
        ("BACKEND_MAX_RETRIES", "50"),
        ("EXAMPLE_QUESTIONS", '[""]'),
    ],
)
def test_invalid_values_fail_fast(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValidationError):
        FrontendSettings(_env_file=None)


@pytest.mark.parametrize(
    ("url", "unencrypted"),
    [
        ("http://127.0.0.1:8000", False),
        ("http://localhost:8000", False),
        ("http://[::1]:8000", False),
        ("https://api.example.com", False),
        ("http://api.example.com", True),
        ("http://10.0.0.5:8000", True),
    ],
)
def test_plain_http_to_a_remote_backend_is_flagged(url: str, unencrypted: bool) -> None:
    settings = FrontendSettings(_env_file=None, backend_api_url=url)  # type: ignore[arg-type]
    assert settings.uses_plain_http_remotely is unencrypted


def test_the_frontend_never_imports_the_backend_or_ai_libraries() -> None:
    """The page reaches the backend over HTTP only: no app.*, models or OpenAI client."""
    code = (
        "import sys, frontend.api_client, frontend.state, frontend.actions, "
        "frontend.formatting, frontend.resources, frontend.components.chat, "
        "frontend.components.sidebar\n"
        "bad = sorted(m for m in sys.modules if m == 'app' or m.startswith('app.') "
        "or m.split('.')[0] in ('torch', 'sentence_transformers', 'openai', 'tiktoken'))\n"
        "print(','.join(bad))"
    )
    result = subprocess.run(  # noqa: S603 - our own interpreter and code
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert result.stdout.strip() == ""
