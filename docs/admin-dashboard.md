# Admin dashboard and staff tooling (Stage 11)

> **Status: implemented, awaiting review.** Stage 11 adds:
> - a separate Streamlit page for staff, with two roles enforced by the backend;
> - FAQs stored in SQLite, edited with a revision history;
> - retrieval that stays consistent while FAQs change;
> - ticket handling with assignment and internal notes;
> - analytics computed from recorded data;
> - an append-only audit log.
>
> Staff access is a **development and small-team prototype**, not a production identity system. **Strengthen it before exposing the application publicly** ([security limitations](#security-limitations)).

Written for developers and operators of this project. Commands are for Windows PowerShell, run from the project root with the virtual environment active (`.\.venv\Scripts\Activate.ps1`).

## Contents

- [Stage 11 overview](#stage-11-overview)
- [Admin architecture](#admin-architecture)
- [Starting the admin dashboard](#starting-the-admin-dashboard)
- [Staff authentication and roles](#staff-authentication-and-roles)
- [FAQ storage in SQLite](#faq-storage-in-sqlite)
- [JSON-to-SQLite migration and backups](#json-to-sqlite-migration-and-backups)
- [FAQ management workflow](#faq-management-workflow)
- [FAQ revision history](#faq-revision-history)
- [Keeping retrieval consistent while FAQs change](#keeping-retrieval-consistent-while-faqs-change)
- [Ticket management](#ticket-management)
- [Analytics: endpoints and definitions](#analytics-endpoints-and-definitions)
- [Audit log](#audit-log)
- [Admin API](#admin-api)
- [Testing](#testing)
- [Evaluation scenarios](#evaluation-scenarios)
- [Security limitations](#security-limitations)
- [Known issues](#known-issues)
- [Stage 12 preview](#stage-12-preview)

## Stage 11 overview

| Area | What exists now |
|------|-----------------|
| Dashboard | `frontend/admin_app.py`, a Streamlit page separate from the customer chat. Sections: **Dashboard overview**, **FAQ management**, **Support tickets**, **Analytics**, **Audit log**, **Administration information**. Which sections appear depends on the role; the backend enforces the role on every request regardless |
| Roles | `admin` (FAQs, tickets, analytics, audit log) and `support` (tickets, with contact details), set per personal token in `STAFF_TOKEN_HASHES` |
| FAQ storage | The `faqs` table in the existing SQLite database is the editable source of truth. It is seeded once from `data/faqs.json` when empty, and never overwritten at startup |
| FAQ management | Create, list, search, filter, edit, deactivate and reactivate. IDs are allocated by the server, edits are version-checked, duplicate questions are refused, and nothing is ever deleted |
| History and audit | `faq_revisions` keeps every FAQ change. `admin_audit_events` keeps who did what, including refused access. Both are written in the same transaction as the change |
| Consistent retrieval | Versioned catalogue snapshots; a version-aware semantic index that re-embeds only changed questions; answers never use outdated or inactive FAQs; retrieval fails closed when the catalogue cannot be verified |
| Tickets | Reference search; assignment to configured staff; internal notes. The Stage 10 workflow is unchanged |
| Analytics | Dashboard summary, chatbot decision analytics and ticket analytics over date ranges, from stored data only, with formulas, sample-size rules and stated limitations |

The customer chat (`frontend/app.py`) is unchanged: it has no staff controls, imports no admin code and calls no staff endpoint (a test checks this).

## Admin architecture

```
Browser (staff) ──> Streamlit admin page (frontend/admin_app.py; sections in admin_*.py)
                       │  AdminApiClient: HTTP only, staff token as a header, no DB access
                       ▼
FastAPI  /api/v1/admin/*  and  /api/v1/support/tickets/*      (app/api/routes/)
   │  authentication + role check: require_staff_permission (dependencies.py)
   ▼
Services: FAQManagementService, TicketWorkflow, AdminDashboardService,
          AnalyticsService (each re-checks the role)
   ▼
Repositories: DatabaseFAQRepository, TicketRepository, AnalyticsRepository,
              AuditRepository: one short transaction each; the audit event
              is written in the same transaction as the change
   ▼
SQLite (DATABASE_URL): faqs, faq_revisions, support_tickets, ticket_events,
                       ticket_notes, admin_audit_events, conversation tables

Retrieval (customer chat) reads FAQs from the repository's in-memory snapshot:
  FAQService ──pins one CatalogSnapshot per decision──> keyword search
                                                     ─> semantic index (same revision)
                                                     ─> evidence check before replying
```

- **Routes are thin.** They translate HTTP and declare the permission they need.
- **Services hold the rules.** They check the role again, so a new caller can't skip it.
- **Repositories own the SQL.** Transactions live there.
- **The page never touches the database.**

## Starting the admin dashboard

1. **Create staff tokens.** Each person gets their own token, printed once:

   ```powershell
   python -m scripts.create_staff_token alex --role admin
   python -m scripts.create_staff_token maria --role support
   ```

   Put the printed entries in `.env` as a JSON list, with double quotes:

   ```
   STAFF_TOKEN_HASHES=["alex:admin:<hash>", "maria:support:<hash>"]
   ```

2. **Start the backend.** It creates or upgrades the tables and seeds an empty FAQ table:

   ```powershell
   uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
   ```

3. **Start the dashboard** in a second terminal. The entry point is `frontend/admin_app.py`:

   ```powershell
   streamlit run frontend/admin_app.py --server.port 8502
   ```

   Plain `streamlit run frontend/admin_app.py` works too, on Streamlit's default port 8501. Pass `--server.port 8502` when the customer chat (`streamlit run frontend/app.py`) is already using 8501.

4. **Sign in.** Open http://localhost:8502 and paste your `stf_...` token.

The dashboard uses the same `BACKEND_API_URL` as the chat page. It inherits `.streamlit/config.toml`: it listens on localhost only and keeps Streamlit's XSRF protection. Reloading the browser tab starts a new session, so you sign in again.

If the backend stops with `Cannot start: STAFF_TOKEN_HASHES ... could not be read`, the `.env` value is not valid JSON. Use double quotes, as above.

## Staff authentication and roles

### How staff credentials are verified

- **Personal bearer tokens.** Each is `stf_` plus 32 random bytes, generated with `secrets`. `scripts/create_staff_token.py` shows it once. The server stores only SHA-256 hashes, in `STAFF_TOKEN_HASHES` (`.env`, which Git ignores). Nothing is hard-coded.
- **Constant-time comparison** (`hmac.compare_digest`) runs against *every* configured hash, so timing reveals neither which staff member matched nor how close a guess was.
- **Customer tokens never work.** A conversation token (`cst_...`) fails the format check and is never in the staff store. This is tested on every admin endpoint.
- **Headers only.** A token in a query string is refused with `400`. The dashboard keeps the token in server-side session state only: never in the URL, a cookie or browser storage. A `401` or "staff access disabled" signs the page out.
- **Never logged, never audited.** Logs and audit events record the staff ID at most. Error bodies are short fixed sentences with a request ID.
- **Off by default.** With no entries configured, every staff endpoint answers `403 STAFF_ACCESS_DISABLED`.
- **Revoking access.** Remove the person's entry and restart the API; their token is refused from then on.

### Roles and permissions

| Permission | `admin` | `support` | Used by |
|------------|:------:|:--------:|---------|
| `manage_tickets` | ✓ | ✓ | `/support/tickets...`, `/admin/staff` |
| `view_customer_contacts` | ✓ | ✓ | contact details on `GET /support/tickets/{id}` |
| `manage_faqs` | ✓ | | `/admin/faqs...` |
| `view_analytics` | ✓ | | `/admin/dashboard/summary`, `/admin/analytics/...` |
| `view_audit_log` | ✓ | | `/admin/audit` |

How it is enforced:

- **Two checks.** Every staff route declares the permission it needs (`require_staff_permission`), and the services check it again (`StaffMember.require`).
- **Refusal.** A role without the permission gets `403 STAFF_FORBIDDEN`, and the refusal is audited.
- **Older entries are `support`.** A Stage 10 entry without a role (`staff_id:hash`) counts as `support`, so upgrading never grants FAQ editing by accident.

**This is a prototype**; see [Security limitations](#security-limitations).

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

**Fail closed.** If a change is committed but the catalogue cannot be read back into memory afterwards, the repository marks it *unverified*. Customer answers are then refused with "help articles temporarily unavailable" (`503`), and `/ready` reports `knowledge_base: unavailable`, until a reload succeeds. Retrieval never answers from a catalogue that no longer matches the database.

**One process sees its own changes.** Changes made by another process (the import script while the API runs, a second API worker) appear after a restart. The dashboard edits through the API, so the API is always current.

## JSON-to-SQLite migration and backups

The migration is a seeding step plus an import command, not a schema migration tool. Tables are created or upgraded by `Database.init_schema()` at startup: new tables are created, new nullable columns added, and tables with changed constraints rebuilt in one transaction with every row copied. The FAQ table is seeded from `FAQ_DATA_PATH` only when it is empty.

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

## FAQ management workflow

The rules live in `app/services/faq_management_service.py` and the SQL in `app/repositories/faq_repository.py`. Routes are in `app/api/routes/admin.py`, and the page is `frontend/admin_faqs.py`.

**Rules (enforced by the backend):**

- **Validation** uses the knowledge base's own field types:
  - questions are trimmed, with repeated spaces collapsed;
  - answers are trimmed only, so line breaks are kept; at most 5,000 characters;
  - the category must be on the fixed list;
  - 1–20 tags, each trimmed, lower-cased and de-duplicated.

  Unknown fields, including a client-supplied `id`, are refused. Validation errors list the fields and rules, never the submitted values.
- **IDs** are allocated by the server inside the write transaction, one above the highest ever used, and never reused. Duplicate IDs cannot occur.
- **No duplicate questions.** Another FAQ with the same question (ignoring case, active or not) is refused with `409 FAQ_DUPLICATE_QUESTION`. This also catches a double-clicked form.
- **Optimistic concurrency.** Edits, deactivation and activation send `expected_version`. If someone saved in between, the result is `409 FAQ_VERSION_CONFLICT` and nothing is written. An edit that changes nothing keeps the version.
- **Deactivate, never delete.** A deactivated FAQ leaves customer answers, search and the public FAQ API at once, but stays stored and can be reactivated. There is no delete endpoint (`405`): stored chat replies cite FAQ IDs, and an ID must keep meaning the same FAQ.
- **At least one FAQ stays active** (`409 FAQ_LAST_ACTIVE`).

**On the page (FAQ management):**

1. **Find an FAQ.** The table shows ID, question, category, status, version and last updated. You can search, filter by category or status, and page through 20 at a time.
2. **Add an FAQ.** Required fields and tag limits are checked before anything is sent. A success message appears only after the backend returns the saved FAQ; then the form resets and the list reloads.
3. **Edit an FAQ.** Open it and edit the form, which keeps the version you opened. If someone else saved first, the page says **not saved**, keeps your text to copy, and loads their version. **Reload latest version** refreshes it on demand.
4. **Deactivate or activate.** Deactivating asks for confirmation; activating does not.
5. **See the history.** The **History** section lists every revision.

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

### When the catalogue cannot be verified

If a committed change cannot be read back into memory, retrieval is paused (`503`, "help articles temporarily unavailable") until a reload succeeds ([FAQ storage](#faq-storage-in-sqlite)). Answering from the outdated catalogue would mean answering from an FAQ that may already be deactivated.

### Guarantees and limits

- Never: an answer from an FAQ that is inactive when the answer is returned; evidence text from one FAQ version paired with another version's question or embedding; a partially built index.
- **Single process only.** Snapshots and the index live in one API process's memory. A second API worker, or the import script running alongside the API, is not seen until that process restarts. Multi-process synchronisation is not implemented (it would need, for example, polling the database for a catalogue revision).
- The rebuild is synchronous and lazy (or on the change itself, through `refresh`). With a large catalogue, a question edit costs one embedding call; there is no background queue.
- The final check compares the evidence's question and answer with the latest catalogue. A change made in the last instant between that check and the response being sent is not detected; it is at most one request old and was valid when checked.

## Ticket management

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

**On the page (Support tickets):**

1. **Find a ticket.** Filter the queue by status or type, or search by reference number.
2. **Open it.** You see the request, and the contact details in an expander marked as personal data.
3. **Work on it.** Use the **Status** (workflow moves only), **Assignment**, **Internal notes** and **History** tabs.

Every form is based on the ticket as you opened it. If a colleague changed it in the meantime, the change is refused and explained, and the current ticket is shown.

## Analytics: endpoints and definitions

| Endpoint | Content |
|----------|---------|
| `GET /admin/dashboard/summary` | Live counts (no period): FAQs, stored conversations, tickets, leads |
| `GET /admin/analytics/chat` | Chatbot decisions, offers, ticket requests, rates, response times, per-day series |
| `GET /admin/analytics/tickets` | Tickets created, resolved and closed, time to resolution, current backlog, per-day series |

**Choosing a period.**
- **Two ways to ask:** pass `days=N` for the last N UTC days including today, or `start_date` and `end_date` (`YYYY-MM-DD`, inclusive UTC days). The default is the last 30 days.
- **Limits:** at most 366 days, and the end can't be after today.
- **Refused:** using both forms at once, or a reversed range, gives `422`.
- **Older paths:** `/admin/overview` and `/admin/analytics` remain as aliases.

All figures are computed by the database from the stored data, using `COUNT`, `GROUP BY` and `AVG` over the conversation, message and ticket tables (`app/repositories/analytics_repository.py`). Nothing is sampled, estimated or invented, no satisfaction score exists, and no personal data or text is ever loaded. An empty database shows zeros and "no activity" messages.

**Dashboard summary** (`GET /admin/dashboard/summary`, live, all time unless stated):

| Figure | Definition |
|--------|-----------|
| Total / active / inactive FAQs | Rows in `faqs` by `is_active` |
| Stored conversations | Conversations currently stored; **not a lifetime total** (expired conversations are deleted by retention cleanup) |
| Started in the last 7 days | Stored conversations created in the last 7 × 24 hours |
| Total tickets | All support tickets and sales inquiries (kept until anonymized, never deleted) |
| Open | Status `open`, `assigned`, `in_progress` or `waiting_for_customer` |
| Resolved / Closed | Status `resolved` (can still be reopened) / `closed` (final) |
| Sales leads | Tickets of type `sales`, any status |

**Chatbot analytics** (`GET /admin/analytics/chat`; `app/services/analytics_service.py`):

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

**Ticket analytics** (`GET /admin/analytics/tickets`):

| Figure | Definition |
|--------|-----------|
| Created | Tickets created in the period, by type, by **current** status, by priority, by lead category, and how many followed a chat offer |
| Resolved / closed | Tickets whose (latest) `resolved_at` / `closed_at` falls in the period |
| Time to resolution | Hours from creation to the latest resolution, for tickets resolved in the period: median, average, 90th percentile, longest. A reopened ticket counts from its latest resolution |
| Backlog | **Now**, whatever the period: tickets open, assigned, in progress or waiting for the customer; how many are unassigned; when the oldest was created |

The ticket analytics have their own limits:
- The status counts are current statuses, not the status during the period.
- There is no first-response time, because replies happen outside the system.
- "Resolved" is staff's judgement, not a confirmed customer outcome.

Every analytics response is computed with bounded aggregate queries over the existing tables; there is no separate analytics database. Responses contain counts and durations only, never message text, ticket text or contact details. An empty database gives zeros, null rates with a note, and no errors.


## Audit log

Table `admin_audit_events` is an append-only record of administrative actions. It is separate from the application log, which holds diagnostics and may be rotated away, and from the FAQ revisions and ticket events, which hold *content* and *status* history.

| Field | Content |
|-------|---------|
| `event_id`, `created_at` | Random UUID; aware UTC time |
| `actor_type`, `actor_id` | `staff` + staff ID (from the authenticated token); `system` + a documented service (`faq-import` for seeding and the import script, `retention` for anonymization runs); `unauthenticated` + no ID |
| `action` | `faq.created`, `faq.updated`, `faq.activated`, `faq.deactivated`, `faq.imported`, `ticket.status_changed`, `ticket.assignment_changed`, `ticket.note_added`, `ticket.anonymized`, `auth.access_denied` |
| `resource_type`, `resource_id` | `faq` + FAQ ID, `faq_catalog`, `ticket` + ticket ID, or `endpoint` + path (for refusals) |
| `outcome` | `success` or `denied` |
| `request_id` | The request's `X-Request-ID`, to find the matching log lines |
| `details` | Small, non-personal context: version and changed field names, from/to status or assignee, the refusal reason and missing permission |

**Guarantees:**
- **Changes and their events commit together.** FAQ and ticket changes write their audit event in the same transaction as the change, so a committed change always has its event and a refused or rolled-back one never does.
- **Refusals are recorded.** Missing or invalid token, role not permitted, staff access disabled. If the audit table can't be written, the failure is logged and the refusal still goes out.
- **Actors are real.** The actor is always the authenticated staff ID or a named service, never an invented person.
- **Nothing sensitive is stored.** Never tokens, passwords, contact details, ticket text, note text or FAQ text.
- **Append-only.** There is no update or delete endpoint, and database triggers refuse `UPDATE` and `DELETE` on the table. Someone with direct access to the database file could still drop the table: this is tamper-evidence for the application, not a tamper-proof store.

**Reading it:**
- **Who:** admins only (`view_audit_log`).
- **Where:** `GET /admin/audit?action=&actor_id=&resource_type=&resource_id=&outcome=&start_date=&end_date=&offset=&limit=` (newest first, at most 200 per page), or the **Audit log** section of the dashboard.

**What isn't recorded:**
- **Customer actions** (creating a ticket, chatting) are not administrative and are not in this log.
- **Reads** (who viewed what) are not recorded.
- **Retention:** there is none; events are kept indefinitely.

## Admin API

All under `/api/v1`, all with `Authorization: Bearer <staff token>` and `Cache-Control: no-store`.

| Method and path | Role | Purpose |
|-----------------|------|---------|
| `GET /admin/me` | any staff | Staff ID, role, permissions, a note on what kind of authentication this is |
| `GET /admin/dashboard/summary` | admin | Live counts: FAQs; stored conversations; tickets by status, type and lead category, open/resolved/closed, leads (alias: `/admin/overview`) |
| `GET /admin/analytics/chat?days=` or `?start_date=&end_date=` | admin | Chatbot decisions, offers, ticket requests, rates with formulas, response times, per-day series, limitations (alias: `/admin/analytics`) |
| `GET /admin/analytics/tickets?days=` or `?start_date=&end_date=` | admin | Tickets created/resolved/closed, time to resolution, backlog, per-day series |
| `GET /admin/audit?...` | admin | The audit log, newest first, filtered and paginated |
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

## Testing

All tests run offline:
- **Temporary databases:** each test uses its own temporary or in-memory database.
- **No downloads:** a deterministic fake embedding model stands in for the real one.
- **No paid calls:** the LLM is off or scripted, and no OpenAI API is called.

```powershell
# Stage 11
pytest tests/test_staff_roles.py tests/test_faq_management.py tests/test_admin_api.py `
       tests/test_faq_consistency.py tests/test_admin_tickets_analytics.py `
       tests/test_admin_audit_and_edge_cases.py tests/test_admin_frontend.py tests/test_admin_eval.py

# Everything (Stages 1-11 regression)
pytest

# The admin scenarios, with a readable report
python -m scripts.evaluate_admin
python -m scripts.evaluate_admin --json data/admin_eval_results.json
```

| Area | Where |
|------|-------|
| Roles, entries, token script, settings errors | `test_staff_roles.py` |
| FAQ repository: create, read, update, (de)activate, versions, duplicates, constraints, ordering, import idempotency and dry run, backups, invalid files | `test_faq_management.py` |
| FAQ API: authorization matrix on every endpoint, listing, creation, editing, invalid payloads, missing FAQs, version conflicts, revisions, logs without tokens, restart safety, mid-request edits through the chat | `test_admin_api.py` |
| Retrieval synchronization: new, updated, deactivated and reactivated FAQs; keyword and semantic refresh; only changed questions embedded; concurrent modification; refresh failure; multi-threaded version mixing | `test_faq_consistency.py` |
| Tickets: listing, status filters, reference search, valid and invalid transitions, assignment, notes, authorization; overview counts; chat analytics (empty, counts, windows, latency, rates, zero denominators) | `test_admin_tickets_analytics.py` |
| Audit events, new analytics endpoints and date filters, and edge cases: database write failure, unreadable change (fail closed), deactivating an FAQ cited in history, empty FAQ database, revoked token | `test_admin_audit_and_edge_cases.py` |
| Streamlit dashboard: sign-in, roles, sign-out, revoked token; FAQ list, create, edit, duplicate, validation, deactivation confirmation, stale edit; ticket list, assignment, notes, status update, stale change; overview, analytics, audit log | `test_admin_frontend.py` |
| The 28 evaluation scenarios | `test_admin_eval.py` |

## Evaluation scenarios

`data/admin_eval.json` describes 28 scenarios across FAQ changes, retrieval synchronization, failures, authorization, tickets and analytics. `scripts/evaluate_admin.py` implements each one and runs it against a fresh in-process API:
- a temporary SQLite database seeded from `data/faqs.json`;
- the LLM off;
- a deterministic stand-in for the embedding model, so the scenarios test admin behaviour, not retrieval quality.

The table is the recorded output of that run (`data/admin_eval_results.json`). Nothing was edited by hand. **28 of 28 passed.**

| ID | Area | Scenario | Preconditions | Action | Expected | Actual (recorded) | Status |
|----|------|----------|---------------|--------|----------|-------------------|--------|
| AE-01 | FAQ creation | An admin creates a new FAQ. | Seeded catalogue; admin token. | POST /admin/faqs with a valid FAQ, then search for it publicly. | 201 with the next faq_NNN ID and version 1; the public keyword search finds it. | 201 faq_031 version 1; public search finds it | pass |
| AE-02 | FAQ creation | The same question is submitted twice (double click). | AE-01's FAQ exists. | POST the same question again, differently capitalised. | 409 FAQ_DUPLICATE_QUESTION; still one FAQ. | 409 FAQ_DUPLICATE_QUESTION; 1 FAQ stored | pass |
| AE-03 | Invalid FAQ content | An FAQ with an empty answer. | Admin token. | POST /admin/faqs with answer "". | 422 VALIDATION_ERROR naming the field, without echoing values; nothing stored. | 422 VALIDATION_ERROR on body.answer; nothing stored | pass |
| AE-04 | Invalid FAQ content | An answer longer than 5,000 characters. | Admin token. | POST /admin/faqs with a 5,001-character answer. | 422 VALIDATION_ERROR; nothing stored. | 422 VALIDATION_ERROR on body.answer; nothing stored | pass |
| AE-05 | Invalid FAQ content | An unknown category. | Admin token. | POST /admin/faqs with category "pets". | 422 VALIDATION_ERROR; nothing stored. | 422 VALIDATION_ERROR on body.category; nothing stored | pass |
| AE-06 | FAQ editing | An admin edits an answer. | faq_001 at version 1. | PATCH faq_001 answer with expected_version 1; ask the chatbot its exact question. | Version 2; the public FAQ and the chatbot's answer are the new text. | version 2; public FAQ and chatbot answer show the new text | pass |
| AE-07 | Concurrent edits | Two admins edit the same version. | faq_001 at version 1. | Two PATCHes, both with expected_version 1. | First saved (version 2); second 409 FAQ_VERSION_CONFLICT; the first text is kept. | first 200; second 409 FAQ_VERSION_CONFLICT; stored text is the first edit | pass |
| AE-08 | FAQ deactivation | An admin deactivates an FAQ. | faq_001 active. | POST /deactivate; fetch it publicly; ask its exact question. | Public 404; the chatbot does not cite faq_001; the FAQ is still stored. | public 404; reply decision ambiguous without faq_001; still stored | pass |
| AE-09 | FAQ reactivation | A deactivated FAQ is activated again. | faq_001 deactivated. | POST /activate; ask its exact question. | Public 200; the chatbot answers with faq_001 again. | public 200; chatbot answers with faq_001 again | pass |
| AE-10 | FAQ editing | An FAQ moves to another category. | faq_001 in orders. | PATCH category to shipping. | Listed under shipping, not orders; the revision records only the category change. | listed under shipping only; revision changed_fields = ['category'] | pass |
| AE-11 | Retrieval synchronization | New tags make an FAQ findable by keyword. | No FAQ tagged "zeppelin". | PATCH faq_001 keywords to include "zeppelin"; keyword search "zeppelin". | Before: no result. After: faq_001 found, without a restart. | before: no result; after: faq_001 found (no restart) | pass |
| AE-12 | Stale embedding prevention | A question edit re-embeds only that question. | Semantic index built. | PATCH faq_001's question; search semantically. | Exactly one FAQ question embedded; the index revision matches the catalogue; the new question is indexed. | 1 question embedded; index revision = catalogue revision; new question indexed | pass |
| AE-13 | Retrieval synchronization | An FAQ is deactivated while a chat request is retrieving. | faq_001 active. | Deactivate faq_001 from inside the request (encoder hook); ask its exact question. | The reply does not cite faq_001. | deactivated mid-request; reply (answered) does not cite faq_001 | pass |
| AE-14 | Stale embedding prevention | An answer is edited while a chat request is retrieving. | faq_001 at version 1. | Edit the answer from inside the request; ask its exact question. | The reply is the new answer, never a mix of versions. | reply decision answered: new answer | pass |
| AE-15 | Stale embedding prevention | The embedding model fails after a question edit. | Index built; the model then fails. | Edit faq_001's question; search. | The stale index is never served (EmbeddingError); decisions fall back to keyword-only over current FAQs. | EmbeddingError (no stale index); decision answered keyword_only | pass |
| AE-16 | Database failure | A committed change cannot be read back into memory. | Reloads fail twice. | Deactivate faq_001; chat; chat again once reloads work. | First chat 503 (help articles unavailable), never an answer from the old catalogue; then answers without faq_001. | first chat 503 SERVICE_UNAVAILABLE (fail closed); then answered without faq_001 | pass |
| AE-17 | Database failure | The database refuses a write. | Writes raise an operational error. | POST /admin/faqs. | 503 SERVICE_UNAVAILABLE without internal details; nothing stored; catalogue revision unchanged. | 503 SERVICE_UNAVAILABLE, no internals in the body; nothing stored | pass |
| AE-18 | Unauthorized modification | FAQ changes without admin rights. | Customer and support tokens exist. | PATCH faq_001 with no token, a customer token, and a support token. | 401, 401, 403 STAFF_FORBIDDEN; faq_001 unchanged. | 401 STAFF_AUTH_REQUIRED; 401 STAFF_AUTH_REQUIRED; 403 STAFF_FORBIDDEN; faq_001 unchanged | pass |
| AE-19 | Staff authorization failure | No staff configured on the server. | STAFF_TOKEN_HASHES empty. | GET /admin/faqs and /support/tickets with a well-formed token. | 403 STAFF_ACCESS_DISABLED for both. | 403 STAFF_ACCESS_DISABLED; 403 STAFF_ACCESS_DISABLED | pass |
| AE-20 | FAQ revision history | History of a created, edited and deactivated FAQ. | Admin token. | Create, edit the answer, deactivate; GET revisions. | Three revisions newest first: deactivated, updated (answer only), created; changed_by is the admin. | deactivated, updated (answer), created; all by alex | pass |
| AE-21 | Support ticket review | Support staff review a customer's request. | A ticket from a conversation. | Support token: list tickets, open it. | Listed without contact details; the detail has the description and contact details. | listed without contact details; detail has description and contact | pass |
| AE-22 | Ticket status updates | A ticket is assigned and moved to in progress. | An open ticket. | Assign to a configured staff member; change status to in_progress. | Status in_progress, assignee set; both changes in the event history with the staff ID. | in_progress, assigned to maria; 2 events by alex | pass |
| AE-23 | Invalid status transitions | Resolving an open ticket directly. | An open ticket. | PATCH status resolved. | 409 INVALID_STATUS_TRANSITION; status still open; no event. | 409 INVALID_STATUS_TRANSITION; still open; only the creation event | pass |
| AE-24 | Ticket status updates | Assigning to someone who is not staff, then reassigning. | An open ticket. | Assign to "intruder"; assign to maria; reassign to sam. | 422 for intruder; then an assignment_changed event maria -> sam. | intruder 422 VALIDATION_ERROR; assignment_changed maria -> sam recorded | pass |
| AE-25 | Analytics accuracy | Counts match what happened. | Empty conversations and tickets. | 3 chats (one answered by exact question) and 2 tickets; GET analytics/chat and analytics/tickets for today. | 3 customer messages, 3 chatbot responses, answered >= 1, decisions sum to 3; 2 tickets created; rates null (fewer than 30 responses). | 3 messages, 3 responses {'answered': 2, 'no_match': 1, 'ambiguous': 0, 'retrieval_unavailable': 0}; 2 tickets; rates withheld (n=3) | pass |
| AE-26 | Empty database | Analytics with no data. | No conversations or tickets. | GET dashboard summary, analytics/chat, analytics/tickets. | Zeros everywhere; every rate null with a note; no division error. | all counts 0; every rate null with a note | pass |
| AE-27 | Staff authorization failure | Changes and refusals are audited. | Admin and support tokens. | Admin edits faq_001; support tries to edit it; read the audit log. | faq.updated by the admin (success) and auth.access_denied by the support member (denied); no tokens in the log. | faq.updated by alex (success); auth.access_denied by maria (denied); no tokens | pass |
| AE-28 | Empty database | A server started with an empty FAQ seed file. | Seed file is []. | GET /ready, then ask a question. | knowledge_base unavailable; the chatbot answers with the no-match message (no crash). | knowledge_base unavailable; reply no_match | pass |

What these scenarios show:
- **Covered:** the behaviour of the admin API and the retrieval synchronization under the stated conditions.
- **Not covered:** answer quality with the real embedding model, or behaviour with several API processes.

## Security limitations

**Staff authentication is development-only. Strengthen it before exposing the API or the dashboard beyond localhost or a trusted VPN.** What is missing:

- **No accounts:** there is no password login, multi-factor authentication or single sign-on. Possession of a token is the whole identity.
- **No token lifecycle:** tokens never expire and there is no rotation schedule. Revoking one means editing `.env` and restarting.
- **Weak guessing protection:** there is no lockout and no per-user rate limit. The per-IP request limit (process-local) is the only throttle.
- **Reads are not audited:** nobody can tell who viewed which ticket or contact details.
- **The dashboard isn't hardened for the internet:** it relies on Streamlit's XSRF protection and localhost binding. There is no HTTPS of its own, and the token travels to the API in clear text over `http://`, so use HTTPS outside one machine.
- **The audit log is tamper-evident to the application only**, not to someone with access to the database file.

Before public exposure, put the staff endpoints and the dashboard behind an identity provider (OIDC/SSO), or at least a VPN and HTTPS, and keep `STAFF_TOKEN_HASHES` out of shared files.

## Known issues

- **One API process only.**
  - FAQ snapshots and the semantic index live in that process's memory.
  - A second worker, or the import script running alongside the API, is seen only after a restart.
  - Multi-process synchronization is not implemented.
- **Analytics cover stored conversations only.**
  - Expired conversations are deleted after `CONVERSATION_TTL_MINUTES`, together with their messages, so message and decision figures undercount longer periods.
  - There is no answer-quality, resolution or satisfaction data.
- **No notifications.** Assignments and status changes notify nobody, and there is no email or SMS integration.
- **No rollback button.** Earlier FAQ texts are in the history and can be re-applied as a normal edit.
- **SQLite limits.** One writer at a time, on one machine. Retention of audit events is not automated.
- **Pre-existing test failure.** `tests/test_frontend_errors.py::test_an_unexpected_bug_shows_a_friendly_message_and_a_way_out` fails in this environment both before and after Stage 11 (a Stage 8–9 customer-page test, unrelated to the admin work).
- **Config tests fail if `.env.example` is edited.** The template must keep `STAFF_TOKEN_HASHES=[]`; putting a real entry in it makes the config tests fail.

## Stage 12 preview

> Intentions, not commitments; scope and order may change.

- **Production identity for staff:** OIDC/SSO login, expiring sessions, MFA, and audit of reads.
- **Deployment:** Docker images; a database server instead of SQLite; durable, shared rate limits.
- **Several API workers:** keep FAQ snapshots and the semantic index in step across workers, for example by polling the catalogue revision.
- **Notifications:** email first, through the existing integration event sink, with a transactional outbox.
- **Lasting analytics:** daily aggregates kept apart from conversation retention, plus customer feedback ("did this answer help?") as a real outcome measure.
- **Content operations:** FAQ rollback from history, bulk import and review, and scheduled retention for audit events.
