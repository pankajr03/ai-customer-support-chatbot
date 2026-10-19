"""Service monitoring section of the staff dashboard (Stage 13).

Shows the backend's ``GET /admin/monitoring``: request counts, errors, latencies and
failures of the **running backend process**, kept in its memory. They start at zero when
the backend starts and are lost when it restarts; the page says so above the figures.
Durable figures (answers, tickets, stored response times) are in the Analytics section.
Counts and timings only: there is no customer data in this view.
"""

import streamlit as st
from pydantic import SecretStr

from frontend.admin_api_client import AdminApiClient, Monitoring, MonitoringLatency
from frontend.admin_common import call

# (series, labels, what it measures)
LATENCY_ROWS: tuple[tuple[str, dict[str, str], str], ...] = (
    ("chat_turn_duration", {}, "Chat turn (whole reply)"),
    ("chat_stage_duration", {"stage": "history"}, "  · reading the conversation"),
    ("chat_stage_duration", {"stage": "rewrite"}, "  · question rewriting"),
    ("chat_stage_duration", {"stage": "pipeline"}, "  · retrieval and answer"),
    ("chat_stage_duration", {"stage": "store"}, "  · saving the exchange"),
    ("retrieval_duration", {}, "FAQ retrieval"),
    ("llm_call_duration", {}, "LLM call (retries included)"),
    ("db_transaction_duration", {"kind": "read"}, "Database read"),
    ("db_transaction_duration", {"kind": "write"}, "Database write (lock wait included)"),
    ("faq_index_build_duration", {}, "Semantic index build"),
)


def render_monitoring(client: AdminApiClient, token: SecretStr) -> None:
    st.header("Service monitoring")
    if st.button("Refresh", icon=":material/refresh:"):
        st.rerun()
    data = call(lambda: client.monitoring(token))
    if data is None:
        return
    st.info(
        f"Since the backend started at {data.process.started_at} "
        f"({_duration(data.process.uptime_seconds)} ago). These figures live in the "
        "backend's memory: they reset when it restarts and are not stored. A copy is "
        "written to the log every few minutes (event=metrics_snapshot).",
        icon=":material/memory:",
    )
    render_requests(data)
    render_latencies(data)
    render_failures(data)
    render_routes(data)


def render_requests(data: Monitoring) -> None:
    st.subheader("Requests")
    requests = data.requests
    cols = st.columns(4)
    cols[0].metric("Requests", f"{requests.total:,}")
    cols[1].metric("Client errors (4xx)", f"{requests.client_errors:,}")
    cols[2].metric("Server errors (5xx)", f"{requests.server_errors:,}")
    cols[3].metric("Uptime", _duration(data.process.uptime_seconds))
    left, right = st.columns(2)
    with left:
        st.caption("By status code")
        st.dataframe(
            [{"Status": code, "Requests": n} for code, n in requests.by_status.items()],
            hide_index=True,
            use_container_width=True,
        )
    with right:
        st.caption("Errors by category (the API error code)")
        if requests.by_error_category:
            st.dataframe(
                [
                    {"Category": code, "Responses": n}
                    for code, n in sorted(
                        requests.by_error_category.items(), key=lambda item: -item[1]
                    )
                ],
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.write("No error responses.")


def render_latencies(data: Monitoring) -> None:
    st.subheader("Latency")
    rows = []
    for name, labels, label in LATENCY_ROWS:
        series = _series(data, name, labels)
        if series is None:
            continue
        rows.append(
            {
                "Measure": label,
                "Count": series.count,
                "Median (ms)": series.p50_ms,
                "95th pct (ms)": series.p95_ms,
                "99th pct (ms)": series.p99_ms,
                "Slowest (ms)": series.max_ms,
            }
        )
    if not rows:
        st.write("Nothing measured yet.")
        return
    st.dataframe(rows, hide_index=True, use_container_width=True)
    st.caption(
        "Percentiles cover each measure's most recent samples (up to 512); count and "
        "slowest are since the backend started. The first chat turn after a start also "
        "loads the search model, unless it was preloaded."
    )


def render_failures(data: Monitoring) -> None:
    st.subheader("Failures")
    llm_errors = sum(data.llm.error_counts.values())
    cols = st.columns(4)
    cols[0].metric("Support requests failed", data.count("ticket_submissions", outcome="failed"))
    cols[1].metric(
        "FAQ index failures",
        data.count("faq_index_builds", outcome="failed")
        + data.count("faq_index_refreshes", outcome="failed")
        + data.count("faq_catalog_refreshes", outcome="failed"),
    )
    cols[2].metric("Database errors", data.count("db_transaction_errors"))
    cols[3].metric("LLM call failures", llm_errors)
    created = data.count("ticket_submissions", outcome="created")
    duplicate = data.count("ticket_submissions", outcome="duplicate")
    integrations = data.count("ticket_integration_failures")
    st.caption(
        f"Support requests: {created} created, {duplicate} recognised as duplicates. "
        f"Ticket integration failures: {integrations}. "
        f"LLM: {'enabled' if data.llm.enabled else 'disabled'}"
        f"{', paused after recent failures' if data.llm.paused else ''}."
    )
    if data.llm.error_counts:
        st.dataframe(
            [{"LLM failure": k, "Count": n} for k, n in sorted(data.llm.error_counts.items())],
            hide_index=True,
        )


def render_routes(data: Monitoring) -> None:
    with st.expander("Requests by route"):
        rows = [
            {
                "Method": row.labels.get("method", ""),
                "Route": row.labels.get("route", ""),
                "Requests": row.count,
                "Median (ms)": row.p50_ms,
                "95th pct (ms)": row.p95_ms,
            }
            for row in sorted(
                data.latencies.get("http_request_duration", []), key=lambda r: -r.count
            )
        ]
        st.dataframe(rows, hide_index=True, use_container_width=True)
        st.caption("Route templates: IDs are never part of a route.")


def _series(data: Monitoring, name: str, labels: dict[str, str]) -> MonitoringLatency | None:
    """The series with exactly these labels (percentiles cannot be merged across series)."""
    return next((row for row in data.latencies.get(name, []) if row.labels == labels), None)


def _duration(seconds: float) -> str:
    minutes, _ = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min"
