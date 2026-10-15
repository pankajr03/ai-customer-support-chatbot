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
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app import __version__
from app.models.usage import ModelPricing

# Resolve .env relative to the project root, not the current working directory,
# so the same file is found whether you run uvicorn, pytest or a script.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

# Real OpenAI keys are far longer; this only catches truncated values and placeholders.
MIN_API_KEY_LENGTH = 20
# Customer chat messages: the configurable limit's default and its ceiling, which is the
# storage limit for a message (MAX_USER_MESSAGE_LENGTH in app.models.conversation).
DEFAULT_MAX_CUSTOMER_MESSAGE_CHARACTERS = 4_000
MAX_CUSTOMER_MESSAGE_CEILING = 10_000
# For checking that MAX_REQUEST_BODY_BYTES fits the longest message: UTF-8 needs up to 4
# bytes per character; the overhead covers the JSON keys, client_message_id and category.
MAX_UTF8_BYTES_PER_CHARACTER = 4
REQUEST_BODY_OVERHEAD_BYTES = 1_024
# Longest wait between two attempts of one LLM call (used by app.services.llm_service).
LLM_MAX_BACKOFF_SECONDS = 4.0

AppEnv = Literal["development", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Typed, validated application configuration."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        # Errors name the setting and the rule but never echo values: a model-level error
        # would otherwise print every input, including OPENAI_API_KEY.
        hide_input_in_errors=True,
    )

    app_name: str = Field(default="AI Customer Support Chatbot API", min_length=1, max_length=100)
    app_env: AppEnv = "development"
    debug: bool = False
    log_level: LogLevel = "INFO"
    api_v1_prefix: str = Field(default="/api/v1", pattern=r"^(/[A-Za-z0-9_-]+)+$")
    service_name: str = Field(default="ai-customer-support-chatbot", pattern=r"^[a-z0-9-]+$")
    # Browser origins allowed to call the API (CORS). Empty = none: the safe default,
    # also for production. Explicit origins only; "*" is rejected (checked below).
    cors_origins: list[str] = Field(default_factory=list)
    # Largest accepted request body. API requests are small JSON documents (a chat message
    # is at most MAX_CUSTOMER_MESSAGE_CHARACTERS), so anything far larger is rejected with
    # 413 unparsed. Must fit the longest allowed chat message (checked below).
    max_request_body_bytes: int = Field(default=32_768, ge=1_024, le=1_048_576)
    # Longest customer chat message, in characters after surrounding whitespace is
    # removed (100 to 10,000, the storage limit). Longer messages get 422. Only the first
    # 1,000 characters are used for search; the whole message is stored.
    max_customer_message_characters: int = Field(
        default=DEFAULT_MAX_CUSTOMER_MESSAGE_CHARACTERS, ge=100, le=MAX_CUSTOMER_MESSAGE_CEILING
    )
    # Basic request-rate limits (process-local, in memory; see app/core/rate_limit.py).
    # A prototype safeguard against runaway clients and casual abuse, not production
    # protection: counters reset on restart and are not shared between workers.
    rate_limit_enabled: bool = True
    # Every API request except health checks, per client IP address.
    rate_limit_requests_per_minute: int = Field(default=120, ge=1, le=100_000)
    # POST /conversations, per client IP address.
    rate_limit_new_conversations_per_minute: int = Field(default=20, ge=1, le=10_000)
    # POST /conversations/{id}/chat, per conversation (and access token).
    rate_limit_chat_messages_per_minute: int = Field(default=20, ge=1, le=10_000)
    # Relative paths are resolved against the project root, not the working directory.
    faq_data_path: Path = Path("data/faqs.json")
    # Conversation storage (Stage 6). SQLite only, for development and small demos; a
    # relative file path is resolved against the project root. "sqlite://" = in memory.
    database_url: str = Field(default="sqlite:///data/conversations.db", min_length=1)
    # A conversation stops accepting messages this long after it was created (1 minute to
    # 7 days). Its history stays readable (with its access token) until it is deleted.
    conversation_ttl_minutes: int = Field(default=60, ge=1, le=10_080)
    # While one message is being answered, other messages to the same conversation get
    # 409. The lock (a lease in the database) expires after this many seconds even if the
    # process died; it must outlast the slowest answer (checked below).
    conversation_processing_lease_seconds: int = Field(default=180, ge=10, le=900)
    # Delete expired conversations (and their messages) when the app starts. The same
    # cleanup can be run any time with: python -m scripts.cleanup_conversations
    conversation_cleanup_on_startup: bool = True
    # Recent history given as context for a new message: at most this many messages ...
    conversation_history_max_messages: int = Field(default=10, ge=1, le=50)
    # ... totalling at most this many characters of message text. Older messages are left
    # out first; messages are never cut in half.
    conversation_history_max_chars: int = Field(default=4000, ge=200, le=50_000)
    # A single history message longer than this (in characters) is shortened to its
    # beginning, so one long reply cannot crowd out the rest of the conversation.
    conversation_history_max_message_chars: int = Field(default=1000, ge=100, le=10_000)
    # TOKEN limit (not characters) for an LLM request that includes history: system
    # instructions, evidence, the new message and the reserved output come first; history
    # gets what is left, newest first. Counted with CONTEXT_TOKENIZER.
    conversation_context_max_tokens: int = Field(default=3000, ge=500, le=200_000)
    # "estimate": UTF-8 bytes / 3, no dependency (usually over-counts English).
    # "tiktoken": exact counts for OpenAI models with TIKTOKEN_ENCODING (downloaded once).
    context_tokenizer: Literal["estimate", "tiktoken"] = "estimate"
    tiktoken_encoding: str = Field(default="o200k_base", pattern=r"^[a-z0-9_]{1,50}$")
    # Rewrite follow-up messages ("what about after that?") into standalone search queries
    # with the LLM when it is enabled; otherwise a conservative rule is used.
    query_rewrite_use_llm: bool = True
    # Most recent history turns considered when rewriting (within the history limits).
    query_rewrite_max_turns: int = Field(default=6, ge=1, le=20)
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

    # --- OpenAI (Stage 5) -----------------------------------------------------------------
    # LLM generation is off unless explicitly enabled. When off, no key is needed and every
    # Stage 1-4 endpoint works normally. When on, a valid key is required at startup.
    llm_enabled: bool = False
    # Secret. SecretStr masks the value in repr(), str() and logs; settings errors never
    # echo it (hide_input_in_errors above).
    openai_api_key: SecretStr | None = None
    # Text model for the Responses API. A small, fast model suits short grounded replies.
    openai_model: str = Field(default="gpt-5.4-mini", min_length=1, max_length=100)
    # Per-attempt timeout; the SDK default (10 minutes) is far too long for a chat reply.
    openai_timeout_seconds: float = Field(default=20.0, gt=0, le=300, allow_inf_nan=False)
    # Extra attempts after a transient failure (timeouts, 408/409/429/5xx), with backoff.
    openai_max_retries: int = Field(default=2, ge=0, le=5)
    # Upper bound on generated tokens per reply (includes reasoning tokens).
    openai_max_output_tokens: int = Field(default=800, ge=16, le=4_096)
    # Reasoning effort for reasoning models; empty = not sent (for models without it).
    openai_reasoning_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None
    ) = "low"
    # Sampling temperature (0-2). Unset = not sent (the model's default). Reasoning models
    # accept it only with OPENAI_REASONING_EFFORT=none (or empty), which is checked below.
    llm_temperature: float | None = Field(default=None, ge=0.0, le=2.0, allow_inf_nan=False)
    # Circuit breaker: after this many consecutive failed LLM calls (configuration errors
    # such as an invalid key count at once), pause LLM calls ...
    llm_failure_threshold: int = Field(default=3, ge=1, le=20)
    # ... for this many seconds; customers get the approved FAQ answer meanwhile. 0 = off.
    llm_failure_cooldown_seconds: float = Field(default=60.0, ge=0, le=3600, allow_inf_nan=False)
    # Grounded context: only the FAQs retrieval selected for a question, never the whole
    # knowledge base. At most this many FAQs per prompt ...
    llm_context_max_faqs: int = Field(default=3, ge=1, le=10)
    # ... within this many characters of rendered evidence (~4 characters per token).
    # Approved answers are never truncated: an FAQ that does not fit is left out.
    llm_context_max_chars: int = Field(default=6000, ge=500, le=50_000)
    # Prices for usage cost *estimates*, per million tokens, keyed by model name (JSON, see
    # .env.example). Nothing is built in: prices change. Unpriced models report no cost.
    llm_pricing: dict[str, ModelPricing] = Field(default_factory=dict)
    # Currency label for those prices (ISO 4217 code, e.g. USD or EUR).
    llm_pricing_currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")

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

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        value = value.strip()
        try:
            url = make_url(value)
        except ArgumentError:
            raise ValueError("DATABASE_URL is not a valid database URL") from None
        if url.get_backend_name() != "sqlite":
            raise ValueError(
                "DATABASE_URL must be a SQLite URL such as 'sqlite:///data/conversations.db'; "
                "other databases are not supported yet."
            )
        return value

    @field_validator("openai_api_key", "openai_reasoning_effort", "llm_temperature", mode="before")
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        # Treat "OPENAI_API_KEY=" in .env as "not set" rather than an empty secret, and drop
        # whitespace pasted around a key.
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("openai_model", "openai_reasoning_effort", mode="before")
    @classmethod
    def _strip_model(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("llm_pricing")
    @classmethod
    def _pricing_keys_are_model_names(
        cls, pricing: dict[str, ModelPricing]
    ) -> dict[str, ModelPricing]:
        cleaned = {name.strip(): price for name, price in pricing.items()}
        if any(not name or len(name) > 100 for name in cleaned):
            raise ValueError("LLM_PRICING keys must be model names (1-100 characters)")
        return cleaned

    @model_validator(mode="after")
    def _llm_configuration_is_usable(self) -> Self:
        """Fail at startup, with a clear message, rather than on the first customer request.

        Messages never include the key itself.
        """
        if self.llm_enabled:
            key = self.openai_api_key.get_secret_value() if self.openai_api_key else ""
            if not key:
                raise ValueError(
                    "LLM_ENABLED=true requires OPENAI_API_KEY. Set it in .env or the "
                    "environment, or set LLM_ENABLED=false."
                )
            if any(character.isspace() for character in key):
                raise ValueError("OPENAI_API_KEY must not contain spaces or line breaks.")
            if len(key) < MIN_API_KEY_LENGTH:
                raise ValueError(
                    f"OPENAI_API_KEY looks incomplete (shorter than {MIN_API_KEY_LENGTH} "
                    "characters). Copy the full key from the OpenAI dashboard."
                )
        if self.llm_temperature is not None and self.openai_reasoning_effort not in (None, "none"):
            raise ValueError(
                "LLM_TEMPERATURE is only supported with OPENAI_REASONING_EFFORT=none or empty "
                "(reasoning models reject it). Unset LLM_TEMPERATURE or change the effort."
            )
        return self

    @model_validator(mode="after")
    def _lease_outlasts_an_answer(self) -> Self:
        """A turn can make two LLM calls (query rewrite, answer), each with retries and
        the waits between them. (A turn whose lease runs out anyway stores nothing if
        another message took the lease meanwhile; see ConversationRepository.)"""
        if self.llm_enabled:
            calls = 2 if self.query_rewrite_use_llm else 1
            attempts = self.openai_max_retries + 1
            backoff = self.openai_max_retries * LLM_MAX_BACKOFF_SECONDS
            slowest = calls * (self.openai_timeout_seconds * attempts + backoff)
            if self.conversation_processing_lease_seconds < slowest:
                raise ValueError(
                    f"CONVERSATION_PROCESSING_LEASE_SECONDS "
                    f"({self.conversation_processing_lease_seconds}) must be at least "
                    f"{slowest:.0f}: the longest a message can take with the LLM settings "
                    "(OPENAI_TIMEOUT_SECONDS x attempts plus the waits between them, for "
                    "each LLM call in a turn)."
                )
        return self

    @model_validator(mode="after")
    def _body_limit_fits_a_message(self) -> Self:
        """A message of the maximum length must not be refused as too large (413)."""
        needed = self.max_customer_message_characters * MAX_UTF8_BYTES_PER_CHARACTER
        needed += REQUEST_BODY_OVERHEAD_BYTES
        if self.max_request_body_bytes < needed:
            raise ValueError(
                f"MAX_REQUEST_BODY_BYTES ({self.max_request_body_bytes}) must be at least "
                f"{needed} to fit a chat message of MAX_CUSTOMER_MESSAGE_CHARACTERS "
                f"({self.max_customer_message_characters}) characters of up to "
                f"{MAX_UTF8_BYTES_PER_CHARACTER} bytes each, plus the rest of the request."
            )
        return self

    @model_validator(mode="after")
    def _forbid_debug_in_production(self) -> Self:
        if self.app_env == "production" and self.debug:
            raise ValueError("DEBUG must be false when APP_ENV=production.")
        return self

    @model_validator(mode="after")
    def _https_origins_in_production(self) -> Self:
        """A production frontend is served over HTTPS; a plain-HTTP or localhost origin
        there is almost certainly a development value left in place."""
        if self.app_env == "production":
            insecure = [o for o in self.cors_origins if not o.startswith("https://")]
            if insecure:
                raise ValueError(
                    f"CORS_ORIGINS must use https:// when APP_ENV=production: {insecure}"
                )
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
            "max_request_body_bytes": self.max_request_body_bytes,
            "max_customer_message_characters": self.max_customer_message_characters,
            "rate_limit_enabled": self.rate_limit_enabled,
            "rate_limit_requests_per_minute": self.rate_limit_requests_per_minute,
            "rate_limit_new_conversations_per_minute": (
                self.rate_limit_new_conversations_per_minute
            ),
            "rate_limit_chat_messages_per_minute": self.rate_limit_chat_messages_per_minute,
            "faq_data_path": self.faq_data_path.as_posix(),
            "database_url": make_url(self.database_url).render_as_string(hide_password=True),
            "conversation_ttl_minutes": self.conversation_ttl_minutes,
            "conversation_processing_lease_seconds": self.conversation_processing_lease_seconds,
            "conversation_cleanup_on_startup": self.conversation_cleanup_on_startup,
            "conversation_history_max_messages": self.conversation_history_max_messages,
            "conversation_history_max_chars": self.conversation_history_max_chars,
            "conversation_history_max_message_chars": (self.conversation_history_max_message_chars),
            "conversation_context_max_tokens": self.conversation_context_max_tokens,
            "context_tokenizer": self.context_tokenizer,
            "tiktoken_encoding": self.tiktoken_encoding,
            "query_rewrite_use_llm": self.query_rewrite_use_llm,
            "query_rewrite_max_turns": self.query_rewrite_max_turns,
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
            "llm_enabled": self.llm_enabled,
            "openai_api_key_set": self.openai_api_key is not None,
            "openai_model": self.openai_model,
            "openai_timeout_seconds": self.openai_timeout_seconds,
            "openai_max_retries": self.openai_max_retries,
            "openai_max_output_tokens": self.openai_max_output_tokens,
            "openai_reasoning_effort": self.openai_reasoning_effort,
            "llm_temperature": self.llm_temperature,
            "llm_failure_threshold": self.llm_failure_threshold,
            "llm_failure_cooldown_seconds": self.llm_failure_cooldown_seconds,
            "llm_context_max_faqs": self.llm_context_max_faqs,
            "llm_context_max_chars": self.llm_context_max_chars,
            "llm_priced_models": sorted(self.llm_pricing),
            "llm_pricing_currency": self.llm_pricing_currency,
        }


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance so the environment is parsed only once."""
    return Settings()
