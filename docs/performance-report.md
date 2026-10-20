# Performance report (Stage 13)

Every performance figure measured for this deployment, in one place, with where it
came from and what it does not cover. Each section links to the document that holds
the method and the raw results. **Nothing here is estimated.** Where something was not
measured, this report says so instead of filling the gap.

**Measured:** 2026-10-19 and 2026-10-20, on the development machine: Windows 10
(19045), 4 logical CPUs, 8 GB RAM, no GPU, Python 3.13.3. Docker 28.1.1 runs in WSL 2
(kernel 5.15, 4 vCPUs, 3.8 GB, cgroup v1). **Not measured on a production server.**

> **Read this first**
>
> - **No maximum number of users is claimed.** The tests show how the application
>   behaved at the concurrency they used, on this machine, with a mocked LLM. Capacity
>   depends on how often customers send messages, the real LLM's latency and the
>   server's hardware. None of these is fixed by the tests.
> - **Live OpenAI latency was not measured.** It needs paid API calls, which were not
>   approved. Every run used `LLM_ENABLED=false` or a local stand-in that replies
>   instantly. With the LLM on, OpenAI's latency will dominate every generated answer.
> - **Docker client-side POST times on this machine are inflated** by the
>   Windows-to-WSL port relay, not by the application
>   ([performance-profile.md](performance-profile.md#docker-post-latency-a-measurement-artefact-on-this-machine)).
>   This report uses the backend's own timings for Docker, or requests sent directly to
>   the container.

## Contents

1. [Summary](#1-summary)
2. [Startup](#2-startup)
3. [Embedding model](#3-embedding-model)
4. [Chat latency](#4-chat-latency)
5. [Retrieval](#5-retrieval)
6. [Database](#6-database)
7. [Concurrency](#7-concurrency)
8. [Restarts and recovery](#8-restarts-and-recovery)
9. [Memory, CPU and disk](#9-memory-cpu-and-disk)
10. [Optimizations made in Stage 13](#10-optimizations-made-in-stage-13)
11. [Not measured](#11-not-measured)
12. [Reproducing the measurements](#12-reproducing-the-measurements)

---

## 1. Summary

| Question | Measured answer | Source |
|---|---|---|
| How long until the stack serves customers? | All services healthy **51–67 s** after `docker compose up -d`; the backend alone is ready in 4–11 s | [deployment-runbook.md §2.5](deployment-runbook.md#25-start-services), [performance-profile.md](performance-profile.md#startup) |
| How long until semantic search works? | **8–24 s** after the backend is healthy, with `SEMANTIC_INDEX_PRELOAD=background` (the production setting). Keyword answers work meanwhile | [shutdown-and-restart.md](shutdown-and-restart.md#results) |
| First chat without preloading | **19–22 s** (the model loads on demand); up to about 60 s on a cold disk cache | [performance-profile.md](performance-profile.md#chat-requests) |
| A warm chat turn | **22 ms** p50 inside the backend (local), 36 ms in the container; 0.08 CPU-seconds | [performance-profile.md](performance-profile.md#chat-requests) |
| Throughput at 4 and 8 requests in flight (mocked LLM) | About **12 chat turns per second** at both; p50 **240 ms** (4) and **512 ms** (8) | [concurrency-and-limits.md](concurrency-and-limits.md#latency) |
| Errors under that load | **0** lost or duplicated replies or tickets; no `503`, `500` or timeout | [concurrency-and-limits.md](concurrency-and-limits.md#correctness-all-checks-passed-at-both-concurrency-levels) |
| Backend memory | **541–614 MiB** once the model is loaded, flat under load; 2 GB container limit | [performance-profile.md](performance-profile.md#memory), [concurrency-and-limits.md](concurrency-and-limits.md#memory) |
| Graceful stop / crash recovery | Stop in about **1 s** when idle; a crashed backend is restarted and healthy again in about **7 s** | [shutdown-and-restart.md](shutdown-and-restart.md#results) |
| Image sizes | backend **1.40 GiB**; customer and admin **546 MiB** each (shared layers) | [performance-profile.md](performance-profile.md#container-images) |
| Staff analytics (20,000 conversations and tickets) | Chat analytics, 30 days: **638 → 208 ms**; deepest ticket page: **739 → 13 ms** | [retrieval-and-database.md](retrieval-and-database.md#results) |

## 2. Startup

Seconds until each endpoint first answers `200`. Three runs each; the first run of a
series is the slowest (cold disk cache).

| Server | Local, 3 runs | Docker, 3 runs |
|---|---|---|
| Backend live (`/api/v1/health`) | 10.27, 3.59, 4.53 s | 10.79, 4.52, 4.22 s |
| Backend ready (`/api/v1/ready`) | 10.80, 4.16, 5.06 s | 11.28, 5.05, 4.71 s |
| Customer page (`/_stcore/health`) | 4.68, 3.10, 3.10 s | 3.60, 2.96, 2.94 s |
| Admin page (`/_stcore/health`) | 3.13, 3.12, 3.16 s | 3.27, 2.61, 2.68 s |

**The whole Compose stack** starts in dependency order (backend healthy, then the pages,
then Nginx). From `up` to all services healthy: 51–67 s measured; 40 s for `down` and
`up` again with the images and volumes in place
([deployment-tests.md](deployment-tests.md#results)).

**Building the images** on this machine: 689 s on a cold cache (runbook §2.3); 25 s with
cached layers. `model-init` downloads the model once: 91.6 MB in 35 s.

## 3. Embedding model

`sentence-transformers/all-MiniLM-L6-v2` at revision
`1110a243fdf4706b3f48f1d95db1a4f5529b4d41` ([embedding-model.md](embedding-model.md)).

| | Local (Windows) | Docker (production limits) |
|---|---:|---:|
| Load in a fresh process, 3 runs | 59.3, 18.4, 18.5 s | — |
| Load and index the FAQs inside the first chat | 18.9 s | 22.3 s; 17.5–17.7 s in later runs |
| Embed one query, p50 | 22–28 ms | 17 ms |
| Memory added by the model | ≈ 497 MiB | ≈ 463 MiB |

The load varies from about 18 s to over 60 s; the slow runs were each the first of a
series, which points to the operating system's disk cache (not isolated). The production
setting `SEMANTIC_INDEX_PRELOAD=background` loads it right after startup, so no customer
waits for it; until it is loaded, questions are answered from keyword evidence when it is
strong enough.

## 4. Chat latency

One conversation at a time, warm (model loaded), 40 turns, LLM off:

| | Local | Docker |
|---|---:|---:|
| First chat request (loads the model on demand) | 19.0 s | 22.4 s |
| Warm chat turn inside the backend (`chat_turn_duration`) p50 / p95 | **22.0 / 31.3 ms** | **35.9 / 86.6 ms** |
| … retrieval and generation (`pipeline`) p50 | 15.0 ms | 20.8 ms |
| … storing the exchange (`store`) p50 | 4.8 ms | 9.8 ms |
| … loading history (`history`) p50 | 2.3 ms | 3.9 ms |
| Warm chat request, client side, p50 / p95 | 33.3 / 41.6 ms | not comparable (port relay) |

**With the LLM path (local stand-in, no network, nothing paid):** whole turn p50 40.5 ms,
p95 47.6 ms; the grounded-answer step (retrieval, prompt, validation) p50 20.1 ms;
prompts of about 4,200 characters. The application adds a few tens of milliseconds
around a generation. The real call's latency is OpenAI's and is **not measured**.

## 5. Retrieval

Inside the backend, p50, 30 calls each:

| | Local | Docker (direct to the container) |
|---|---:|---:|
| Keyword search (`GET /faqs/search`) | 2.3 ms | 2.9 ms |
| Semantic search (`POST /semantic-search`) | 17.6 ms | 17.1 ms |
| Hybrid search (`POST /hybrid-search`) | 17.1 ms | — |
| Retrieval inside a chat turn (`retrieval_duration`) | 14.6 ms | 21.8 ms |

The query embedding dominates. Scaling with the catalogue size (synthetic, from
[retrieval-and-database.md](retrieval-and-database.md#reviewed-and-left-as-they-are)):

| FAQs | Keyword search | Semantic matrix product |
|---:|---:|---:|
| 30 | 0.25 ms | 0.01–0.02 ms |
| 2,030 | 23–27 ms | 0.50–0.53 ms |

An in-memory index suits a support FAQ of hundreds to a few thousand entries. No vector
database is used or needed at this size.

## 6. Database

Whole transactions, including commit (`db_transaction_duration`), during the profiling
run:

| | Local p50 / p95 / max | Docker p50 / p95 / max |
|---|---:|---:|
| Read transaction (289) | 1.0 / 2.1 / 4.6 ms | 1.5 / 4.0 / 9.7 ms |
| Write transaction (105) | 2.2 / 5.3 / 59.0 ms | 7.1 / 15.1 / 79.0 ms |
| History read, client side (20) | 8.7 / 16.6 ms | 9.9 / 20.9 ms |

Writes are about 3× slower in the container, where the database sits on a Docker volume
in the WSL 2 virtual disk. A Linux server with local disk was not measured.

On a database of 20,000 conversations and 20,000 tickets (112 MB), see
[section 10](#10-optimizations-made-in-stage-13).

## 7. Concurrency

`scripts/evaluate_concurrency.py`: the real application under uvicorn (one worker), real
HTTP on loopback, synthetic data, **mocked LLM**, rate limits off so that concurrency
rather than the limiter is tested. At most 4 (then 8) requests in flight
([concurrency-and-limits.md](concurrency-and-limits.md)).

Client-observed latency, ms, p50 / p90 / p99:

| Request | Concurrency 4 | Concurrency 8 |
|---|---|---|
| Chat turn, 16 conversations | 240 / 295 / 1,631 | 512 / 1,762 / 2,007 |
| Chat turn while staff read analytics | 253 / 317 / 430 | 522 / 610 / 799 |
| Chat turn while an FAQ is edited | 332 / 396 / 405 | 647 / 810 / 890 |
| Chat turn under write contention | 261 / 317 / 400 | 411 / 446 / 456 |
| Create a ticket | 65 / 112 / 139 | 169 / 269 / 327 |
| Save an FAQ edit | 53 / 81 / 81 | 96 / 161 / 161 |

- **More concurrency did not add throughput** on 4 CPUs: 64 chat turns took 5.2 s at
  concurrency 4 and 5.5 s at 8 (about 12 turns per second), while the median doubled.
- **Correctness held at both levels:** database integrity `ok`; 0 acknowledged replies or
  tickets missing; 0 duplicates from simultaneous retries; 0 replies mixing two FAQ
  versions; another customer's conversation always refused.
- **Staff analytics did not slow customers measurably** (253 against 240 ms p50).
- **SQLite's single writer queued writes but refused none** at 8 concurrent requests.
- The only non-`2xx` responses were the designed `409 REQUEST_IN_PROGRESS` for a second
  message to a busy conversation. Their `Retry-After` was cut from 180 s to 2 s in this
  stage, so the customer page now waits and retries by itself.

The Stage 12 load test (`scripts/load_test_local.py`, concurrency 8, 200 requests per
scenario, mocked LLM) gave chat p50 573 ms and p99 7.1 s with 0 errors in 1,000 requests
([test-and-quality-report.md](test-and-quality-report.md)). It predates the Stage 13
changes and was not repeated.

## 8. Restarts and recovery

`scripts/evaluate_restarts.py` against the production backend image with Compose's
settings ([shutdown-and-restart.md](shutdown-and-restart.md)):

| Scenario | Measured |
|---|---|
| Clean start to healthy | 6.9 s; semantic index ready 8.8 s later |
| Graceful stop, nothing running | 1.1 s |
| Request in flight, shorter than the 20 s graceful timeout | Completed (`200`), then the stop finished |
| Request in flight, longer than the timeout | Cancelled after 20 s; nothing stored; the client's retry answered on the first attempt |
| Backend crash (`SIGKILL` under write load) | Restarted by the policy, healthy 7.0 s after the kill; 0 of 26 acknowledged replies lost |
| Customer page crash | Restarted, healthy 6.8 s after the kill |
| Restart inside the deployment tests | Healthy again 17 s after the kill |

A restart resets the in-memory metrics and rate-limit counters, and costs 8–17 s (up to
about a minute on a cold disk) until semantic retrieval is back.

## 9. Memory, CPU and disk

| | Local | Docker | Container limit |
|---|---:|---:|---:|
| Backend before the model | 94 MiB | 76–91 MiB | 2 GB |
| Backend after the model | 591 MiB | 541 MiB | |
| Backend peak, profiling run | 591.5 MiB | 553.0 MiB (27% of the limit) | |
| Test process after about 1,100 concurrent requests | 606 MiB (c=4), 614 MiB (c=8), flat across 3 soak rounds | — | |
| Customer page, idle | 68 MiB | 51–59 MiB | 512 MB |
| Admin page, idle | 68–69 MiB | 51–53 MiB | 512 MB |

| CPU | Local | Docker (`--cpus 2.0`) |
|---|---:|---:|
| Backend idle | 0.0% of one core | 0.7% of one core |
| First chat (model load) | 16.7 CPU-s | 16.6 CPU-s |
| Per warm chat turn | 0.082 CPU-s | 0.083 CPU-s |
| CPU throttling during 30 back-to-back semantic searches | — | 0 of 25 periods |

| Disk | Size |
|---|---|
| Backend image | 1,507,746,069 bytes (1.40 GiB) |
| Customer and admin images | 572,503,653 bytes (546 MiB) each, the same layers |
| `models` volume | 92 MB (12 files) |
| Database with 20,000 conversations and 20,000 tickets | 112 MB; the five Stage 13 indexes added 3.4 MB (3.0%) |
| Container logs | At most 5 × 10 MB per container |

The memory soak was short (minutes). Leaks that need hours or many more conversations to
show would not appear in it.

## 10. Optimizations made in Stage 13

Each was measured before and after on the same data and is pinned by a regression test.
Nothing else was tuned.

| Change | Before | After | Source |
|---|---:|---:|---|
| Staff chat analytics, 30 days (merged queries, one-pass statistics) | 638 ms, 29 statements | 208 ms, 12 statements | [retrieval-and-database.md](retrieval-and-database.md#results) |
| Staff chat analytics, 7 days | 403 ms | 51 ms | same |
| Staff ticket analytics, 30 days | 253 ms, 16 statements | 76 ms, 6 statements | same |
| Ticket list, last page (offset 19,950; new index) | 739 ms | 13 ms | same |
| Ticket list, filtered by assignee | 50 ms | 6 ms | same |
| `model-init` download (only the 12 files the backend reads) | 977 MB | 91.6 MB | [embedding-model.md](embedding-model.md#fetching-only-what-the-backend-reads) |
| `Retry-After` for a busy conversation | 180 s (customer saw an error) | 2 s (page retries by itself) | [concurrency-and-limits.md](concurrency-and-limits.md#observed-failures) |
| Retry after a request cut off by a restart | refused for up to 170 s | answered on the first retry | [shutdown-and-restart.md](shutdown-and-restart.md#the-fix-leases-left-by-a-stopped-process) |

## 11. Not measured

| Item | Why | What to do before relying on it |
|---|---|---|
| Live OpenAI latency, token use and cost | Paid; not approved | A reviewed live evaluation with `RUN_OPENAI_LIVE_TESTS` and the cost accepted |
| How many turns can wait on OpenAI at once without delaying others | Needs the real LLM | Load test with the LLM on, on the target server |
| A Linux server without WSL 2; cgroup v2 | Not available | Run `scripts/profile_performance.py --target docker` and `scripts/evaluate_concurrency.py` on the target server |
| Browser rendering and Streamlit with many open sessions | Pages measured with `AppTest` and health endpoints only | Manual checklist: [frontend-manual-testing.md](frontend-manual-testing.md) |
| Nginx per-visitor limits under real traffic | Local run only | Observe `429` rates after go-live |
| Several backend workers or servers | Not supported (process-local state) | Not applicable while the deployment runs one worker |
| Long soak (hours, days) | Time | Watch backend memory in `docker stats` after go-live |

## 12. Reproducing the measurements

```powershell
pip install -r requirements.txt
python scripts/profile_performance.py --target local --output data/performance_profile_local.json
python scripts/measure_retrieval_db.py --output data/retrieval_db_profile_after.json
python scripts/evaluate_concurrency.py --concurrency 4 --output data/concurrency_results.json
python scripts/evaluate_concurrency.py --concurrency 8 --output data/concurrency_results_c8.json
python scripts/measure_performance.py --offline --llm mock --skip-frontend
```

```bash
# Where Docker runs (images built from deploy/Dockerfile, tag "restart")
python3 scripts/evaluate_restarts.py --tag restart --output restart-results.json
```

None of these sends a request to OpenAI. Raw results in this repository:
`data/performance_profile_local.json`, `data/performance_profile_docker.json`,
`data/retrieval_db_profile_before.json`, `data/retrieval_db_profile_after.json`,
`data/concurrency_results.json`, `data/concurrency_results_c8.json`,
`data/restart_results_before.json`, `data/restart_results_after.json`.
