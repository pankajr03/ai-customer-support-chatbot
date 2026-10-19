# Structured application logging (Stage 13)

All three application services (the FastAPI backend, the customer page and the staff
dashboard) and the backup scheduler write their logs to **standard output**, in one of
two formats chosen by `LOG_FORMAT`. Docker collects standard output with its
`json-file` driver (`deploy/compose.yaml`: 10 MB per file, 5 files per container). No
logging platform, agent or paid service is used. Any collector that reads Docker's logs
or JSON lines (Loki, Vector, Fluent Bit, the ELK stack, plain `jq`) can be added later
without changing the application.

| Where | `LOG_FORMAT` | Why |
|---|---|---|
| Docker deployment (`deploy/compose.yaml`) | `json`, fixed by compose | One JSON object per line, fixed field names, a traceback stays inside its record |
| Development override, local runs | `text` (the default) | Readable lines in a terminal |

`LOG_LEVEL` (default `INFO`) applies to all of them. The production and staging
environment files set `INFO`.

## Fields

Every record has the **standard fields**; the others appear when they apply. Fields
without a value are left out instead of being written as `null`.

| Field | Example | Meaning |
|---|---|---|
| `timestamp` | `2026-10-19T13:31:43.828Z` | UTC, ISO 8601, milliseconds |
| `level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `service` | `ai-customer-support-chatbot` | Backend: `SERVICE_NAME`. Pages: `customer-frontend`, `admin-frontend`. Backups: `backup` |
| `logger` | `app.request` | The Python logger (module) |
| `request_id` | `411e8580…` | The backend's ID of one HTTP request, also returned in `X-Request-ID` and in every error body. On a page's record: the backend request that call produced |
| `correlation_id` | `d04971b1…` | One page action (below), on the page's records and on every backend record it caused |
| `event` | `http_request` | What happened, as a stable name: `http_request`, `api_error`, `backend_call`, `conversation_turn`, `answer_decision`, `grounded_answer`, `ticket_submitted`, `faq_updated`, `metrics_snapshot` ([docs/monitoring.md](monitoring.md)), … |
| `message` | `POST /api/v1/conversations -> 201 (106.0 ms)` | The same record in words (the text format prints only this) |
| `method` | `POST` | HTTP method |
| `route` | `/api/v1/conversations/{session_id}/chat` | The route **template**, never the path: session, ticket and FAQ IDs stay out of access records. `unmatched` when no route matched |
| `path` | `/api/v1/does-not-exist` | Only with `route: unmatched`: the path as sent, escaped (control characters cannot forge lines) |
| `status` | `404` | HTTP status |
| `duration_ms` | `21.4` | Time spent on the request (backend), or waiting for the backend (pages) |
| `error_category` | `SESSION_NOT_FOUND` | For a failure: the API error code (`docs/chat-api.md`), the same one the client receives; on the pages, that code or the page's error kind (`timeout`, `backend_unreachable`, …) |
| `cause` | `ConversationNotFoundError` | On `api_error`: the exception type, never its message |
| `error_type`, `exception` | `RuntimeError`, `Traceback …` | An unexpected error: every frame, but the exception message replaced by `[message not logged]` |

Domain events add their own fields (for example `answer_decision` adds `decision`,
`retrieval_mode`, `faq_id`, `top_similarity`). A structured field can never replace a
standard one.

### Examples

An access record and the error record of the same request (backend, JSON):

```json
{"timestamp": "2026-10-19T13:11:42.054Z", "level": "INFO", "service": "ai-customer-support-chatbot", "logger": "app.errors", "request_id": "11c1738d9a594142a274abee9f90b5de", "correlation_id": "3f2b8c1e7a4d4e9b9c612d5f0a8e4b17", "event": "api_error", "message": "event=api_error status=404 code=NOT_FOUND cause=HTTPException method=GET route=/api/v1/faqs/{faq_id}", "status": 404, "error_category": "NOT_FOUND", "cause": "HTTPException", "method": "GET", "route": "/api/v1/faqs/{faq_id}"}
{"timestamp": "2026-10-19T13:11:42.054Z", "level": "INFO", "service": "ai-customer-support-chatbot", "logger": "app.request", "request_id": "11c1738d9a594142a274abee9f90b5de", "correlation_id": "3f2b8c1e7a4d4e9b9c612d5f0a8e4b17", "event": "http_request", "message": "GET /api/v1/faqs/{faq_id} -> 404 (13.6 ms) error=NOT_FOUND", "method": "GET", "route": "/api/v1/faqs/{faq_id}", "status": 404, "duration_ms": 13.6, "error_category": "NOT_FOUND"}
```

The same access record in the text format:

```
2026-10-19 18:11:42,054 | INFO     | app.request | req=11c1738d9a594142a274abee9f90b5de | GET /api/v1/faqs/{faq_id} -> 404 (13.6 ms) error=NOT_FOUND
```

## Correlating the pages and the backend

1. Each **page action** (one Streamlit script run: the page loading, a message sent, a
   button pressed) gets a new random correlation ID (`frontend/log_setup.py`).
2. Every backend call made during that action, **automatic retries included**, sends it
   as `X-Correlation-ID`.
3. The backend accepts it only in a fixed shape (8 to 64 letters, digits, `-` or `_`,
   starting with a letter or digit); anything else is ignored, so a caller cannot forge
   or garble log lines with it. It is logged next to the backend's own `request_id`,
   **never instead of it**: request IDs are still generated by the server.
4. The page logs one `backend_call` record per call, with the backend's `request_id`
   from the response and the `correlation_id`.

So, starting from what a customer reports (the reference shown on the page is the
`request_id`):

```sh
# Everything about one backend request, on every service:
docker compose logs --no-log-prefix backend customer | jq -c 'select(.request_id == "70dd9c41d5c54bf1818c0a6ae67f000e")'
# Then the whole page action it belonged to (several requests, retries included):
docker compose logs --no-log-prefix backend customer | jq -c 'select(.correlation_id == "8d035937fb9648a3b13d018d826ec807")'
```

Other useful queries:

```sh
docker compose logs --no-log-prefix backend | jq -c 'select(.level == "WARNING" or .level == "ERROR")'
docker compose logs --no-log-prefix backend | jq -c 'select(.event == "http_request" and .status >= 500)'
docker compose logs --no-log-prefix backend | jq -r 'select(.error_category) | .error_category' | sort | uniq -c
docker compose logs --no-log-prefix backend | jq -c 'select(.event == "http_request" and .duration_ms > 5000)'
```

Other API clients may send their own `X-Correlation-ID` too (CORS allows the header).

## Levels

| Level | Used for |
|---|---|
| `DEBUG` | Successful health checks (`/health`, `/ready`, `/status`: Docker polls every 15 s, which would otherwise be most of the log), search candidate counts, context building. Not for production |
| `INFO` | **Production level.** One access record per request; business events (conversation turns, decisions, tickets, FAQ changes, staff sign-in); startup |
| `WARNING` | 5xx responses, degraded operation (semantic search unavailable, LLM retries, circuit opened), a page that cannot reach the backend |
| `ERROR` | Unexpected errors (with traceback), storage failures, invalid stored data |
| `CRITICAL` | The service cannot start, or customer answers are paused (FAQ catalogue unverified) |

The noisy libraries (`uvicorn.access`, `httpx`, `httpcore`, `openai`) are held at
`WARNING` whatever `LOG_LEVEL` says, because their `INFO`/`DEBUG` lines contain full URLs
or request details.

## What is never logged

| Data | How it is kept out |
|---|---|
| Customer messages, search queries, generated or approved answers, FAQ text | Never passed to a logger. Records carry IDs, counts, decisions, scores and timings only. FAQ content appears only as FAQ IDs (`faq_id`, `sources`) |
| Conversation and staff tokens, API keys | Never passed; the `Authorization` header and query strings are not logged. Redacted as a safety net (`Bearer …`, `cst_…`, `stf_…`, `sk-…`, `token=…`, `api_key: …`) |
| Contact details (names, email addresses, phone numbers) | Never passed: support requests log the ticket ID, type and outcome only. Email addresses and international phone numbers (`+44 20 …`) are redacted as a safety net. Bare digit runs are not, since they are far more often counts, durations and IDs |
| Session and ticket IDs in access records | The route template is logged instead of the path. Domain events (`conversation_turn`, `ticket_submitted`) do carry the session or ticket ID: an identifier needed for support, not a credential (reading a conversation needs its token) |
| Exception messages | Replaced by `[message not logged]` in tracebacks (they can quote customer text or SQL parameters). Kept only with `DEBUG=true`, which production refuses |
| Client-controlled text (an unmatched path) | Escaped: control characters, newlines and BiDi overrides cannot forge or hide lines; truncated at 200 characters |

Redaction runs on every string **before** it is encoded as JSON, so escaping can never
split a pattern. The backend and the pages use identical rules (a test compares them).

## No duplicate handlers

- **Backend:** one handler on the root logger; every logger (the app's, uvicorn's,
  libraries') propagates to it. `configure_logging` uses `dictConfig`, which replaces the
  root handlers, so repeated calls (tests, `--reload`) never duplicate output, and
  uvicorn's own handlers are removed so its lines share the format.
- **Pages:** Streamlit configures only its own `streamlit` logger. The pages put one
  handler on the `frontend` logger, with `propagate=False`, and replace it on every script
  run. Before Stage 13 nothing configured them: their `INFO` records (including the
  `request_id` of every backend call) were dropped, and warnings were printed bare.
- Streamlit's **own** messages (its startup banner, its internal warnings) keep
  Streamlit's text format; they carry no customer data.

## Docker

- Every record is one line, so `docker compose logs`, `docker logs --since`, and any
  line-based collector see whole records, tracebacks included.
- `docker compose logs --no-log-prefix <service>` prints the JSON lines without Compose's
  `service-1 |` prefix, ready for `jq`.
- Rotation: the `json-file` driver keeps 5 × 10 MB per container. A collector should
  ship logs off the server if they must be kept longer or searched across restarts.
- Nginx keeps its own access log format; it is not part of this change.

## Verified (2026-10-19)

**Two local processes:** a uvicorn backend and the customer page (Streamlit's AppTest),
both with `LOG_FORMAT=json`. A page load and one chat message containing a name, an
email address and a phone number:

- The page logged one `backend_call` record per call (readiness, create, chat), each
  with the backend's `request_id` and the action's `correlation_id`.
- The chat action's correlation ID was on every backend record of that request: the
  model load, `answer_decision`, `grounded_answer`, `conversation_turn` and the access
  record (`/api/v1/conversations/{session_id}/chat`).
- Neither log contained the name, email address, phone number, message text or token.

**Docker** (full stack, `DEPLOY_ENV=staging`, Docker 28.1 on WSL 2):

| Check | Result |
|---|---|
| Backend and backup logs | Every record one JSON line. The only other lines: the embedding library's progress bar while the model loads (backend) |
| Customer and admin logs | The pages' records are JSON; Streamlit's own startup banner is text, as expected |
| Through Nginx | `GET /api/v1/faqs/faq_999`: access record with `route: /api/v1/faqs/{faq_id}`, `error_category: NOT_FOUND`. A request with `X-Correlation-ID` carried it into the backend record |
| A page action in the customer container | The same `correlation_id` on the page's `backend_call` and on the backend's records, with matching `request_id` |
| Health checks | No `/ready` access records at `INFO`, though Docker polled it every 15 s |
| Error records | A storage failure showed as `ERROR` and `WARNING` records with `error_category: SERVICE_UNAVAILABLE` and the correlation ID, which made it quick to trace. It came from a reused database volume left over from earlier test runs, and did not recur after a restart or a clean `down`/`up` |

## Tests

| File | Covers |
|---|---|
| `tests/test_structured_logging.py` | Standard fields and ISO timestamps on every record; one valid JSON object per line; access records with route template, status, duration, request ID and error category (404, 401, 429, 500, unmatched); IDs kept out of access records; health checks at `DEBUG`, 5xx at `WARNING`; tracebacks inside one record without exception messages; correlation ID accepted, ignored when malformed, never echoed, never replacing the request ID; a full chat turn with contact details and a token logs none of them, nor FAQ text; phone redaction without false positives; no duplicate handlers; frontend records reach stdout once, with correlation; the pages send the correlation ID on every attempt and log the backend's request ID; frontend and backend redaction identical |
| `tests/test_logging.py` | Levels, text format, uvicorn sharing the format, quiet libraries, query strings never logged, redaction |
| `tests/test_error_handling.py`, `tests/test_tracing_cors_limits.py` | Error records, control characters escaped, request IDs, no customer data or secrets in a real chat turn |
