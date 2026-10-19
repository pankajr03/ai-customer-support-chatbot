"""``GET /api/v1/admin/monitoring``: the process's operational metrics (Stage 13).

Process-local and in memory: every value counts from ``process.started_at`` and is lost
on restart (``scope`` says so in every response). Durable business figures are in the
analytics endpoints. Counts, timings and fixed labels only: no IDs, no customer data.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SCOPE = (
    "Process-local, in memory: counted since process.started_at by this backend process "
    "only, and reset when it restarts. Not durable; a periodic copy is written to the log "
    "(event=metrics_snapshot). Latency percentiles cover each series' most recent samples."
)


class ProcessInfo(BaseModel):
    started_at: str = Field(description="When this process started counting (UTC).")
    uptime_seconds: float


class CounterRow(BaseModel):
    labels: dict[str, str] = Field(description="Fixed, low-cardinality labels; never IDs.")
    count: int


class LatencyRow(BaseModel):
    labels: dict[str, str]
    count: int = Field(description="Observations since start.")
    average_ms: float | None = Field(description="All-time average.")
    max_ms: float | None = Field(description="All-time maximum.")
    recent_samples: int = Field(description="Samples behind the percentiles (newest first).")
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None


class RequestTotals(BaseModel):
    total: int = Field(description="HTTP requests answered, health checks included.")
    client_errors: int = Field(description="4xx responses.")
    server_errors: int = Field(description="5xx responses.")
    by_status: dict[str, int]
    by_error_category: dict[str, int] = Field(
        description="Error responses by API error code (the code the client received)."
    )


class LLMStatus(BaseModel):
    enabled: bool = Field(description="A key is configured and LLM_ENABLED=true.")
    paused: bool = Field(description="The circuit breaker is open after recent failures.")
    error_counts: dict[str, int] = Field(
        description="LLM call failures per category since start (LLMService.error_counts)."
    )
    usage: dict[str, Any] | None = Field(
        description="Token totals and cost estimates (the existing usage tracker); null "
        "when usage tracking is off."
    )


class MonitoringSnapshot(BaseModel):
    """Operational metrics of this backend process."""

    model_config = ConfigDict(extra="forbid")

    scope: str = Field(default=SCOPE)
    process: ProcessInfo
    requests: RequestTotals
    counters: dict[str, list[CounterRow]] = Field(
        description="Event counters: ticket_submissions, faq_index_builds, "
        "faq_index_refreshes, faq_catalog_refreshes, db_transaction_errors, "
        "ticket_integration_failures, http_requests, http_errors."
    )
    latencies: dict[str, list[LatencyRow]] = Field(
        description="Durations: http_request_duration, chat_turn_duration, "
        "chat_stage_duration, retrieval_duration, llm_call_duration, "
        "db_transaction_duration, faq_index_build_duration."
    )
    llm: LLMStatus
