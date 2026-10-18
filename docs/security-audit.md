# Security audit (Stage 12)

A structured review of the customer support chatbot: the FastAPI backend, the customer
Streamlit page, the staff admin dashboard, the SQLite storage and the OpenAI integration.
It records what was checked, how, what was found, what was fixed, and what remains open.

**Audit date:** 2026-10-18. **Code reviewed:** `main` at `d1f2dce` plus the Stage 12 fixes
listed below. **Verification suite:** `tests/test_security_audit.py` (103 tests) and one
added page test in `tests/test_admin_frontend.py`.

> **This is not a compliance statement.** The review used the OWASP lists below as a map
> of what to look at. Working through a checklist does not make the application
> "OWASP-compliant" or secure; it means these specific risks were examined with the
> evidence shown. There was no external penetration test, no dynamic scanner against a
> deployed instance, and no review of hosting, TLS or network configuration, because
> the application has not been deployed.

## Contents

1. [Scope and method](#1-scope-and-method)
2. [Severity scale](#2-severity-scale)
3. [Findings summary](#3-findings-summary)
4. [Findings in detail](#4-findings-in-detail)
5. [Authentication and authorization review (Task 3)](#5-authentication-and-authorization-review-task-3)
6. [API input validation review (Task 4)](#6-api-input-validation-review-task-4)
7. [SQL injection and database review (Task 5)](#7-sql-injection-and-database-review-task-5)
8. [Secrets and configuration review (Task 6)](#8-secrets-and-configuration-review-task-6)
9. [Logging and privacy review (Task 7)](#9-logging-and-privacy-review-task-7)
10. [OWASP coverage map](#10-owasp-coverage-map)
11. [Re-running the verification](#11-re-running-the-verification)
12. [Limits of this audit](#12-limits-of-this-audit)

---

## 1. Scope and method

### In scope

| Component | Files |
|---|---|
| Customer API (FAQs, search, answer, conversations, support tickets) | `app/api/routes/*.py`, `app/models/*.py` |
| Staff API (tickets, FAQ management, analytics, audit log) | `app/api/routes/support.py`, `app/api/routes/admin.py` |
| Authentication | `app/core/security.py`, `app/core/staff_auth.py`, `app/api/dependencies.py` |
| Error handling, logging, middleware | `app/api/errors.py`, `app/core/logging_config.py`, `app/core/middleware.py`, `app/core/rate_limit.py` |
| Storage | `app/database/*.py`, `app/repositories/*.py` (SQLAlchemy over SQLite) |
| LLM integration | `app/prompts/*.py`, `app/services/llm_service.py`, `context_builder.py`, `grounding_validator.py`, `query_rewrite_service.py` |
| Frontends | `frontend/app.py` and components (customer), `frontend/admin_*.py` (staff) |
| Configuration and secrets | `app/core/config.py`, `.env.example`, `.gitignore`, `.streamlit/config.toml`, git history |
| Dependencies | `requirements.txt` |

### Reference lists

- **OWASP API Security Top 10 (2023)**: API1 to API10.
- **OWASP Top 10 for web applications (2021)** and the OWASP Cheat Sheets for
  authentication, session management, logging and input validation.
- **OWASP Top 10 for LLM Applications (2025)**: LLM01 prompt injection, LLM02 sensitive
  information disclosure, LLM05 improper output handling, LLM06 excessive agency,
  LLM09 misinformation, LLM10 unbounded consumption.

### Method

1. **Manual code review** of every route, dependency, model, repository and frontend
   renderer listed above.
2. **Executable verification**: every check in sections 5 to 9 is a test in
   `tests/test_security_audit.py` that runs the real application in-process against a
   temporary SQLite file. No production database was used; no existing record was
   touched; all names, emails and phone numbers are synthetic (the phone number is from a
   range reserved for fiction).
3. **Regression proof** for each fix: the new test was run against the code without the
   fix (it failed) and with it (it passed).
4. **Tools**, run locally in an isolated virtual environment:
   - `pip-audit 2.x` against `requirements.txt` (sends only package names and versions
     to the PyPI/OSV vulnerability database; no code or secrets).
   - `bandit` static analysis over `app/`, `frontend/`, `scripts/`.
   - A pattern scan of git-tracked files and the full git history for API keys, staff
     and conversation tokens, and private keys (values were never printed).
   No secret was sent to any third-party service.

---

## 2. Severity scale

Severity combines **exploitability** (who can trigger it, what they need) and **impact**
(what they gain) in the application's current state: a local or internal prototype.
Findings that only matter once the application is exposed publicly are marked
**deployment blocker** and rated for that situation, with the reasoning stated.

| Severity | Meaning here |
|---|---|
| **Critical** | Exploitable by an unauthenticated outsider with no special conditions; leads to broad compromise of customer data, credentials or the server. |
| **High** | Exploitable with modest preconditions (e.g. any customer account) and leads to another person's data, privilege escalation, or significant cost or integrity damage. |
| **Medium** | Needs specific conditions, or the impact is limited (phishing aid, accountability gap, bounded cost abuse), or it becomes serious only in a realistic deployment. |
| **Low** | Defence-in-depth gap: the primary control works, but a secondary one is missing or inconsistent. |
| **Informational** | No exploitable weakness found; a residual risk, a design trade-off, or a recommendation. |

No finding was rated Critical or High. That reflects what was found, not an assurance
that none exist.

---

## 3. Findings summary

| ID | Finding | Component | Severity | Status |
|---|---|---|---|---|
| [SEC-01](#sec-01) | Staff dashboard rendered customer-written ticket text as Markdown | `frontend/admin_tickets.py` | Medium | **Fixed** |
| [SEC-02](#sec-02) | Customer ticket read accepted a credential in the URL when a header was also sent | `GET /support/tickets/{id}` | Low | **Fixed** |
| [SEC-03](#sec-03) | Staff tokens not redacted from log text unless preceded by "Bearer" | `app/core/logging_config.py` | Low | **Fixed** |
| [SEC-04](#sec-04) | `.env.example` (working copy) held a real staff token hash instead of a placeholder | `.env.example` | Low | **Fixed** (never committed) |
| [SEC-05](#sec-05) | Staff reads of customer contact details are not recorded in the audit log | Ticket read, audit log | Medium | Open |
| [SEC-06](#sec-06) | Any support staff member can read every ticket's contact details | Staff roles | Low | Open (accepted for prototype) |
| [SEC-07](#sec-07) | Unauthenticated `/answer` can drive paid LLM calls up to the general per-IP limit | `POST /answer` | Medium | Open |
| [SEC-08](#sec-08) | Staff authentication is a development prototype | `app/core/staff_auth.py` | Medium (deployment blocker) | Open |
| [SEC-09](#sec-09) | Rate limits are per process and per client IP | `app/core/rate_limit.py` | Medium (deployment blocker) | Open (architectural) |
| [SEC-10](#sec-10) | Several API workers would serve a stale FAQ catalogue after an edit | FAQ catalogue, SQLite | Medium (deployment blocker) | Open (architectural) |
| [SEC-11](#sec-11) | Personal data typed into chat is sent to OpenAI and stored as typed | Chat pipeline | Low | Open (documented) |
| [SEC-12](#sec-12) | Retention and anonymization run only on start-up or by hand | Cleanup scripts | Low | Open |
| [SEC-13](#sec-13) | No HTTP security headers set by the application | API, Streamlit | Informational | Open (deployment) |
| [SEC-14](#sec-14) | Prompt injection: layered controls in place, residual risk remains | LLM pipeline | Informational | Accepted |
| [SEC-15](#sec-15) | AI-written answers are checked by heuristics, not verified | Grounding validator | Informational | Accepted |
| [SEC-16](#sec-16) | Dependencies: no known vulnerabilities; transitive versions not locked | `requirements.txt` | Informational | Open (process) |
| [SEC-17](#sec-17) | Static-analysis warnings triaged as false positives | Various | Informational | Closed |

---

## 4. Findings in detail

### SEC-01

**Staff dashboard rendered customer-written ticket text as Markdown**

| Field | Value |
|---|---|
| Finding ID | SEC-01 |
| Affected component | Staff dashboard ticket page: `frontend/admin_tickets.py` (`render_ticket`); FAQ history captions in `frontend/admin_faqs.py` |
| OWASP | LLM05 / web A03 (improper output handling, injection into a trusted UI) |
| Description | The ticket heading (`st.subheader`) and the contact block (`st.write`) interpolated the customer's subject, name, email and phone into Markdown. Streamlit renders Markdown links and images (not HTML: `unsafe_allow_html` is never used). The customer page already escaped everything with `plain_markdown`; the staff page did not. |
| Severity | **Medium.** Exploitable by anyone who can start a conversation and send a support request (no account needed, 5 requests per minute per conversation). No script execution is possible, so it is not stored XSS. The impact is a convincing phishing link inside the trusted staff tool (`[Verify your staff account](https://...)` as the subject) and a remote image that acts as a tracking beacon, revealing a staff member's IP address and when the ticket was opened. |
| Preconditions | A support request submitted with Markdown in the subject or name; a staff member opens that ticket. |
| Impact | Credential phishing of staff (whose tokens give access to every customer's contact details), staff IP and activity disclosure. |
| Evidence | `frontend/admin_tickets.py` before the fix: `st.subheader(f"{ticket.reference_number}: {ticket.subject}")`, `st.write(f"**Name:** {ticket.contact.name}")`. The new page test renders a ticket whose subject is `[Verify your account](https://evil.example/login) ![](https://evil.example/p.png)`; without the fix the Markdown is passed through unescaped and the test fails. |
| Recommended fix | Escape every customer-controlled value before it reaches a Markdown-rendering call; prefer `st.text` for free text. |
| Remediation status | **Fixed.** `literal()` (`plain_markdown(...).rstrip()`) now wraps the reference, subject, name, email and phone. The description and notes were already shown with `st.text`. FAQ history captions now escape stored values too, so the history shows exactly what was saved (admin-authored, lower risk, fixed for consistency). |
| Verification test | `tests/test_admin_frontend.py::test_customer_text_on_the_ticket_page_is_shown_as_typed` (fails without the fix, passes with it); `tests/test_admin_frontend.py::test_the_page_shows_an_faqs_history`. |

### SEC-02

**Customer ticket read accepted a credential in the URL when a header was also sent**

| Field | Value |
|---|---|
| Finding ID | SEC-02 |
| Affected component | `GET /api/v1/support/tickets/{ticket_id}` (customer path), `app/api/routes/support.py` |
| OWASP | API2 broken authentication (credential handling) |
| Description | Every other authenticated route refuses requests that carry `access_token`, `token` or `authorization` as a query parameter (`400`), so a client that leaks a token into URLs, logs and browser history fails loudly. This route reads the bearer header itself, and its customer branch skipped that check. |
| Severity | **Low.** It does not grant access: the header token is still required and verified. It only removes a safeguard against a client mistake that leaks a token. |
| Preconditions | A client puts the token in the URL and also sends the header. |
| Impact | A conversation token could end up in proxy logs or history without the API refusing the request. |
| Evidence | Before the fix, `GET /support/tickets/{id}?session_id=...&access_token=<token>` with a valid header returned `200`. |
| Recommended fix | Apply the same URL check on every authenticated path. |
| Remediation status | **Fixed.** A shared `reject_token_in_url()` in `app/api/dependencies.py` is used by the conversation token dependency, staff authentication and this route. |
| Verification test | `tests/test_security_audit.py::test_credentials_in_the_url_are_refused_even_with_a_valid_header` (fails without the fix). |

### SEC-03

**Staff tokens not redacted from log text unless preceded by "Bearer"**

| Field | Value |
|---|---|
| Finding ID | SEC-03 |
| Affected component | `RedactingFormatter` in `app/core/logging_config.py` |
| OWASP | API8 security misconfiguration; web A09 logging failures |
| Description | The log formatter is a last line of defence. It masked `Bearer <anything>`, `sk-...` keys, conversation tokens (`cst_...`) on their own, and email addresses, but a staff token (`stf_...`) appearing without "Bearer" (pasted into a message, quoted in an exception) was not masked. |
| Severity | **Low.** No code path was found that logs a staff token; this is defence in depth. |
| Preconditions | A future code change or third-party library logs a staff token outside an Authorization header. |
| Impact | A long-lived staff credential (see SEC-08: no expiry) in log files. |
| Evidence | `redact("pasted stf_...")` returned the token unchanged before the fix. |
| Recommended fix | Mask both token formats on their own. |
| Remediation status | **Fixed.** The pattern is now `\b(?:cst|stf)_[A-Za-z0-9_-]{16,}`. |
| Verification test | `tests/test_security_audit.py::test_staff_tokens_are_redacted_from_log_text_even_without_bearer` (fails without the fix). |

### SEC-04

**`.env.example` (working copy) held a real staff token hash instead of a placeholder**

| Field | Value |
|---|---|
| Finding ID | SEC-04 |
| Affected component | `.env.example` |
| OWASP | API8 security misconfiguration |
| Description | An uncommitted change set `STAFF_TOKEN_HASHES` in the example file to a real entry for a named admin. The example file is tracked and meant to be copied by everyone. The entry was also malformed (63 hex characters instead of 64), so a copied `.env` would have failed to start. |
| Severity | **Low.** It is a SHA-256 hash of a 256-bit random token, so the token cannot be recovered from it. Risks: anyone copying the example inherits an admin account they did not create; the file stops being a template. |
| Preconditions | The change is committed and the example is copied. |
| Impact | An unintended admin entry on other installations. |
| Evidence | `git diff .env.example` showed the entry. `git log --all -S <hash prefix>` found no commit containing it, so it never reached history. The value is not reproduced here. |
| Recommended fix | Keep real entries only in the local `.env` (ignored by git); keep `[]` in the example. |
| Remediation status | **Fixed.** The placeholder `STAFF_TOKEN_HASHES=[]` is restored. The local `.env` holds a valid entry and starts correctly. **No rotation is needed**: the value was never committed or shared through git. Rotate it if it was shared elsewhere. |
| Verification test | `tests/test_security_audit.py::test_env_example_holds_placeholders_only` (rejects keys, tokens and 60 to 64 character hex hashes in the example file). |

### SEC-05

**Staff reads of customer contact details are not recorded in the audit log**

| Field | Value |
|---|---|
| Finding ID | SEC-05 |
| Affected component | `GET /support/tickets/{id}` (staff path), `app/models/audit.py` |
| OWASP | Web A09 logging and monitoring failures; privacy accountability |
| Description | The audit log records FAQ and ticket *changes* and refused access, but not who *viewed* a ticket's name, email and phone. A staff member could browse every customer's contact details without a trace. |
| Severity | **Medium.** It needs a valid staff token (insider or stolen token), but personal data access cannot be reviewed afterwards, which matters most when a token is misused (see SEC-08). |
| Preconditions | Any staff token with `view_customer_contacts` (admin and support roles). |
| Impact | Undetectable bulk access to customer contact data; no answer to "who saw my data?". |
| Evidence | `AuditAction` has no view action; `get_ticket` in `app/api/routes/support.py` records nothing on success. |
| Recommended fix | Add a `ticket.contact_viewed` audit action recorded on each staff detail read (staff ID, ticket ID, request ID; never the data). The audit table has a CHECK constraint on `action` and append-only triggers and is not in `REBUILDABLE_TABLES`, so this needs a careful migration (rebuild that keeps the triggers), which is why it was not rushed into this stage. |
| Remediation status | **Open.** |
| Verification test | To add with the fix: open a ticket as `maria`, then `GET /admin/audit?action=ticket.contact_viewed` lists one event with `actor_id=maria` and no contact data. |

### SEC-06

**Any support staff member can read every ticket's contact details**

| Field | Value |
|---|---|
| Finding ID | SEC-06 |
| Affected component | `ROLE_PERMISSIONS` in `app/core/staff_auth.py` |
| OWASP | API1 broken object-level authorization (by design) |
| Description | Ticket access is role-based, not object-based: the `support` role can list and open all tickets, not only those assigned to them. This is a documented design choice for a small team. |
| Severity | **Low.** Intended behaviour for a small team; no privilege boundary is crossed. It widens the impact of a stolen support token. |
| Preconditions | A support token. |
| Impact | Every ticket's contact details are reachable with any staff token. |
| Evidence | `ROLE_PERMISSIONS[StaffRole.SUPPORT]` includes `VIEW_CUSTOMER_CONTACTS`; `test_support_staff_cannot_perform_admin_operations` confirms support staff read contact details. |
| Recommended fix | For larger teams: show contact details only to the assignee (and admins), or require assignment before contact details are shown. Combine with SEC-05. |
| Remediation status | **Open, accepted for the prototype.** |
| Verification test | When changed: support staff get `403` (or a ticket without `contact`) for tickets assigned to someone else. |

### SEC-07

**Unauthenticated `/answer` can drive paid LLM calls up to the general per-IP limit**

| Field | Value |
|---|---|
| Finding ID | SEC-07 |
| Affected component | `POST /api/v1/answer` (also `semantic-search`, `hybrid-search` for CPU) |
| OWASP | API4 unrestricted resource consumption; LLM10 unbounded consumption |
| Description | `/answer` needs no conversation or token. When the LLM is enabled, any question that matches an FAQ makes one OpenAI call (bounded to 800 output tokens). The only limit is the general 120 requests per minute per IP. The chat endpoint has its own 20-per-minute limit per conversation; `/answer` has none, and there is no daily spending cap. |
| Severity | **Medium.** Easy to trigger once the API is reachable, but bounded per IP; impact is cost and quota exhaustion (which then degrades answers for everyone through the circuit breaker), not data exposure. In the current local setup it is not reachable from outside. |
| Preconditions | `LLM_ENABLED=true`, the API reachable by the attacker. |
| Impact | Up to roughly 170,000 LLM calls per day per IP address; exhaustion of the OpenAI budget or rate limit. |
| Evidence | `app/api/routes/answer.py` has no auth dependency and no route-specific limiter; `RateLimits` in `app/core/rate_limit.py` has none for it. |
| Recommended fix | Before exposure: either remove `/answer`, `semantic-search` and `hybrid-search` from the public surface (the chat endpoint covers the customer use) or give them a low dedicated per-IP limit; add a daily LLM call or cost budget that switches to approved-answer-only mode when reached; set a usage limit in the OpenAI project. |
| Remediation status | **Open.** |
| Verification test | To add with the fix: the (N+1)th `/answer` call within a minute from one IP returns `429 RATE_LIMITED`; with the budget exhausted, answers come back with `generation_method=approved_faq`. |

### SEC-08

**Staff authentication is a development prototype**

| Field | Value |
|---|---|
| Finding ID | SEC-08 |
| Affected component | `app/core/staff_auth.py`, `STAFF_TOKEN_HASHES`, `frontend/admin_app.py` |
| OWASP | API2 broken authentication; web A07 identification and authentication failures |
| Description | Staff use long-lived personal bearer tokens configured as hashes. What is done well: 256-bit random tokens, only SHA-256 hashes stored, constant-time comparison against every entry, conversation tokens never accepted, refusals audited, tokens never in URLs or logs, the dashboard keeps the token in server-side session state only. What is missing for public use: expiry, rotation, revocation without a restart, MFA, per-person login with a password manager or SSO, lockout or alerting on repeated failures, session timeout in the dashboard, and HTTPS (the app does not terminate TLS). |
| Severity | **Medium, deployment blocker.** Guessing is infeasible (2^256 tokens; the per-IP limit allows 120 tries a minute). The risk is a token that leaks (screenshot, shared chat, stolen laptop): it works indefinitely, from anywhere, with no second factor, until someone edits the configuration and restarts. Locally, behind `localhost`, the exposure is small. |
| Preconditions | The staff API is reachable from outside and a token leaks. |
| Impact | Read access to all tickets and contact details (SEC-06), FAQ edits that change customer answers (admin tokens), without detection of views (SEC-05). |
| Evidence | Module docstring of `staff_auth.py`; `PROTOTYPE_AUTH_NOTICE` in `app/models/admin_api.py`; `docs/admin-dashboard.md#security-limitations`. |
| Recommended fix | Before public deployment: put the staff dashboard and staff API behind an identity provider (OIDC/SSO with MFA) or at least a VPN or authenticating reverse proxy; issue short-lived tokens with server-side revocation; alert on repeated `auth.access_denied` events; serve only over HTTPS. Building a full identity provider was out of scope for this stage. |
| Remediation status | **Open; accepted for local and internal use only.** |
| Verification test | Existing: `test_staff_endpoints_refuse_missing_invalid_and_customer_credentials`, `test_support_staff_cannot_perform_admin_operations`, `test_staff_endpoints_are_switched_off_when_no_staff_are_configured`, `test_refused_staff_requests_are_audited_without_the_token`. With SSO: an expired or revoked token returns `401` without a restart. |

### SEC-09

**Rate limits are per process and per client IP**

| Field | Value |
|---|---|
| Finding ID | SEC-09 |
| Affected component | `app/core/rate_limit.py`, `RequestRateLimitMiddleware` |
| OWASP | API4 unrestricted resource consumption |
| Description | Limits are in-memory token buckets: they reset on restart, each worker counts separately (N workers allow N times the limit), and several servers do not share them. Behind a reverse proxy every client has the proxy's address unless uvicorn runs with `--proxy-headers` and `--forwarded-allow-ips`, in which case one abusive client can exhaust everyone's allowance, or none can be told apart. |
| Severity | **Medium, deployment blocker.** No effect locally; in a deployment it decides whether SEC-07 and brute-force limits mean anything. |
| Preconditions | Several workers, several servers, or a proxy without forwarded-header configuration. |
| Impact | Limits weaker than configured, or shared by all customers. |
| Evidence | Module docstring of `rate_limit.py`; README "Rate limits are process-local". |
| Recommended fix | Enforce limits at the gateway or reverse proxy, or in a shared store (e.g. Redis); configure trusted proxy headers explicitly. |
| Remediation status | **Open (architectural).** |
| Verification test | Existing limit tests in `tests/test_tracing_cors_limits.py`; a deployment test should send N+1 requests through the real proxy and expect `429`. |

### SEC-10

**Several API workers would serve a stale FAQ catalogue after an edit**

| Field | Value |
|---|---|
| Finding ID | SEC-10 |
| Affected component | FAQ catalogue snapshots and semantic index (`app/repositories/faq_repository.py`, `semantic_search_service.py`); SQLite |
| OWASP | Integrity; LLM09 misinformation |
| Description | Each API process keeps its own FAQ snapshot and semantic index. An edit or deactivation made through one worker is not seen by other workers (or while the import script runs alongside) until they restart. If an FAQ is deactivated because it is wrong, other workers keep answering with it. SQLite also allows one writer at a time and is unreliable on network file systems. |
| Severity | **Medium, deployment blocker.** No effect with the documented single worker. With several workers, customers can receive withdrawn answers for an unbounded time. |
| Preconditions | `uvicorn --workers N` with N > 1, or several servers. |
| Impact | Customers receive outdated or withdrawn policy answers. |
| Evidence | `docs/admin-dashboard.md` ("Single process only"); README "One machine only, one worker recommended". |
| Recommended fix | Run one API worker until fixed; then poll the catalogue revision in the database before each answer (it is already stored) and move to a database server (e.g. PostgreSQL) for several workers or servers. |
| Remediation status | **Open (architectural).** |
| Verification test | To add: two app instances on one database file; deactivate an FAQ through one; the other stops citing it on its next request. |

### SEC-11

**Personal data typed into chat is sent to OpenAI and stored as typed**

| Field | Value |
|---|---|
| Finding ID | SEC-11 |
| Affected component | Chat pipeline (`customer_chat_service.py`, `llm_service.py`, `query_rewrite_service.py`) |
| OWASP | LLM02 sensitive information disclosure |
| Description | Customers may type names, emails, phone or order numbers into chat. Messages are stored in the conversation (deleted with it or at expiry) and, when the LLM is enabled, the question and recent turns are sent to OpenAI. Controls in place: `store=False` (OpenAI does not keep the conversation for later retrieval), a non-identifying `safety_identifier` derived from the session, only customer text and approved FAQ text are sent (never contact form data, tokens or other customers' data). There is no redaction of personal data before the call. |
| Severity | **Low.** It is the expected data flow of an LLM chatbot, limited to what the customer chose to type, and it is not shared with other customers. It needs disclosure and a data processing agreement rather than a code fix. |
| Preconditions | `LLM_ENABLED=true` and the customer types personal data. |
| Impact | Personal data processed by a third-party processor under its retention terms (abuse monitoring). |
| Evidence | `llm_service.py` (`"store": False`, `safety_identifier`); `test_a_full_support_flow_logs_no_credentials_or_personal_data` confirms none of it reaches the logs. |
| Recommended fix | Tell customers on the page not to type sensitive data; consider masking email, phone and long digit runs before the LLM call (answers are grounded in FAQs, so masking costs little); have a DPA with the model provider; consider zero-data-retention where available. |
| Remediation status | **Open (documented).** |
| Verification test | With masking: a chat message containing an email results in an LLM request (fake client) with `[EMAIL]` in its input. |

### SEC-12

**Retention and anonymization run only on start-up or by hand**

| Field | Value |
|---|---|
| Finding ID | SEC-12 |
| Affected component | `scripts/cleanup_conversations.py`, `scripts/anonymize_support_data.py`, `CONVERSATION_CLEANUP_ON_STARTUP` |
| OWASP | Privacy (data minimisation) |
| Description | Expired conversations are refused at once (`410`) but stay on disk until the next start-up cleanup or a manual run. Closed tickets are anonymized after `SUPPORT_TICKET_RETENTION_DAYS` only when the script is run. There is no scheduler. |
| Severity | **Low.** Data is not reachable through the API after expiry; it lingers in the database file and backups. |
| Preconditions | A long-running server, or nobody running the script. |
| Impact | Personal data kept longer than the stated retention. |
| Evidence | README "There is no background worker or scheduler"; `docs/human-handoff.md` deletion path. |
| Recommended fix | Schedule both scripts (Task Scheduler or cron) and record each run; include `data/backups/` in the retention policy. |
| Remediation status | **Open.** |
| Verification test | Existing: `tests/test_conversation_retention.py`, `tests/test_support_privacy.py`. |

### SEC-13

**No HTTP security headers set by the application**

| Field | Value |
|---|---|
| Finding ID | SEC-13 |
| Affected component | FastAPI app, Streamlit pages |
| OWASP | API8 security misconfiguration |
| Description | The API sets `Cache-Control: no-store` on conversation, ticket and admin responses, but not `Strict-Transport-Security`, `X-Content-Type-Options` or `Content-Security-Policy`. Responses are JSON, CORS is an explicit allow list without credentials, and docs are off in production. |
| Severity | **Informational.** No exploitable path found for a JSON API; HSTS and frame protection belong to the HTTPS deployment. |
| Preconditions | Public deployment. |
| Impact | Missing browser-side hardening. |
| Evidence | No such headers in `app/`; `.streamlit/config.toml` binds to `localhost` with XSRF protection on. |
| Recommended fix | Set them at the reverse proxy that terminates TLS (HSTS, `nosniff`, `frame-ancestors 'none'` for the staff dashboard). |
| Remediation status | **Open (deployment).** |
| Verification test | Deployment check: `curl -I https://<host>/api/v1/health` shows the headers. |

### SEC-14

**Prompt injection: layered controls in place, residual risk remains**

| Field | Value |
|---|---|
| Finding ID | SEC-14 |
| Affected component | `app/prompts/*.py`, `context_builder.py`, `customer_chat_service.py` |
| OWASP | LLM01 prompt injection; LLM06 excessive agency |
| Description | Reviewed controls: static system instructions sent in the separate `instructions` field and never templated; customer text and FAQ evidence escaped inside labelled data blocks; the model is called only when approved evidence exists; replies are forced into a strict JSON schema and validated (citations must be FAQs actually supplied, bounded length, internal markers rejected); the model has no tools and cannot act (no agency). Query rewrites are used only for retrieval and are validated. A successful injection can at most change the wording of one reply, which then still passes the grounding checks or is replaced by the approved FAQ text. |
| Severity | **Informational.** No bypass found; no prompt is a complete defence. |
| Preconditions | A crafted customer message, or a maliciously edited FAQ (needs an admin token). |
| Impact | A misleading reply in the attacker's own conversation. |
| Evidence | Prompt module docstrings; existing tests `tests/test_customer_support_prompt.py`, `tests/test_grounding_validator.py`, `tests/test_query_rewrite_service.py`. |
| Recommended fix | Keep the model tool-less; re-run the evaluation sets after any prompt change; monitor `event=api_error` and grounding-failure rates. |
| Remediation status | **Accepted.** |
| Verification test | Existing prompt and grounding tests (see Evidence). |

### SEC-15

**AI-written answers are checked by heuristics, not verified**

| Field | Value |
|---|---|
| Finding ID | SEC-15 |
| Affected component | `app/services/grounding_validator.py`, frontends |
| OWASP | LLM09 misinformation (excessive trust in AI output) |
| Description | Generated answers are checked for changed numbers, dates, fees, promises, dropped restrictions and claimed actions; any failure falls back to the approved FAQ text. The validator documents its limits: a paraphrase that reverses a condition without numbers or listed words passes. The customer page labels AI-written replies ("AI-written summary of our help articles") and lists sources; the LLM's output is rendered escaped (`plain_markdown`), never as HTML. |
| Severity | **Informational.** |
| Preconditions | LLM enabled. |
| Impact | An occasional wrong paraphrase shown to a customer. |
| Evidence | Validator docstring "Known limitations"; `frontend/formatting.py`. |
| Recommended fix | For policy-critical categories (refunds, legal), serve approved answers verbatim; keep labelling; sample-review AI replies. |
| Remediation status | **Accepted.** |
| Verification test | Existing `tests/test_grounding_validator.py`, `data/grounded_answer_eval.json`. |

### SEC-16

**Dependencies: no known vulnerabilities; transitive versions not locked**

| Field | Value |
|---|---|
| Finding ID | SEC-16 |
| Affected component | `requirements.txt` |
| OWASP | Web A06 vulnerable and outdated components |
| Description | `pip-audit` on 2026-10-18 found **no known vulnerabilities** in the 90 resolved packages. Direct dependencies are pinned exactly, but transitive ones are not pinned or hash-checked, so a later install can pull different versions. |
| Severity | **Informational.** |
| Preconditions | A future install. |
| Impact | Unreviewed transitive updates. |
| Evidence | `pip-audit -r requirements.txt` output: "No known vulnerabilities found". The result is only as current as the advisory database on that date. |
| Recommended fix | Generate a fully pinned lock file with hashes (`pip-compile --generate-hashes`), run `pip-audit` in CI and on a schedule. |
| Remediation status | **Open (process).** |
| Verification test | `pip-audit -r requirements.txt` in CI exits 0. |

### SEC-17

**Static-analysis warnings triaged as false positives**

| Field | Value |
|---|---|
| Finding ID | SEC-17 |
| Affected component | Various |
| Description | `bandit` reported 11 issues (8 low, 3 medium, all low or medium confidence). B105 "hardcoded password" (5): the public token prefixes `cst_`, `stf_`, a session-state key name and OpenAPI examples. B608 "SQL injection" (3): `connection.py` table rebuild and `analytics_repository.py` duration queries build SQL only from the code's own table, column and constant SQL strings; values are bound parameters. B101 asserts (2): type narrowing, not security checks. B311 `random` (1): retry jitter, not cryptography. |
| Severity | **Informational.** |
| Remediation status | **Closed.** One residual note: the table rebuild interpolates index names read from the database file's own schema; exploiting it requires write access to the database file, which already means full compromise. |
| Verification test | Re-run `bandit -r app frontend scripts`; the same 11 items. |

---

## 5. Authentication and authorization review (Task 3)

### Customer sessions

| Check | Result | Test (`tests/test_security_audit.py`) |
|---|---|---|
| Missing session token | `401 UNAUTHORIZED_SESSION` with `WWW-Authenticate: Bearer` on every conversation and ticket endpoint, before any lookup | `test_a_missing_session_token_is_401_everywhere` |
| Invalid token (garbage, never issued, staff token, SQL text) | `404 SESSION_NOT_FOUND`, byte-for-byte the same as an unknown session | `test_an_invalid_session_token_looks_exactly_like_an_unknown_session` |
| Expired session | `410 SESSION_EXPIRED` only with the right token; a wrong token gets `404`, so expiry does not reveal existence | `test_an_expired_session_is_410_only_for_its_own_token` |
| Closed session | `409 SESSION_CLOSED` on chat; wrong token gets `404` | `test_a_closed_session_accepts_no_messages` |
| Deleted session, reuse of its token | Every endpoint `404`; messages are gone from the database | `test_a_deleted_session_and_its_token_are_gone_for_good` |
| Another customer's conversation | `404` on read, chat, close and delete; the victim's data is unchanged | `test_one_customer_cannot_use_another_customers_conversation` |
| Another customer's ticket | `404 TICKET_NOT_FOUND`, identical to a non-existent ticket; filing on another's conversation refused; customers never see contact details | `test_one_customer_cannot_read_or_file_another_customers_ticket` |
| Session ID guessing | IDs are UUIDv4; tokens 256-bit random; 25 random guesses all identical `404` | `test_session_ids_and_tokens_cannot_be_guessed` |
| Credentials in URLs | `400` on every authenticated route, even with a valid header (SEC-02) | `test_credentials_in_the_url_are_refused_even_with_a_valid_header` |

### Staff authorization

| Check | Result | Test |
|---|---|---|
| Missing staff credentials | `401 STAFF_AUTH_REQUIRED` on all 21 staff operations | `test_staff_endpoints_refuse_missing_invalid_and_customer_credentials` |
| Invalid staff credentials (garbage, well-formed unknown) | `401`, nothing changed | same |
| Customer token on admin endpoints | `401`; on the shared ticket read, treated as a customer and refused without revealing the ticket | same |
| Support role on admin-only operations (13 operations incl. FAQ editing and analytics) | `403 STAFF_FORBIDDEN`; the missing permission is logged, not returned | `test_support_staff_cannot_perform_admin_operations` |
| Unauthorized FAQ editing, ticket listing, status updates, analytics | Refused as above; FAQ version, ticket status, assignee and notes verified unchanged | `assert_nothing_changed` in the tests above |
| No staff configured | `403 STAFF_ACCESS_DISABLED` everywhere, even for a token that would be valid elsewhere | `test_staff_endpoints_are_switched_off_when_no_staff_are_configured` |
| Existence of private resources | Unauthenticated requests get the same `401` for real and unknown tickets | `test_unauthenticated_staff_requests_do_not_reveal_whether_a_ticket_exists` |
| Hidden frontend controls | The dashboard hides admin sections from support staff, but the API is the control: a hand-made request is refused | `test_the_server_enforces_roles_whatever_the_dashboard_shows` |
| Refusals recorded | `auth.access_denied` events with the staff ID (or "unauthenticated"), never the token | `test_refused_staff_requests_are_audited_without_the_token` |

### Token generation, storage, expiry and comparison

| Topic | Conversation tokens (`cst_`) | Staff tokens (`stf_`) |
|---|---|---|
| Generation | `secrets.token_urlsafe(32)`: 256 bits from the OS CSPRNG | same |
| Storage | SHA-256 hash in `conversation_sessions`; the token is returned once at creation | SHA-256 hash in `STAFF_TOKEN_HASHES` (environment); shown once by `scripts/create_staff_token.py` |
| Hash choice | Unsalted SHA-256 is adequate for long random tokens (no dictionary to try); passwords would need a slow salted hash | same |
| Comparison | `hmac.compare_digest`, with a dummy hash when the session is missing so timing does not reveal existence | `hmac.compare_digest` against **every** entry, always |
| Format check | Strict regex before lookup; malformed tokens do the same work and fail | same; conversation tokens can never match |
| Expiry | `CONVERSATION_TTL_MINUTES` (default 60) after creation, not extended by activity; `410` afterwards | **none** (SEC-08) |
| Invalidation | Close (no new messages) and delete (token useless) | Remove the entry and restart (SEC-08) |
| Transport | `Authorization: Bearer` only; never in URLs, never in cookies (no CSRF surface); CORS without credentials | same; the dashboard's password field keeps it in server-side session state |
| Brute force | 256-bit space; per-IP request limit; per-conversation chat limit keyed by token hash | 256-bit space; per-IP request limit; every refusal audited. No lockout or alerting (SEC-08) |
| Logging | Never logged; redacted if it ever is | Never logged; redaction fixed (SEC-03) |

### Missing controls before public deployment (staff access)

Staff authentication remains development-only. Before public exposure, at minimum:

1. **Identity**: SSO/OIDC (or an authenticating reverse proxy or VPN) with per-person
   accounts and **MFA**, instead of static bearer tokens.
2. **Session lifetime**: short-lived tokens or sessions with idle timeout, and
   server-side revocation without a restart.
3. **Detection**: alerts on bursts of `auth.access_denied`; audit of contact-detail views
   (SEC-05).
4. **Transport**: HTTPS only, HSTS, the dashboard not reachable from the public internet.
5. **Least privilege**: contact details only for the assignee (SEC-06).
6. **Shared rate limiting** at the gateway (SEC-09).

---

## 6. API input validation review (Task 4)

### Endpoint inventory

| Endpoint | Auth | Body / parameters validated by |
|---|---|---|
| `GET /health`, `GET /ready` | none | none (fixed responses; readiness shows component states only) |
| `GET /faqs`, `/faqs/categories`, `/faqs/search`, `/faqs/{faq_id}` | none | `Query` bounds (`offset ≥ 0`, `1 ≤ limit ≤ max`), search text 1 to 200 chars after stripping, `faq_id` ≤ 64 |
| `POST /semantic-search`, `/hybrid-search`, `/answer` | none | Pydantic models, `extra="forbid"`, strict ints, bounded query |
| `POST /conversations` | none (per-IP creation limit) | no body |
| `GET/DELETE /conversations/{id}`, `GET .../messages`, `POST .../chat`, `POST .../close` | conversation token | UUID path (malformed = `404`), bounded pagination, `CustomerChatRequest` (`extra="forbid"`, trimmed, non-blank, no control characters, length limit, UUID `client_message_id`) |
| `POST /support/tickets` | conversation token | `SupportTicketCreateRequest` (`extra="forbid"`, canonical UUIDs, single-line name and subject, bounded description, `EmailStr`, phone format and digit count, strict `true` consent and confirmation) |
| `GET /support/tickets/{id}` | conversation or staff token | UUID path (malformed = `404`), `session_id` ≤ 64 |
| Staff ticket endpoints | staff token | Enums for status, type, priority; offset-aware timestamps; staff ID pattern; reference ≤ 20; bounded pagination; workflow transitions |
| `/admin/*` | staff token (admin for all but `/me`, `/staff`) | FAQ models (`extra="forbid"`, length limits, `expected_version ≥ 1`), dates, bounded periods (≤ 366 days, not in the future), bounded pagination |

### Results

| Case | Behaviour | Test |
|---|---|---|
| Malformed JSON, invalid UTF-8, `null` body | `400`/`422 VALIDATION_ERROR`, no parser messages or tracebacks | `test_malformed_json_is_refused_without_internal_details` |
| Missing required fields, wrong types | `422` with `details[].loc` and rule type | `test_chat_input_is_validated` |
| Empty, whitespace-only, invisible-only strings | `422 blank_message` / `blank_text` | `test_chat_input_is_validated`, `test_contact_details_are_validated_without_echoing_them` |
| Oversized input | `422 string_too_long` within the body limit; `413 PAYLOAD_TOO_LARGE` beyond `MAX_REQUEST_BODY_BYTES` before parsing | `test_oversized_bodies_are_refused_before_parsing` |
| Unexpected fields (`role`, `status`, `priority`, `assigned_to`, `fax`) | `422 extra_forbidden`: customers cannot set server-owned fields | `test_chat_input_is_validated`, `test_ticket_requests_are_validated` |
| Invalid UUIDs in paths | `404` (same as unknown), never `500` | `test_invalid_ids_in_paths_are_simply_not_found` |
| Invalid pagination (negative, zero, huge, non-integer) | `422` on all six paginated endpoints | `test_pagination_is_bounded_everywhere` |
| Invalid timestamps (no offset, impossible dates, words), reversed or over-long periods, future end | `422` | `test_invalid_timestamps_and_periods_are_refused` |
| Invalid ticket status transitions | `409 INVALID_STATUS_TRANSITION`; unknown status or assignee mismatch `422` | `test_invalid_ticket_status_changes_are_refused` |
| Invalid FAQ versions (0, negative, text, null, float, list, missing) | `422`; stale version `409 FAQ_VERSION_CONFLICT`, never an overwrite | `test_invalid_faq_versions_are_refused`, `test_a_stale_faq_version_is_a_conflict_not_an_overwrite` |
| Invalid contact details | `422`; the submitted values are **not echoed** in the response | `test_contact_details_are_validated_without_echoing_them` |
| Legitimate Unicode (accents, CJK, Arabic, emoji, combining marks) | Accepted and returned byte-for-byte | `test_unicode_is_preserved_exactly` |
| Internal exception | `500 INTERNAL_ERROR` with a generic message; exception text logged without its message | `test_customer_facing_errors_carry_no_diagnostics` |

No new validation defect was found in this stage; the only input-handling fix was SEC-02.

---

## 7. SQL injection and database review (Task 5)

| Check | Result | Evidence or test |
|---|---|---|
| Parameterized queries | All data access goes through SQLAlchemy expressions with bound parameters. The three f-string SQL sites combine only module constants and table or column names from the code (SEC-17). | Code review; bandit B608 triage |
| LIKE search | User text is escaped for `%`, `_` and the escape character before `LIKE ... ESCAPE` | `test_like_wildcards_in_admin_search_match_only_themselves` |
| Injection payloads | 10 classic payloads through public search, admin FAQ search, ticket filters, audit filters and path IDs: normal `200`/`404`/`422`, zero matches, every table's row count unchanged | `test_injection_payloads_are_data_not_sql` |
| Payloads as data | Stored and returned literally in chat history and ticket text | `test_payloads_are_stored_and_returned_literally` |
| Foreign keys | `PRAGMA foreign_keys=ON` on every connection; an orphan row is refused | `test_foreign_keys_and_secure_delete_are_on_for_every_connection` |
| Secure delete | `PRAGMA secure_delete=ON`: deleted content is overwritten in the file | same |
| Transaction rollback | A failure mid-transaction leaves no partial change | `test_a_failed_transaction_leaves_no_partial_changes` |
| Constraints | CHECK constraints refuse invalid statuses, types and categories even for direct SQL | `test_database_constraints_refuse_invalid_rows` |
| Audit log integrity | Triggers make `admin_audit_events` append-only at the database level | `test_the_audit_log_is_append_only_in_the_database` |
| Duplicate requests | Same `client_request_id`: one ticket, replayed with `Idempotent-Replayed: true`; reuse with different content `422` | `test_a_repeated_ticket_request_creates_one_ticket` |
| Concurrent updates | Optimistic checks on ticket assignment and FAQ versions: the second writer gets `409`, nothing is overwritten | `test_concurrent_staff_updates_do_not_overwrite_each_other` |
| Customer data isolation | Every customer query is scoped by session ID **and** verified token | Section 5 tests |
| Staff authorization before data access | Dependencies authenticate and check the role before the handler runs; services check again | Section 5 tests |
| Migration safety | `init_schema` only adds nullable columns or rebuilds listed tables inside one transaction with a foreign-key check, rolling back on any problem. The audit table is not rebuildable, which constrains SEC-05. | `app/database/connection.py`; existing `tests/test_ticket_repository.py`, `tests/test_faq_repository.py` |
| Test databases | Every test uses a temporary SQLite file or `sqlite://` in memory; `tests/conftest.py` forces `DATABASE_URL=sqlite://` before import | `tests/conftest.py` |

### SQLite limits that need architectural change for deployment

- **One writer at a time.** `BEGIN IMMEDIATE` plus a 5-second busy timeout serialises
  writes; under load, writers get `503` with `Retry-After`. Fine for a demo, not for
  real traffic.
- **One machine.** Several servers cannot share the file; SQLite locking is unreliable
  on network file systems.
- **Several workers on one machine.** Conversation leases and idempotency keys live in
  the database and are designed to hold across processes (tested with two engines, not
  under real `uvicorn --workers` load). FAQ snapshots, the semantic index, rate limits
  and the LLM circuit breaker are per process (SEC-09, SEC-10).
- **No encryption at rest.** The database file and `data/backups/` hold customer contact
  details in clear; protect them with disk encryption and file permissions.

**Recommendation:** before deployment, move to a database server (for example
PostgreSQL; only the repositories talk to the database) and shared state for limits and
catalogue revision.

---

## 8. Secrets and configuration review (Task 6)

| Item | Finding | Test |
|---|---|---|
| `.env` | Present locally, **ignored and never tracked**. Holds an OpenAI key and a valid staff entry; values were not read into this report. | `test_no_secret_files_or_databases_are_tracked` |
| `.env.example` | Placeholders only after SEC-04: `OPENAI_API_KEY=` empty, `STAFF_TOKEN_HASHES=[]` | `test_env_example_holds_placeholders_only` |
| `.gitignore` | Covers `.env`, `.env.*` (except the example), `.streamlit/secrets.toml`, `*.pem`, `*.key`, `secrets/`, `*.db`, `*.sqlite*`, WAL and journal files, logs | `test_local_secret_files_and_databases_are_ignored` |
| Streamlit secrets | No `secrets.toml` exists or is used; it is ignored. `config.toml`: `localhost` binding, XSRF protection, error details hidden, usage stats off | Review |
| OpenAI configuration | Key held as `SecretStr` (masked in `repr`, `str` and logs); settings errors name variables, never values; startup summary logs only whether a key is set | `test_the_api_key_never_appears_in_settings_output` |
| Staff credentials | Only hashes are configured; validation errors never echo an entry | `tests/test_staff_roles.py` |
| Session token handling | Section 5 | Section 5 |
| Database configuration | SQLite only; any password in a URL is hidden in the startup summary | `Settings.safe_summary` |
| Logs | Path only (no query strings, headers or bodies); exception messages withheld; redaction of tokens, keys and emails | Section 9 |
| Git-tracked files | No live credential patterns; test fixtures use obviously fake keys (`sk-test-...`, short dummies). Full history scanned: only those fixtures. | `test_tracked_files_contain_no_live_credentials` |
| Secrets in responses | No endpoint returns tokens (except the one-time `access_token` at creation), keys or hashes; errors never echo the Authorization header | `test_responses_never_echo_credentials` |
| Local databases | `data/conversations.db` and `data/backups/*.db` are ignored and not tracked | `git check-ignore` during the audit |

**No real credential was found in tracked files or git history**, so no rotation is
recommended. Git history was not rewritten.

---

## 9. Logging and privacy review (Task 7)

### Where personal data goes

| Data | Stored | Logged | Sent to OpenAI | Shown to | Retention and deletion |
|---|---|---|---|---|---|
| Chat messages (may contain anything the customer types) | `conversation_messages` | No (IDs and counts only) | Yes, when LLM enabled: question and recent turns (`store=False`) | The conversation's token holder | Deleted with the conversation (customer `DELETE`) or after expiry by cleanup (SEC-12) |
| Session ID | `conversation_sessions` | Yes, on creation (a random ID, not personal by itself) | Only as `safety_identifier`, a SHA-256 derivative, never the ID | Token holder | With the conversation |
| Access tokens | Hash only | Never; redacted | Never | Returned once | With the conversation |
| Customer name, email, phone | `customer_contacts`, separate from chat | Never (emails redacted as a fallback) | Never | Staff with `view_customer_contacts` (SEC-05, SEC-06) | Anonymized after `SUPPORT_TICKET_RETENTION_DAYS` or on request, by script |
| Ticket subject and description | `support_tickets` | Never | Never | Customer (subject only) and staff | Anonymized with the contact data |
| Staff notes | `ticket_notes` | Never | Never | Staff only | Text removed on anonymization |
| Staff tokens | Hash in configuration | Never; redacted (SEC-03) | Never | Shown once to the owner | Removed from configuration |
| LLM requests and responses | Final reply stored as the assistant message | Model, latency, token counts, request ID only | n/a | Customer | With the conversation |
| Audit events | `admin_audit_events` | n/a | Never | Admins | Append-only, kept indefinitely (no personal data by design) |

### Checks

| Requirement | Result | Test |
|---|---|---|
| No raw personal data in logs | A full flow with a synthetic name, email, phone and order number in chat and ticket, staff read, refused read, validation error and token-in-URL request: none of it, and no token, appears in the logs | `test_a_full_support_flow_logs_no_credentials_or_personal_data` |
| No keys or tokens in logs | Middleware logs path only; formatter redacts bearer tokens, `cst_`/`stf_`, `sk-` keys, `password=`-style pairs and emails | same; `test_staff_tokens_are_redacted_from_log_text_even_without_bearer`; `tests/test_logging.py` |
| Sensitive headers | Never logged (no header logging at all; `uvicorn.access`, `httpx` and `openai` loggers quietened) | `app/core/logging_config.py` |
| Customer-facing errors | Fixed messages, request ID only; tracebacks keep frames but withhold exception messages unless `DEBUG` (refused in production) | `test_customer_facing_errors_carry_no_diagnostics` |
| Cross-session isolation | Section 5 | Section 5 |
| Conversation deletion | Customer can delete; messages removed; tickets deliberately kept (they are a separate request with consent) | `test_a_deleted_session_and_its_token_are_gone_for_good` |
| Staff access to personal data | Role-based, not audited on read (SEC-05, SEC-06) | Section 4 |
| External evaluation services | No customer data is sent to evaluation services; evaluations use fictional FAQs and synthetic journeys | `data/*_eval.json` |
| Test data | Synthetic names, `example.com` emails, a fictional-range phone number | This test file |

### Privacy limitations (stated plainly)

- Customer-typed personal data reaches OpenAI when the LLM is on (SEC-11).
- Nobody can see who viewed a customer's contact details (SEC-05).
- Retention depends on someone running the scripts (SEC-12).
- The database and backups are not encrypted by the application.
- There is no self-service data export or verified erasure workflow; erasure is a
  staff-run script.
- No regulatory compliance (GDPR or otherwise) is claimed.

---

## 10. OWASP coverage map

| Risk | Where examined | Findings |
|---|---|---|
| API1 Broken object-level authorization | §5 customer sessions, tickets | SEC-06 (by design) |
| API2 Broken authentication | §5 tokens | SEC-02 (fixed), SEC-08 |
| API3 Broken object property level authorization (excessive data exposure, mass assignment) | §6 `extra="forbid"`, customer vs staff views | None found |
| API4 Unrestricted resource consumption | Limits, body size, pagination | SEC-07, SEC-09 |
| API5 Broken function-level authorization | §5 staff matrix | None found |
| API6 Unrestricted access to sensitive business flows | Ticket creation limits, idempotency | Covered by SEC-09 in deployment |
| API7 Server-side request forgery | No user-supplied URLs are fetched | None found |
| API8 Security misconfiguration | §8, CORS, docs, debug | SEC-03, SEC-04 (fixed), SEC-13 |
| API9 Improper inventory management | §6 endpoint inventory; legacy aliases `/admin/overview`, `/admin/analytics` hidden from the schema but protected identically | None found |
| API10 Unsafe consumption of APIs | OpenAI replies parsed into a strict schema and validated | SEC-14, SEC-15 |
| Web A03 Injection | §7 SQL; Markdown in UI | SEC-01 (fixed) |
| Web A06 Vulnerable components | §1 tools | SEC-16 |
| Web A09 Logging and monitoring failures | §9 | SEC-03 (fixed), SEC-05 |
| LLM01 Prompt injection | §4 | SEC-14 |
| LLM02 Sensitive information disclosure | §9 | SEC-11 |
| LLM05 Improper output handling | Frontend rendering | SEC-01 (fixed), SEC-15 |
| LLM06 Excessive agency | Model has no tools or actions | None found |
| LLM09 Misinformation | Grounding validator, stale catalogue | SEC-10, SEC-15 |
| LLM10 Unbounded consumption | `/answer`, token caps | SEC-07 |

---

## 11. Re-running the verification

```powershell
# The Stage 12 verification suite (offline, temporary databases, synthetic data)
python -m pytest tests/test_security_audit.py -q
python -m pytest tests/test_admin_frontend.py -q -k "shown_as_typed or history"

# Whole suite
python -m pytest -q

# Dependency and static checks (in a separate virtual environment)
pip install pip-audit bandit
pip-audit -r requirements.txt
bandit -r app frontend scripts -q
```

Recorded on 2026-10-18: `tests/test_security_audit.py` 103 passed;
`tests/test_admin_frontend.py` 42 passed; pip-audit "No known vulnerabilities found";
bandit 11 issues, all triaged in SEC-17. Each Stage 12 regression test was also run
against the code without its fix and failed, as expected.

---

## 12. Limits of this audit

- Reviewed by reading code and running tests in-process. No deployed instance, proxy,
  TLS, operating system or hosting configuration was tested.
- No fuzzing beyond the payload lists in the tests; no load or denial-of-service testing.
- Streamlit's and FastAPI's own security were not audited; their versions were checked
  against known advisories only.
- The OpenAI provider's handling of data is governed by its terms, not by this code.
- A clean result for a check means no problem was found with the method described, not
  that none exists.
