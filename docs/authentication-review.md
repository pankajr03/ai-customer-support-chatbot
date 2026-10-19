# Authentication review for the containerized deployment (Stage 13)

A reassessment of customer and staff authentication as deployed with Docker Compose
behind Nginx ([docs/deployment-architecture.md](deployment-architecture.md)). It builds
on the Stage 12 audit ([docs/security-audit.md](security-audit.md), section 5) and
records what was checked again, what changed, and what still blocks deployment.

**Date:** 2026-10-19. **Evidence:** tests run in-process, and checks on the running
Docker stack (WSL 2) with development-only certificates and real-looking hostnames
(`chat.example.test`, `admin.example.test`). The hostnames matter: Streamlit trusts
`localhost` specially, which hid a defect until real names were used (section 7).

> **Verdict.** Customer authentication is **ready with limitations** for this
> deployment. Staff authentication **remains a development-grade prototype** (static
> tokens: no expiry, no MFA, revocation only by restart). As this task requires, the
> staff dashboard is therefore **not exposed publicly**. Two independent controls
> enforce that: a loopback bind address, and a private-network allow-list. The
> improvements in [section 11](#11-deployment-blockers) are **deployment blockers**.
> No production identity system is proposed or simulated here.

## Contents

1. [Authentication architecture as deployed](#1-authentication-architecture-as-deployed)
2. [Staff endpoints and public access](#2-staff-endpoints-and-public-access)
3. [Customer session tokens](#3-customer-session-tokens)
4. [Staff authentication](#4-staff-authentication)
5. [Authorization on every protected endpoint](#5-authorization-on-every-protected-endpoint)
6. [Credentials in the browser and in URLs](#6-credentials-in-the-browser-and-in-urls)
7. [Streamlit session state and WebSockets](#7-streamlit-session-state-and-websockets)
8. [CSRF](#8-csrf)
9. [Transport](#9-transport)
10. [Changes made in this review](#10-changes-made-in-this-review)
11. [Deployment blockers](#11-deployment-blockers)

---

## 1. Authentication architecture as deployed

| Who | Credential | Presented as | Checked by | Expires | Revoked by |
|---|---|---|---|---|---|
| Customer (customer page, or an API client) | Conversation token `cst_…`, 256-bit random, one per conversation, returned once | `Authorization: Bearer` | Backend: SHA-256 hash in the database, constant-time comparison | `CONVERSATION_TTL_MINUTES` after creation (default 60), not extended by activity | Customer closes (no new messages) or deletes (token useless) the conversation |
| Staff, dashboard layer 1 | Network position: SSH tunnel or VPN | n/a | Nginx bind address and allow-list | n/a | n/a |
| Staff, dashboard layer 2 | Basic-auth user and password (`deploy/nginx/admin.htpasswd`) | Browser credential cache | Nginx | When the browser forgets it (usually on close) | Remove the line from the file |
| Staff, application | Staff token `stf_…`, 256-bit random, per person, with a role (admin, support) | Typed into the dashboard, then `Authorization: Bearer` from the dashboard container to the backend | Backend: SHA-256 hashes in `STAFF_TOKEN_HASHES`, constant-time comparison against every entry | **Never** (the dashboard forgets it after 30 minutes idle or 8 hours, but the token stays valid) | **Remove the entry and restart the backend** |

The browser talks only to Nginx. The pages talk to the backend over private Docker
networks. The staff API is reachable only from the dashboard container.

## 2. Staff endpoints and public access

| Surface | Public internet | Evidence |
|---|---|---|
| Staff API (`/api/v1/admin/*`, ticket list, status, assignment, notes, events) | **Not routed**: Nginx `404` | Allow-list test over every route; Docker: `404` on all of them |
| Staff token presented on the public site | **Refused before routing** (`403 STAFF_FORBIDDEN`) | Docker: `403` |
| `/api/…` on the dashboard listener | Not routed (`404`) | Docker: `404` |
| API documentation (`/docs`, `/redoc`, `/openapi.json`) | Off in production, and not routed in any environment | `test_docs_are_disabled_in_production` |
| Staff dashboard | **Not public**: published on `127.0.0.1` by default; now also refused to any non-private source address | Section 10; Docker: allowed from the server, `403` when the source is not on the list |
| Development override (`compose.dev.yaml`) | Backend, docs and pages on `127.0.0.1` only | Development only; the backend port is unreachable on the network address (verified in Task 6) |

Residual inside the server: the **customer** page container can open TCP connections to
the backend's staff API (they share the customer network). It holds no staff token, and
tokens are 256-bit random, so this is not exploitable by guessing. But a compromised
customer container is one step closer to staff functions than it needs to be.
Restricting staff routes to the admin network is recommended (blocker B6).

## 3. Customer session tokens

All checked through Nginx on Docker (TTL set to 1 minute for the expiry check):

| Check | Result |
|---|---|
| Issued | `cst_` + 43 characters, returned once by `POST /api/v1/conversations` |
| Used correctly | `200` |
| Missing | `401 UNAUTHORIZED_SESSION` with `WWW-Authenticate: Bearer` |
| Another (random) token | `404`, the same as an unknown conversation |
| In the URL (`?access_token=`), even with a valid header | `400 VALIDATION_ERROR` ("Send the access token…") |
| Closed, then a chat | `409 SESSION_CLOSED` |
| Deleted, then a read | `404 SESSION_NOT_FOUND` |
| Expired, own token | `410 SESSION_EXPIRED` |
| Expired, wrong token | `404 SESSION_NOT_FOUND`: expiry does not reveal that a conversation exists |

Storage: only the SHA-256 hash is stored. Unsalted SHA-256 is adequate for 256-bit
random tokens, which have no dictionary to attack. Logs never contain tokens (redacted
as a safety net). Rate limits: per conversation for chat, per client address for new
conversations (real client addresses for API calls through the trusted proxy).

**Limitations (accepted):** a token is a bearer credential: whoever holds it can use
the conversation until it expires or is deleted. The customer page keeps it in server
memory (section 7); an API client must keep it safe itself, and should not put it in
`localStorage` ([docs/chat-api.md](chat-api.md)).

## 4. Staff authentication

| Requirement | Status |
|---|---|
| Strong secret | Yes: 256-bit random, shown once, stored only as a hash |
| Per person | Yes: one entry per staff ID, with a role |
| Roles enforced server-side | Yes: `support` gets `403` on all 13 admin-only operations (Stage 12 tests) |
| **Expiry** | **No.** The token is valid until removed. The dashboard's 30-minute idle and 8-hour limits only make the page forget it |
| **Revocation** | **Restart only.** Verified on Docker: after the entry was removed from `backend.env`, the token was still accepted (`200`) until the backend was recreated (then refused) |
| **MFA** | **No** |
| Sign-in auditing | Partial: refused staff requests and contact-detail views are in the audit log; successful sign-ins are not recorded as such; no alerting on bursts of failures |
| Lockout or throttling of guesses | Per-address request limits only; guessing a 256-bit token is not feasible, but there is no lockout or alert |
| Shared secrets | The basic-auth file can hold one shared user. **Give each person their own line**; it has no expiry or logout either |

**Conclusion:** this is not a production identity system, and adding expiry here would
not make it one. It is acceptable only for a small, trusted team reaching a dashboard
that is **not** on the public internet. Section 11 lists what must replace it.

## 5. Authorization on every protected endpoint

New: [tests/test_authorization_coverage.py](../tests/test_authorization_coverage.py)
enumerates **every operation in the API schema**, plus the two hidden legacy admin
routes, so an operation added later is covered without anyone remembering to add it.

| Check | Result |
|---|---|
| Without credentials | Every operation not on the explicit public list answers `401` (`UNAUTHORIZED_SESSION` or `STAFF_AUTH_REQUIRED`) with `WWW-Authenticate: Bearer`: 28 protected operations, 11 public, 39 in total |
| Public list | `GET /`, health, readiness, the four FAQ reads, the three token-free search and answer endpoints, `POST /conversations`. Each is deliberate; a new public operation fails the test until added on purpose |
| Invalid credentials (garbage token, a well-formed unknown staff token, a basic-auth header) | Every protected operation refuses (`401`, `403` or `404`); no customer data in any response |
| Mutation check | Treating `POST /conversations` as protected made the test fail (`201` reported as unprotected) |

Already covered by the Stage 12 suite and unchanged: customer isolation (another
customer's conversation or ticket is `404`); roles (support staff `403` on admin
operations); refusals audited without the token.

Observed nuance: with a garbage token **and an invalid body**, chat and ticket creation
answer `422` (body validation runs before the token is verified). That reveals only the
public request schema, never data. With a valid body the answer is `404`.

## 6. Credentials in the browser and in URLs

| Credential | Reaches the browser? |
|---|---|
| Customer conversation token (customer page) | **No.** The page receives it server-side from the backend and keeps it in Streamlit session state as a `SecretStr`. It is never rendered or sent to the browser |
| Customer conversation token (API client) | Yes, by design: the client is the token holder |
| Staff token | **Only while typed.** It goes into a password field in a form with `clear_on_submit=True`, travels once to the dashboard server over the encrypted WebSocket, and is then held server-side as a `SecretStr`. It is never rendered back |
| Basic-auth password | Cached by the browser for the session (inherent to basic authentication) |
| Any credential in a URL | **Never.** The API refuses tokens in query strings (`400`, verified through Nginx). The pages use no query parameters. Nginx logs paths without query strings. The staff API is not reachable from a browser at all |

## 7. Streamlit session state and WebSockets

Streamlit keeps each browser tab's state in the page server's memory, tied to that
tab's WebSocket. Consequences:

| Property | Consequence |
|---|---|
| In memory only | Restarting a page container signs every staff member out and loses customers' conversation handles (their conversations remain in the database) |
| Per tab | A second tab is a separate session, needing its own sign-in |
| Not shared between processes | More than one page container needs sticky sessions; this deployment runs one of each |
| Server-side | Credentials in session state are not readable by the browser (section 6) |
| Expiry is page-side | The dashboard checks idle and maximum time on each interaction and then forgets the token; the backend token itself never expires |
| No server-side revocation | An open dashboard session cannot be ended remotely, short of restarting the page container (or expiry at the next interaction) |

**WebSocket origin check, and a defect found in this review.** Streamlit accepts a
WebSocket only if its `Origin` matches the `Host` header (or names `localhost`). Nginx
forwarded `Host` without the port. With real hostnames on a non-default port, as the
dashboard always is (`:8443`), every WebSocket was refused. The page would load but
never work. Verified on Docker before the fix: `403` for same-origin connections on
both sites. Streamlit's log read
`origin=https://chat.example.test:18443, host=chat.example.test`. Earlier checks passed
only because they used `localhost`. **Fixed:** Nginx now forwards `$http_host`
(section 10). Same-origin `101`; foreign origins still `403`.

## 8. CSRF

| Path | Credential sent automatically by the browser? | CSRF exposure |
|---|---|---|
| Customer API (bearer header) | No: a page cannot make the browser add `Authorization: Bearer` to another site | **None.** CORS allows no other origins (`CORS_ORIGINS=[]`) |
| Customer page | A `_streamlit_xsrf` cookie, and no login | Streamlit's double-submit XSRF token protects its HTTP endpoints; the WebSocket checks `Origin` (foreign origin `403`, verified). Nothing to forge a login for |
| Staff dashboard | **Yes: cached basic-auth credentials** are attached to cross-site requests | A malicious page could try to open the dashboard's WebSocket in a signed-in staff browser (cross-site WebSocket hijacking). **Blocked by Streamlit's origin check:** foreign origin **with** the cached password gets `403` (verified on Docker). Framing is blocked (`X-Frame-Options: DENY`, `frame-ancestors 'none'`); the cookie is `SameSite=Lax; Secure`; the staff API is not reachable from browsers |

The origin check is therefore load-bearing for the dashboard. It must stay enabled
(`server.enableXsrfProtection = true`, and Streamlit's CORS setting left on, as in
`.streamlit/config.toml`), and the `Host` header must keep the port (section 7).

## 9. Transport

| Segment | Protection |
|---|---|
| Browser to Nginx, customer site | HTTPS only (HTTP redirects), TLS 1.2/1.3, HSTS one year; trusted certificate per [docs/https-setup.md](https-setup.md) |
| Browser to Nginx, dashboard | HTTPS, and additionally inside an SSH tunnel or VPN; never public |
| Nginx to the pages and the backend; pages to the backend | Plain HTTP on internal Docker networks on one host, with no route outside. Tokens cross only these. Acceptable for a single server; encrypt (or use a service mesh) if the containers ever span hosts |
| Cookies | `Secure` added by Nginx (Task 9) |

## 10. Changes made in this review

| Change | Why | Verified |
|---|---|---|
| Nginx forwards `Host $http_host` (port kept) to the upstreams ([deploy/nginx/snippets/proxy-headers.conf](../deploy/nginx/snippets/proxy-headers.conf)) | Streamlit's origin check refused every WebSocket on non-default ports with real hostnames | Docker: `403` → `101` same-origin; foreign origins still `403`. `test_upstream_host_keeps_the_port_for_streamlit_origin_checks` |
| Dashboard accepts **private source addresses only** ([deploy/nginx/snippets/admin-network-allowlist.conf](../deploy/nginx/snippets/admin-network-allowlist.conf)): loopback, RFC 1918, `100.64.0.0/10` (VPNs), IPv6 ULA | With development-grade staff authentication, the dashboard must not be public even if `ADMIN_BIND` is widened for a VPN | Docker: allowed from the server (bridge source); `403` when that range is removed from the list, so the list is evaluated against the real source. `test_the_dashboard_accepts_private_networks_only` |
| Authorization coverage test over every route | Hand-written lists miss new routes | Section 5 |

Limits of the allow-list: Docker keeps the real source address of outside connections,
so a public client is refused. With Docker's userland proxy handling outside traffic
(some rootless or IPv6 setups), every client looks like a bridge address. The bind
address (`ADMIN_BIND=127.0.0.1`) therefore remains the first control. A refusal of a
genuinely public source address could not be produced on this machine (it has none).
What was verified is that the list is applied to the real source.

## 11. Deployment blockers

Staff authentication remains development-grade. Until **B1 to B3** are done, the
dashboard must stay off the public internet (enforced: section 10), and the
Authentication category of [docs/deployment-readiness.md](deployment-readiness.md)
stays **Blocked**.

| # | Blocker | Required improvement (not implemented here) |
|---|---|---|
| **B1** | Static staff tokens; no MFA (SEC-08) | Staff sign in through an identity provider with per-person accounts and MFA. For example, an authenticating proxy in front of the dashboard (OIDC), with the dashboard trusting only the identity the proxy asserts, or the backend validating short-lived tokens from the provider. A design decision, not a configuration change |
| **B2** | Staff tokens never expire | Short-lived credentials (minutes to hours) issued per sign-in, with server-side session expiry |
| **B3** | Revocation requires a restart | Server-side revocation that takes effect at once (identity provider, or a revocation list the backend checks), plus a documented procedure for a leaked credential |
| B4 | No sign-in auditing or alerting | Record successful and failed sign-ins per person; alert on bursts of `auth.access_denied` (readiness: Monitoring) |
| B5 | Support staff see every ticket's contact details (SEC-06) | Contact details only for the assignee, or on recorded need |
| B6 | The customer page container can reach the staff API over the customer network | Restrict staff routes to requests from the admin network (in the backend, or with a separate backend listener on the admin network only) |
| B7 | Basic authentication: no logout, no expiry, possibly shared | Per-person entries now; replaced by B1 |

**Customer authentication** needs no blocker-level change for this deployment. Before
API clients other than the bundled page are built, document token storage for them,
and decide whether `/answer` and the search endpoints should ever be public (SEC-07).

### What was not done

- No identity provider, SSO, MFA or token expiry was added. Those are B1 to B3, and
  inventing them here would hide the blocker, not remove it.
- No external test from a public address, and no browser session through a real
  certificate (see [docs/https-setup.md](https-setup.md), section 10).
