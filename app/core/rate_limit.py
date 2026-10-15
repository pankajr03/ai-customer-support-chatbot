"""Basic request-rate limits (Stage 7): a prototype safeguard, not production protection.

Three limits, each a token bucket per key (``N`` requests per minute, refilled evenly,
so a client can burst up to ``N`` and then gets one request every ``60/N`` seconds):

- every API request except health checks, per client IP address
  (``RATE_LIMIT_REQUESTS_PER_MINUTE``);
- starting a conversation, per client IP address
  (``RATE_LIMIT_NEW_CONVERSATIONS_PER_MINUTE``);
- chat messages, per conversation and hash of the presented token
  (``RATE_LIMIT_CHAT_MESSAGES_PER_MINUTE``).

The chat key includes a hash of the presented token, so somebody who only knows a
session ID cannot use up its owner's allowance; requests with made-up tokens are still
counted by the per-IP limit. A request over a limit gets ``429 RATE_LIMITED`` with
``Retry-After``.

**Process-local and in memory.** The counters live in this process only: they reset on
restart, each worker process has its own, and several servers do not share them. The
number of tracked keys is bounded (``max_keys``); when it is full the least recently
used key is forgotten, which resets its count. Behind a reverse proxy every client has
the proxy's address unless uvicorn is started with ``--proxy-headers`` and
``--forwarded-allow-ips`` for that proxy. Durable, shared limits (a gateway or Redis)
belong to Stage 8.
"""

import hashlib
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

DEFAULT_MAX_KEYS = 10_000


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimitExceededError(Exception):
    """Too many requests; retry after ``retry_after_seconds``."""

    def __init__(self, retry_after_seconds: float, limit: str) -> None:
        super().__init__(f"rate limit {limit} exceeded")
        self.retry_after_seconds = retry_after_seconds
        self.limit = limit  # which limit, for logs (never the key)


class RateLimiter:
    """A thread-safe token bucket per key, ``per_minute`` requests per minute."""

    def __init__(
        self,
        name: str,
        per_minute: int,
        *,
        max_keys: int = DEFAULT_MAX_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if per_minute < 1:
            raise ValueError("per_minute must be at least 1")
        if max_keys < 1:
            raise ValueError("max_keys must be at least 1")
        self.name = name
        self.capacity = float(per_minute)
        self.refill_per_second = per_minute / 60.0
        self.max_keys = max_keys
        self._clock = clock
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._lock = threading.Lock()

    def hit(self, key: str) -> None:
        """Count one request for ``key``.

        Raises:
            RateLimitExceededError: no request is left for ``key`` right now.
        """
        now = self._clock()
        with self._lock:
            bucket = self._buckets.pop(key, None)
            if bucket is None:
                bucket = _Bucket(self.capacity, now)
            else:
                elapsed = max(now - bucket.updated, 0.0)
                bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_second)
                bucket.updated = now
            self._buckets[key] = bucket  # most recently used last
            while len(self._buckets) > self.max_keys:
                self._buckets.popitem(last=False)
            if bucket.tokens < 1.0:
                wait = (1.0 - bucket.tokens) / self.refill_per_second
                raise RateLimitExceededError(wait, self.name)
            bucket.tokens -= 1.0

    def __len__(self) -> int:
        return len(self._buckets)


@dataclass(frozen=True)
class RateLimits:
    """The application's limiters; ``None`` = that limit is off."""

    requests: RateLimiter | None
    new_conversations: RateLimiter | None
    chat_messages: RateLimiter | None

    @classmethod
    def from_settings(cls, settings: object) -> "RateLimits":
        if not getattr(settings, "rate_limit_enabled", False):
            return cls(None, None, None)

        def limiter(name: str, setting: str) -> RateLimiter:
            return RateLimiter(name, int(getattr(settings, setting)))

        return cls(
            limiter("requests", "rate_limit_requests_per_minute"),
            limiter("new_conversations", "rate_limit_new_conversations_per_minute"),
            limiter("chat_messages", "rate_limit_chat_messages_per_minute"),
        )


def session_key(session_id: str, access_token: str) -> str:
    """The chat limit's key: the session and a hash of the presented token (never the
    token itself, which is also why nothing secret is kept in memory here)."""
    digest = hashlib.sha256(access_token.encode("utf-8")).hexdigest()[:16]
    return f"{session_id}:{digest}"


def retry_after_header(seconds: float) -> str:
    return str(max(1, math.ceil(seconds)))
