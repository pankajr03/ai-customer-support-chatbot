"""Operational monitoring: a snapshot of the process metrics, for staff and for the log.

The values come from ``app.core.metrics`` (recorded where the work is already timed or
counted), plus what other components already track and are reused here rather than
counted twice: the LLM failure counters (``LLMService.error_counts``), the LLM token
usage (``UsageTracker.summary``) and the circuit breaker's state. docs/monitoring.md
"""

import asyncio
import logging
from typing import Any

from app.core.metrics import Metrics, metrics
from app.models.monitoring import (
    CounterRow,
    LatencyRow,
    LLMStatus,
    MonitoringSnapshot,
    ProcessInfo,
    RequestTotals,
)

logger = logging.getLogger(__name__)


def monitoring_snapshot(state: Any, registry: Metrics = metrics) -> MonitoringSnapshot:
    """Everything the process measures, as one consistent-enough view (each part is
    read under its own lock; values may move by a request between parts)."""
    counters = registry.counters()
    requests = counters.get("http_requests", [])
    by_status: dict[str, int] = {}
    for row in requests:
        status = row["labels"]["status"]
        by_status[status] = by_status.get(status, 0) + row["count"]
    llm = state.llm_service
    tracker = getattr(state, "usage_tracker", None)
    return MonitoringSnapshot(
        process=ProcessInfo(**registry.process()),
        requests=RequestTotals(
            total=sum(by_status.values()),
            client_errors=sum(n for code, n in by_status.items() if code.startswith("4")),
            server_errors=sum(n for code, n in by_status.items() if code.startswith("5")),
            by_status=dict(sorted(by_status.items())),
            by_error_category={
                row["labels"]["category"]: row["count"] for row in counters.get("http_errors", [])
            },
        ),
        counters={
            name: [CounterRow.model_validate(row) for row in rows]
            for name, rows in counters.items()
        },
        latencies={
            name: [LatencyRow.model_validate(row) for row in rows]
            for name, rows in registry.latencies().items()
        },
        llm=LLMStatus(
            enabled=llm.is_available,
            paused=llm.is_available and llm.circuit_breaker.is_open,
            error_counts=llm.error_counts(),
            usage=tracker.summary() if tracker is not None else None,
        ),
    )


def log_snapshot(state: Any, registry: Metrics = metrics) -> None:
    """One ``event=metrics_snapshot`` record: the headline figures, so they outlive the
    process in the logs. Counts and timings only."""
    snapshot = monitoring_snapshot(state, registry)
    latencies = snapshot.latencies

    def p95(name: str, **labels: str) -> float | None:
        for row in latencies.get(name, []):
            if all(row.labels.get(key) == value for key, value in labels.items()):
                return row.p95_ms
        return None

    def count(name: str, **labels: str) -> int:
        return sum(
            row.count
            for row in snapshot.counters.get(name, [])
            if all(row.labels.get(key) == value for key, value in labels.items())
        )

    fields: dict[str, Any] = {
        "event": "metrics_snapshot",
        "uptime_seconds": snapshot.process.uptime_seconds,
        "requests_total": snapshot.requests.total,
        "client_errors": snapshot.requests.client_errors,
        "server_errors": snapshot.requests.server_errors,
        "errors_by_category": snapshot.requests.by_error_category,
        "chat_turn_p95_ms": p95("chat_turn_duration"),
        "retrieval_p95_ms": p95("retrieval_duration"),
        "llm_call_p95_ms": p95("llm_call_duration"),
        "db_write_p95_ms": p95("db_transaction_duration", kind="write"),
        "db_errors": count("db_transaction_errors"),
        "ticket_failures": count("ticket_submissions", outcome="failed"),
        "faq_index_failures": count("faq_index_builds", outcome="failed")
        + count("faq_index_refreshes", outcome="failed"),
        "llm_errors": sum(snapshot.llm.error_counts.values()),
        "llm_paused": snapshot.llm.paused,
    }
    logger.info(
        "event=metrics_snapshot uptime_s=%.0f requests=%d 4xx=%d 5xx=%d chat_p95_ms=%s "
        "retrieval_p95_ms=%s llm_p95_ms=%s db_write_p95_ms=%s db_errors=%d "
        "ticket_failures=%d faq_index_failures=%d llm_errors=%d",
        fields["uptime_seconds"],
        fields["requests_total"],
        fields["client_errors"],
        fields["server_errors"],
        _or_dash(fields["chat_turn_p95_ms"]),
        _or_dash(fields["retrieval_p95_ms"]),
        _or_dash(fields["llm_call_p95_ms"]),
        _or_dash(fields["db_write_p95_ms"]),
        fields["db_errors"],
        fields["ticket_failures"],
        fields["faq_index_failures"],
        fields["llm_errors"],
        extra={"log_fields": fields},
    )


async def log_snapshots_every(state: Any, interval_seconds: float) -> None:
    """Write a snapshot to the log every ``interval_seconds`` until cancelled."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            log_snapshot(state)
        except Exception as exc:  # noqa: BLE001 - monitoring must never stop the service
            logger.warning("Metrics snapshot failed (%s)", type(exc).__name__)


def _or_dash(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"
