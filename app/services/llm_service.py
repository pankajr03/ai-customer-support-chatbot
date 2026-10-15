"""Reusable client for OpenAI text generation (Responses API).

One ``LLMService`` per application wraps one ``openai.OpenAI`` client. The client is
created lazily on the first request (so starting the app, running tests or using the
search endpoints never needs a key or the network) and then reused by every request:
it holds the HTTP connection pool and is safe to share between threads.

Configuration comes from ``Settings`` (environment / ``.env``). The API key is a
``SecretStr`` that is unwrapped only when the client is created; it never appears in
exceptions raised from this module, in ``repr`` output or in logs.

Timeouts and retries
--------------------
The SDK's default timeout is 10 minutes and it honours server ``Retry-After`` delays of
up to 2 minutes per retry: far too long for a customer waiting on a chat reply. So the
SDK's own retries are disabled (``max_retries=0``) and this module retries instead, with
the same rules but a bounded total time:

- Retried (transient): timeouts, connection errors, HTTP 408, 409, 429 and 5xx.
- Not retried: 400, 401, 403, 404, 422 and other client errors (a retry cannot fix an
  invalid key, a missing model or a malformed request), and 429 ``insufficient_quota``
  (the account is out of credit, not rate-limited).
- Backoff: exponential from ``INITIAL_BACKOFF_SECONDS`` with up to 25% jitter, capped at
  ``MAX_BACKOFF_SECONDS``. A server ``Retry-After`` is honoured only if it is within that
  cap; a longer one ends the retries at once and is reported as ``retry_after_seconds``.

Worst case per call: ``(max_retries + 1) x timeout`` plus at most
``max_retries x MAX_BACKOFF_SECONDS`` of waiting.

Privacy
-------
Requests are sent with ``store=False``, so OpenAI does not keep the conversation for
later retrieval. Logs contain the model, attempt count, timings, token counts and
OpenAI's request ID only: never the key, the instructions, the input or the output.
"""

import logging
import random
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import SecretStr

from app.core.config import Settings
from app.services.usage_tracker import TokenUsage, UsageTracker

logger = logging.getLogger(__name__)

INITIAL_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 4.0
RETRYABLE_STATUS_CODES = frozenset({408, 409, 429})
NON_RETRYABLE_ERROR_CODES = frozenset({"insufficient_quota"})
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


# --- Errors (never contain the key, the prompt or the provider's message text) -----------------


class LLMError(Exception):
    """Base class for LLM failures."""


class LLMNotConfiguredError(LLMError):
    """No API key is configured, so no request can be made."""


class LLMDisabledError(LLMNotConfiguredError):
    """LLM generation is switched off (``LLM_ENABLED=false``)."""


class LLMUnavailableError(LLMError):
    """A transient failure persisted after the allowed retries (or the server asked for a
    longer wait than allowed). ``retry_after_seconds`` is set when the server gave one."""

    def __init__(
        self, message: str, *, status_code: int | None = None, retry_after_seconds: float | None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class LLMRequestError(LLMError):
    """The request was rejected and retrying cannot help: invalid key, no permission,
    unknown model, malformed request or exhausted quota. Usually a configuration issue."""

    def __init__(self, message: str, *, status_code: int | None, code: str | None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class LLMResponseError(LLMError):
    """The request succeeded but produced no usable text (empty, refused, incomplete or
    failed). ``reason`` says which. Such a response still used tokens: ``usage``,
    ``model`` and the cost estimate are attached when the API reported them."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.model: str | None = None
        self.usage: TokenUsage | None = None
        self.estimated_cost: float | None = None
        self.cost_currency: str | None = None


# --- Client interface (satisfied by openai.OpenAI and by test fakes) ----------------------------


class ResponsesAPI(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class ResponsesClient(Protocol):
    """The part of ``openai.OpenAI`` this service uses."""

    @property
    def responses(self) -> ResponsesAPI: ...

    def close(self) -> None: ...


ClientFactory = Callable[[str], ResponsesClient]  # api key -> client


def openai_client_factory(timeout_seconds: float) -> ClientFactory:
    """Build ``openai.OpenAI`` clients with this module's timeout and no SDK retries."""

    def factory(api_key: str) -> ResponsesClient:
        import httpx2  # installed with openai
        from openai import OpenAI

        return OpenAI(
            api_key=api_key,
            timeout=httpx2.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds)),
            max_retries=0,  # retries are bounded here instead; see the module docstring
        )

    return factory


# --- Results ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LLMResult:
    """Generated text plus metadata that is safe to log."""

    text: str
    model: str
    response_id: str | None
    request_id: str | None  # OpenAI's x-request-id, useful when contacting their support
    usage: TokenUsage  # as reported by the API; None values were not reported
    attempts: int
    latency_ms: float
    estimated_cost: float | None = None  # only with a configured price (LLM_PRICING)
    cost_currency: str | None = None

    @property
    def input_tokens(self) -> int | None:
        return self.usage.input_tokens

    @property
    def output_tokens(self) -> int | None:
        return self.usage.output_tokens

    @property
    def total_tokens(self) -> int | None:
        return self.usage.total_tokens


# --- Service ------------------------------------------------------------------------------------


class LLMService:
    """Generate text with the OpenAI Responses API, with bounded timeouts and retries."""

    def __init__(
        self,
        api_key: SecretStr | None,
        *,
        model: str,
        timeout_seconds: float = 20.0,
        max_retries: int = 2,
        max_output_tokens: int = 800,
        reasoning_effort: str | None = None,
        temperature: float | None = None,
        enabled: bool = True,
        usage_tracker: UsageTracker | None = None,
        client_factory: ClientFactory | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """
        Args:
            api_key: OpenAI key, or None when not configured (generation then raises
                ``LLMNotConfiguredError``; nothing else is affected).
            model: Model name, e.g. ``gpt-5.4-mini``.
            timeout_seconds: Per-attempt timeout (connect timeout is at most 5 s).
            max_retries: Extra attempts after a transient failure (0-5).
            max_output_tokens: Upper bound on generated tokens (including any reasoning).
            reasoning_effort: For reasoning models, e.g. ``low``; None sends nothing (use
                None for models that do not support reasoning settings).
            temperature: Sampling temperature 0-2; None sends nothing (model default).
            enabled: False switches generation off entirely (``LLMDisabledError``).
            usage_tracker: Records token usage and estimated cost of every response.
            client_factory: Builds the client from the key; replaced in tests.
            sleep: Waits between retries; replaced in tests.
        """
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if not 0 < timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be between 0 and 300")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int):
            raise ValueError("max_retries must be an integer")
        if not 0 <= max_retries <= 5:
            raise ValueError("max_retries must be between 0 and 5")
        if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int):
            raise ValueError("max_output_tokens must be an integer")
        if not 16 <= max_output_tokens <= 32_000:
            raise ValueError("max_output_tokens must be between 16 and 32000")
        if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(f"reasoning_effort must be one of {', '.join(REASONING_EFFORTS)}")
        if temperature is not None and (
            isinstance(temperature, bool)
            or not isinstance(temperature, int | float)
            or not 0 <= temperature <= 2
        ):
            raise ValueError("temperature must be a number between 0 and 2")

        self._api_key = api_key
        self.model = model.strip()
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.temperature = temperature
        self.enabled = enabled
        self.usage_tracker = usage_tracker
        self._client_factory = client_factory or openai_client_factory(timeout_seconds)
        self._sleep = sleep
        self._client: ResponsesClient | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> "LLMService":
        """Build the service from application settings (creates no client yet)."""
        return cls(
            settings.openai_api_key,
            model=settings.openai_model,
            timeout_seconds=settings.openai_timeout_seconds,
            max_retries=settings.openai_max_retries,
            max_output_tokens=settings.openai_max_output_tokens,
            reasoning_effort=settings.openai_reasoning_effort,
            temperature=settings.llm_temperature,
            enabled=settings.llm_enabled,
            **kwargs,
        )

    def __repr__(self) -> str:  # never show the key
        return (
            f"LLMService(model={self.model!r}, enabled={self.enabled}, "
            f"configured={self.is_configured}, "
            f"timeout_seconds={self.timeout_seconds}, max_retries={self.max_retries})"
        )

    @property
    def is_configured(self) -> bool:
        return self._api_key is not None and bool(self._api_key.get_secret_value().strip())

    @property
    def is_available(self) -> bool:
        """True when generation is enabled and a key is configured."""
        return self.enabled and self.is_configured

    @property
    def client_created(self) -> bool:
        return self._client is not None

    def generate(
        self,
        *,
        instructions: str,
        input: str | Iterable[Mapping[str, Any]],
        max_output_tokens: int | None = None,
        safety_identifier: str | None = None,
        text_format: Mapping[str, Any] | None = None,
    ) -> LLMResult:
        """Generate one reply.

        Args:
            instructions: System-level guidance for the model.
            input: The user text, or a list of Responses API input messages
                (``{"role": "user" | "assistant", "content": "..."}``).
            max_output_tokens: Overrides the configured limit for this call.
            safety_identifier: A stable, non-identifying ID for the end user (e.g. a
                hashed session ID) that OpenAI can use for abuse detection. Never pass
                an email address or a name.
            text_format: A Responses API output format, e.g. a strict ``json_schema``
                (Structured Outputs). ``LLMResult.text`` is then the JSON document.

        Raises:
            LLMDisabledError: generation is switched off (``LLM_ENABLED=false``).
            LLMNotConfiguredError: no API key.
            LLMRequestError: rejected for a reason a retry cannot fix.
            LLMUnavailableError: transient failure after the allowed retries.
            LLMResponseError: no usable text in the response.
            ValueError: empty instructions or input.
        """
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError("instructions must be a non-empty string")
        if isinstance(input, str):
            if not input.strip():
                raise ValueError("input must not be empty")
        else:
            input = list(input)
            if not input:
                raise ValueError("input must not be empty")

        request: dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": input,
            "max_output_tokens": max_output_tokens or self.max_output_tokens,
            "store": False,  # do not keep customer conversations at OpenAI
        }
        if self.reasoning_effort is not None:
            request["reasoning"] = {"effort": self.reasoning_effort}
        if self.temperature is not None:
            request["temperature"] = self.temperature
        if safety_identifier:
            request["safety_identifier"] = safety_identifier
        if text_format is not None:
            request["text"] = {"format": dict(text_format)}

        client = self._get_client()
        started = time.perf_counter()
        response, attempts = self._create_with_retries(client, request)
        latency_ms = (time.perf_counter() - started) * 1000
        usage = TokenUsage.from_response(response)
        # Record before inspecting the text: a refused or incomplete response still
        # consumed (and is billed for) the tokens it reports.
        cost, currency = None, None
        if self.usage_tracker is not None:
            record = self.usage_tracker.record(
                model=getattr(response, "model", None) or self.model,
                status=getattr(response, "status", None) or "unknown",
                usage=usage,
                duration_ms=latency_ms,
                attempts=attempts,
                response_id=getattr(response, "id", None),
                request_id=getattr(response, "_request_id", None),
            )
            cost, currency = record.estimated_cost, record.cost_currency
        try:
            result = _to_result(response, usage, attempts, latency_ms, cost, currency)
        except LLMResponseError as exc:
            exc.model = getattr(response, "model", None) or self.model
            exc.usage, exc.estimated_cost, exc.cost_currency = usage, cost, currency
            raise
        logger.info(
            "LLM response: model=%s attempts=%d latency_ms=%.0f input_tokens=%s "
            "output_tokens=%s request_id=%s",
            result.model,
            result.attempts,
            result.latency_ms,
            result.input_tokens,
            result.output_tokens,
            result.request_id,
        )
        return result

    def close(self) -> None:
        """Release the HTTP connection pool (e.g. at application shutdown)."""
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            client.close()

    # --- Internals -----------------------------------------------------------------------------

    def _get_client(self) -> ResponsesClient:
        client = self._client
        if client is not None:
            return client
        if not self.enabled:
            raise LLMDisabledError("LLM generation is disabled (LLM_ENABLED=false)")
        if not self.is_configured:
            raise LLMNotConfiguredError("OPENAI_API_KEY is not set")
        with self._lock:
            if self._client is None:  # another thread may have created it meanwhile
                self._client = self._client_factory(self._api_key.get_secret_value())  # type: ignore[union-attr]
                logger.info(
                    "Created OpenAI client: model=%s timeout_s=%s max_retries=%d",
                    self.model,
                    self.timeout_seconds,
                    self.max_retries,
                )
            return self._client

    def _create_with_retries(
        self, client: ResponsesClient, request: dict[str, Any]
    ) -> tuple[Any, int]:
        import openai

        for attempt in range(1, self.max_retries + 2):
            try:
                return client.responses.create(**request), attempt
            except openai.APIStatusError as exc:
                status, code = exc.status_code, exc.code
                retry_after = _retry_after_seconds(exc)
                transient = (status in RETRYABLE_STATUS_CODES or status >= 500) and (
                    code not in NON_RETRYABLE_ERROR_CODES
                )
                if not transient:
                    logger.error(
                        "LLM request rejected: %s status=%s code=%s (not retried)",
                        type(exc).__name__,
                        status,
                        code,
                    )
                    raise LLMRequestError(
                        f"OpenAI rejected the request (HTTP {status}{f', {code}' if code else ''})",
                        status_code=status,
                        code=code,
                    ) from None
                failure = f"HTTP {status}"
            except openai.APIConnectionError as exc:  # includes APITimeoutError
                status, retry_after = None, None
                failure = type(exc).__name__

            if attempt > self.max_retries:
                break
            if retry_after is not None and retry_after > MAX_BACKOFF_SECONDS:
                logger.warning(
                    "LLM %s: server asked to wait %.0f s; not retrying", failure, retry_after
                )
                raise LLMUnavailableError(
                    f"OpenAI is temporarily unavailable ({failure})",
                    status_code=status,
                    retry_after_seconds=retry_after,
                )
            delay = retry_after if retry_after is not None else _backoff(attempt)
            logger.warning(
                "LLM %s on attempt %d/%d; retrying in %.2f s",
                failure,
                attempt,
                self.max_retries + 1,
                delay,
            )
            self._sleep(delay)

        logger.error("LLM %s after %d attempt(s); giving up", failure, attempt)
        raise LLMUnavailableError(
            f"OpenAI is temporarily unavailable ({failure})",
            status_code=status,
            retry_after_seconds=retry_after,
        )


# --- Helpers ------------------------------------------------------------------------------------


def _backoff(attempt: int) -> float:
    """Exponential backoff for retry ``attempt`` (1-based), capped, with up to 25% jitter."""
    delay = min(INITIAL_BACKOFF_SECONDS * 2 ** (attempt - 1), MAX_BACKOFF_SECONDS)
    return delay * (1 - 0.25 * random.random())  # noqa: S311 - jitter, not cryptography


def _retry_after_seconds(exc: Any) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    for header, divisor in (("retry-after-ms", 1000), ("retry-after", 1)):
        value = headers.get(header)
        if value is None:
            continue
        try:
            seconds = float(value) / divisor
        except ValueError:
            continue
        if seconds >= 0 and seconds != float("inf"):
            return seconds
    return None


def _to_result(
    response: Any,
    usage: TokenUsage,
    attempts: int,
    latency_ms: float,
    estimated_cost: float | None,
    cost_currency: str | None,
) -> LLMResult:
    status = getattr(response, "status", None)
    if status == "incomplete":
        details = getattr(response, "incomplete_details", None)
        reason = getattr(details, "reason", None) or "unknown"
        raise LLMResponseError(f"response incomplete ({reason})", reason=f"incomplete:{reason}")
    if status == "failed":
        raise LLMResponseError("response failed", reason="failed")

    text = (getattr(response, "output_text", "") or "").strip()
    if not text:
        refused = any(
            getattr(content, "type", None) == "refusal"
            for item in getattr(response, "output", None) or []
            for content in getattr(item, "content", None) or []
        )
        reason = "refusal" if refused else "empty"
        raise LLMResponseError(f"response contained no text ({reason})", reason=reason)

    return LLMResult(
        text=text,
        model=getattr(response, "model", None) or "unknown",
        response_id=getattr(response, "id", None),
        request_id=getattr(response, "_request_id", None),
        usage=usage,
        attempts=attempts,
        latency_ms=latency_ms,
        estimated_cost=estimated_cost,
        cost_currency=cost_currency,
    )
