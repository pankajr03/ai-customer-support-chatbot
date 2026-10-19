# Deployment architecture: single server, Docker Compose (Stage 13)

The design for running the chatbot on one Linux server with Docker Compose: Nginx in
front of the FastAPI backend, the customer Streamlit page and the staff Streamlit
dashboard. The files are in [deploy/](../deploy/):

| File | Purpose |
|---|---|
| [deploy/compose.yaml](../deploy/compose.yaml) | Services, networks, volumes, hardening |
| [deploy/Dockerfile](../deploy/Dockerfile) | Two images: `backend` and `frontend` (both pages) |
| [deploy/nginx/nginx.conf](../deploy/nginx/nginx.conf) | TLS, routing, per-visitor limits, staff access |
| [deploy/.env.example](../deploy/.env.example) | Compose variables: environment, ports, model revision |
| `deploy/environments/<env>/*.env.example` | Application settings per environment (`production`, `staging`) |
| [.dockerignore](../.dockerignore) | Allow-list of what enters the build |
| [tests/test_deployment_config.py](../tests/test_deployment_config.py) | Checks of the properties below (22 tests) |

> **Status: designed and statically checked, not yet built or run.** Docker is not
> installed on the development machine, so no image has been built and no container
> has started. [Section 11](#11-verification) lists what was checked and what must be
> checked on a Docker host before relying on it. Even when the containers run, this
> architecture does not by itself make the application production-ready.
> [docs/deployment-readiness.md](deployment-readiness.md) still has blockers this design
> does not address (staff identity, monitoring, scheduled backups).

## Contents

1. [Overview](#1-overview)
2. [Networks](#2-networks)
3. [Traffic flows](#3-traffic-flows)
4. [Staff access restriction](#4-staff-access-restriction)
5. [Persistent storage](#5-persistent-storage)
6. [Environment-specific configuration](#6-environment-specific-configuration)
7. [Container hardening](#7-container-hardening)
8. [Rate limiting behind the proxy](#8-rate-limiting-behind-the-proxy)
9. [Redis and PostgreSQL: assessment](#9-redis-and-postgresql-assessment)
10. [First deployment](#10-first-deployment)
11. [Verification](#11-verification)
12. [What this does and does not change in the readiness assessment](#12-what-this-does-and-does-not-change-in-the-readiness-assessment)

---

## 1. Overview

```mermaid
flowchart LR
    Internet(("Internet")) -- "80 → redirect<br/>443 HTTPS" --> Nginx
    Staff(("Staff via SSH tunnel<br/>or VPN")) -- "127.0.0.1:8443 HTTPS<br/>+ basic auth" --> Nginx
    subgraph host["One server (Docker Compose)"]
        Nginx["nginx<br/>(public, customer_tier, admin_tier)"]
        subgraph ct["customer_tier (internal)"]
            Customer["customer<br/>Streamlit :8501"]
        end
        subgraph at["admin_tier (internal)"]
            Admin["admin<br/>Streamlit :8502"]
        end
        Backend["backend<br/>FastAPI :8000, 1 worker"]
        DB[("db volume<br/>SQLite")]
        Models[("models volume<br/>read-only")]
        Backups[("backups volume")]
    end
    Nginx -- "WebSocket + HTTP" --> Customer
    Nginx -- "WebSocket + HTTP" --> Admin
    Nginx -- "GET /healthz only" --> Backend
    Customer -- "HTTP JSON, Bearer" --> Backend
    Admin -- "HTTP JSON, staff Bearer" --> Backend
    Backend --- DB
    Backend --- Models
    Backend --- Backups
    Backend -. "egress network<br/>(OpenAI, if LLM on)" .-> OpenAI(("OpenAI"))
```

| Service | Image | Listens on | Published | Notes |
|---|---|---|---|---|
| `nginx` | `nginxinc/nginx-unprivileged:1.28-alpine` | 8080, 8443, 9443 | 80, 443 on all interfaces; 8443 on 127.0.0.1 | Only service with published ports |
| `backend` | `support-chatbot-backend` (`deploy/Dockerfile`, target `backend`) | 8000 | No | One uvicorn worker, `--no-server-header` |
| `customer` | `support-chatbot-frontend` (target `frontend`) | 8501 | No | `frontend/app.py` |
| `admin` | `support-chatbot-frontend` | 8502 | No | `frontend/admin_app.py` |
| `model-init` | backend image | none | No | One-off (profile `setup`): fetches the pinned model |

Service names are fixed in the Compose file (`name: support-chatbot`), so containers,
networks and volumes are always `support-chatbot-<name>`, and services find each other
by name (`http://backend:8000`).

No Kubernetes, Redis or PostgreSQL is introduced ([section 9](#9-redis-and-postgresql-assessment)).

## 2. Networks

| Network | Internal | Members | Purpose |
|---|---|---|---|
| `public` | No | `nginx` | Carries the published ports |
| `customer_tier` | **Yes** | `nginx`, `customer`, `backend` | Customer page traffic |
| `admin_tier` | **Yes** | `nginx`, `admin`, `backend` | Staff dashboard traffic |
| `egress` | No | `backend`, `model-init` | Outbound only: OpenAI (when `LLM_ENABLED=true`) and the one-off model download. Nothing is published on it |

- **The backend is never published** and is not on `public`. From outside the server
  it can only be reached through what Nginx proxies, which is `GET /healthz` alone.
- **Streamlit ports are never published.** 8501 and 8502 exist only on the internal
  networks.
- **The two pages share no network.** A compromised customer container cannot open
  the staff dashboard's port, and the reverse holds too.
- Internal networks have **no route to the internet**, so neither Streamlit container
  can make outbound connections.
- The backend keeps `egress` even with the LLM off. To remove its internet access
  entirely in that case, drop `egress` from the backend in a Compose override file.

## 3. Traffic flows

| Flow | Path |
|---|---|
| Customer chat | Browser → `nginx:8443` (TLS) → `customer:8501` (HTTP and the `/_stcore/stream` WebSocket) → `backend:8000` (server-side HTTP with the conversation token) |
| Staff dashboard | Browser → SSH tunnel or VPN → `nginx:9443` (TLS + basic auth) → `admin:8502` → `backend:8000` (staff token) |
| Uptime monitoring | Monitor → `nginx:8443/healthz` → `backend:8000/api/v1/ready` (200 when the knowledge base and storage are usable, 503 otherwise) |
| HTTP | `nginx:8080` answers with a `308` redirect to HTTPS |
| Outbound | `backend` → OpenAI over `egress`, only when the LLM is enabled |

**Why the API is not exposed:** the browser talks only to Streamlit, and Streamlit calls
the API server-side ([docs/integration-guide.md](integration-guide.md)). No current
client needs the API from the internet. Exposing it later for an external client
([docs/chat-api.md](chat-api.md)) would need an Nginx route allow-list. Customer and
staff ticket routes share `/support/tickets`, so it cannot be done by prefix. It would
also need `CORS_ORIGINS` and trusted forwarded headers. Not done here.

Traffic between Streamlit and the backend is plain HTTP on a Docker bridge that never
leaves the server. The frontend logs its standard warning for `http://` to a
non-loopback host (`frontend/config.py`); here that warning is expected.

## 4. Staff access restriction

Three independent layers, each of which must be passed:

1. **Network:** the dashboard listener is published on `127.0.0.1:8443` by default
   (`ADMIN_BIND`). Staff reach it through an SSH tunnel
   (`ssh -L 8443:127.0.0.1:8443 user@server`, then `https://localhost:8443`) or a VPN
   address set in `ADMIN_BIND`. An optional `allow`/`deny` list is in `nginx.conf`.
2. **HTTP basic authentication** at Nginx (`deploy/nginx/admin.htpasswd`). This is a
   credential separate from the staff token. It also stops anonymous visitors from
   reaching the Streamlit app at all.
3. **The staff bearer token** checked by the backend, with roles
   ([docs/admin-dashboard.md](admin-dashboard.md)).

The staff API (`/api/v1/admin/*`, ticket status changes) is unreachable from outside:
only the admin container calls it, over `admin_tier`. API documentation (`/docs`) is
off in production (`APP_ENV=production`) and not proxied in any environment.

This restricts **where** staff can connect from. It does not fix the staff identity
model (static tokens, no MFA, revocation needs a restart: SEC-08). A tunnel or VPN plus
basic auth makes a leaked token much harder to use, but the token is still the final
credential.

## 5. Persistent storage

| Volume | Mounted at | Holds | Notes |
|---|---|---|---|
| `db` | `backend:/var/lib/chatbot` | `conversations.db` and its WAL files: conversations, tickets, contacts, **FAQs** (since Stage 11), audit log | `DATABASE_URL=sqlite:////var/lib/chatbot/conversations.db`, fixed in `compose.yaml` |
| `backups` | `backend:/app/data/backups` | Copies made by `scripts.import_faqs --backup` | Copy off the server; holds customer data unencrypted |
| `models` | `backend:/models` **read-only**; `model-init:/models` read-write | The embedding model folder `all-MiniLM-L6-v2` | Written once by `model-init` |

**Required application data.** The FAQ knowledge base lives in the database, so it is
covered by `db`. The FAQ seed file `data/faqs.json` is built into the backend image
(`FAQ_DATA_PATH=/app/data/faqs.json`). It is read only to fill an empty FAQ table on the
first start, and it is versioned with the code. No other runtime data exists:
evaluation files are excluded from the images.

**Model cache.** The backend loads the model from a plain folder
(`EMBEDDING_MODEL_NAME=/models/all-MiniLM-L6-v2`) with `HF_HUB_OFFLINE=1`, so it never
contacts Hugging Face. `model-init` downloads revision
`1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, the revision in the local cache that every
Stage 12 evaluation used. This closes the "model revision not pinned" limitation in
the readiness assessment. Loading that revision from a plain folder gives vectors
identical to loading it by name (maximum difference 0.0, checked on 2026-10-19). If
the model is missing, the backend still serves keyword-only answers, and
`/api/v1/ready` reports `semantic_search: unavailable`.

**Not persisted, by design:** Streamlit session state (in memory). Restarting the
`customer` container ends open chats in the page. The conversations stay in the
database, but the page loses their tokens, so customers start a new conversation.
Rate-limit counters and the LLM circuit breaker also reset on a backend restart.

Volume directories are created in the image and owned by the app user (uid 10001), so
new named volumes are writable by the non-root process without any `chown` at runtime.

## 6. Environment-specific configuration

Two levels, both kept out of git:

| File | Read by | Contents |
|---|---|---|
| `deploy/.env` (from `.env.example`) | Docker Compose | `DEPLOY_ENV`, published addresses and ports, image tag, model revision |
| `deploy/environments/<DEPLOY_ENV>/backend.env` | `backend` | Application settings: `APP_ENV`, rate limits, staff hashes, LLM switch and key |
| `deploy/environments/<DEPLOY_ENV>/customer.env` | `customer` | Page settings: demo notice, example questions, timeouts |
| `deploy/environments/<DEPLOY_ENV>/admin.env` | `admin` | Dashboard session limits, timeouts |

- `DEPLOY_ENV` selects the folder: `production` and `staging` are provided. Staging
  uses `APP_ENV=staging`, the demo notice and lower site-wide limits. Compose refuses
  to start if `DEPLOY_ENV` is unset.
- **Paths and wiring are fixed in `compose.yaml`**, not in the environment files:
  `DATABASE_URL`, `FAQ_DATA_PATH`, `EMBEDDING_MODEL_NAME`, `HF_HUB_OFFLINE` and
  `BACKEND_API_URL`. An environment file cannot point the backend at another database
  or the pages at another backend.
- Running Compose from `deploy/` means it reads `deploy/.env`, never the project
  root's development `.env`. The root `.env` is also excluded from the build context,
  so it can never end up inside an image, where `Settings` would load it.
- Every example validates against the application's settings classes
  (`test_backend_environment_files_are_valid_settings`,
  `test_page_environment_files_are_valid_settings`). The application itself still
  refuses invalid values at startup (verified in
  [docs/deployment-readiness.md §4](deployment-readiness.md#4-verification-performed-for-this-assessment)).
- Secrets (`OPENAI_API_KEY`, `STAFF_TOKEN_HASHES`) live in `backend.env`, readable only
  by the deploying user (`chmod 600`). No secrets manager is used. Compose `secrets:`
  would need code changes, because the settings read environment variables only.

## 7. Container hardening

Applied to every service through the `x-hardening` block in `compose.yaml`, and
enforced by `test_every_service_is_hardened`:

| Control | Setting |
|---|---|
| No privileged containers | No `privileged`; no host network, PID or IPC namespace |
| No Docker socket | No container mounts `/var/run/docker.sock` or any host system path (`test_no_container_mounts_the_docker_socket_or_host_system_paths`) |
| No Linux capabilities | `cap_drop: [ALL]`, no `cap_add`. Nginx uses the unprivileged image and ports above 1024, so it needs no `NET_BIND_SERVICE` |
| No privilege escalation | `no-new-privileges:true` |
| Non-root | App images run as uid 10001; Nginx as uid 101 |
| Read-only root filesystem | `read_only: true`; writable paths are the named volumes and a size-limited tmpfs at `/tmp` |
| Resource limits | Memory: backend 2 GB (measured ~0.6 GB warm), pages 512 MB, Nginx 128 MB. CPU limits; `pids_limit: 256` |
| Log size | `json-file` driver, 10 MB × 5 files per container |
| Minimal images | Frontend image has no AI packages; backend excludes Streamlit and development tools; build context is an allow-list |

Base images are referenced by tag (`python:3.13-slim`, `nginx-unprivileged:1.28-alpine`).
For production, pin them by digest (`PYTHON_IMAGE` build argument; edit the Nginx
image line).

## 8. Rate limiting behind the proxy

**Finding (from reading the code, not observed in a running deployment):** the backend
keys its per-IP limits on the TCP peer address (`app/api/dependencies.py`), and the
frontend does not forward the visitor's address. Behind Streamlit, every customer
reaches the backend from the `customer` container's address. Under the default limits,
**20 new conversations per minute and 120 requests per minute would apply to all
customers together**. This is already true in the development setup; it matters once
there is real traffic.

The design handles it without code changes:

- **Per visitor at Nginx** (the real client address): 10 requests per second (burst
  40), **20 new Streamlit sessions per minute** (each starts one conversation), and at
  most 20 open connections per address.
- **Site-wide ceilings in the backend:** `backend.env.example` raises the per-IP
  limits to 3,000 requests and 300 new conversations per minute in production (600 and
  60 in staging). Behind the page these act as a whole-site safety valve. Size them to
  expected traffic.
- **Unchanged and still per customer:** chat messages (20 per minute) and support
  requests (5 per minute) are keyed by conversation and token, not by address.

What Nginx cannot limit: messages inside an open WebSocket. That is what the
per-conversation chat limit in the backend covers. A future code change could have the
pages forward the visitor's address, with the backend trusting it only from the page
containers. That would make the backend's own per-IP limits per visitor again. It is
recommended, not done here.

## 9. Redis and PostgreSQL: assessment

The task allows Redis or PostgreSQL only for a verified blocker in the existing
implementation. The verified findings were checked against this architecture:

| Finding | Verified where | Blocks this architecture? | Decision |
|---|---|---|---|
| SEC-09: rate limits are per process | Security audit | **No.** It blocks *several* backend processes. This design runs exactly one (`--workers 1`, one service, no replicas; `test_backend_runs_exactly_one_worker`) | No Redis |
| SEC-10: FAQ catalogue and semantic index are per process; other workers serve stale FAQs after an edit | Security audit | **No**, for the same reason | No Redis |
| SQLite allows one writer; chat p99 7.1 s at 8 parallel conversations, 0 errors | Load test (test report) | **No.** It is a capacity limit, not a failure at measured load. Writers wait up to 5 s, then get `503` with `Retry-After` | No PostgreSQL now |
| SQLite cannot be shared by several servers | Security audit §7 | **No.** This is a single-server design | No PostgreSQL |
| Per-IP limits see the page container ([section 8](#8-rate-limiting-behind-the-proxy)) | Code reading (this design) | **No.** Handled at Nginx | No Redis |

**Conclusion: no verified blocker requires Redis or PostgreSQL for a single-server,
single-worker deployment.** None is introduced.

**Recommendation for later**, before any change of architecture: move to PostgreSQL,
and shared rate limits and catalogue revision (Redis or the database), when **any** of
these becomes true:

- more than one backend process or server is needed;
- `503` responses caused by the SQLite busy timeout appear in the logs under real
  traffic;
- chat p99 under real traffic exceeds an agreed target.

That move needs versioned migrations (the readiness assessment, "Database
migrations") and a load test of its own.

**Operating constraint that follows:** never scale the backend
(`docker compose up --scale backend=2`) or add `--workers`. Compose cannot forbid this;
the test and this document are the guard.

## 10. First deployment

On a Linux server with Docker Engine and the Compose plugin, from a checkout of the
repository:

```bash
cd deploy
cp .env.example .env                                   # set DEPLOY_ENV (production or staging)
for f in backend customer admin; do
  cp environments/production/$f.env.example environments/production/$f.env
done
chmod 600 environments/production/*.env                # then edit backend.env

# TLS: certificates from your CA or certbot (outside Compose), or self-signed for staging
mkdir -p nginx/certs
openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=staging.example" \
  -keyout nginx/certs/privkey.pem -out nginx/certs/fullchain.pem      # staging only

# Staff basic-auth file (SHA-512 crypt; you are prompted for the password)
printf 'staff:%s\n' "$(openssl passwd -6)" > nginx/admin.htpasswd

# Nginx runs as uid/gid 101 and must be able to read the key and the password file
sudo chown root:101 nginx/certs/privkey.pem nginx/admin.htpasswd
sudo chmod 640 nginx/certs/privkey.pem nginx/admin.htpasswd

docker compose build
docker compose --profile setup run --rm model-init     # fetch the pinned model once

# Staff tokens: the printed entry goes into STAFF_TOKEN_HASHES in backend.env
docker compose run --rm --no-deps backend \
  python -m scripts.create_staff_token alice --role admin

docker compose up -d
docker compose ps                                      # all services "healthy"
curl -fsS https://<host>/healthz
```

Routine operations run inside the backend container, against the same database:

```bash
docker compose exec backend python -m scripts.cleanup_conversations
docker compose exec backend python -m scripts.anonymize_support_data --retention
```

These are not scheduled. There is no standalone backup command: the only code that
copies the database is `scripts.import_faqs --backup`, which also imports FAQs (and
skips the backup with `--dry-run`). Scheduled backups copied off the server are still
open (readiness assessment, "Backup and recovery").

## 11. Verification

**Checked on 2026-10-19 (development machine, no Docker):**

| Check | Result |
|---|---|
| `tests/test_deployment_config.py` | **22 passed**: hardening on every service, no Docker socket or host paths, only Nginx publishes ports, dashboard on loopback by default, internal networks and tier separation, one backend worker, named volumes and read-only model, pinned revision, every environment example validates against `Settings` / `FrontendSettings`, no secrets in examples, build-context allow-list, git-ignore rules, no backend route in Nginx except readiness, WebSocket header inheritance |
| Tests detect regressions | Three deliberate breakages each failed exactly one test (a `privileged` service, the customer page joining `admin_tier`, a WebSocket location losing `Host`); files restored afterwards |
| Model from a plain folder | The cached revision `1110a243…` loaded from its folder gives embeddings identical to loading by name (max abs difference 0.0) |
| Command-line options | `uvicorn` 0.54 accepts `--workers`, `--no-server-header`, `--timeout-graceful-shutdown`; the Streamlit options used are standard server options |
| Git ignore | Real `*.env`, certificates and `admin.htpasswd` are ignored; the `*.env.example` files are tracked. (The folder is `environments/`, not `env/`, because the virtualenv rule `env/` would have ignored it whole.) |
| Lint | `ruff check` and `ruff format --check` pass on the new test file |

**Not verified: must be checked on a Docker host before relying on this design:**

1. `docker compose config` accepts the file (YAML merge keys with a list, `profiles`,
   `mem_limit` and `cpus` with the installed Compose version).
2. Both images build. PyTorch's CPU wheel exists for the pinned version on
   `python:3.13-slim`. The frontend import check passes.
3. Every container starts and turns healthy **with the read-only root filesystem**: the
   Nginx image's entrypoint scripts, Streamlit writing under `HOME=/tmp`, PyTorch and
   Hugging Face reading a read-only model folder.
4. `nginx -t` accepts the configuration, including `server ... resolve` in upstreams
   (open-source Nginx 1.27.3 or later) and `http2 on`.
5. Streamlit works through the proxy: the WebSocket upgrades, the XSRF check passes
   over HTTPS, and sessions survive the 1-hour proxy read timeout with Streamlit's
   keep-alive.
6. Basic auth accepts an `openssl passwd -6` hash on the Alpine image, and the browser
   sends it on the WebSocket upgrade.
7. From outside the server: only 80 and 443 answer; 8000, 8501 and 8502 do not; the
   dashboard port answers only on the server's loopback (`ss -tlnp` and an external
   port scan).
8. Per-visitor Nginx limits return `429` as configured, and the site-wide backend
   ceilings are not hit by normal traffic.
9. `model-init` writes a folder the backend loads, with `/api/v1/ready` then reporting
   `semantic_search: ready` after the background preload.
10. Restart behaviour: `docker compose restart backend` keeps data and recovers;
    `docker compose down` followed by `up` keeps all volumes.

## 12. What this does and does not change in the readiness assessment

| Readiness category | Effect of this design |
|---|---|
| Security | **Addresses** TLS termination, HSTS, proxy timeouts and request limits, frame protection for the Streamlit pages, the `server` header, and keeping `/docs` and `/admin` off the internet. **Still open:** a Streamlit-specific CSP, SEC-08, a deployed-instance scan, and building and running this design |
| Authentication | **Reduces exposure** (tunnel or VPN plus basic auth before the staff token). Does **not** fix SEC-08 |
| AI model initialization | **Addresses** the unpinned revision and runtime downloads (pinned folder, offline) |
| Resource consumption | **Adds** per-visitor limits, connection limits and proxy timeouts at Nginx, and container memory, CPU and PID limits |
| Operational documentation | **Adds** a deployment procedure for this target (section 10). A runbook is still missing |
| Monitoring | **Adds** a public readiness URL for an uptime monitor. Metrics and alerting are still missing |
| Backup and recovery | **Unchanged:** volumes make the data location explicit, but nothing backs them up |
| Data persistence, migrations, performance | **Unchanged:** still one SQLite file, one writer, implicit upgrades at start |

The readiness results stay as they are until this design has been built, run and
verified on a Docker host (section 11).
