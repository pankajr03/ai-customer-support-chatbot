# Deployment readiness assessment (Stage 13)

An evaluation of whether the customer support chatbot (FastAPI backend, customer and
staff Streamlit pages, SQLite storage, local embedding model, optional OpenAI
generation) can be deployed, category by category, with the evidence for each result.

**Deployment target assessed:** a public, internet-facing deployment serving real
customers, with staff using the dashboard, on one Linux server with Docker Compose
([docs/deployment-architecture.md](deployment-architecture.md)). Where a category is fine
for a smaller target (a local or internal demonstration), the section says so.

This document holds two assessments:

- **[Current status](#current-status-end-of-stage-13)**, at the end of Stage 13
  (2026-10-20, `main` at `77ff327`), after the deployment work.
- **The baseline assessment** (sections 1–7), made on 2026-10-19 at `14ad9c3`, before
  any deployment definition existed. It is kept as the record of what was found and
  why; corrections and later notes inside it are marked *Stage 13*.

## Contents

- [Current status (end of Stage 13)](#current-status-end-of-stage-13)

Baseline assessment (2026-10-19):

1. [Classification key](#1-classification-key)
2. [Summary](#2-summary)
3. [Category assessments](#3-category-assessments)
4. [Verification performed for this assessment](#4-verification-performed-for-this-assessment)
5. [Not evaluated](#5-not-evaluated)
6. [Path to readiness](#6-path-to-readiness)
7. [Sources](#7-sources)

## Current status (end of Stage 13)

> **Overall verdict: still not ready for public production deployment.** The
> deployment now exists and has been built, run and tested on Docker
> ([docs/deployment-tests.md](deployment-tests.md): 28 opt-in tests passed), with a
> runbook, a rollback strategy and recovery procedures. What remains is of two kinds:
>
> 1. **Operator actions on the target server**, which the repository cannot do: a
>    trusted certificate, an external port scan and a dynamic scan, off-server backups,
>    alerting, and scheduled retention. The steps are written
>    ([docs/deployment-security-checklist.md](deployment-security-checklist.md)).
> 2. **One design gap:** staff identity (static tokens, no expiry or MFA, revocation by
>    restart; [authentication-review.md](authentication-review.md#11-deployment-blockers)
>    B1–B3). Until it is closed, the dashboard stays off the internet, as the deployment
>    enforces, and someone must accept the gap explicitly.
>
> With `LLM_ENABLED=true`, the LLM preconditions also apply (category 8). The
> deployment is suitable now for a **staging run on the target server** and for local or
> internal demonstration. Running containers are not by themselves evidence of
> production readiness.

| # | Category | Baseline (2026-10-19) | **Now** | What changed in Stage 13 | What remains |
|---|---|---|---|---|---|
| 1 | Security | Blocked | **Blocked** | Nginx with TLS 1.2/1.3, HSTS, a customer API allow-list, staff tokens refused on the public site, security headers, limits and timeouts; hardened non-root containers; digest-pinned bases and hash-locked dependencies. Verified on Docker with a development certificate ([deployment-architecture.md §11](deployment-architecture.md#11-verification), [§14](deployment-architecture.md#14-reverse-proxy)) | Verification on the target server: trusted certificate, external port scan, dynamic scan. SEC-08 (category 2) |
| 2 | Authentication | Blocked | **Blocked** | Dashboard on loopback behind a tunnel or VPN, basic auth, and a private-network allow-list; the Streamlit WebSocket origin defect found and fixed ([authentication-review.md](authentication-review.md)) | B1–B3: per-person sign-in with MFA, expiring credentials, revocation without a restart |
| 3 | Authorization | Ready with limitations | Ready with limitations | A coverage test over every route, so a new route cannot miss its check | SEC-06 (support staff see all contact details); B6 |
| 4 | Data persistence | Ready with limitations | Ready with limitations | SQLite reviewed under the real workload and kept; a network filesystem refused in production ([sqlite-review.md](sqlite-review.md)); 0 acknowledged replies or tickets lost or duplicated at 8 concurrent requests ([concurrency-and-limits.md](concurrency-and-limits.md)); leases left by a stopped process released at startup ([shutdown-and-restart.md](shutdown-and-restart.md)) | One writer, one host, no encryption at rest by the application |
| 5 | Database migrations | Ready with limitations | Ready with limitations | A verified backup is taken automatically before any change to existing tables; an older release refuses a newer schema instead of discarding columns; releases recorded in `app_releases`; rollback with and without a schema change tested on Docker ([deployment-rollback.md](deployment-rollback.md)) | No versioned migration tool; no downgrade (by design: restore the pre-upgrade backup) |
| 6 | Environment configuration | Ready with limitations | Ready with limitations | Per-environment files; Compose refuses a missing environment or hostname; production refuses an in-memory database and disabled rate limits; every variable documented, checked both ways by tests | No secrets manager; `docker compose config` prints secrets |
| 7 | AI model initialization | Ready with limitations | Ready with limitations | Revision pinned; `model-init` fetches only the 12 needed files (91.6 MB, was 977 MB), verified against the Hub's checksums; a manifest checked before every load; offline; background preload in production ([embedding-model.md](embedding-model.md)) | 18–60 s until semantic search after each restart (keyword answers meanwhile); about 0.5 GB |
| 8 | External API dependency | Blocked (LLM on) | **Blocked** (LLM on); not applicable with the LLM off | Nothing: no paid call was approved | Live evaluation, spending cap, data-processing agreement, customer notice |
| 9 | Resource consumption | Ready with limitations | Ready with limitations | Per-visitor request and connection limits, body limits and timeouts at Nginx; container memory, CPU and PID limits; concurrency evaluated; `Retry-After` for a busy conversation cut from 180 s to 2 s | Measured on a 4-CPU development machine only; no maximum user count established |
| 10 | Logging | Ready with limitations | Ready with limitations | JSON lines with fixed fields, request and correlation IDs across the pages and the backend; the pages' logs, silently dropped before, now kept ([logging.md](logging.md)) | No shipping off the server; Docker keeps 5 × 10 MB per container |
| 11 | Monitoring | Blocked | **Blocked** | Liveness, readiness and a public verdict-only status ([health-checks.md](health-checks.md)); process-local metrics for staff with a periodic log copy; backup health; certificate check script; the alerts to set up and a cron check ([monitoring.md](monitoring.md#monitoring-the-deployment)) | No alerting configured: connecting it to a person is an operator action. Metrics reset on restart |
| 12 | Backup and recovery | Blocked | **Blocked** | Scheduled verified backups; automatic pre-upgrade backups; encrypted off-server copies (restic) tested against a local stand-in; a recovery guide by scenario ([backup-and-recovery.md](backup-and-recovery.md)) | No off-server destination configured; retention not scheduled (SEC-12); recovery onto a new server not rehearsed |
| 13 | Performance | Ready with limitations | Ready with limitations | Profiled on the running servers and production containers; analytics and ticket paging optimized (for example 739 → 13 ms for the deepest ticket page) ([performance-report.md](performance-report.md)) | Development machine, mocked LLM; live OpenAI latency not measured |
| 14 | Operational documentation | Blocked | **Ready with limitations** | Runbook with incident response, secret rotation and rollback ([deployment-runbook.md](deployment-runbook.md)); rollback strategy; recovery guide; go-live security checklist; HTTPS guide; 28 opt-in deployment tests | Never carried out on a real server; the CI workflow has never run (no remote) |

**Totals now:** Ready 0 · Ready with limitations 10 · Blocked 4, plus External API
dependency (Blocked with the LLM on, not applicable with it off). Baseline: Ready with
limitations 8 · Blocked 6.

### From here to go-live

In order. Items 1–5 are operator actions with written steps; item 6 is a decision.

1. **Deploy to the target server as staging**, following the runbook, and work through
   the [security checklist](deployment-security-checklist.md), including the checks from
   outside (port scan, public exposure) and a dynamic scan (Security).
2. **Trusted certificate and expiry monitoring** from another machine (Security).
3. **Off-server backups** configured, the first copy verified, and one timed recovery
   onto another machine ([backup-and-recovery.md §5](backup-and-recovery.md#5-recovering-onto-a-new-server))
   (Backup and recovery).
4. **Retention scheduled** with cron (runbook 3.9; SEC-12) (Backup and recovery).
5. **Alerting connected to a person**: external uptime and certificate checks, and the
   in-server alerts ([monitoring.md](monitoring.md#alerts-to-set-up)) (Monitoring).
6. **Staff identity**: implement B1–B3, or record an explicit, time-limited acceptance
   with the dashboard kept off the internet (Authentication).
7. Before enabling the LLM: the four preconditions in category 8.

### Evidence for the current status

All on the development machine (Windows 10, 4 CPUs, 8 GB; Docker 28.1.1 in WSL 2), with
synthetic data, `LLM_ENABLED=false` and no paid request:

| Evidence | Result |
|---|---|
| Opt-in deployment tests (`RUN_DEPLOYMENT_TESTS=1`): build, start, health, readiness, both pages, API, staff access, safe errors, configuration refusals, backup and restore, persistence, restarts, rollbacks | 28 passed in 6 min 25 s ([deployment-tests.md](deployment-tests.md#results)) |
| Ordinary test suite (Task 24 run) | 3,924 passed, 33 skipped (opt-in), 2 failed. One was a test's inventory of Markdown values that lacked the monitoring page's server-controlled values: the list was completed in Task 27, no application change. The other passed when run again, alone and with its module |
| Restart, crash and in-flight request scenarios against the production image | [shutdown-and-restart.md](shutdown-and-restart.md#results) |
| Concurrency at 4 and 8 requests in flight | All correctness checks passed ([concurrency-and-limits.md](concurrency-and-limits.md)) |
| Performance profile, local and in the production containers | [performance-report.md](performance-report.md) |

**Still not evaluated:** anything on the real target server or from the internet; a
trusted certificate in a browser; live OpenAI behaviour, latency and cost; several
backend workers (not supported); recovery onto another server; Linux without WSL 2, and
cgroup v2.

---

# Baseline assessment (2026-10-19)

**Assessment date:** 2026-10-19. **Code assessed:** `main` at `14ad9c3`.

> **Overall verdict (baseline): not ready for public production deployment.** Six of
> the fourteen categories were **Blocked** (*Stage 13:* first stated as five; the
> summary table always listed six). The application was suitable for a local or
> internal demonstration on one machine, which is how it had been run and tested.
>
> This verdict does not rest on whether the application starts. Starting cleanly
> (verified below) shows that configuration loads and the database opens. It says
> nothing about identity, recovery, monitoring or behaviour under real traffic. At the
> time, the repository had **no Dockerfile, compose file or other deployment
> definition**, so there was no container to start. A successful container start would
> not be evidence of production readiness either.

---

## 1. Classification key

| Result | Meaning |
|---|---|
| **Ready** | Meets the needs of the target deployment, with evidence. No known gap. |
| **Ready with limitations** | Works and has evidence, but only within stated constraints (for example one process, one host, low traffic). Deployable if those constraints are accepted and enforced. |
| **Blocked** | A gap that must be closed before the target deployment. Accepting the risk is not a reasonable option for real customers. |
| **Not evaluated** | No evidence either way. Used for sub-items in [section 5](#5-not-evaluated); every category as a whole had enough evidence to classify. |

"Evidence" means a test, a measurement, a check run for this assessment, or a specific
line of code. A statement in the README alone does not count.

## 2. Summary

| # | Category | Result | Main reason |
|---|---|---|---|
| 1 | [Security](#31-security) | **Blocked** | No TLS or reverse proxy exists; three open deployment blockers in the security audit; never tested as a deployed instance |
| 2 | [Authentication](#32-authentication) | **Blocked** | Staff tokens never expire, have no MFA, and can only be revoked by a restart (SEC-08). Customer sessions are sound |
| 3 | [Authorization](#33-authorization) | Ready with limitations | Role checks enforced server-side and tested on all 21 staff operations; support staff can see every ticket's contact details (SEC-06) |
| 4 | [Data persistence](#34-data-persistence) | Ready with limitations | Transactional and tested, but SQLite: one writer, one host, no encryption at rest |
| 5 | [Database migrations](#35-database-migrations) | Ready with limitations | Automatic, transactional, tested upgrades; no versioned migration tool, no rollback |
| 6 | [Environment configuration](#36-environment-configuration) | Ready with limitations | Strict validation that fails closed (verified); secrets come from a `.env` file or environment variables, with no secrets manager |
| 7 | [AI model initialization](#37-ai-model-initialization) | Ready with limitations | Lazy or background load with keyword fallback; 22–160 s cold load, about 0.5–0.6 GB per process; model revision not pinned |
| 8 | [External API dependency](#38-external-api-dependency) | **Blocked** (with the LLM on) | Failure handling is strong and tested, but there is no live evaluation, no spending cap and no data-processing agreement. With `LLM_ENABLED=false` there is no external API dependency |
| 9 | [Resource consumption](#39-resource-consumption) | Ready with limitations | Every limit has a ceiling; rate limits are per process and there is no global concurrency cap or per-request timeout |
| 10 | [Logging](#310-logging) | Ready with limitations | Request IDs and redaction, with tests; plain-text output to stdout, no shipping or retention |
| 11 | [Monitoring](#311-monitoring) | **Blocked** | Health and readiness endpoints only: no metrics, alerting, error tracking or uptime monitor |
| 12 | [Backup and recovery](#312-backup-and-recovery) | **Blocked** | Scheduled, verified backups and encrypted off-server copies are built and tested, but no off-server destination is configured yet (an operator action), and retention scripts are not scheduled |
| 13 | [Performance](#313-performance) | Ready with limitations | Measured locally: chat p50 573 ms, p99 7.1 s at 8 concurrent conversations (mocked model); capacity on real hardware with a real model is unknown |
| 14 | [Operational documentation](#314-operational-documentation) | **Blocked** | Extensive development documentation, but no deployment guide, runbook or incident procedure |

**Totals:** Ready 0 · Ready with limitations 8 · Blocked 6 · Not evaluated 0 (as
categories). (*Stage 13:* this line first read 9 and 5, which did not match the table
above; corrected.)

---

## 3. Category assessments

Each section gives the result, the evidence, the limitations or blockers, and what
would change the result.

### 3.1 Security

**Result: Blocked**

**Evidence**

- Stage 12 security audit ([docs/security-audit.md](security-audit.md), 2026-10-18):
  24 findings. 13 fixed, 2 partly fixed or mitigated, 2 accepted, 1 closed (false
  positives), 6 open. Each fix has a regression test that failed without the fix
  (security audit §13; test report §7).
- Application-level controls verified on a production-mode server for this assessment
  ([section 4](#4-verification-performed-for-this-assessment)): `/docs` and
  `/openapi.json` return `404`; every response carries `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and
  `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`.
- `DEBUG=true` and plain-HTTP CORS origins are refused at startup when
  `APP_ENV=production` (verified, `app/core/config.py` `_forbid_debug_in_production`,
  `_https_origins_in_production`).
- Dependency audit: `pip-audit` found no known vulnerabilities in 95 installed packages
  on 2026-10-18 (test report §5). `bandit`: 11 low-confidence findings, all triaged
  (SEC-17).
- 231 tests in the security group pass (test report §2).

**Blockers**

1. **No transport security.** Nothing in the repository terminates TLS or sets HSTS.
   Bearer tokens for customers and staff would cross the network in clear over plain
   HTTP. The audit leaves TLS, HSTS, proxy timeouts and Streamlit headers to a reverse
   proxy that does not exist yet (security audit §11).
2. **Open deployment blockers from the audit:** SEC-08 (staff identity, see
   [3.2](#32-authentication)), SEC-09 (per-process rate limits) and SEC-10 (stale FAQ
   catalogue across workers).
3. **Never tested as deployed.** No penetration test, dynamic scan, or review of host,
   network or TLS configuration (security audit §14).

**Limitations (not blocking on their own)**

- The `server: uvicorn` response header names the server software (observed in
  section 4). Minor; normally removed at the proxy.
- Grounding checks on AI-written answers are heuristics (SEC-15, SEC-21).
- Transitive dependency versions are not hash-locked (SEC-16).

**To move to Ready with limitations:** a reverse proxy with TLS, HSTS, timeouts and
request limits, plus Streamlit security headers; close SEC-08; enforce one worker or
close SEC-09/SEC-10; a dynamic scan of the deployed instance.

### 3.2 Authentication

**Result: Blocked**

> Reassessed for the Docker deployment in
> [docs/authentication-review.md](authentication-review.md) (2026-10-19): customer
> tokens verified through the proxy; staff tokens confirmed to have no expiry and to
> need a restart to revoke; the dashboard now refuses non-private source addresses;
> blockers B1 to B7 listed there.

**Evidence: customer sessions (sound)**

- 256-bit random tokens (`secrets.token_urlsafe(32)`), stored only as SHA-256 hashes,
  compared in constant time, with a 60-minute lifetime that activity does not extend
  (security audit §5).
- Missing token: `401`. Invalid token: `404`, identical to an unknown session. Expired:
  `410` only for the right token. Credentials in URLs refused (`400`). All tested in
  `tests/test_security_audit.py`.

**Evidence: staff (prototype)**

- Static `stf_` bearer tokens, configured as hashes in `STAFF_TOKEN_HASHES`
  (`app/core/staff_auth.py`). The module states: "not a production identity system:
  there is no password login, token expiry, rotation schedule, MFA or audit of
  sign-ins. Revoke a token by removing its entry and restarting."
- The dashboard expires an idle tab after 30 minutes and any session after 8 hours
  (SEC-20), but only on the page side. The token itself stays valid indefinitely.
- With no staff configured, every staff endpoint answers `403 STAFF_ACCESS_DISABLED`
  (verified in section 4).

**Blocker:** SEC-08. A leaked staff token grants admin access, including customer
contact details, until someone notices, edits the configuration and restarts every
process. There is no lockout or alerting on failed attempts.

**To move to Ready:** SSO/OIDC or an authenticating proxy with per-person accounts and
MFA; short-lived sessions; revocation without a restart; staff dashboard and `/admin/*`
reachable only from staff networks (security audit §5, "Missing controls").

### 3.3 Authorization

**Result: Ready with limitations**

**Evidence**

- Two roles with explicit permission sets (`ROLE_PERMISSIONS` in
  `app/core/staff_auth.py`). Checks run in dependencies before the handler and again in
  services.
- Missing or invalid staff credentials get `401` on all 21 staff operations; support
  staff get `403` on all 13 admin-only operations; state is verified unchanged after
  each refusal (security audit §5).
- Customer data is scoped by session ID **and** verified token. Another customer's
  conversation or ticket returns `404`, indistinguishable from one that does not exist.
- Refused staff requests and staff views of customer contact details are written to the
  append-only audit log (SEC-05 fixed; `tests/test_audit_contact_views.py`).

**Limitations**

- SEC-06: every support staff member can read every ticket's contact details, not only
  the assignee's. Accepted for a small trusted team; not least privilege.
- Authorization is only as strong as authentication ([3.2](#32-authentication)).

### 3.4 Data persistence

**Result: Ready with limitations**

> Reassessed in [docs/sqlite-review.md](sqlite-review.md) (2026-10-19): SQLite kept.
> Foreign keys enforced, WAL, 5 s busy timeout, `synchronous=FULL`; no write transaction
> spans an LLM call (measured); concurrent writer processes lost nothing (8 processes, 0
> errors); production now refuses a database on a network filesystem.

**Evidence**

- SQLAlchemy over SQLite (`app/database/connection.py`): WAL journal, foreign keys on
  every connection, `secure_delete` on, writes under `BEGIN IMMEDIATE` with a 5-second
  busy timeout. Each unit of work commits or rolls back as a whole.
- Idempotency keys and processing leases live in the database. Concurrent ticket and
  FAQ updates use optimistic checks; the second writer gets `409`
  (`tests/test_idempotency_and_concurrency.py`, security audit §7).
- Every test uses a temporary or in-memory database; `tests/conftest.py` forces
  `DATABASE_URL=sqlite://`.

**Limitations**

- **SQLite only.** `DATABASE_URL` rejects any other backend (verified in section 4). One
  writer at a time; the file cannot be shared by several hosts, and SQLite locking is
  unreliable on network file systems.
- **One process recommended.** FAQ snapshots, the semantic index, rate limits and the
  LLM circuit breaker are per process (SEC-09, SEC-10). Leases and idempotency were
  tested with two engines, not with real `uvicorn --workers`.
- **No encryption at rest.** The database and `data/backups/` hold customer names,
  emails and phone numbers in clear. They depend on disk encryption and file
  permissions on the host.
- Retention depends on scripts that nobody schedules (SEC-12, see
  [3.12](#312-backup-and-recovery)).

**To lift the limitations:** a database server (for example PostgreSQL; only the
repositories talk to the database), and shared state for limits and catalogue revision.

### 3.5 Database migrations

**Result: Ready with limitations**

**Evidence**

- `Database.init_schema()` runs at every start. It creates missing tables and indexes,
  adds missing **nullable** columns with `ALTER TABLE ... ADD COLUMN`, and rebuilds the
  listed support tables when their stored definition differs from the code. The rebuild
  follows SQLite's documented procedure in one transaction with a foreign-key check, and
  rolls back on any problem.
- A missing required column stops startup with a named error rather than guessing.
- SEC-23 (audit-table rebuild silently dropping the append-only triggers) was found and
  fixed, with a regression test.
- Verified for this assessment: the current code started from a database written on
  2026-10-17 (6 tables), upgraded it to the current schema (9 tables, 2 audit triggers)
  and reported ready ([section 4](#4-verification-performed-for-this-assessment)).

**Limitations**

- No versioned migration tool (no Alembic, no schema version number), and no downgrade
  path. Rolling back the code after an upgrade may leave a schema the old code does not
  expect.
- Upgrades run implicitly at startup in whichever process starts first. With several
  processes starting together, concurrent upgrades have not been tested.
- Not portable to another database; moving off SQLite means writing real migrations.

**Recommended before deployment:** take a backup before every deploy (none is taken
automatically, see [3.12](#312-backup-and-recovery)); adopt versioned migrations when
moving to a database server.

### 3.6 Environment configuration

**Result: Ready with limitations**

**Evidence**

- Every setting is typed and validated by `Settings` (`app/core/config.py`), with
  cross-field checks: the lease must outlast the slowest LLM turn, the body limit must
  fit the longest message, answer candidates must not exceed retrieval depth.
- Startup refuses invalid configuration with a message naming the variable, never its
  value. Five refusals verified in section 4: `DEBUG=true` in production, a PostgreSQL
  URL, an `http://` CORS origin in production, `LLM_ENABLED=true` without a key, and a
  missing FAQ seed file on an empty database.
- `.env.example` documents every variable with placeholders only (SEC-04 fixed,
  `test_env_example_holds_placeholders_only`). `.env` is git-ignored and was never
  tracked; no credential was found in tracked files or history (security audit §8).
- `OPENAI_API_KEY` is a `SecretStr`; the startup log shows only whether it is set
  (observed in section 4).

**Limitations**

- Secrets come from environment variables or a `.env` file. There is no integration
  with a secrets manager.
- `.env` is always read from the project root (`ENV_FILE`). On a server, a leftover
  development `.env` would be loaded silently for any variable not set in the
  environment.
- Staff changes (`STAFF_TOKEN_HASHES`) need a restart of every process.
- Helper scripts (`scripts/start-dev.ps1`, `start-demo.ps1`, `quality_checks.ps1`) are
  PowerShell. The application has only been run on Windows; the Linux CI workflow has
  never run ([section 5](#5-not-evaluated)).

### 3.7 AI model initialization

**Result: Ready with limitations**

**Evidence**

- Importing the application loads no model (`tests/test_main_import.py`; the startup
  in section 4 reports `semantic_search: not_loaded`).
- `SEMANTIC_INDEX_PRELOAD` chooses when the model loads: on first use, before serving,
  or in a background thread (serving after 0.18 s; a chat that arrives earlier waits for
  the same load). Concurrent first requests load the model once
  (`test_concurrent_first_requests_load_the_model_once`).
- Loads from the local cache first (`local_files_only=True`) and downloads only if
  missing; `HF_HUB_OFFLINE=1` forbids downloading.
- If the model cannot load, the app still serves: keyword-only answers when the evidence
  is strong, otherwise a fallback with `Retry-After`. The failure is remembered for
  `EMBEDDING_LOAD_RETRY_SECONDS` so queued requests do not each retry a slow download.
- Readiness reports the model's state but does not depend on it (`app/api/routes/health.py`).

**Limitations**

- **Cold load is slow and variable:** 22–88 s (Stage 9) and 159 s on the first semantic
  request of the Stage 12 load test. With the default `SEMANTIC_INDEX_PRELOAD=false`,
  the first customer waits longer than the frontend's 60-second read timeout.
- **Memory:** about 561 MB per process after warm-up, 591 MB peak under load (test
  report, Performance).
- **The model revision is not pinned.** `EMBEDDING_MODEL_NAME` names the repository
  only, so a fresh host downloads whatever Hugging Face serves that day. Retrieval
  results were evaluated against the locally cached copy. *Resolved in Stage 13:* the
  deployment fetches commit `1110a243…` and verifies it, and the application loads the
  same commit for the default Hub ID ([docs/embedding-model.md](embedding-model.md)).
- A host without the cached model needs internet access to Hugging Face at first load.

**Required for deployment:** `SEMANTIC_INDEX_PRELOAD=background` or `true`; the model
files copied onto the host (or into an image) at a recorded revision, with
`HF_HUB_OFFLINE=1`.

### 3.8 External API dependency

**Result: Blocked** for a deployment with `LLM_ENABLED=true`. With the default
`LLM_ENABLED=false`, the application makes no external API calls at runtime and this
category does not apply.

**Evidence: failure handling (strong)**

- `app/services/llm_service.py`: 20 s timeout per attempt, at most 2 retries with
  capped, jittered backoff, and a circuit breaker that pauses calls for 60 s after 3
  failures. Configuration errors (such as an invalid key) open it immediately.
- When the model fails, customers get the approved FAQ answer, not an error. Readiness
  reports `llm: paused` while the breaker is open and stays ready.
- Requests use `store=False`; the session is sent only as a hashed `safety_identifier`.
- Every control case in the unified AI evaluation passed (35/35): manipulated models,
  model failures, FAQ changes during an answer (AI report §7).

**Blockers**

1. **No live evaluation.** Every recorded run used a scripted model. A real model's
   wording, refusal rate, latency and token use have not been measured. The opt-in live
   tests (`RUN_OPENAI_LIVE_TESTS`) were not run (test report §8). The AI report
   recommends a reviewed live evaluation before customers see generated answers.
2. **No spending cap.** `/answer` needs no token; it is rate-limited to 30 requests per
   minute per IP (SEC-07 mitigated), but per process and per IP, with no budget.
   `LLM_PRICING` is empty by default, so cost estimates are off.
3. **Personal data goes to the provider.** Customer-typed text is sent to OpenAI as
   written (SEC-11). A data-processing agreement and a customer notice are needed.

**Other notes:** the default model `gpt-5.4-mini` is set in configuration, and its
availability was not checked for this assessment. The ticket integrations are a null
sink (`NullTicketEventSink`): no email, CRM or helpdesk is called, and nothing claims to
notify anyone.

### 3.9 Resource consumption

**Result: Ready with limitations**

**Evidence**

- Request bodies above 32 KB get `413` before parsing (including chunked bodies). Chat
  messages are capped at 4,000 characters. History and LLM context have character and
  token budgets. LLM output is capped at 800 tokens (security audit §11).
- Every limit setting has a ceiling, so configuration cannot make it unbounded
  (`test_every_resource_limit_has_a_ceiling`).
- Rate limits: 120 requests per minute per IP; 20 new conversations per minute per IP;
  20 chat messages per minute per conversation; 5 support requests per minute; 30 per
  minute for the token-free search and answer endpoints.
- Measured memory: 92 MB before the model, 561 MB warm, 591 MB peak (test report).

**Limitations**

- Rate limits live in process memory: they reset on restart, are not shared between
  workers, and see the proxy's address unless uvicorn runs with `--proxy-headers` and
  `--forwarded-allow-ips` (SEC-09).
- No global concurrency cap beyond the server's thread pool, and no per-request timeout
  in uvicorn. Both are expected from a proxy that does not exist yet.
- Each additional worker adds about 0.6 GB and its own model load.
- No LLM spending cap ([3.8](#38-external-api-dependency)).

### 3.10 Logging

**Result: Ready with limitations**

**Evidence**

- One root handler, one format:
  `timestamp | level | logger | req=<request ID> | message` (`app/core/logging_config.py`).
  The server-generated request ID is returned in `X-Request-ID` and in every error body,
  so a customer report can be matched to its log lines (observed in section 4).
- Requests are logged by path only. Chat messages and search text are never logged.
  Tokens, API keys, `password=`-style values and email addresses are redacted as a
  safety net. Exception messages are withheld unless `DEBUG` is on, which production
  refuses.
- A full support flow with synthetic personal data logs none of it
  (`test_a_full_support_flow_logs_no_credentials_or_personal_data`).
- Structured `event=...` lines for errors, answer decisions, grounded answers, LLM usage
  and conversation turns, with durations.
- The startup line logs only an allow-listed settings summary (`Settings.safe_summary`).

**Limitations**

- Plain text on stdout. The structured fields exist on records (`decision_fields`), but
  no JSON formatter is configured.
- No collection, retention or rotation: that is left to whatever runs the process, and
  nothing runs it yet.
- Startup logs the session ID of each new conversation. This is a random ID, not
  personal data by itself (security audit §9).

### 3.11 Monitoring

**Result: Blocked**

**Evidence (what exists)**

- `GET /api/v1/health` (liveness) and `GET /api/v1/ready` (knowledge base and storage
  checks, plus informational model and LLM state). Both are cheap, exempt from rate
  limits and send `Cache-Control: no-store`. Verified in section 4.
- Stage 13 ([docs/health-checks.md](health-checks.md)): readiness also fails on a
  read-only or almost full database disk, and reports a missing model folder at once;
  `GET /api/v1/status` gives the public site the verdict only (`/healthz`), and
  `/api/v1/ready` is no longer reachable from outside; all checks answer `HEAD`.
- Stage 13 ([docs/logging.md](logging.md)): structured JSON logs on standard output in
  Docker (fixed fields: timestamp, level, service, request and correlation IDs, route
  template, status, duration, error category), correlated between the pages and the
  backend, health checks at `DEBUG`. The pages' logs were silently dropped before.
  Stage 13 ([docs/monitoring.md](monitoring.md)): in-memory operational metrics (requests,
  errors, latencies, failures, uptime) for staff, with a periodic copy in the log; process-
  local and reset on restart. Still no alerting or log shipping off the server.
- LLM token usage and cost estimates are recorded in a bounded in-memory buffer and in
  the log (`app/services/usage_tracker.py`). Nothing is persisted and no endpoint
  exposes them.
- The admin dashboard shows business analytics (tickets, conversations, FAQ use), not
  service health.

**Blockers**

- No metrics (request rates, latency, error rates, queue time for the SQLite write
  lock, memory).
- No alerting on anything: failed readiness, error bursts, bursts of
  `auth.access_denied`, an open LLM circuit breaker, or disk space for the database.
- No error tracking and no uptime monitor configured.

Without these, an outage or an attack on the staff endpoints would be noticed only when
someone complains.

**To move to Ready with limitations:** an uptime check on `/healthz` (`/api/v1/status`); log collection with
alerts on `event=api_error` status 5xx, `auth.access_denied` bursts and LLM circuit
events; host metrics for memory and disk.

### 3.12 Backup and recovery

**Result: Blocked**, now for two narrow reasons: no off-server destination is configured
yet, and the retention scripts are not scheduled. Everything else is in place and was
verified (updated 2026-10-19; details in
[docs/backup-and-restore.md](backup-and-restore.md)).

**Evidence (what exists)**

| Requirement | Status |
|---|---|
| Consistent online backups | `scripts/backup_database.py`: SQLite's online backup API, verified (integrity, foreign keys, every table), timestamped, never overwriting, with a checksum manifest, files `0600` |
| **Scheduled** | The `backup` service in Compose: daily at `BACKUP_AT` (UTC), keeps `BACKUP_KEEP`, no network at all, and turns **unhealthy** when backups stop succeeding. Verified on Docker: backed up on start, next run scheduled, health check passing, stopped in 0.85 s on `SIGTERM` |
| **Encrypted off-server copies** | The opt-in `backup-offsite` service (restic): client-side encryption, daily at `OFFSITE_AT`, retention 14 daily, 8 weekly, 6 monthly, read-only access to the backups, outbound network only. Verified against a **local stand-in** repository: snapshots made, no readable database or schema text in the repository, a wrong password refused, and a backup restored from the repository verified against its manifest and test-restored |
| Restore procedure | `scripts/restore_database.py`: a test restore into a separate database by default; replacing the active database only with `--confirm`, after a safety backup. A local restore test with synthetic data passed on Docker |
| Backup before upgrades | Documented as a step of every upgrade (`scripts.backup_database`); not enforced |
| Recovery objectives (measured, proposed) | **RPO:** up to 24 hours (daily backups; take one by hand before risky changes). **RTO:** under 15 minutes for this database size (restore and verify took seconds; the backend becomes ready in about a minute), plus fetching the copy from off-server storage. Confirm these with the business |

**Blockers (remaining)**

- **No off-server destination configured.** Until `deploy/environments/<env>/offsite.env`
  names a restic repository on storage you control, and the `offsite` profile is
  started, every backup stays on the same server as the database. This needs your
  storage account, credentials and an encryption password; it cannot be done in the
  repository.
- **Retention is not scheduled** (SEC-12): `cleanup_conversations` runs only at startup,
  `anonymize_support_data --retention` only by hand.
- Not yet tested: recovery onto a **different** server from the off-server copy, and a
  timed drill against agreed objectives.

**To move to Ready with limitations:** configure `offsite.env` with real off-server
storage and start the `offsite` profile; confirm the first copy (`docker compose ps`
healthy; `restic snapshots`); schedule the retention scripts; run one timed recovery
drill onto another machine.

### 3.13 Performance

**Result: Ready with limitations** (low traffic, one process, one host)

**Evidence** (`scripts/load_test_local.py`, real uvicorn on loopback, real embedding
model, **mocked LLM**, 200 requests per scenario at concurrency 8, rate limits off;
results in `data/load_test_results.json`; test report, Performance):

| Scenario | Throughput | p50 | p99 | Errors |
|---|---|---|---|---|
| Health | 116.0/s | 59.5 ms | 210.4 ms | 0 |
| Keyword search | 87.1/s | 76.7 ms | 218.4 ms | 0 |
| Hybrid search | 24.3/s | 322.8 ms | 447.9 ms | 0 |
| Create conversation | 81.8/s | 66.9 ms | 715.8 ms | 0 |
| Chat, 8 parallel conversations | 9.1/s | 573.4 ms | 7,122.3 ms | 0 |

- 0 errors in 1,000 requests. Database calls take a few milliseconds each.
- Warm in-process chat: median 67.8 ms over 44 questions (grounding evaluation).

**Limitations**

- Chat p99 of about 7 s comes from writers queuing for SQLite's single write lock. A
  wait beyond the 5-second busy timeout returns `503`; none did at this load, but the
  margin is small.
- A real LLM adds seconds per reply. That latency is not measured.
- Measured on one developer laptop (Windows 10, 4 CPUs), not on target hardware. The
  capacity of a deployment is unknown.
- Cold start: 159 s for the first semantic request without preload
  ([3.7](#37-ai-model-initialization)).
- Several workers were not load-tested, and SQLite under real multi-process writes is
  untested.

### 3.14 Operational documentation

**Result: Blocked**

**Evidence (what exists)**

- README (about 300 KB): setup, every configuration variable, logging, performance,
  limitations and troubleshooting. Plus thirteen documents in `docs/`, including the
  security audit, AI evaluation, test and quality report, admin guide, chat API and
  integration guides.
- `scripts/check_setup.py` checks settings, FAQ data, the database, the model cache and
  backend readiness before startup.
- `.github/workflows/quality.yml` defines offline quality checks and a dependency audit.

**Blockers**

- **No deployment guide** for any target: no host requirements, process manager,
  reverse proxy configuration, TLS or container definition.
- **No runbook:** nothing on what to do when readiness fails, the model will not load,
  the database is locked, the LLM breaker stays open, or a staff token leaks.
- **No incident or restore procedure** ([3.12](#312-backup-and-recovery)).
- The CI workflow has never run: the repository has no remote (test report §8).

**Corrected during this assessment:** the README's "Development vs production" section
recommended `--workers 2`, contradicting the single-worker requirement in its own Stage
12 section and in the security audit. An operator following it would have got
per-worker rate limits and stale FAQ answers after edits (SEC-09, SEC-10). It now
recommends one worker and explains why.

**To move to Ready with limitations:** write a deployment guide for the chosen host;
write a runbook covering the failure modes above and staff token revocation.

---

## 4. Verification performed for this assessment

Run on 2026-10-19 on the development machine (Windows 10, Python 3.13.3, project
virtual environment). Every run used a **temporary database in a scratch folder**.
`data/conversations.db` was not opened. No model was downloaded (`HF_HUB_OFFLINE=1`),
and no OpenAI request was made (`LLM_ENABLED=false`).

**Startup refusals** (`APP_ENV=production`, by importing `app.main.app`):

| Configuration | Result |
|---|---|
| `DEBUG=true` | Refused: "DEBUG must be false when APP_ENV=production." |
| `DATABASE_URL=postgresql://...` | Refused: "DATABASE_URL must be a SQLite URL ..." |
| `CORS_ORIGINS=["http://example.com"]` | Refused: "CORS_ORIGINS must use https:// when APP_ENV=production" |
| `LLM_ENABLED=true`, no key | Refused: "LLM_ENABLED=true requires OPENAI_API_KEY ..." |
| `FAQ_DATA_PATH` missing, empty database | Refused: "Cannot start: the FAQ knowledge base failed to load (FAQFileNotFoundError)" |

**Production-mode server** (`uvicorn app.main:app` on `127.0.0.1:8765`,
`APP_ENV=production`, fresh database, no staff, LLM off, no preload):

| Check | Result |
|---|---|
| Startup | FAQ table seeded with 30 FAQs (29 active); "startup complete" in about 0.7 s |
| `GET /api/v1/health` | `200`, with `X-Request-ID` and the four security headers listed in [3.1](#31-security) |
| `GET /api/v1/ready` | `200`: `knowledge_base: ok`, `conversation_storage: ok`, `semantic_search: not_loaded`, `llm: disabled` |
| `GET /docs`, `GET /openapi.json` | `404` both |
| `GET /api/v1/admin/me` with a well-formed token | `403 STAFF_ACCESS_DISABLED` |
| `POST /api/v1/conversations` | `201` |
| `GET /api/v1/faqs/search?q=refund` | `200` |
| Log | Path-only access lines with request IDs; allow-listed settings summary; no tokens or message text |

The server was stopped by force, so graceful shutdown was not observed.

**Restore drill:** `data/backups/import-20261017T091625Z.db` was copied to the scratch
folder. On the copy: `PRAGMA integrity_check` returned `ok` (6 tables, 9 FAQs, 0
conversations, 0 tickets). `create_app()` upgraded it to 9 tables with 2 audit triggers,
and `/api/v1/ready` returned `200`.

**Tests:**
`python -m pytest -q tests/test_readiness.py tests/test_config.py tests/test_main_import.py tests/test_health.py tests/test_logging.py`
gave **68 passed**. The full suite (3,646 tests) was not re-run for this assessment. Its
last recorded result is 3,635 passed, 0 failed, 4 opt-in skipped (test report §2).

## 5. Not evaluated

Items for which there is no evidence. Each could change a result above.

| Item | Affects | Why not evaluated |
|---|---|---|
| Any container or deployment build | 1, 14 | No Dockerfile, compose file or deployment definition exists |
| Running on Linux | 6, 13 | Only Windows has been used; the Linux CI workflow has never run |
| TLS, reverse proxy, host and network configuration | 1, 9 | Not built |
| Several uvicorn worker processes | 4, 5, 9, 13 | Not tested with real workers; concurrent schema upgrades untested |
| Live OpenAI behaviour, latency and cost | 8, 13 | Paid; opt-in tests not run |
| Availability of the configured `gpt-5.4-mini` model | 8 | Would need a live call |
| Graceful shutdown under load | 4, 7 | Not exercised |
| Recovery from loss of the host | 12 | No off-host backups exist |
| Behaviour with thousands of FAQs or long-lived databases | 4, 13 | Data set is 30 FAQs; synthetic scoring benchmarks only (README, Memory) |
| Streamlit pages in a real browser beyond `AppTest` | 1 | Manual checklist exists ([docs/frontend-manual-testing.md](frontend-manual-testing.md)); not run here |

## 6. Path to readiness

In order of risk. Items 1–6 clear the five **Blocked** categories. Items 7–9 address the
most important limitations.

1. **Hosting foundation** (Security, Operational documentation): choose a host; add a
   deployment definition and a reverse proxy with TLS, HSTS, timeouts, request limits
   and Streamlit headers; run **one** backend worker; write the deployment guide.
2. **Staff identity** (Authentication): SSO/OIDC or an authenticating proxy with MFA;
   staff surfaces reachable only from staff networks (SEC-08).
3. **Backups** (Backup and recovery): scheduled encrypted off-host backups, a backup
   before each deploy, a written and timed restore procedure; schedule the retention
   scripts (SEC-12).
4. **Monitoring** (Monitoring): uptime check on `/ready`, log collection, alerts on 5xx
   errors, refused staff access and LLM circuit events, host memory and disk.
5. **Runbook** (Operational documentation): the failure modes in [3.14](#314-operational-documentation).
6. **Before enabling the LLM** (External API dependency): a reviewed live evaluation, a
   provider spending cap, a data-processing agreement and a customer notice (SEC-07,
   SEC-11). Until then, deploy with `LLM_ENABLED=false`.
7. **Model provisioning** (AI model initialization): pin the embedding model revision,
   bake it onto the host, set `HF_HUB_OFFLINE=1` and preload.
8. **Scale-out** (Data persistence, Resource consumption, Performance): a database
   server with versioned migrations, shared rate limits and catalogue state (SEC-09,
   SEC-10), then load testing on target hardware with a real model.
9. **Answer quality** (not a deployment category, but customer-facing): the retrieval
   tuning in AI report §10. The production decision gives 5 wrong answers in 82 cases
   (6.1%).

## 7. Sources

- [docs/security-audit.md](security-audit.md): findings SEC-01 to SEC-24, §5 authentication and authorization, §7 database, §8 secrets, §9 logging and privacy, §11 resource limits.
- [docs/test-and-quality-report.md](test-and-quality-report.md): test counts, coverage, dependency audit, load test, items not executed.
- [docs/ai-evaluation-report.md](ai-evaluation-report.md): retrieval, grounding, injection and unified evaluation results.
- [docs/performance-and-evaluation.md](performance-and-evaluation.md): Stage 9 cold-start and per-stage timings.
- Code: `app/main.py`, `app/core/config.py`, `app/core/staff_auth.py`, `app/core/rate_limit.py`, `app/core/logging_config.py`, `app/database/connection.py`, `app/api/routes/health.py`, `app/services/embedding_service.py`, `app/services/llm_service.py`, `app/services/usage_tracker.py`, `scripts/import_faqs.py`, `.streamlit/config.toml`, `.github/workflows/quality.yml`, `requirements.txt`.
- Data: `data/load_test_results.json`, `data/ai_evaluation_results.json`, `data/backups/import-20261017T091625Z.db`.
