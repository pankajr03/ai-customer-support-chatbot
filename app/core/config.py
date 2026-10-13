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
    # Hugging Face model ID (or local folder) for semantic search. Only loaded when first
    # needed, never at import time; downloaded and cached on first use if not present.
    embedding_model_name: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2", min_length=1, max_length=200
    )
    # Build the semantic index at startup (in a worker thread) instead of on first use.
    # Slower startup, but the first semantic search is fast.
    semantic_index_preload: bool = False
    # After a failed model load, answer semantic requests with 503 for this many seconds
    # before trying again, so queued requests do not each repeat a slow failing download.
    embedding_load_retry_seconds: float = Field(default=30.0, ge=0, le=3600, allow_inf_nan=False)
    # Default result filter of the POST /semantic-search endpoint (cosine, -1..1).
    # EXPERIMENTAL; -1 effectively disables filtering. Not used by the answer decision,
    # which has its own ANSWER_MIN_SIMILARITY.
    semantic_min_similarity: float = Field(default=0.40, ge=-1.0, le=1.0, allow_inf_nan=False)

    # --- Decision thresholds ------------------------------------------------------------
    # Three separate responsibilities. All values are EXPERIMENTAL starting points, not
    # universally reliable: calibrate them with a labelled evaluation dataset for each
    # model and knowledge base (README, "Decision thresholds").
    #
    # 1. Retrieval ranking: orders candidates (Reciprocal Rank Fusion). Never decides
    #    whether to answer.
    hybrid_rrf_k: int = Field(default=60, ge=1, le=1000)
    # Candidates taken from each retriever before fusion (also the largest hybrid top_k).
    hybrid_candidate_depth: int = Field(default=20, ge=2, le=50)
    # How many top-ranked candidates the answer decision evaluates (<= candidate depth).
    answer_candidates: int = Field(default=5, ge=2, le=50)
    #
    # 2. Answer acceptance: is a candidate relevant enough to show?
    answer_min_similarity: float = Field(default=0.40, ge=-1.0, le=1.0, allow_inf_nan=False)
    # Treat an exact (normalised) question match as sufficient evidence on its own.
    answer_accept_exact_question: bool = True
    # Semantic model unavailable: keyword-only answers need a clear keyword score at least
    # this high (40 = the whole query appears in the FAQ question) ...
    degraded_min_keyword_score: int = Field(default=40, ge=1, le=1000)
    # ... and at least this many meaningful (non-stopword) words in the query.
    degraded_min_query_terms: int = Field(default=2, ge=1, le=20)
    #
    # 3. Ambiguity detection: are acceptable candidates too close to choose between?
    # Another acceptable FAQ within this cosine margin of the top one (or above it) means
    # "ask the customer to clarify".
    answer_ambiguity_margin: float = Field(default=0.05, ge=0.0, le=1.0, allow_inf_nan=False)
    answer_max_clarification_options: int = Field(default=3, ge=2, le=10)
    #
    # Fallback messages shown when no FAQ answer is given (presentation, not thresholds).
    fallback_no_match_message: str = Field(
        default=(
            "Sorry, I couldn't find a reliable answer to that question. "
            "Please contact our support team for assistance."
        ),
        min_length=1,
        max_length=500,
    )
    fallback_unavailable_message: str = Field(
        default=(
            "Sorry, I can't search our help articles right now. "
            "Please try again in a few minutes or contact our support team for assistance."
        ),
        min_length=1,
        max_length=500,
    )

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

    @field_validator(
        "app_name",
        "embedding_model_name",
        "fallback_no_match_message",
        "fallback_unavailable_message",
        mode="before",
    )
    @classmethod
    def _strip_text(cls, value: object) -> object:
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

    @model_validator(mode="after")
    def _candidates_within_retrieval_depth(self) -> Self:
        if self.answer_candidates > self.hybrid_candidate_depth:
            raise ValueError(
                f"ANSWER_CANDIDATES ({self.answer_candidates}) must not exceed "
                f"HYBRID_CANDIDATE_DEPTH ({self.hybrid_candidate_depth})."
            )
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
            "embedding_model_name": self.embedding_model_name,
            "semantic_index_preload": self.semantic_index_preload,
            "embedding_load_retry_seconds": self.embedding_load_retry_seconds,
            "semantic_min_similarity": self.semantic_min_similarity,
            "hybrid_rrf_k": self.hybrid_rrf_k,
            "hybrid_candidate_depth": self.hybrid_candidate_depth,
            "answer_candidates": self.answer_candidates,
            "answer_min_similarity": self.answer_min_similarity,
            "answer_accept_exact_question": self.answer_accept_exact_question,
            "degraded_min_keyword_score": self.degraded_min_keyword_score,
            "degraded_min_query_terms": self.degraded_min_query_terms,
            "answer_ambiguity_margin": self.answer_ambiguity_margin,
            "answer_max_clarification_options": self.answer_max_clarification_options,
            "openai_api_key_set": self.openai_api_key is not None,
        }


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance so the environment is parsed only once."""
    return Settings()
