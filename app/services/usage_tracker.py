"""Lightweight tracking of LLM token usage and estimated cost.

Every completed Responses API call is recorded with the usage metadata the API itself
returned: input, output and total tokens, plus cached-input, cache-write and reasoning
tokens when present. Nothing is estimated or recomputed: a value the API did not return
stays ``None``. Responses that are later rejected (refused, incomplete, invalid JSON)
are recorded too, because they still consumed tokens.

Estimated cost
--------------
Only computed when a price for the model is explicitly configured (``LLM_PRICING``);
otherwise ``estimated_cost`` is ``None``. It is labelled an *estimate*: it uses your
configured rates and the reported token counts, and ignores anything else that can
affect a bill (taxes, discounts, batch or priority tiers, tool fees, rounding). It is
for monitoring trends, not billing.

    cost = ((input - cached) x input_rate + cached x cached_rate + output x output_rate) / 1e6

Reasoning tokens are part of ``output_tokens`` and are not counted twice. If either the
input or output count is missing, the estimate is ``None``.

A configured model name also prices its dated snapshots ("gpt-5.4-mini" prices
"gpt-5.4-mini-2026-03-17"), but never a different model ("gpt-5.4" does not price
"gpt-5.4-mini").

Privacy
-------
Records contain only the model, IDs from OpenAI, token counts, timings and the cost
estimate: never the customer's question, the prompt, the answer or any user identifier.
Records live in memory (bounded) and in the log; nothing is persisted.
"""

import logging
import math
import re
import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings
from app.models.usage import ModelPricing

logger = logging.getLogger(__name__)

DEFAULT_MAX_RECENT = 100
COST_LABEL = "estimated_cost"
_SNAPSHOT_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token counts exactly as reported by the API (None = not reported)."""

    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None

    @classmethod
    def from_response(cls, response: Any) -> "TokenUsage":
        """Read ``response.usage`` from a Responses API object; missing values stay None."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return cls()
        input_details = getattr(usage, "input_tokens_details", None)
        output_details = getattr(usage, "output_tokens_details", None)
        return cls(
            input_tokens=_count(getattr(usage, "input_tokens", None)),
            cached_input_tokens=_count(getattr(input_details, "cached_tokens", None)),
            cache_write_tokens=_count(getattr(input_details, "cache_write_tokens", None)),
            output_tokens=_count(getattr(usage, "output_tokens", None)),
            reasoning_tokens=_count(getattr(output_details, "reasoning_tokens", None)),
            total_tokens=_count(getattr(usage, "total_tokens", None)),
        )

    @property
    def is_reported(self) -> bool:
        return self.input_tokens is not None or self.output_tokens is not None


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """One API call. Contains no customer text or personal data."""

    timestamp: datetime
    model: str
    status: str  # the API response status, e.g. "completed" or "incomplete"
    usage: TokenUsage
    duration_ms: float
    attempts: int
    response_id: str | None
    request_id: str | None
    estimated_cost: float | None  # None when no price is configured for the model
    cost_currency: str | None
    pricing_model: str | None  # the LLM_PRICING entry that was used, if any


@dataclass
class ModelTotals:
    """Running totals for one model since the process started."""

    requests: int = 0
    requests_without_usage: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    priced_requests: int = 0
    estimated_cost: float = 0.0  # sum over priced requests only

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "requests_without_usage": self.requests_without_usage,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "priced_requests": self.priced_requests,
            COST_LABEL: round(self.estimated_cost, 6) if self.priced_requests else None,
        }


@dataclass
class _State:
    totals: dict[str, ModelTotals] = field(default_factory=dict)
    recent: deque[UsageRecord] = field(default_factory=deque)


class UsageTracker:
    """Records token usage per API call and keeps running totals (thread-safe)."""

    def __init__(
        self,
        pricing: Mapping[str, ModelPricing] | None = None,
        *,
        currency: str = "USD",
        max_recent: int = DEFAULT_MAX_RECENT,
    ) -> None:
        if not 1 <= max_recent <= 10_000:
            raise ValueError("max_recent must be between 1 and 10000")
        self._pricing = dict(pricing or {})
        self.currency = currency
        self._state = _State(recent=deque(maxlen=max_recent))
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> "UsageTracker":
        return cls(settings.llm_pricing, currency=settings.llm_pricing_currency)

    def pricing_for(self, model: str) -> tuple[str, ModelPricing] | None:
        """The configured price for ``model``, or for the model it is a dated snapshot of."""
        if model in self._pricing:
            return model, self._pricing[model]
        base = _SNAPSHOT_SUFFIX.sub("", model)
        if base != model and base in self._pricing:
            return base, self._pricing[base]
        return None

    def estimate_cost(self, model: str, usage: TokenUsage) -> tuple[float | None, str | None]:
        """(estimated cost, pricing entry used); (None, None) when it cannot be estimated."""
        found = self.pricing_for(model)
        if found is None or usage.input_tokens is None or usage.output_tokens is None:
            return None, None
        name, price = found
        cached = min(usage.cached_input_tokens or 0, usage.input_tokens)
        cached_rate = (
            price.cached_input_per_million_tokens
            if price.cached_input_per_million_tokens is not None
            else price.input_per_million_tokens
        )
        cost = (
            (usage.input_tokens - cached) * price.input_per_million_tokens
            + cached * cached_rate
            + usage.output_tokens * price.output_per_million_tokens
        ) / 1_000_000
        return cost, name

    def record(
        self,
        *,
        model: str,
        status: str,
        usage: TokenUsage,
        duration_ms: float,
        attempts: int,
        response_id: str | None = None,
        request_id: str | None = None,
    ) -> UsageRecord:
        """Record one API call and log it (IDs and numbers only)."""
        cost, pricing_model = self.estimate_cost(model, usage)
        record = UsageRecord(
            timestamp=datetime.now(UTC),
            model=model,
            status=status,
            usage=usage,
            duration_ms=duration_ms,
            attempts=attempts,
            response_id=response_id,
            request_id=request_id,
            estimated_cost=cost,
            cost_currency=self.currency if cost is not None else None,
            pricing_model=pricing_model,
        )
        with self._lock:
            totals = self._state.totals.setdefault(model, ModelTotals())
            totals.requests += 1
            if not usage.is_reported:
                totals.requests_without_usage += 1
            totals.input_tokens += usage.input_tokens or 0
            totals.output_tokens += usage.output_tokens or 0
            totals.total_tokens += usage.total_tokens or 0
            if cost is not None:
                totals.priced_requests += 1
                totals.estimated_cost += cost
            self._state.recent.append(record)
        logger.info(
            "event=llm_usage model=%s status=%s input_tokens=%s cached_input_tokens=%s "
            "output_tokens=%s reasoning_tokens=%s total_tokens=%s duration_ms=%.0f "
            "attempts=%d %s=%s currency=%s request_id=%s",
            model,
            status,
            _show(usage.input_tokens),
            _show(usage.cached_input_tokens),
            _show(usage.output_tokens),
            _show(usage.reasoning_tokens),
            _show(usage.total_tokens),
            duration_ms,
            attempts,
            COST_LABEL,
            f"{cost:.6f}" if cost is not None else "-",
            record.cost_currency or "-",
            request_id or "-",
        )
        return record

    def summary(self) -> dict[str, Any]:
        """Totals per model and overall, for monitoring. Costs are estimates (or None)."""
        overall = ModelTotals()
        with self._lock:  # one consistent view: no record() can interleave
            per_model = {name: totals.as_dict() for name, totals in self._state.totals.items()}
            for totals in self._state.totals.values():
                for name in ("requests", "requests_without_usage", "input_tokens",
                             "output_tokens", "total_tokens", "priced_requests"):  # fmt: skip
                    setattr(overall, name, getattr(overall, name) + getattr(totals, name))
                overall.estimated_cost += totals.estimated_cost
        return {
            "currency": self.currency,
            "cost_note": "Estimated from configured prices; not a bill.",
            "models": per_model,
            "overall": overall.as_dict(),
        }

    def recent(self) -> tuple[UsageRecord, ...]:
        """The most recent records, oldest first (bounded)."""
        with self._lock:
            return tuple(self._state.recent)


def _count(value: Any) -> int | None:
    """An integer token count, or None if the API did not report a usable one."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value < 0 or value != int(value):
        return None
    return int(value)


def _show(value: int | None) -> str:
    return "-" if value is None else str(value)
