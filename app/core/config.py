"""Application settings loaded from environment variables and an optional .env file.

How values are resolved (highest priority first):

1. Arguments passed directly to ``Settings(...)`` (used by tests).
2. Real environment variables, e.g. ``$env:APP_ENV = "staging"`` in PowerShell.
3. The ``.env`` file in the project root.
4. The defaults defined on ``Settings`` below.

Variable names are case-insensitive and match the field names (``app_env`` -> ``APP_ENV``).
Every value is validated when the app starts, so a typo fails immediately instead of
causing surprising behaviour later.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import __version__

# Resolve .env relative to the project root, not the current working directory,
# so the same file is found whether you run uvicorn, pytest or a script.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

AppEnv = Literal["development", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Typed, validated application configuration."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = Field(default="AI Customer Support Chatbot API", min_length=1, max_length=100)
    app_env: AppEnv = "development"
    debug: bool = False
    log_level: LogLevel = "INFO"
    api_v1_prefix: str = Field(default="/api/v1", pattern=r"^(/[A-Za-z0-9_-]+)+$")
    service_name: str = Field(default="ai-customer-support-chatbot", pattern=r"^[a-z0-9-]+$")
    cors_origins: list[str] = Field(default_factory=list)
    # Relative paths are resolved against the project root, not the working directory.
    faq_data_path: Path = Path("data/faqs.json")

    # Optional until the AI stage. SecretStr masks the value in repr(), str() and logs.
    openai_api_key: SecretStr | None = None

    @field_validator("app_env", mode="before")
    @classmethod
    def _normalise_app_env(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("app_name", mode="before")
    @classmethod
    def _strip_app_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("cors_origins")
    @classmethod
    def _validate_cors_origins(cls, origins: list[str]) -> list[str]:
        for origin in origins:
            if not origin.startswith(("http://", "https://")) or origin.endswith("/"):
                raise ValueError(
                    f"Invalid CORS origin {origin!r}: use a full origin such as "
                    "'https://example.com' (no wildcard, no trailing slash)."
                )
        return origins

    @field_validator("openai_api_key", mode="before")
    @classmethod
    def _empty_key_is_none(cls, value: object) -> object:
        # Treat "OPENAI_API_KEY=" in .env as "not set" rather than an empty secret.
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _forbid_debug_in_production(self) -> Self:
        if self.app_env == "production" and self.debug:
            raise ValueError("DEBUG must be false when APP_ENV=production.")
        return self

    @property
    def app_version(self) -> str:
        """Version comes from the code, not the environment, so it cannot drift."""
        return __version__

    @property
    def docs_enabled(self) -> bool:
        """Interactive API docs are exposed everywhere except production."""
        return self.app_env != "production"

    def safe_summary(self) -> dict[str, object]:
        """Non-secret settings that are safe to write to logs."""
        return {
            "app_env": self.app_env,
            "debug": self.debug,
            "log_level": self.log_level,
            "api_v1_prefix": self.api_v1_prefix,
            "cors_origins": self.cors_origins,
            "faq_data_path": self.faq_data_path.as_posix(),
            "openai_api_key_set": self.openai_api_key is not None,
        }


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance so the environment is parsed only once."""
    return Settings()
