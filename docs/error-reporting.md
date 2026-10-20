# Safe error reporting (Stage 13)

What a customer, a page or an integration sees when something fails in production, and
what the operator gets instead. The handlers live in
[app/api/errors.py](../app/api/errors.py) and
[app/core/middleware.py](../app/core/middleware.py); the body model is in
[app/models/errors.py](../app/models/errors.py). Every scenario below is exercised with
`APP_ENV=production` in
[tests/test_safe_error_reporting.py](../tests/test_safe_error_reporting.py).

## The rules

| Rule | How it holds |
|------|--------------|
| Safe customer-facing errors | Every error is `{"error": {"code", "message", "request_id", "retryable"}}` (plus `details` for validation). `message` is one of our own fixed sentences, never an exception's text |
| Request IDs preserved | `request_id` equals the `X-Request-ID` header and appears on every log record of the request, including the `event=api_error` line and any traceback. The pages show it as "Reference: …" |
| Diagnostics server-side | One `event=api_error` record per error response: status, `error_category` (the code), `retryable`, `cause` (exception type), method and route template. Known failures add a descriptive line (which store, which FAQ file, which model); unexpected ones log the full traceback |
| No stack traces in responses | An unhandled exception becomes `500 INTERNAL_ERROR` with a generic message in `RequestContextMiddleware`. `DEBUG=true`, which would render tracebacks, is refused at startup when `APP_ENV=production` |
| No environment values | Responses never contain settings. A configuration error stops the process naming the variable and the rule, never the value (`describe_settings_error`). The log formatter masks keys, bearer tokens, emails and phone numbers as a last line of defence ([docs/logging.md](logging.md)) |
| Recoverable vs non-recoverable | `retryable` in the body (and in the log record). See below |
| Approved fallbacks preserved | LLM failures give the approved FAQ answer verbatim; an unavailable embedding model gives keyword-only retrieval or the safe "temporarily unavailable" reply. Both are normal `200` replies, not errors |
| Critical failures never hidden | Fallbacks are logged at ERROR, counted in the monitoring metrics, and reflected in `/ready` where they degrade service. See "Visibility" below |

## Recoverable and non-recoverable errors

`retryable` follows from `code` alone (`RETRYABLE_CODES` in
[app/models/errors.py](../app/models/errors.py)):

| `retryable` | Codes | Meaning | Client action |
|-------------|-------|---------|---------------|
| `true` | `SERVICE_UNAVAILABLE` (503), `RATE_LIMITED` (429), `REQUEST_IN_PROGRESS` (409) | A temporary condition: storage busy or down, a dependency unavailable, a limit, another message being answered | Send the **same** request again later, with the same `client_message_id` / `client_request_id` (nothing is duplicated). Wait for `Retry-After` when it is present |
| `false` | everything else, including `INTERNAL_ERROR` (500) | Retrying unchanged will not help | Fix the request, start a new conversation, sign in again, or report the `request_id`. A 500 is an unexpected fault: it is logged with its traceback and should be investigated, not retried in a loop |

`Retry-After` is sent with `429`, with `409 REQUEST_IN_PROGRESS`, with `503` for a locked
database (conversation, FAQ and ticket storage: `1` second), and with `503` from
semantic search when the model is not being retried yet. A `503` without it (for example
storage that cannot be opened) is still retryable, but nothing says when it will recover.

The customer page keeps its own classification (`frontend/api_client.py`, `RETRYABLE`),
which agrees with this one and adds the transport failures only the page can see:
backend unreachable, timeout, broken connection.

## Scenarios

| Scenario | Customer / caller gets | `retryable` | Server-side record |
|----------|------------------------|-------------|--------------------|
| Backend unavailable | The page shows a connection message with a "Try again" action; the typed message is kept | page: yes | Page log: WARNING `backend unreachable`, `error_category=backend_unreachable` |
| OpenAI timeout | `200`, the approved FAQ answer verbatim (`generation_method: approved_faq`) | n/a | ERROR `LLM APITimeoutError after N attempt(s); giving up`; `llm.errors.timeout` counter; repeated failures pause the LLM (`/ready`: `llm=paused`) |
| Embedding model unavailable | Chat: `200` with keyword-only retrieval or the safe "temporarily unavailable" reply (with a reference). `POST /semantic-search`: `503 SERVICE_UNAVAILABLE` | yes | ERROR `Failed to load embedding model …` with the cause; `/ready`: `semantic_search=unavailable` |
| Database locked | `503 SERVICE_UNAVAILABLE`, `Retry-After: 1` | yes | ERROR naming the store and `*StoreBusyError`; `db_transaction_errors` counter |
| Invalid environment configuration | The process does not start. The message names each variable and rule, never a value | no | stderr: `Cannot start: N invalid setting(s) …` |
| Missing API key | With `LLM_ENABLED=true`: refuses to start (`LLM_ENABLED=true requires OPENAI_API_KEY`). With the LLM off: approved FAQ answers | no | Startup line `LLM generation disabled`; `/ready`: `llm=disabled` |
| Invalid session | `404 SESSION_NOT_FOUND`, identical for an unknown session and a wrong token | no | INFO `event=api_error … SESSION_NOT_FOUND` |
| Staff authorization failure | `401 STAFF_AUTH_REQUIRED`, `403 STAFF_FORBIDDEN` or `403 STAFF_ACCESS_DISABLED`; the missing permission is not named | no | INFO `event=api_error`; `auth.access_denied` in the audit log (never the token) |
| FAQ index-refresh failure | The staff edit is saved and served at once; customer searches use keyword-only retrieval until the index is rebuilt (never the stale index) | n/a | ERROR `event=semantic_index_refresh_failed`; `faq_index_refreshes{outcome=failed}` counter; `/ready`: `semantic_search=unavailable` until a rebuild succeeds |
| Support ticket creation failure | `503 SERVICE_UNAVAILABLE` ("sending the same request again will not create a duplicate"); `Retry-After` when the database was locked | yes | ERROR `Support storage unavailable …: TicketStoreError` (or `TicketStoreBusyError`); `ticket_submissions{outcome=failed}` counter |
| Anything unexpected | `500 INTERNAL_ERROR`, generic message, `Cache-Control: no-store` | no | ERROR `Unhandled error on METHOD route` with the traceback (frames kept, exception messages withheld unless `DEBUG=true`) |

## Visibility: where an operator sees each failure

- **Logs** (`docker compose logs backend`, JSON lines): every error response has an
  `event=api_error` record; 5xx records are WARNING or ERROR. Select the recoverable
  ones with `jq 'select(.event=="api_error" and .retryable)'`.
- **Readiness** (`GET /api/v1/ready`, private network): optional dependencies that are
  degraded show as `semantic_search: unavailable` or `llm: paused` while chat stays
  `ready`. Essential ones (knowledge base, conversation storage) make it `503`.
- **Monitoring** (`GET /api/v1/admin/monitoring`, dashboard **Service monitoring**):
  error counts by category, ticket and FAQ-index failures, LLM errors by category
  ([docs/monitoring.md](monitoring.md)). A copy goes to the log every five minutes.

## Changes in this task

- Error bodies gained `retryable` (additive; existing fields unchanged). The log
  record of every error response carries it too.
- `/ready` no longer reports `semantic_search: ready` for an index built from an older
  FAQ revision: after a failed refresh it reports `unavailable` until a rebuild
  succeeds. The refresh failure is logged at ERROR (was WARNING).
- A support request refused because the database was locked now gets `Retry-After: 1`,
  and the log names the real cause (`TicketStoreBusyError` / `TicketStoreError`)
  instead of the service wrapper.
- `500 INTERNAL_ERROR` responses carry `Cache-Control: no-store`.
