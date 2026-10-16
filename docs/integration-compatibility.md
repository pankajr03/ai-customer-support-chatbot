# Frontend–backend integration compatibility report (Stage 9)

An audit of the Streamlit frontend (`frontend/api_client.py`) against the FastAPI backend (`app/api/routes/`, `app/models/`), done on 2026-10-16. Each item compares what the frontend sends or expects with what the backend actually does, verified in code, against the running app and by tests.

**Result:** no blocking mismatch. The frontend and backend agree on every method, path, field, header, idempotency key, status code and error code the page uses. The audit found **five smaller problems**, all fixed without breaking changes, and **one missing capability** (readiness), now added. The checks are now automated in `tests/test_frontend_contract.py`, so a future change on either side that breaks the frontend fails the test suite.

## How the contract is kept in one place

The frontend has its own small Pydantic models instead of importing `app.models`. Importing the backend would load its services (and, through them, settings, the database layer and search code) into the Streamlit process, which this project deliberately avoids. `tests/test_frontend_config.py` checks that the frontend never imports `app`, `torch`, `sentence_transformers`, `openai` or `tiktoken`.

These models are **projections, not copies**: each one declares only the fields the page reads and ignores the rest (`extra="ignore"`), so the backend can add fields freely. To stop them drifting, `tests/test_frontend_contract.py` checks each frontend model against its backend counterpart:

| Frontend model | Backend response model | Endpoint |
|----------------|------------------------|----------|
| `ReadinessStatus`, `ReadinessChecks` | `ReadinessResponse`, `ReadinessChecks` | `GET /api/v1/ready` |
| `HealthStatus` | `HealthResponse` | `GET /api/v1/health` |
| `ConversationCreated` | `ConversationCreateResponse` | `POST /api/v1/conversations` |
| `ConversationInfo` | `ConversationSessionResponse` | `GET /api/v1/conversations/{id}`, `POST .../close` |
| `ChatReply`, `SourceReference` | `CustomerChatResponse`, `ChatSourceReference` | `POST .../chat` |
| `_HistoryPage`, `HistoryMessage` | `ConversationHistoryResponse`, `ConversationMessageResponse` | `GET .../messages` |

For each pair, the test checks that every field the frontend reads exists in the backend model, that a field the frontend requires is never nullable on the backend, that the backend's documented OpenAPI examples parse with the frontend model, and that enum values are identical on both sides.

## Findings

| # | Area | Finding | Severity | Resolution |
|---|------|---------|----------|------------|
| 1 | Health / readiness | The page decided "Online" from `GET /health`, which only proves the process is running. With conversation storage broken, the badge said "Online", the page tried to start a conversation and the customer got a `503` error instead of a clear "unavailable" | Medium | New `GET /api/v1/ready` endpoint; the page now uses it (see [Readiness](#readiness)) |
| 2 | Timestamps | The frontend accepted timestamps without a time zone. The backend always sends UTC with `Z`, but if one ever arrived without an offset, the expiry check (`expires_at - now(UTC)`) would have crashed the page with a `TypeError` | Low | Frontend timestamps are now `AwareDatetime`: a timestamp without an offset is reported as a contract error (`INVALID_RESPONSE`), not a crash |
| 3 | Configuration | The frontend read `.env` from the **current directory**; the backend reads it from the **project root**. Started from another directory, the page silently ignored `.env` (for example `BACKEND_API_URL`) | Low | The frontend now resolves `.env` from the project root, the same file as the backend |
| 4 | Message validation | The backend removes whitespace **and zero-width characters** (U+200B, U+200C, U+200D, U+2060, U+FEFF) from the ends of a message; the frontend removed only whitespace. A message of only zero-width characters was sent and refused with `422`, and the page's length check counted characters the backend ignores | Low | The frontend trims exactly like the backend (`frontend.state.strip_message`); a test compares the two |
| 5 | Startup errors | An invalid setting stopped both services with a long Pydantic traceback; an invalid frontend setting showed the customer a generic "Something went wrong" | Low | The backend exits with one line per invalid variable (names, never values); the page shows which frontend variable is invalid (see [Startup](#startup-errors)) |
| 6 | Readiness 503 body | A new endpoint that returns `503` would break the rule "every error body has an `error` object" | Design | A not-ready `503` returns the readiness checks **plus** the standard `error` object (`SERVICE_UNAVAILABLE`), so generic error handling still works |

## Item-by-item audit

### HTTP methods and URL paths

All requests use the configurable prefix `BACKEND_API_PREFIX` (default `/api/v1`, must equal the backend's `API_V1_PREFIX`; `scripts/check_setup.py` compares them).

| Frontend call | Method and path | Backend route | Match |
|---------------|-----------------|---------------|-------|
| `check_readiness` | `GET /ready` | `health.ready` (new) | Yes |
| `check_health` | `GET /health` | `health.health` | Yes |
| `create_conversation` | `POST /conversations` | `conversations.create_conversation` | Yes |
| `get_conversation` | `GET /conversations/{session_id}` | `conversations.get_conversation` | Yes |
| `get_history` | `GET /conversations/{session_id}/messages?offset&limit` | `conversations.get_messages` | Yes |
| `send_message` | `POST /conversations/{session_id}/chat` | `conversations.chat` | Yes |
| `close_conversation` | `POST /conversations/{session_id}/close` | `conversations.close_conversation` | Yes |
| `delete_conversation` | `DELETE /conversations/{session_id}` | `conversations.delete_conversation` | Yes |

Test: `test_every_call_matches_a_backend_route_method_and_header` drives every client method against the real app and matches each request with an operation in the backend's OpenAPI schema.

### Request JSON

Only `POST .../chat` has a body: `{"message": str, "client_message_id": "<uuid>"}`. The backend model (`CustomerChatRequest`) forbids unknown fields; the frontend sends no others and never sends the optional `category`. The test validates the exact body the client sends with `CustomerChatRequest`.

### Response JSON

All fields the frontend reads are present and typed compatibly (see the table above). Fields the backend sends that the frontend ignores: `usage`, the deprecated `user_message` / `assistant_message` on chat replies, `offset` / `limit` on history pages, `Location` on create. `generation_method` is read as a plain string because the page only uses it for a caption; the backend's four values (`llm_grounded`, `approved_faq`, `clarification`, `fallback`) are all labelled in `frontend/formatting.py`.

### Authorization headers

`Authorization: Bearer <token>` on every conversation request except create; none on create, health and readiness. The token is never put in a URL (the backend rejects that with `400`) and is a `SecretStr` on the frontend. Redirects are not followed, so the header cannot be forwarded elsewhere. Checked per request by the route test.

### Idempotency identifiers

The frontend generates one `client_message_id` (UUID4) per message and reuses it for every retry, including automatic retries after a network error, `409`, `429` or `503`. The backend stores it per conversation and returns the stored reply for an identical retry with `Idempotent-Replayed: true`, which the client turns into `ChatReply.replayed`. The frontend does not use the alternative `Idempotency-Key` header. The client also checks that the reply's `session_id` and `client_message_id` are the ones it sent.

### HTTP status codes

| Endpoint | Success | Frontend check |
|----------|---------|----------------|
| `POST /conversations` | `201` | Requires exactly `201` |
| `DELETE /conversations/{id}` | `204` | Requires exactly `204`; otherwise the conversation is kept |
| `GET /ready` | `200` ready, `503` not ready | Both are answers, not errors |
| Everything else | `200` | Any `2xx` |

### Error response schema

Every backend error is `{"error": {"code", "message", "request_id", "details"?}}`. The frontend reads `code`, `request_id` (or the `X-Request-ID` header), `details[].type`, `details[].ctx.max_length` and the `Retry-After` header. The `message` text is never shown: the page has its own wording per error kind.

Every `ErrorCode` the backend defines maps to an explicit frontend behaviour, and `test_every_backend_error_code_is_handled_by_the_frontend` fails if the backend adds a code the frontend has not mapped:

| Status | Code | Frontend `ErrorKind` | What the page does |
|--------|------|----------------------|--------------------|
| `422` | `VALIDATION_ERROR` | `INVALID_MESSAGE` | Puts the message back in the input with the reason (e.g. the length limit from `ctx.max_length`) |
| `400` | `VALIDATION_ERROR` | `CLIENT_ERROR` | Never sent by this client (unreadable body, token in URL) |
| `401` | `UNAUTHORIZED_SESSION` | `UNAUTHORIZED` | Ends the conversation (a frontend bug) |
| `404` | `SESSION_NOT_FOUND` | `SESSION_NOT_FOUND` | Offers a new conversation, keeping the unsent message |
| `404` | `NOT_FOUND` | `CLIENT_ERROR` | Wrong `BACKEND_API_URL` or prefix; also triggers the `/health` fallback for an older backend without `/ready` |
| `405` | `METHOD_NOT_ALLOWED` | `CLIENT_ERROR` | Not expected |
| `409` | `SESSION_CLOSED` | `SESSION_CLOSED` | Conversation ended; offers a new one |
| `409` | `REQUEST_IN_PROGRESS` | `BUSY` | Retried automatically with the same ID |
| `410` | `SESSION_EXPIRED` | `SESSION_EXPIRED` | Offers a new conversation |
| `413` | `PAYLOAD_TOO_LARGE` | `INVALID_MESSAGE` | As `422` |
| `422` | `DUPLICATE_MESSAGE_CONFLICT` | `MESSAGE_CONFLICT` | Not retried (a frontend bug) |
| `429` | `RATE_LIMITED` | `RATE_LIMITED` | Retried after `Retry-After` if short, otherwise shown |
| `503` | `SERVICE_UNAVAILABLE` | `SERVICE_UNAVAILABLE` | Retried, then "Try again" |
| `500` | `INTERNAL_ERROR` | `SERVER_ERROR` | "Try again" |

### Timestamp formats

The backend stores and returns timezone-aware UTC timestamps, serialized as ISO 8601 with microseconds and `Z`, for example `2026-10-16T07:09:48.033419Z` (`created_at`, `updated_at`, `expires_at`). The frontend parses them as `AwareDatetime` (finding 2). Tested by `test_backend_timestamps_are_utc_with_an_offset` and `test_a_timestamp_without_an_offset_is_a_contract_error`.

### Optional and nullable fields

| Field | Backend | Frontend |
|-------|---------|----------|
| `expires_at` | Nullable (`null` when `CONVERSATION_TTL_MINUTES` would not expire; currently always set) | `AwareDatetime \| None` |
| `client_message_id` (chat reply) | Nullable when none was sent | Nullable; must equal the sent ID when present |
| `request_id` (chat reply) | Nullable | Nullable |
| `decision` (history message) | `null` for user messages | Nullable |
| `usage` (chat reply) | Nullable | Not read |
| `error` (readiness) | Only present when not ready | Not read (the status decides) |

The test checks generally that no field the frontend requires is nullable on the backend.

### Pagination behaviour

`GET .../messages` takes `offset` (≥ 0) and `limit` (1–100, default 50) and returns messages oldest first with `total_messages` and `has_more`. The frontend asks for pages of 100 (the backend's maximum, checked by a test). For a long conversation it loads only the newest `MAX_HISTORY_MESSAGES` (1,000), skipping straight to `offset = total - 1000`. It keeps a message that appears on two pages (one stored while paging) only once, and rejects a page for another conversation. Paging stops on `has_more = false` or an empty page.

### Limits shared by configuration

| Frontend setting | Backend setting | Relationship |
|------------------|-----------------|--------------|
| `MAX_MESSAGE_CHARACTERS` (4,000) | `MAX_CUSTOMER_MESSAGE_CHARACTERS` (4,000) | Defaults equal (tested). If they differ, the backend's limit wins: its `422` carries `ctx.max_length`, which the page adopts. `scripts/check_setup.py` reports a difference |
| `BACKEND_API_PREFIX` | `API_V1_PREFIX` | Must be equal; `scripts/check_setup.py` fails if not |
| `BACKEND_READ_TIMEOUT_SECONDS` (60) | LLM timeouts and retries | A turn with the LLM can take longer than 60 s only with large `OPENAI_TIMEOUT_SECONDS` / retries. Without `SEMANTIC_INDEX_PRELOAD`, the **first** message also loads the search model, which can exceed 60 s; the page then shows a timeout, and retrying with the same ID is safe |

## Readiness

`GET /api/v1/ready` reports whether chat requests can be answered now. Only local dependencies decide it; the rest is reported but never loaded or called:

| Check | Values | Affects readiness |
|-------|--------|-------------------|
| `knowledge_base` | `ok`, `unavailable` (no active FAQs) | Yes |
| `conversation_storage` | `ok`, `unavailable` (cannot open, or a table is missing) | Yes |
| `semantic_search` | `ready`, `not_loaded` (loads on first question), `unavailable` (failed to load recently) | No: chat falls back to keyword-only retrieval or `retrieval_unavailable` |
| `llm` | `disabled`, `enabled` (key set, not tested), `paused` (circuit breaker open) | No: chat falls back to the approved FAQ answer |

The page maps it to a sidebar badge: **Online**; **Online** with "the first answer may take a little longer" (`not_loaded`); **Limited** (`unavailable` semantic search); **Unavailable** (not ready, or unreachable), in which case it does not try to start a conversation. A backend without `/ready` (older than Stage 9) answers `404`, and the page falls back to `/health`.

**How the page tells "unavailable" from "no match".** A `no_match` reply is a normal `200` chat answer ("I couldn't find a reliable answer") and the badge stays Online. Search being down inside a turn is `200` with `decision: retrieval_unavailable` and an "Ask again" button. The whole service being down is the **Unavailable** badge plus an error notice with "Try again".

## Startup errors

- **Backend:** invalid settings stop `uvicorn` with `Cannot start: N invalid setting(s)` and one line per variable, naming it and the rule it broke, never its value. A missing or invalid FAQ file stops it with `Cannot start: the FAQ knowledge base failed to load` and a pointer to `FAQ_DATA_PATH`.
- **Frontend:** invalid settings are shown on the page and logged as `Invalid frontend settings ...: BACKEND_API_URL: ...`, without values.
- **Both at once:** `python scripts/check_setup.py` checks both services' settings and the local dependencies without starting anything.

## Not changed, deliberately

- **Deprecated `user_message` / `assistant_message`** in chat replies stay until their announced removal (README, Stage 9 preview). The frontend does not read them.
- **`generation_method` stays a string** on the frontend: it is only displayed, and a new method should not break the page.
- **No shared schema package.** A package used by both services would couple the Streamlit process to backend code. The projection models plus the contract test give the same safety with no coupling.
