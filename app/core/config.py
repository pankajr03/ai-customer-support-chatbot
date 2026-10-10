"""Application settings loaded from environment variables and an optional .env file."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Typed, validated configuration.

    Values are read from (highest priority first): real environment variables,
    the .env file, then the defaults below. Invalid values fail at startup.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AI Customer Support Chatbot API"
    app_version: str = "0.1.0"
    service_name: str = "ai-customer-support-chatbot"
    environment: Environment = "development"
    log_level: LogLevel = "INFO"
    api_v1_prefix: str = "/api/v1"
    cors_origins: list[str] = Field(default_factory=list)

    @property
    def docs_enabled(self) -> bool:
        """Interactive API docs are exposed everywhere except production."""
        return self.environment != "production"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance so the environment is parsed only once."""
    return Settings()
