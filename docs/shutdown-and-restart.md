# Graceful shutdown and restart behaviour (Stage 13)

What happens to the service, its data and the requests in progress when a container is
stopped, crashes or restarts. Verified on 2026-10-20 with the production images on
Docker 28.1.1 (WSL 2), run as compose runs them, by
[scripts/evaluate_restarts.py](../scripts/evaluate_restarts.py). The application's part
is kept by [tests/test_shutdown_restart.py](../tests/test_shutdown_restart.py). Raw results:
[data/restart_results_before.json](../data/restart_results_before.json) and
[data/restart_results_after.json](../data/restart_results_after.json).

## How it was tested

```bash
docker build -f deploy/Dockerfile --target backend  -t support-chatbot-backend:restart .
docker build -f deploy/Dockerfile --target customer -t support-chatbot-customer:restart .
python3 scripts/evaluate_restarts.py --tag restart --output restart-results.json
```

The script needs only Python 3.10+ and the docker CLI, and runs where Docker runs. It
starts the backend with compose's settings: `--init` (tini as PID 1), `--restart
unless-stopped`, read-only root filesystem, 2 GB / 2 CPUs, a named volume for the
database, the model volume read-only (filled by the image's own `model-init` fetch,
verified at load), `SEMANTIC_INDEX_PRELOAD=background`, the image's health check, and
`docker stop -t 30` (compose's `stop_grace_period: 30s`). Requests go straight to the
container's address on the Docker network. No LLM is configured
(`LLM_ENABLED=false`): nothing paid. All data is synthetic. It was run twice: on the
code before this task (`restart-before`) and after the fix described below
(`restart-after`).

## Results

| Scenario | Before the fix | After the fix |
|---|---|---|
| **Clean start**: to healthy; model files verified; semantic index ready | 7.2 s; yes; 23.8 s later | 6.9 s; yes; 8.8 s later |
| **Graceful stop** (`docker stop`) with no request running | 0.8 s, exit code 143 | 1.1 s, exit code 143 |
| **Persistence**: conversation, ticket, FAQ edit after stop + start | all present; edited answer served and returned by semantic search | same |
| Health after restart | healthy in 7.2 s, semantic index 12.3 s later | 7.0 s, 8.0 s later |
| **Request in flight, shorter than the 20 s graceful timeout** | client got its `200` after 11.1 s; the stop waited for it (8.6 s) | `200` after 9.6 s; stop 7.3 s |
| … the client's retry with the same `client_message_id` | the stored reply (`Idempotent-Replayed: true`) | same |
| **Request in flight, longer than the timeout** (CPU limited to 0.3) | cancelled after 20 s; client: connection closed, no response; nothing stored | same |
| … the client's retry with the same `client_message_id` after restart | **refused (`409`) for 158 s** until the lease expired | **answered on the first retry** (9.1 s: that request loads the model) |
| **Crash** (server process `SIGKILL`ed under write load) | restarted by the policy (restart count 0 → 1), healthy 7.2 s after the kill | 0 → 1, 7.0 s |
| … acknowledged replies lost; half-written exchanges | 0 of 23; 0 | 0 of 26; 0 |
| … leases left; retries of the 6 requests whose outcome the client never learned | 3; 5 answered at once, **1 refused for 170 s** | 0; all 6 answered within 4.5 s |
| **Customer page crash** (Streamlit process killed) | restarted, healthy 6.4 s after the kill | 6.8 s |
| Database after every scenario: `integrity_check`, foreign keys, numbering | ok, 0, 0 broken | ok, 0, 0 broken |

An earlier pair of runs the same day (their result files were lost) gave the same picture:
156 s and 170 s of refused retries before the fix, none after.

Exit code 143 is uvicorn re-raising `SIGTERM` after its clean shutdown. The log shows the
whole sequence: *Shutting down → Waiting for connections to close → Waiting for
application shutdown → Application shutdown complete → Finished server process*. With a
request past the timeout, *Cancel 1 running task(s), timeout graceful shutdown exceeded*
comes before *Waiting for application shutdown*.

## The fix: leases left by a stopped process

A conversation answers one message at a time: the turn takes a **processing lease**
stored in the database, and any other message gets `409 REQUEST_IN_PROGRESS` until the
turn stores its reply or releases the lease. If the process stops mid-answer (a request
past the graceful timeout, a crash, a kill), the lease stays in the database. It used to
expire only after `CONVERSATION_PROCESSING_LEASE_SECONDS` (180 s), and every retry until
then was refused, although nothing was answering any more: the customer could not use
the conversation for up to three minutes after a restart (158 s and 170 s observed).

Now the backend **releases every lease at startup**
(`ConversationService.release_abandoned_leases`, called in the application's lifespan)
and logs a warning with the count. This is safe because:

- the deployment runs **one** backend process, so a lease present at startup belongs to
  a process that no longer exists (docs/concurrency-and-limits.md);
- a turn can store its reply only while its own key still holds the lease, so a turn of
  the stopped process could never store a late reply after the release
  (`test_the_stopped_turn_can_never_store_after_the_release`);
- leases taken after startup are untouched (`test_a_lease_taken_after_startup_is_respected`).

With several backend processes sharing the database (not supported), a restarting
process would release leases another process still uses; that turn would then fail to
store, and its client would retry. Nothing would be stored twice, but this was not
tested.

## Uncertain outcomes and idempotent retries

**An answer in progress is never resumed.** Nothing about it survives the process: the
retrieval, any LLM call (OpenAI's request is not resumable, and it may or may not have
completed on OpenAI's side, possibly billed), the reply being built. What survives is
only what was committed, and a customer message and its reply are committed together in
one transaction, or not at all.

So when a request gets **no response** (connection closed by a stop past the timeout or a
crash, a client timeout, a proxy's 502/504), its outcome is **unknown to the client**: it
was either fully stored just before the stop, or not stored at all. The client must not
guess. It retries **with the same `client_message_id` and the same text**, and the server
answers exactly once ([docs/chat-api.md](chat-api.md#4-retry-safely-with-client_message_id)):

| What happened before the stop | The retry gets | Observed |
|---|---|---|
| Message and reply were committed | The stored reply: `200`, `Idempotent-Replayed: true`, the same `message_id`, no LLM call | The in-flight request completed during the stop; in an earlier crash run, 1 of 6 retries |
| Nothing was committed | A fresh answer: `200`, stored once | Crash scenario: 6 of 6; the request past the timeout |
| (A retry arrives while the restarted server answers the same ID) | `409 REQUEST_IN_PROGRESS`, `Retry-After: 2` | |

The customer page does this by itself: it keeps the message's ID, retries temporary
failures with it, and after a timeout offers "Try again" with the same ID
(`frontend/api_client.py`). A message sent again with a **new** ID is a new message and
may be answered twice: an API client must keep the ID.

**Support requests** follow the same rule with `client_request_id`: a retry after an
unknown outcome returns the ticket if it was created (`200`) or creates it once (`201`).

**Staff edits** (FAQ changes, ticket status) carry the version or status they were based
on: after an unknown outcome, reload and check before repeating; a repeated edit based on
the old version is refused with `409` instead of being applied twice.

## What else was checked

- **Database transactions:** each write is one short transaction committed in SQLite's
  write-ahead log. A kill mid-write leaves the last committed state: after every crash and
  stop, `PRAGMA integrity_check` was `ok`, no foreign key was broken, no conversation had
  a customer message without its reply, and every reply acknowledged to a client (`200`)
  was stored. No transaction is open while an answer is generated
  (`tests/test_sqlite_workload.py`), so a stop never interrupts a long transaction.
- **Application shutdown:** closes the OpenAI client and disposes of the database
  connections (`test_shutdown_closes_the_llm_client_and_the_database`), and logs a last
  metrics snapshot ([docs/monitoring.md](monitoring.md)). The in-memory metrics are reset
  by a restart.
- **Embedding caches:** they live in memory and are rebuilt at start from the database's
  current FAQs (the model files are verified against their manifest first,
  [docs/embedding-model.md](embedding-model.md)). An FAQ edited before the stop was the
  one semantic search returned after the restart. A cache that survived a restart could
  go stale; there is none.
- **Restart policy:** `restart: unless-stopped` restarted the backend and the customer
  page when their process died. Docker does not restart a container stopped with
  `docker stop`/`docker compose stop`, nor one that is only *unhealthy*
  ([docs/health-checks.md](health-checks.md)).
- **Health checks** went from *starting* to *healthy* 6.7–7.4 s after every start or
  restart; the semantic index followed 8–17 s later, in the background, while chat was
  already served (keyword retrieval meanwhile).

## Operating notes

- Stop with `docker compose stop` (or `down`): requests up to 20 s are completed, and
  Docker waits 30 s before `SIGKILL`. Answers longer than 20 s (only possible with a slow
  LLM) are cancelled; their customers retry and are answered once.
- After a crash or a stop past the timeout, the log line *Released N processing lease(s)
  left by a stopped process* counts the answers that were interrupted.
- A restart costs: the in-memory metrics, the rate-limit counters, and 8–17 s (up to about
  a minute on a cold disk) until semantic retrieval is back; keyword answers and
  everything else are available as soon as the container is healthy.
