"""Lightweight operational metrics, kept in this process's memory (Stage 13).

What this is, and is not:
- **Process-local and in memory.** Every value counts from this process's start and is
  lost when it stops, restarts or is redeployed. Nothing is written to disk or sent
  anywhere. The deployment runs one backend process (one uvicorn worker), so the values
  cover all its traffic; with several processes each would count only its own share.
- **Not durable.** For history, the backend writes the same snapshot to the log every
  ``METRICS_LOG_INTERVAL_SECONDS`` (``event=metrics_snapshot``), and the logs are kept as
  long as Docker's log rotation (or a collector) keeps them. Durable business figures
  (answers, escalations, tickets, stored response times) come from the database, through
  the staff analytics endpoints; they are not repeated here.
- **Low overhead.** Recording is a lock, a dictionary update and, for a latency, an
  append to a bounded window: microseconds, no I/O. Percentiles are computed only when a
  snapshot is read.
- **Bounded labels, no personal data.** Names are fixed in code. Label values are route
  templates (never paths), HTTP methods from a fixed list, status codes, error codes and
  outcomes from enumerations: never session, ticket or customer IDs, or any text from a
  request. ``LabelGuard`` caps each metric at ``MAX_SERIES`` label combinations anyway,
  folding any excess into ``other``.

Usage:
    from app.core.metrics import metrics
    metrics.increment("ticket_submissions", outcome="created")
    metrics.observe_ms("db_transaction", 3.2, kind="write")
"""

import math
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Most recent samples kept per latency series, for percentiles: a recent window, not
# all-time. Count, total and maximum are all-time.
LATENCY_WINDOW = 512
# Label combinations allowed per metric before the rest are folded into "other".
MAX_SERIES = 200
OTHER = "other"

HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})

Labels = tuple[tuple[str, str], ...]


def http_method(method: str) -> str:
    """The method as a label: one of the standard ones, else ``other`` (a client can send
    any token as a method, which would otherwise create a new series each time)."""
    upper = method.upper()
    return upper if upper in HTTP_METHODS else OTHER


@dataclass
class _Latency:
    count: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    window: deque[float] = field(default_factory=lambda: deque(maxlen=LATENCY_WINDOW))

    def add(self, value_ms: float) -> None:
        self.count += 1
        self.total_ms += value_ms
        self.max_ms = max(self.max_ms, value_ms)
        self.window.append(value_ms)

    def summary(self) -> dict[str, Any]:
        recent = sorted(self.window)
        return {
            "count": self.count,
            "average_ms": round(self.total_ms / self.count, 1) if self.count else None,
            "max_ms": round(self.max_ms, 1) if self.count else None,
            "recent_samples": len(recent),
            "p50_ms": _percentile(recent, 0.50),
            "p95_ms": _percentile(recent, 0.95),
            "p99_ms": _percentile(recent, 0.99),
        }


def _percentile(ordered: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile of an ascending list, or None when it is empty."""
    if not ordered:
        return None
    rank = max(1, math.ceil(fraction * len(ordered)))
    return round(ordered[rank - 1], 1)


class Metrics:
    """Counters and latency series for one process. Thread-safe."""

    def __init__(self, clock: Any = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._counters: dict[str, dict[Labels, int]] = {}
        self._latencies: dict[str, dict[Labels, _Latency]] = {}
        self._started_monotonic = clock()
        self._started_at = datetime.now(UTC)

    # -- Recording ----------------------------------------------------------------------

    def increment(self, name: str, amount: int = 1, **labels: str) -> None:
        with self._lock:
            series = self._counters.setdefault(name, {})
            key = _bounded(series, _labels(labels))
            series[key] = series.get(key, 0) + amount

    def observe_ms(self, name: str, value_ms: float, **labels: str) -> None:
        if not math.isfinite(value_ms) or value_ms < 0:
            return
        with self._lock:
            series = self._latencies.setdefault(name, {})
            key = _bounded(series, _labels(labels))
            latency = series.get(key)
            if latency is None:
                latency = series[key] = _Latency()
            latency.add(value_ms)

    # -- Reading ------------------------------------------------------------------------

    def uptime_seconds(self) -> float:
        return max(0.0, self._clock() - self._started_monotonic)

    def counter(self, name: str, **labels: str) -> int:
        """One counter's value (0 when never incremented). For tests and summaries."""
        with self._lock:
            return self._counters.get(name, {}).get(_labels(labels), 0)

    def counters(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            return {
                name: [
                    {"labels": dict(key), "count": value} for key, value in sorted(series.items())
                ]
                for name, series in sorted(self._counters.items())
            }

    def latencies(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            snapshot = {
                name: [
                    (key, latency.count, latency.total_ms, latency.max_ms, list(latency.window))
                    for key, latency in sorted(series.items())
                ]  # fmt: skip
                for name, series in sorted(self._latencies.items())
            }
        # Sorting for percentiles happens outside the lock.
        result: dict[str, list[dict[str, Any]]] = {}
        for name, rows in snapshot.items():
            result[name] = []
            for key, count, total, maximum, window in rows:
                latency = _Latency(count=count, total_ms=total, max_ms=maximum)
                latency.window.extend(window)
                result[name].append({"labels": dict(key), **latency.summary()})
        return result

    def process(self) -> dict[str, Any]:
        return {
            "started_at": self._started_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "uptime_seconds": round(self.uptime_seconds(), 1),
        }

    def reset(self) -> None:
        """Forget everything and restart the uptime clock (tests only)."""
        with self._lock:
            self._counters.clear()
            self._latencies.clear()
            self._started_monotonic = self._clock()
            self._started_at = datetime.now(UTC)


def _labels(labels: dict[str, str]) -> Labels:
    return tuple(sorted((key, str(value)) for key, value in labels.items()))


def _bounded(series: dict[Labels, Any], key: Labels) -> Labels:
    """``key``, or its ``other`` fold once the metric has ``MAX_SERIES`` combinations."""
    if key in series or len(series) < MAX_SERIES:
        return key
    return tuple((name, OTHER) for name, _ in key)


def total(rows: Iterable[dict[str, Any]], **where: str) -> int:
    """Sum of ``count`` over counter rows whose labels match ``where``."""
    return sum(
        row["count"]
        for row in rows
        if all(row["labels"].get(key) == value for key, value in where.items())
    )


# The process's registry. Module-level, like a logger: every component records into it
# without being handed it, and it lives exactly as long as the process.
metrics = Metrics()
