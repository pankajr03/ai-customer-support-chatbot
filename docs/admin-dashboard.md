# Admin dashboard, staff roles and FAQ management (Stage 11)

> **Status: Tasks 2–6 implemented, awaiting review.** A separate Streamlit page for staff, two staff roles enforced by the backend, and FAQs stored in SQLite so admins can edit them. Staff access is still a **development and small-team prototype**, not a production identity system ([limitations](#known-limitations)).

## Contents

- [What it adds](#what-it-adds)
- [Running the dashboard](#running-the-dashboard)
- [Staff authentication and roles](#staff-authentication-and-roles)
- [FAQ storage in SQLite](#faq-storage-in-sqlite)
- [Importing FAQs and backups](#importing-faqs-and-backups)
- [FAQ management rules](#faq-management-rules)
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
| Admin API | `/api/v1/admin/me`, `/overview`, `/faqs`… ([below](#admin-api)) |

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

**Retrieval stays fast.** `DatabaseFAQRepository` keeps a validated, immutable snapshot of the catalogue in memory with the same read interface as the old JSON repository, so keyword, semantic and hybrid search, answers and conversations are unchanged and no chat turn queries the FAQ table. Every committed change through the repository replaces the snapshot at once, and the semantic index is dropped and rebuilt on next use.

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

## Admin API

All under `/api/v1`, all with `Authorization: Bearer <staff token>` and `Cache-Control: no-store`.

| Method and path | Role | Purpose |
|-----------------|------|---------|
| `GET /admin/me` | any staff | Staff ID, role, permissions, a note on what kind of authentication this is |
| `GET /admin/overview` | admin | Counts: FAQs by category and status; tickets by status, type and lead category, last 7 days, waiting on staff. No ticket text or contact details |
| `GET /admin/faqs?q=&category=&is_active=&offset=&limit=` | admin | FAQs in ID order (active and inactive), with versions and authors |
| `POST /admin/faqs` | admin | Create (`question`, `answer`, `category`, `keywords`, `is_active`); `201` with `Location` |
| `GET /admin/faqs/{id}` | admin | One FAQ with its `version` |
| `PATCH /admin/faqs/{id}` | admin | `expected_version` plus only the fields to change |
| `POST /admin/faqs/{id}/deactivate` · `/activate` | admin | `{"expected_version": n}` |
| `GET/PATCH /support/tickets…` | admin, support | Stage 10 ticket endpoints ([docs/human-handoff.md](human-handoff.md#http-api)) |

New error codes: `403 STAFF_FORBIDDEN`, `404 FAQ_NOT_FOUND`, `409 FAQ_VERSION_CONFLICT`, `409 FAQ_LAST_ACTIVE`; `503 SERVICE_UNAVAILABLE` when FAQ storage fails (or on an app whose FAQs are not in the database, which only tests build).

## Tests

```powershell
pytest tests/test_staff_roles.py tests/test_faq_management.py tests/test_admin_api.py tests/test_admin_frontend.py
```

They cover entries and roles, the authorization matrix on every admin endpoint (no token, invalid, customer token, support, admin, staff disabled, token in URL), tokens and FAQ text absent from logs, seeding and restart safety, database constraints, validation, ID allocation, version conflicts, deactivation and the last-active rule, semantic index rebuilds, the import script (idempotency, dry run, backup, invalid files), the dashboard client against the real backend, and the Streamlit page (sign-in, sections by role, sign-out, editing an FAQ).

## Known limitations

- **Prototype staff authentication** ([above](#what-this-is-not)).
- **Analytics are counts only**: no conversation, answer-quality or response-time analytics yet.
- **No FAQ revision history**: `version`, `updated_at` and `updated_by` record the latest change, not every earlier text.
- **One process**: another process's FAQ changes are seen after a restart; several API workers would each keep their own snapshot.
- **SQLite**: one writer at a time, one machine.
- **The dashboard is not hardened for public exposure** (no CSRF-style protections beyond Streamlit's defaults, no lockout).
