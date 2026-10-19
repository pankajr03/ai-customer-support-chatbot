# Health and readiness checks (Stage 13)

Three checks, each answering a different question. All three are cheap enough to poll
every few seconds, exempt from the request-rate limit, sent with `Cache-Control:
no-store`, and answer `HEAD` as well as `GET` (many uptime monitors send `HEAD`).
**None of them loads or downloads the embedding model, calls OpenAI, or reads a
conversation row.**

| Endpoint | Question | `200` | `503` | Who uses it | Reachable from |
|---|---|---|---|---|---|
| `GET /api/v1/health` | **Liveness:** is the process answering HTTP? | Always, if it answers at all | Never | Process supervisors, deploy scripts | Private networks, and the public site |
| `GET /api/v1/ready` | **Readiness:** can it handle the essential workload (chat) now? With the state of each dependency, optional ones included | Knowledge base and storage usable | Either unusable | Docker's health check, the customer page, the staff dashboard, `start-dev.ps1` | Private networks only |
| `GET /api/v1/status` | Readiness, **public form:** the verdict only | As `/ready` | As `/ready` | Uptime monitors (through `/healthz`) | Public site (`/healthz`, `/api/v1/status`) |

## Liveness versus readiness

**Liveness** says the process is running. When it fails, the process is stuck or gone, and
the fix is to restart it. It therefore checks nothing else: if liveness depended on the
database, a full disk would make a supervisor restart a healthy process over and over,
which fixes nothing and drops requests in flight. `/health` stays `200` with a broken
database (`tests/test_health.py`).

**Readiness** says requests sent now will succeed. When it fails, the process is fine but
something it needs is not, and the fix is to stop sending it traffic (or not start what
depends on it) until it recovers. It must be meaningful: `200` has to mean chat works.

So `/health` can be `200` while `/ready` is `503` (alive, cannot serve). The reverse does
not happen.

## What decides readiness

Only the **essential** dependencies, without which no chat request can succeed:

| Check | `ok` means | `unavailable` means |
|---|---|---|
| `knowledge_base` | Approved, active FAQs are loaded | No active FAQs, or the FAQ store failed to reload (fails closed) |
| `conversation_storage` | The database opens, every table exists, and it **can be written**: the file and its folder are writable and the disk has at least 32 MiB free | Cannot open, a table is missing, read-only, or the disk is almost full |

Writability is judged without writing: no write lock is taken, so a probe cannot queue
behind (or delay) a chat turn holding SQLite's single write lock. Every chat message is a
write, so a full or read-only disk makes chat fail even though reads still work; before
Stage 13, readiness said `ok` in that state.

## Optional dependencies: reported, never decisive

| Check | Values | When it is not available |
|---|---|---|
| `semantic_search` | `ready` (model and index in memory); `not_loaded` (load on the first question, which is then slow); `unavailable` (failed to load in the last `EMBEDDING_LOAD_RETRY_SECONDS`, **or** the model is configured as a local folder that holds no model: in Docker, `model-init` has not been run) | Keyword-only retrieval, or `retrieval_unavailable` for questions keyword search cannot match |
| `llm` | `disabled` (`LLM_ENABLED=false`); `enabled` (a key is configured; **not tested**, since testing would be a paid call); `paused` (the circuit breaker opened after recent failures, for its cooldown) | The approved FAQ answer, word for word |

An LLM outage is therefore **not** an application failure: with `llm: paused` the service
stays `ready` (`200`), and customers get approved FAQ answers. The same holds for the
embedding model. The customer page shows these states as a badge (*Online*, *Online*
with "the first answer may take a little longer", *Limited*), from `/ready`.

How the checks avoid cost:

- **No model download or load.** `semantic_search` reads in-memory flags; the
  missing-folder test is one `stat` of `<folder>/config.json`, and only for an absolute
  path (a Hugging Face Hub ID cannot be checked without the network, so it is never
  reported missing).
- **No paid LLM request.** `llm` reads configuration and the circuit breaker's state;
  reading it does not half-open the breaker. The OpenAI client is not even created.

## What is public, and what is not

The public site must not describe the deployment. `/ready` does: it tells anyone whether
an LLM is configured, whether it is failing right now, and whether the search model
loaded. So:

- `https://PUBLIC_HOSTNAME/healthz` and `/api/v1/status` give `{"status": "ready"}`, or
  `503` with `{"status": "not_ready", "error": {...}}`, which names no dependency.
- `/api/v1/ready` is **not** proxied by Nginx (`404` from outside). It stays available to
  the containers on the private networks, which need the detail.
- `/api/v1/health` stays public: it says only `healthy` and the service name.

No check includes keys, paths, hostnames, versions or exception text; the reason for
`unavailable` is in the server log (`Readiness: conversation storage unavailable (…)`).

## Docker Compose

Docker has one health check per container, so each service uses the one that matches what
depends on it (`deploy/Dockerfile`, `deploy/compose.yaml`):

| Service | Health check | Kind | Why |
|---|---|---|---|
| `backend` | `GET /api/v1/ready` every 15 s (start period 60 s) | Readiness | The pages start only once it is healthy (`depends_on: service_healthy`). Does not wait for the model or the LLM |
| `customer`, `admin` | Streamlit's `/_stcore/health` | Liveness | Deliberately not the backend's state: the page shows *Unavailable* itself, and a backend outage must not mark every container unhealthy |
| `nginx` | `/nginx-health` (loopback only) | Liveness | Same reason; it must keep serving the error pages |
| `backup` | `backup_scheduler --check` | Its own job | Unhealthy when backups stop succeeding |
| `backup-offsite` | `offsite.sh check` | Its own job | Opt-in profile |

Docker (without Swarm) does **not** restart an unhealthy container; `restart:
unless-stopped` restarts one whose process exits. The health status gates start order and
is visible in `docker compose ps`, which is where a not-ready backend shows up. Alert on it,
or on `/healthz` from an external monitor.

## Tests

| File | Covers |
|---|---|
| `tests/test_health.py` | Liveness: shape, `no-store`, `HEAD`, stays `200` with a broken database |
| `tests/test_readiness.py` | Readiness verdicts; nothing loaded or called; optional dependencies never block; full disk and unwritable file; missing model folder; public status hides every dependency state and still gives `503`; `HEAD` on all three; no rate limit |
| `tests/test_deployment_config.py` | `/healthz` goes to `/api/v1/status`; the public allow-list contains `/api/v1/status` and not `/api/v1/ready` |
| `tests/test_authorization_coverage.py` | The three checks are public on purpose (no credentials) |

### Verified on Docker (2026-10-19)

Full stack (`DEPLOY_ENV=staging`, development certificate, Docker 28.1 on WSL 2), in a
throwaway copy of the working tree:

| Check | Result |
|---|---|
| Start | All five services healthy; the pages and Nginx started after the backend was healthy |
| Private network (from the customer container) | `/health`, `/ready`, `/status` `200`; `/ready` with all four checks; `HEAD` `200` with an empty body on all three |
| Public site | `/healthz` and `/api/v1/status` `200` `{"status":"ready"}`; `/api/v1/ready` `404` (JSON error); `/api/v1/health` `200`; `HEAD /healthz` `200` |
| Database file made read-only (`chmod 444`) | `/health` still `200`; `/ready` `503` with `conversation_storage: unavailable` (`semantic_search` and `llm` unchanged); `/healthz` `503` `{"status":"not_ready", "error": …}`, no dependency named; log: `Readiness: conversation storage unavailable (database check failed (not writable))`, no path |
| Docker's view of it | The backend turned `unhealthy` after three failed checks and was **not** restarted; customer, admin, Nginx and backup stayed `healthy` |
| Restored (`chmod 644`) | `/ready` `200`; backend `healthy` again within 20 s |
| Missing model folder, in the image | `/models/all-MiniLM-L6-v2` (downloaded) not missing; `/models/not-downloaded` missing |

Not exercised on Docker: an actual full disk (simulated in `tests/test_readiness.py`), and
an LLM outage (the breaker states are covered by the tests; no paid request was made).
