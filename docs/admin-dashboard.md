# Admin dashboard, staff roles and FAQ management (Stage 11)

> **Status: Tasks 2–15 implemented, awaiting review.** A separate Streamlit page for staff, with two staff roles enforced by the backend. It covers FAQs stored in SQLite and edited there with a revision history, retrieval that stays consistent while FAQs change, ticket handling with assignment and internal notes, and an overview and chatbot analytics computed from recorded data. Staff access is still a **development and small-team prototype**, not a production identity system ([limitations](#known-limitations)).

## Contents

- [What it adds](#what-it-adds)
- [Running the dashboard](#running-the-dashboard)
- [Staff authentication and roles](#staff-authentication-and-roles)
- [FAQ storage in SQLite](#faq-storage-in-sqlite)
- [Importing FAQs and backups](#importing-faqs-and-backups)
- [FAQ management rules](#faq-management-rules)
- [FAQ revision history](#faq-revision-history)
- [Keeping retrieval consistent while FAQs change](#keeping-retrieval-consistent-while-faqs-change)
- [The dashboard pages](#the-dashboard-pages)
- [Ticket handling, assignment and notes](#ticket-handling-assignment-and-notes)
- [Overview and analytics: what each figure means](#overview-and-analytics-what-each-figure-means)
- [Admin API](#admin-api)
- [Tests](#tests)
- [Known limitations](#known-limitations)

## What it adds

| Area | Details |
|------|---------|
| Dashboard | `frontend/admin_app.py`, a Streamlit page separate from the customer chat: **Dashboard overview**, **FAQ management**, **Support tickets**, **Analytics**, **Administration information**. Sections are shown by role; the backend enforces the role on every request regardless |
| Roles | `admin` (FAQ management, tickets, analytics) and `support` (tickets, with contact details). Configured per token in `STAFF_TOKEN_HASHES`; Stage 10 entries without a role are `support` |
| FAQ storage | A `faqs` table in the existing SQLite database is the editable source of truth. It is seeded once from `data/faqs.json` when empty and never overwritten at startup afterwards |
| Import | `python -m scripts.import_faqs`: validated, idempotent, one transaction, `--dry-run`, `--backup` |
| FAQ management | Create, list, search, filter, paginate, read, edit, deactivate and reactivate, with server-allocated IDs and optimistic concurrency (`version`). No permanent deletion |
| History | Every creation, edit, activation and deactivation recorded in `faq_revisions`, in the same transaction as the change ([below](#faq-revision-history)) |
| Consistent retrieval | Versioned catalogue snapshots, a version-aware semantic index that re-embeds only changed questions, and checks that no answer uses an outdated or inactive FAQ ([below](#keeping-retrieval-consistent-while-faqs-change)) |
| Tickets | Reference search, assignment and reassignment to configured staff (`assigned_at`, recorded as events), internal notes; the Stage 10 workflow unchanged ([below](#ticket-handling-assignment-and-notes)) |
| Overview and analytics | Live counts and chatbot decision analytics from the stored data, with formulas, minimum sample sizes and stated limitations ([below](#overview-and-analytics-what-each-figure-means)) |
| Admin API | `/api/v1/admin/me`, `/overview`, `/analytics`, `/staff`, `/faqs`, `/faqs/{id}/revisions`… ([below](#admin-api)) |

The customer chat (`frontend/app.py`) is unchanged: it has no staff controls, imports no admin code and calls no staff endpoint (a test checks this).

## Running the dashboard

1. Create an admin token and (optionally) a support token, and add the printed entries to `STAFF_TOKEN_HASHES` in `.env` (a JSON list):

   ```powershell
   python -m scripts.create_staff_token alex --role admin
   python -m scripts.create_staff_token maria --role support
   ```

   `.env`: `STAFF_TOKEN_HASHES=["alex:admin:<hash>", "maria:support:<hash>"]`. Each token is printed once; give it only to its owner.

2. Start the backend (it seeds the FAQ table on first start), then the dashboard on its own port:

   ```powershell
   uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
   streamlit run frontend/admin_app.py --server.port 8502
   ```

3. Open http://localhost:8502 and paste your staff token. The dashboard uses the same `BACKEND_API_URL` setting as the chat page.

Reloading the browser tab starts a new Streamlit session, so you sign in again. Run the dashboard only where staff can reach it (localhost or a VPN): it is not hardened for the open internet.

## Staff authentication and roles

### How staff credentials are verified (review of Stage 10)

The Stage 10 mechanism was reviewed and kept, with roles added:

- **Personal bearer tokens**, `stf_` + 32 random bytes (`secrets`), shown once by `scripts/create_staff_token.py`. The server stores only SHA-256 hashes, in `STAFF_TOKEN_HASHES` (from `.env`, which Git ignores). Nothing is hard-coded.
- **Constant-time comparison** (`hmac.compare_digest`) against *every* configured hash, so timing reveals neither which staff member matched nor how close a guess was.
- **Customer tokens never work.** A conversation token (`cst_…`) fails the `stf_` format check before any comparison, and is never in the staff store. Tested on every admin endpoint.
- **Headers only.** Tokens in a query string (`access_token`, `token`, `authorization`) are refused with `400` before anything else; the dashboard never puts the token in the URL, a cookie or browser storage.
- **Never logged.** Logs hold the staff ID and role at most; error bodies are short fixed sentences with a request ID (tested with captured logs).
- **Off by default.** With no entries every staff endpoint answers `403 STAFF_ACCESS_DISABLED`.

### Roles and permissions

| Permission | `admin` | `support` | Used by |
|------------|:------:|:--------:|---------|
| `manage_tickets` | ✓ | ✓ | `GET/PATCH /support/tickets…` |
| `view_customer_contacts` | ✓ | ✓ | contact details on `GET /support/tickets/{id}` |
| `manage_faqs` | ✓ | | `/admin/faqs…` |
| `view_analytics` | ✓ | | `/admin/overview` |

Every staff route declares the permission it needs (`require_staff_permission` in `app/api/dependencies.py`), and the services check it again (`StaffMember.require`), so a new caller cannot skip it. A role without the permission gets `403 STAFF_FORBIDDEN`; which permission was missing is logged, not returned. Support staff keep contact details because they need them to reply.

**Why legacy entries are `support`:** after upgrading, nobody gains FAQ editing rights by accident; an admin must be configured on purpose.

### What this is not

A development and small-team prototype. There is no password login, session expiry, token expiry or rotation schedule, multi-factor authentication, account lockout or audit of reads, and the per-IP rate limit is the only protection against guessing. Revoking a token means removing its entry and restarting the API. Before real use, put the staff endpoints and the dashboard behind an identity provider (SSO/OIDC) or at least a VPN, and serve them over HTTPS.

## FAQ storage in SQLite

**Table `faqs`** (in `DATABASE_URL`, next to conversations and tickets):

| Column | Notes |
|--------|-------|
| `id` | Primary key, the existing `faq_NNN` IDs (3–6 digits, CHECK constraint) |
| `question`, `answer` | Non-blank (CHECK), max 500 / 5000 characters |
| `category` | One of the `FAQCategory` values (CHECK) |
| `keywords` | The FAQ's tags: a JSON list, at least one (CHECK) |
| `is_active` | 0/1 |
| `version` | ≥ 1, +1 on every change |
| `created_at`, `updated_at` | Aware UTC timestamps, `updated_at >= created_at` (CHECK) |
| `created_by`, `updated_by` | Staff IDs; `NULL` for FAQs imported from JSON |

Indexes: `(category, is_active)`, `is_active`, `updated_at`. The table is in `REBUILDABLE_TABLES`, so a future change to its CHECK constraints (for example a new category) is applied by `init_schema` by copying every row in one transaction.

**Startup.** `create_app` creates missing tables, then: if the table is **empty**, it imports `FAQ_DATA_PATH` once (the file is validated in full; a bad file stops startup); if it has **any row**, the file is not read. Staff edits are therefore never overwritten by a restart, and nothing re-imports JSON per request.

**Retrieval stays fast.** `DatabaseFAQRepository` keeps a validated, immutable snapshot of the catalogue in memory with the same read interface as the old JSON repository, so no chat turn queries the FAQ table. Every committed change through the repository publishes a new snapshot at once ([consistency](#keeping-retrieval-consistent-while-faqs-change)).

**One process sees its own changes.** Changes made by another process (the import script while the API runs, a second API worker) appear after a restart. The dashboard edits through the API, so the API is always current.

## Importing FAQs and backups

```powershell
python -m scripts.import_faqs --dry-run    # what would happen; writes nothing
python -m scripts.import_faqs --backup     # back up the database, then import
python -m scripts.import_faqs --file data/client_faqs.json
```

- Reads and validates the whole file first (UTF-8 JSON, every record a valid FAQ, no duplicate IDs). Any problem: exit code `1`, the record and field are named, **nothing is written**.
- Adds only FAQs whose ID is not in the database, keeping ID, category, tags and active flag, in one transaction. The JSON file has no timestamps, so imported FAQs get the import time and no author.
- Existing IDs are never overwritten: identical ones are reported as unchanged; ones that differ (usually edited by staff) keep the database version and are listed for review.
- Running it again changes nothing. The JSON file is only read.
- Restart the API afterwards if FAQs were added.

**Backups.** `--backup` copies the SQLite database with SQLite's online backup API (safe while the API runs) to `data/backups/<name>-<UTC time>.db` (git-ignored via `*.db`). Manually: stop the API and copy `data/conversations.db` together with any `conversations.db-wal` and `conversations.db-shm` next to it. To restore, stop the API and copy the backup back over `data/conversations.db` (removing the `-wal`/`-shm` files).

## FAQ management rules

Implemented in `app/services/faq_management_service.py` (rules) over `app/repositories/faq_repository.py` (database); the routes in `app/api/routes/admin.py` only translate HTTP.

- **Validation** uses the knowledge base's own field types: questions trimmed with repeated spaces collapsed; answers trimmed only (line breaks kept); categories from the fixed list; 1–20 tags, each trimmed, lower-cased, de-duplicated, letters/digits/spaces/hyphens/apostrophes. Unknown fields (including `id`) are refused. Validation errors list fields and rules, never submitted values.
- **IDs** are allocated by the server inside the write transaction: one more than the highest ever used. They are never reused.
- **Optimistic concurrency.** Edits, deactivation and activation send `expected_version`. If the FAQ changed meanwhile: `409 FAQ_VERSION_CONFLICT`, nothing written; reload and reapply. An edit that changes nothing keeps the version.
- **Deactivate, don't delete.** A deactivated FAQ disappears from customer answers, search and the public FAQ API at once, but stays stored and can be reactivated. **Permanent deletion is not offered**: stored chat replies cite FAQ IDs (`source_faq_ids`), and an ID must keep meaning the same FAQ.
- **At least one FAQ stays active** (`409 FAQ_LAST_ACTIVE`), so the assistant always has something to answer with.
- **Logs** record the FAQ ID, the operation, the changed field names and the staff ID; never FAQ text.

## FAQ revision history

Table `faq_revisions`, written by the repository **in the same transaction** as the change it records, so the history never misses a committed change or records a rolled-back one.

| Column | Notes |
|--------|-------|
| `revision_id` | Random UUID |
| `faq_id`, `version` | The FAQ and its version *after* the change; unique together (CHECK and UNIQUE constraints) |
| `action` | `created`, `updated` (question, answer, category or tags, possibly with the active flag), `activated`, `deactivated` (only the active flag changed) |
| `changed_fields` | e.g. `["question", "keywords"]` |
| `previous_values`, `new_values` | JSON objects with **only the changed fields** (the full content for a creation, whose `previous_values` is `NULL`) |
| `changed_by`, `changed_at` | Staff ID (`NULL` for the JSON import); aware UTC time |

- Refused edits (version conflict, validation, last active FAQ) and edits that change nothing write no revision.
- Only FAQ content and a staff ID are stored: no tokens, no customer data. Changed fields only, so unchanged answers are not copied again and again.
- Admins read it with `GET /admin/faqs/{id}/revisions` (newest first, paginated) and under **History** in the FAQ editor. It is never part of a customer response.
- FAQs imported by Stage 11's first version (before history existed) have no creation entry; their history starts at their next change. Rollback is not automated: an admin can re-apply an earlier text from the history as a normal edit.

## Keeping retrieval consistent while FAQs change

### What was there before (review of Stages 3 and 4)

- **Stage 3** embeds every active FAQ question once and keeps the vectors with *copies of the FAQ objects* in an immutable in-memory index.
- **Stage 4** fuses keyword and semantic results; the semantic results carried the index's FAQ copies, and those overrode the keyword ones.
- Retrieval read the catalogue several times per turn (keyword search, the index, the answer, the sources).

With editable FAQs this allowed three problems: an index built before an edit could supply **old answer text as evidence**; one turn could combine **different versions** of an FAQ; and the first Stage 11 version re-embedded **every** FAQ after any change.

### How it works now

1. **Versioned snapshots** (`app/services/faq_catalog.py`). Each committed change publishes a new immutable `CatalogSnapshot` whose `revision` increases. Readers holding an older snapshot are unaffected.
2. **One snapshot per decision.** `AnswerDecisionService.decide` pins the latest snapshot for its duration (a context variable, so it applies only to that request). Keyword search, the semantic index and the chosen evidence all read that snapshot, and the stored FAQ objects are the snapshot's own.
3. **Version-aware semantic index.** Each index records the revision it was built from, and is never used for another one. A different revision triggers a rebuild that reuses cached vectors by question text: an answer, tag, category or activation change embeds **nothing** (the model is not even called), and a question edit embeds **only that question**. Inactive FAQs are never indexed. Concurrent requests build it once (lock and double-check). A request still pinned to an older revision gets an index for that revision from the cache, without replacing the newer published one.
4. **Changed during the decision?** If the catalogue's revision moved while deciding, the decision is made again on the new snapshot. If it moved again, the answer is the safe `retrieval_unavailable` message, never a decision on outdated FAQs.
5. **Changed after the decision?** Just before an answer leaves the pipeline (after any LLM call), `GroundedAnswerService` compares its evidence with the latest catalogue. If an evidence FAQ was edited or deactivated meanwhile, the reply becomes the safe `retrieval_unavailable` message, with no sources (logged as `event=faq_evidence_outdated`, fallback reason `knowledge_changed`).
6. **Proactive refresh.** After each change, FAQ management calls `SemanticSearchService.refresh()`, so the index is current before the next customer asks. It never loads the model for an index nobody has used yet.

### When the index cannot be rebuilt

If embedding fails (the model cannot load, or raises while encoding, which is now always an `EmbeddingError`), the stale index is **not** used: the error reaches the decision service, which answers from keyword-only retrieval over the **current** snapshot (strict degraded rules) or gives the safe `retrieval_unavailable` message. The refresh failure is logged (`event=semantic_index_refresh_failed`, error type only), and every later search retries the rebuild until it succeeds. Customers therefore get a cautious answer or a "try again shortly", never one based on outdated evidence.

### Guarantees and limits

- Never: an answer from an FAQ that is inactive when the answer is returned; evidence text from one FAQ version paired with another version's question or embedding; a partially built index.
- **Single process only.** Snapshots and the index live in one API process's memory. A second API worker, or the import script running alongside the API, is not seen until that process restarts. Multi-process synchronisation is not implemented (it would need, for example, polling the database for a catalogue revision).
- The rebuild is synchronous and lazy (or on the change itself, through `refresh`). With a large catalogue, a question edit costs one embedding call; there is no background queue.
- The final check compares the evidence's question and answer with the latest catalogue. A change made in the last instant between that check and the response being sent is not detected; it is at most one request old and was valid when checked.

## The dashboard pages

All pages are native Streamlit components and talk only to the protected API, through `frontend/admin_api_client.py`. Neither the page nor any part of it opens the database.

- **FAQ management** (`frontend/admin_faqs.py`):
  - The list is a table (FAQ ID, question, category, status, version, last updated), with search over question, answer and tags (or an exact ID), category and status filters, and pages of 20.
  - Every change goes through a form: **Add an FAQ**, the edit form, and activate/deactivate. Deactivating asks for confirmation; activating does not.
  - The editor keeps the version the user opened. If someone else saves first, the backend refuses with `409`, and the page says it was **not saved**, keeps the user's text visible to copy, and loads the latest version. The page never claims success before the backend returns the saved FAQ; the list then reloads.
  - The page checks required fields and tag limits before sending; the backend validates again and its field messages are shown.
  - A submitted form is replaced by a fresh one. The backend also refuses a second FAQ with the same question (`409 FAQ_DUPLICATE_QUESTION`), which covers a double click.
  - **History** under each FAQ shows its revisions.
- **Support tickets** (`frontend/admin_tickets.py`):
  - The queue can be filtered by status, by type, or by reference number. It shows reference, type, status, priority, assignee, subject and creation time.
  - Opening a ticket shows the customer's request and contact details (in an expander marked as personal data), plus **Status**, **Assignment**, **Internal notes** and **History** tabs.
  - Each form is based on the ticket as opened. A **Reload ticket** button refreshes it, and a change refused because someone else acted first is explained, after which the current ticket is shown.
- **Dashboard overview** and **Analytics** (`frontend/admin_insights.py`): see [below](#overview-and-analytics-what-each-figure-means).

Errors are shown as one plain sentence with the request ID and, for validation errors, the invalid fields. Raw errors and stack traces are never shown, and neither are the values that were sent.

## Ticket handling, assignment and notes

The Stage 10 state machine is reused unchanged (`ALLOWED_TRANSITIONS`). Status changes still need the staff permission, still send the status they were based on, and are still recorded as events. Nothing changes a ticket automatically, and nothing claims a notification: no email or SMS is sent by any of this.

**Assignment (Task 13).** Staff IDs are stable, because each person has their own token, so assignment is real rather than simulated:

- **Who can be assigned:** only configured staff whose role handles tickets, listed by `GET /admin/staff`. Anyone else is refused with `422`, so nobody is invented.
- **Who can assign:** any staff member with ticket permission (admin or support) can assign, reassign or unassign.
- **Open and assigned tickets:** assigning an `open` ticket moves it to `assigned`. Unassigning an `assigned` ticket moves it back to `open`.
- **Tickets being worked on:** while `assigned`, `in_progress` or `waiting_for_customer`, the assignee can change without a status change. This is recorded as an `assignment_changed` event with `previous_assigned_to` → `assigned_to`.
- **Finished tickets:** resolved and closed tickets keep their assignee (`409 INVALID_ASSIGNMENT`).
- **No duplicates or overwrites:** the same assignee again records nothing. A request based on an outdated assignee is refused (`409 TICKET_CHANGED`) rather than overwriting a colleague's assignment.
- **`assigned_at`:** records when the current assignee was assigned. It is empty for assignments made before Stage 11. It does **not** mean the assignee has seen the ticket, and nobody is notified.
- **Integrations:** assignment changes are published to the integration event sink (`assignment_changed`), ready for a future helpdesk adapter.

**Internal notes.** A minimal, secure mechanism in the `ticket_notes` table:

- **Access:** staff only (`GET`/`POST /support/tickets/{id}/notes`). Notes are never part of a customer response.
- **Contents:** each note records its author and time, and is limited to 2,000 characters.
- **Safe retries:** a client-generated `client_note_id` makes a retry return the stored note instead of adding it twice.
- **Anonymization:** notes may quote the customer, so anonymizing a closed ticket replaces their text, and no new notes can be added afterwards (`409 TICKET_ANONYMIZED`).

**Contact details** are shown only to roles with `view_customer_contacts`, which both current roles have, because they need it to reply. The overview and analytics never contain them.

## Overview and analytics: what each figure means

All figures are computed by the database from the stored data, using `COUNT`, `GROUP BY` and `AVG` over the conversation, message and ticket tables (`app/repositories/analytics_repository.py`). Nothing is sampled, estimated or invented, no satisfaction score exists, and no personal data or text is ever loaded. An empty database shows zeros and "no activity" messages.

**Dashboard overview** (`GET /admin/overview`, live, all time unless stated):

| Figure | Definition |
|--------|-----------|
| Total / active / inactive FAQs | Rows in `faqs` by `is_active` |
| Stored conversations | Conversations currently stored; **not a lifetime total** (expired conversations are deleted by retention cleanup) |
| Started in the last 7 days | Stored conversations created in the last 7 × 24 hours |
| Total tickets | All support tickets and sales inquiries (kept until anonymized, never deleted) |
| Open | Status `open`, `assigned`, `in_progress` or `waiting_for_customer` |
| Resolved / Closed | Status `resolved` (can still be reopened) / `closed` (final) |
| Sales leads | Tickets of type `sales`, any status |

**Chatbot analytics** (`GET /admin/analytics?days=7|30|90`, whole UTC days with today included; `app/services/analytics_service.py`):

| Figure | Definition |
|--------|-----------|
| Customer messages / chatbot responses | Stored user / assistant messages created in the period |
| Decisions | Chatbot responses by recorded decision: `answered`, `ambiguous` (asked to clarify), `no_match`, `retrieval_unavailable` |
| Human-support offers | Chatbot responses that offered human support or a sales contact (`escalation_reason` recorded with the reply) |
| Support requests / sales inquiries | Tickets created in the period, by type; *after a chat offer* = those created from an offer |
| Answered response rate | answered ÷ chatbot responses × 100 |
| Fallback rate | (no_match + retrieval_unavailable) ÷ chatbot responses × 100 |
| Clarification rate | ambiguous ÷ chatbot responses × 100 |
| Escalation offer rate | responses with an offer ÷ chatbot responses × 100 |
| Response time | Server processing time per exchange: the reply's stored time minus the customer message's stored time (both written by the API), as median, average, 90th percentile and slowest. Excludes network time to the customer |
| Per day | Conversations started, chatbot responses (answered, fallbacks) and tickets created per UTC day |

**Rates and small samples.** Every rate is returned with its numerator, denominator and formula. A rate based on fewer than **30** chatbot responses is not shown: the value is null and a note gives the reason. A zero denominator never divides.

**Chatbot decisions are not business outcomes.**
- An *answered* response means the assistant replied from an approved FAQ. It does **not** mean the customer's issue was resolved: nobody confirmed that, so **no resolution rate is reported**.
- A ticket is a request for help. It is not counted as a failure of the assistant.
- The answered response rate is not a customer issue resolution rate.

**Limitations (also returned by the API and shown on the page).**
- Expired conversations are deleted after `CONVERSATION_TTL_MINUTES` (60 by default) by retention cleanup, together with their messages. Message and decision counts therefore cover only conversations still stored, and undercount longer periods.
- Ticket counts are complete.
- For the same reason, no conversation-to-ticket rate is computed: its denominator would be too small, which would make the rate look higher than it is.
- Lasting analytics would need counts kept separately from conversations, which is not built.

## Admin API

All under `/api/v1`, all with `Authorization: Bearer <staff token>` and `Cache-Control: no-store`.

| Method and path | Role | Purpose |
|-----------------|------|---------|
| `GET /admin/me` | any staff | Staff ID, role, permissions, a note on what kind of authentication this is |
| `GET /admin/overview` | admin | Counts: FAQs; stored conversations; tickets by status, type and lead category, open/resolved/closed, leads. No ticket text or contact details |
| `GET /admin/analytics?days=` | admin | Chatbot decisions, offers, ticket requests, rates with formulas, response times, per-day series, limitations (1–365 days) |
| `GET /admin/staff` | admin, support | Configured staff who can be assigned tickets (IDs and roles only) |
| `GET /admin/faqs?q=&category=&is_active=&offset=&limit=` | admin | FAQs in ID order (active and inactive), with versions and authors |
| `POST /admin/faqs` | admin | Create (`question`, `answer`, `category`, `keywords`, `is_active`); `201` with `Location` |
| `GET /admin/faqs/{id}` | admin | One FAQ with its `version` |
| `PATCH /admin/faqs/{id}` | admin | `expected_version` plus only the fields to change |
| `POST /admin/faqs/{id}/deactivate` · `/activate` | admin | `{"expected_version": n}` |
| `GET /admin/faqs/{id}/revisions?offset=&limit=` | admin | The FAQ's change history, newest first |
| `GET/PATCH /support/tickets…` | admin, support | Stage 10 ticket endpoints ([docs/human-handoff.md](human-handoff.md#http-api)); the list also takes `reference=` |
| `PUT /support/tickets/{id}/assignment` | admin, support | `{"assigned_to", "expected_assigned_to", "expected_status"}`: assign, reassign or unassign (`null`) |
| `GET`/`POST /support/tickets/{id}/notes` | admin, support | Internal notes; `POST {"body", "client_note_id"}` is `201`, or `200` for a retry |

Every error has the standard body `{"error": {"code", "message", "request_id"}}` and the same request ID in `X-Request-ID`; database errors are reported by type in the log only. New error codes: `403 STAFF_FORBIDDEN`, `404 FAQ_NOT_FOUND`, `409 FAQ_VERSION_CONFLICT`, `409 FAQ_LAST_ACTIVE`, `409 FAQ_DUPLICATE_QUESTION`, `409 INVALID_ASSIGNMENT`, `409 TICKET_ANONYMIZED` (and `409 TICKET_CHANGED` also for an outdated assignee); `503 SERVICE_UNAVAILABLE` when FAQ storage fails (or on an app whose FAQs are not in the database, which only tests build).

**Duplicate IDs cannot be created:** the server allocates every new ID inside the write transaction (SQLite's write lock serialises creations), the column is the primary key, and a client-supplied `id` is refused as an unknown field (`422`). The existing customer endpoints (`/api/v1/faqs…`) are unchanged and read-only; customers cannot reach any `/admin` endpoint (a conversation token is never a staff token).

## Tests

```powershell
pytest tests/test_staff_roles.py tests/test_faq_management.py tests/test_admin_api.py `
       tests/test_admin_frontend.py tests/test_faq_consistency.py tests/test_admin_tickets_analytics.py
```

They cover entries and roles, the authorization matrix on every admin endpoint (no token, invalid, customer token, support, admin, staff disabled, token in URL), tokens and FAQ text absent from logs, seeding and restart safety, database constraints, validation, ID allocation, version conflicts, deactivation and the last-active rule, semantic index rebuilds, the import script (idempotency, dry run, backup, invalid files), the revision history (actions, changed fields only, refused edits record nothing, pagination, admins only, no credentials stored, one revision per version), the dashboard client against the real backend, and the Streamlit page. The page tests cover sign-in, sections by role and sign-out; creating an FAQ with confirmation only after the save, a duplicate submission refused, validation messages, deactivation needing confirmation, a stale edit refused with the user's text kept, and history; assigning, adding a note, changing status and finding a ticket by reference, plus a stale ticket change refused; and the overview and analytics with data and on an empty database.

`tests/test_admin_tickets_analytics.py` covers reference search, the staff directory, assignment (assign, reassign, unassign, reopen on unassign, no duplicate event, outdated assignee, unknown assignee, finished tickets, customer tokens), internal notes (internal only, safe retries, conflicting retries, anonymization), the overview's counts, and analytics. The analytics tests run on an empty database and with recorded chats; they check the period window, admins-only access, the absence of personal data, latency computed from stored timestamps, and rates with zero, small and sufficient samples.

`tests/test_faq_consistency.py` drives the race conditions directly: hooks in a fake encoder commit FAQ edits and deactivations **while** a search or a chat request is running. It checks re-deciding on the new catalogue, the safe message when it keeps changing, evidence changed after the decision, a failed rebuild never serving the stale index, only changed questions being embedded, older pinned requests, and a multi-threaded run in which concurrent edits never mix versions in one answer. `tests/test_admin_api.py` repeats the mid-request edit and deactivation through the real chat endpoint.

## Known limitations

- **Prototype staff authentication** ([above](#what-this-is-not)).
- **Analytics cover stored conversations only**: expired conversations are deleted, so message and decision figures undercount longer periods; there is no answer-quality, resolution or satisfaction data ([details](#overview-and-analytics-what-each-figure-means)).
- **Notifications**: assignments and ticket changes notify nobody; there is no email or SMS integration yet.
- **No rollback button**: earlier texts are in the history and can be re-applied as an edit.
- **One process**: another process's FAQ changes are seen after a restart; several API workers would each keep their own snapshot and index ([details](#guarantees-and-limits)).
- **SQLite**: one writer at a time, one machine.
- **The dashboard is not hardened for public exposure** (no CSRF-style protections beyond Streamlit's defaults, no lockout).
