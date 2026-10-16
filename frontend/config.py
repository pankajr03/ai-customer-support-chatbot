"""Frontend settings (Stage 8), read from the environment or the project's ``.env``.

The frontend has its own small settings class instead of importing ``app.core.config``:
it talks to the backend only over HTTP, so it can run on another machine with nothing
of the backend installed except this folder's dependencies.

Every variable is optional; the defaults suit a backend on ``http://127.0.0.1:8000``.
The ``.env`` file is found relative to the project root, like the backend's, so the page
reads the same file whichever directory Streamlit is started from.
"""

import ipaddress
import logging
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AfterValidator, Field, HttpUrl, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"

LOCAL_HOSTS = frozenset({"localhost"})


def _no_trailing_slash(value: str) -> str:
    if not value.startswith("/") or (len(value) > 1 and value.endswith("/")):
        raise ValueError('must start with "/" and have no trailing slash')
    return value


class FrontendSettings(BaseSettings):
    """Where the backend is and how long to wait for it."""

    # The backend's .env is shared during development, so its variables are ignored.
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    backend_api_url: HttpUrl = Field(
        default=HttpUrl("http://127.0.0.1:8000"),
        description="Base URL of the FastAPI backend (scheme, host, port; optional path).",
    )
    backend_api_prefix: Annotated[str, AfterValidator(_no_trailing_slash)] = Field(
        default="/api/v1", description="The backend's API_V1_PREFIX."
    )
    backend_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    # A chat reply can include an LLM call (OPENAI_TIMEOUT_SECONDS x retries) and query
    # rewriting, so the read timeout is generous. Other requests are fast.
    backend_read_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    # Automatic retries of one chat message (always with the same client_message_id)
    # after a network error, 409 REQUEST_IN_PROGRESS, 429 or 503.
    backend_max_retries: int = Field(default=2, ge=0, le=5)
    # Longest Retry-After the page waits for by itself; longer ones are shown to the
    # customer instead, so the page never hangs.
    backend_max_retry_wait_seconds: float = Field(default=5.0, ge=0, le=30)
    # Should match the backend's MAX_CUSTOMER_MESSAGE_CHARACTERS (the backend enforces it).
    max_message_characters: int = Field(default=4000, ge=100, le=10_000)
    # Optional "contact support" link offered when no reliable answer is found.
    support_contact_url: HttpUrl | None = None
    # Shown as one-click suggestions on an empty conversation (JSON list in the
    # environment). The defaults match the sample knowledge base in data/faqs.json.
    example_questions: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default=[
            "How can I track my order?",
            "What is your return policy?",
            "How do I reset my password?",
        ],
        max_length=6,
    )

    @field_validator("support_contact_url", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @property
    def base_url(self) -> str:
        return str(self.backend_api_url).rstrip("/")

    @property
    def uses_plain_http_remotely(self) -> bool:
        """True when tokens would cross a network unencrypted (http:// to a remote host)."""
        parts = urlsplit(self.base_url)
        if parts.scheme != "http":
            return False
        host = parts.hostname or ""
        if host in LOCAL_HOSTS:
            return False
        try:
            return not ipaddress.ip_address(host).is_loopback
        except ValueError:
            return True


def describe_settings_error(exc: ValidationError) -> str:
    """Which variables are invalid and why, without their values."""
    problems = [
        f"{str(error['loc'][0]).upper() if error['loc'] else 'settings'}: {error['msg']}"
        for error in exc.errors(include_url=False, include_input=False)
    ]
    return f"Invalid frontend settings in .env or the environment: {'; '.join(problems)}"


@lru_cache
def get_frontend_settings() -> FrontendSettings:
    settings = FrontendSettings()
    if settings.uses_plain_http_remotely:
        logger.warning(
            "BACKEND_API_URL uses http:// to a remote host: conversation tokens are sent "
            "unencrypted. Use https:// outside local development."
        )
    return settings
