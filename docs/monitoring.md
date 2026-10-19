# Basic operational monitoring (Stage 13)

The backend measures its own operation in memory and shows it to staff, in the
dashboard's **Service monitoring** section and at `GET /api/v1/admin/monitoring`. A copy
goes to the log every five minutes. There is no Prometheus, Grafana, agent or external
service: at this scale (one server, one backend process) the figures below answer the
operational questions without new infrastructure. If several servers, alerting rules or
long-term graphs are needed later, the same measurement points can feed an exporter. That
needs its own decision; it is not part of this stage.

## Read this first: process-local, not durable

- **Every figure counts from the moment the backend process started**
  (`process.started_at`), in that process's memory. A restart, crash, redeploy or
  `docker compose up --build` **resets all of them to zero**. Nothing is written to the
  database or to disk.
- The deployment runs **one** backend process (one uvicorn worker, security audit
  SEC-09/SEC-10), so the figures cover all its traffic. With several workers or servers,
  each would count only its own share, and the figures would have to be added up.
- **For history**, the backend writes the headline figures to the log as
  `event=metrics_snapshot`, every `METRICS_LOG_INTERVAL_SECONDS` (default 300; `0` turns
  it off) and once more at shutdown. They last exactly as long as the logs: Docker keeps
  5 × 10 MB per container ([docs/logging.md](logging.md)) unless a collector ships them
  elsewhere.
- The response says the same in its `scope` field, and the dashboard says it above the
  figures.

**Durable figures are elsewhere and are not repeated here.** Chatbot decisions, answered
and fallback rates, escalation offers, tickets created and resolved, and stored response
times all come from the database, through the staff analytics
(`/api/v1/admin/analytics/chat`, `/analytics/tickets`; [docs/admin-dashboard.md](admin-dashboard.md)).
They survive restarts. Monitoring covers what the database cannot know: request and error
rates, latency per stage, and failures that leave no row behind.

## What is measured

| Metric | Kind | Labels | Recorded where (existing measurement reused) |
|---|---|---|---|
| `http_requests` | counter | `method`, `route`, `status` | The access record of every request (`RequestContextMiddleware`) |
| `http_request_duration` | latency | `method`, `route` | Same: the duration already logged as `duration_ms` |
| `http_errors` | counter | `category` | Same: the API error code (`error_category`) |
| `chat_turn_duration` | latency | none | The chat turn's existing stage timer (`_StageTimer`), for turns that were answered and stored |
| `chat_stage_duration` | latency | `stage`: `history`, `rewrite`, `pipeline`, `store` | Same timer: each stage of the turn |
| `retrieval_duration` | latency | none | The answer decision's existing timing (`event=answer_decision duration_ms`) |
| `llm_call_duration` | latency | none | `LLMService`, around the API call that already measured `latency_ms` (retries included; successful calls) |
| `db_transaction_duration` | latency | `kind`: `read`, `write` | `Database.read()` / `write()`. A write includes waiting for SQLite's write lock, so slow writes show contention |
| `db_transaction_errors` | counter | `kind` | Same, on a database error only (not on "not found" and other application outcomes raised inside a transaction) |
| `ticket_submissions` | counter | `outcome`: `created`, `duplicate`, `failed` | `EscalationService`: `failed` when the ticket store refused the request (the customer got `503 SUPPORT_UNAVAILABLE`) |
| `ticket_integration_failures` | counter | `event` | `publish_safely`: a ticket integration that failed after the ticket was saved |
| `faq_index_builds` | counter | `outcome`: `succeeded`, `failed` | `SemanticSearchService` building the semantic FAQ index (model load and embedding) |
| `faq_index_build_duration` | latency | none | Same: the build time the index already recorded |
| `faq_index_refreshes` | counter | `outcome` | The semantic index refresh after a staff FAQ change |
| `faq_catalog_refreshes` | counter | `outcome` | The FAQ catalogue re-read after a staff change; `failed` pauses customer answers until it succeeds |
| uptime | gauge | none | Time since `process.started_at` |

Reused as they are, not counted a second time:

| Already tracked by | Shown as |
|---|---|
| `LLMService.error_counts()`: LLM failures per category (`timeout`, `rate_limited`, `authentication`, `circuit_open`, …) | `llm.error_counts` |
| `UsageTracker.summary()`: LLM requests, tokens and estimated cost per model | `llm.usage` |
| The circuit breaker | `llm.paused` |

**Latency figures**: `count`, `average_ms` and `max_ms` are since the start. `p50_ms`,
`p95_ms` and `p99_ms` are nearest-rank percentiles over each series' **most recent 512
samples**, so they describe recent behaviour, and `recent_samples` says how many they
rest on (a percentile over 3 samples means little). Durations are server-side: they do
not include the network between the customer and the server.

## Labels and personal data

- **No IDs.** Routes are templates (`/api/v1/conversations/{session_id}/chat`), never
  paths; a request that matched no route is counted as `unmatched`. Session, ticket,
  FAQ and staff IDs, tokens, IP addresses and any request text never become a label or a
  value.
- **Bounded.** Methods outside the standard set count as `other` (a client can send any
  token as a method). Every metric is capped at 200 label combinations; anything beyond
  that is folded into `other`, so scanners and odd clients cannot grow memory without
  limit. A test sends random paths, IDs and methods and checks the series stay few.
- **Rate-limited requests** are refused before routing, so they count under `unmatched`
  with category `RATE_LIMITED`.
- Nothing here is logged except the periodic snapshot, which holds counts and timings only.

## Overhead

Recording takes a lock, a dictionary update and, for a latency, an append to a bounded
window, with no I/O. Measured: **about 6 µs per request** for its counter and latency,
against requests that take milliseconds. Percentiles are computed only when a snapshot is
read (about 0.03 ms). Memory is bounded: at most 200 series per metric, each holding 512
numbers.

## Access

- `GET /api/v1/admin/monitoring` needs a staff token with the `view_analytics`
  permission (the `admin` role). No token: `401 STAFF_AUTH_REQUIRED`; the `support`
  role: `403 STAFF_FORBIDDEN`. Responses are `Cache-Control: no-store`.
- Like every staff route, it is **not proxied by Nginx**: the public site's allow-list
  excludes `/api/v1/admin/*`, and the dashboard listener proxies no API
  ([docs/deployment-architecture.md](deployment-architecture.md), section 14). Staff read
  it through the dashboard (**Service monitoring**), which calls the backend over the
  private admin network.
- It is not part of the health checks: `/health`, `/ready` and `/status`
  ([docs/health-checks.md](health-checks.md)) stay cheap and say nothing about traffic.

Reading it from the server without the dashboard:

```sh
docker compose exec backend python -c "import json, os, urllib.request as u; \
  r = u.Request('http://127.0.0.1:8000/api/v1/admin/monitoring', \
  headers={'Authorization': 'Bearer ' + os.environ['TOKEN']}); \
  print(json.dumps(json.load(u.urlopen(r)), indent=1))"
# with TOKEN passed via: docker compose exec -e TOKEN=stf_... backend ...
```

A token typed on the command line stays in the shell history; prefer the dashboard.

The history in the logs:

```sh
docker compose logs --no-log-prefix backend | jq -c 'select(.event == "metrics_snapshot")
  | {timestamp, uptime_seconds, requests_total, server_errors, chat_turn_p95_ms, db_errors, ticket_failures}'
```

A falling `uptime_seconds` between two snapshots marks a restart: the figures after it
start again from zero.

## What to look at

| Question | Where |
|---|---|
| Is the service failing requests? | `requests.server_errors`, `requests.by_error_category` (`SERVICE_UNAVAILABLE`, `INTERNAL_ERROR`) |
| Is chat slow, and where? | `chat_turn_duration` p95, then `chat_stage_duration`: `pipeline` is retrieval and generation; `retrieval_duration` and `llm_call_duration` split it; `store` and `db_transaction_duration{kind=write}` show database contention |
| Is the LLM failing or paused? | `llm.error_counts`, `llm.paused` (customers then get approved FAQ answers) |
| Did support requests get lost? | `ticket_submissions{outcome=failed}`: each one is a customer who saw "temporarily unavailable" (resending is safe; duplicates are recognised) |
| Is search degraded? | `faq_index_builds` / `faq_index_refreshes` `failed`, together with `/ready`'s `semantic_search` |
| Did the backend restart? | `process.started_at`, `uptime_seconds` |

## Not covered

- **Alerting.** Nothing notifies anyone. An external uptime monitor on `/healthz`, plus
  log-based alerts on `event=metrics_snapshot` fields (or on `level=ERROR`), is the
  lightest next step.
- **Nginx, the pages and the host** (connections, CPU, memory, disk) are not measured
  here. `docker stats` and the host's own tools cover the host. The pages log every
  backend call ([docs/logging.md](logging.md)).
- Failed chat turns are counted as errors (`http_errors`), but their stage times are not
  recorded: only stored turns have a complete set of stages.

## Tests

`tests/test_monitoring.py`: percentiles over the recent window, all-time count and
maximum, impossible durations ignored, the series cap and `other` fold, method
normalisation, thread safety and cost, uptime. The endpoint's permissions (`401`, `403`,
`200`) and `no-store`; the `scope` statement; request, status and error-category counts;
a chat turn's stages, retrieval and database timings with no session ID, token or message
text in the response; a failed support request counted; index build failures and
successes counted; LLM latency recorded with failures taken from the existing counters;
database errors counted, application exceptions not; random paths and methods creating
no series; the log snapshot's fields, its periodic task and the final snapshot at
shutdown (and none when disabled). The dashboard section renders in
`tests/test_admin_frontend.py`, and the route is in the authorization coverage test.
