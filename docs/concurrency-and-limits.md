# Concurrency and resource limits (Stage 13)

What happens when several customers and staff use the prototype at once, measured on
2026-10-20 on the development machine (Windows 10, 4 logical CPUs, 8 GB RAM, Python
3.13.3), and the operating assumptions that follow for a single-server deployment.
Raw results: [data/concurrency_results.json](../data/concurrency_results.json)
(concurrency 4) and [data/concurrency_results_c8.json](../data/concurrency_results_c8.json)
(concurrency 8). Script: [scripts/evaluate_concurrency.py](../scripts/evaluate_concurrency.py).
Regression test: [tests/test_concurrency_evaluation.py](../tests/test_concurrency_evaluation.py).

**No maximum number of users is stated here, on purpose.** The tests show how the
application behaves at the concurrency they used, on this machine, with a mocked LLM.
How many customers a deployment can serve depends on how often they send messages, the
real LLM's latency and the server's hardware, none of which these tests fix.

## How it was tested

```powershell
python scripts/evaluate_concurrency.py --concurrency 4 --output data/concurrency_results.json
python scripts/evaluate_concurrency.py --concurrency 8 --output data/concurrency_results_c8.json
```

- **The real application** under uvicorn on `127.0.0.1` (in the test's own process, one
  worker), real HTTP, a temporary SQLite database seeded from `data/faqs.json`, the
  locally cached embedding model.
- **The LLM is mocked:** a scripted stand-in that returns the approved FAQ answer
  instantly. The LLM code path runs (prompt, validation, grounding), but no request is
  sent and nothing is paid. Real OpenAI latency is not in these figures.
- **Synthetic data only:** generated questions, `@example.com` contacts.
- **Bounded:** at most 4 (then 8) requests in flight; the script refuses more than 8.
  Rate limits are off in this test server, so that concurrency, not the limiter, is
  exercised. Each run takes about 2 minutes, plus 18–64 s for the model's first load.

Scenarios, in order, then three soak rounds of scenario 1 to watch memory:

| # | Scenario | What runs at once |
|---|---|---|
| 1 | Multiple customer sessions | 16 conversations, 4 messages each, one message at a time per conversation |
| 2 | Concurrent chat requests | 4 (8) different messages sent at once to *one* conversation; then one message retried 4 (8) times at once with the same `client_message_id` |
| 3 | Simultaneous ticket creation | 16 support requests from 16 conversations; then one request submitted 4 (8) times at once with the same `client_request_id` |
| 4 | FAQ updates during retrieval | A staff editor changes an FAQ's answer in a loop while 4 (8) conversations ask about it; then two editors save the same version at once |
| 5 | Admin analytics during customer activity | Dashboard, chat analytics, ticket analytics and monitoring reads while 16 conversations chat |
| 6 | Database write contention | 10 rounds of: 4 new conversations, 4 chat turns, 4 tickets and an FAQ edit, all at once |

Then, directly in the database and through the API: integrity, isolation, idempotency and
consistency checks (below).

## Correctness: all checks passed at both concurrency levels

| Check | Concurrency 4 | Concurrency 8 |
|---|---|---|
| `PRAGMA integrity_check` / foreign key violations | ok / 0 | ok / 0 |
| Conversations whose messages are not numbered 1..n, alternating customer/reply | 0 | 0 |
| Processing leases left behind | 0 | 0 |
| Acknowledged replies (200) missing from the database | 0 of 378 | 0 of 394 |
| Retry keys stored twice; refused requests stored anyway | 0; 0 | 0; 0 |
| Acknowledged tickets missing; tickets nobody was told about | 0 of 57; 0 | 0 of 57; 0 |
| Duplicate reference numbers; tickets without a creation event | 0; 0 | 0; 0 |
| FAQ versions without a matching revision record | 0 | 0 |
| Answered replies whose text is not an approved version of the FAQ answer | 0 of 379 | 0 of 392 |
| **Isolation:** conversations holding another's message; another's token not refused (404) | 0 of 16 | 0 of 16 |
| **Idempotency:** distinct replies for one message retried at once | 1 | 1 |
| A retry after the first completed returns the stored reply (`Idempotent-Replayed`) | yes | yes |
| **Idempotency:** tickets for one `client_request_id` submitted at once | 1 | 1 |
| Two editors saving the same FAQ version | 1 saved, 1 `409 FAQ_VERSION_CONFLICT` | same |

In scenario 4, every answered reply equalled one of the versions the FAQ had during the
run: no reply mixed an old and a new version, and none was refused.

## Observed failures

**One defect, found and fixed.** A message sent to a conversation that is still
answering another one is refused with `409 REQUEST_IN_PROGRESS`, as designed (one
message per conversation at a time). Its `Retry-After` was the time left on the
conversation's processing lease: **180 seconds**, although the other message was
answered within a second. The customer page waits automatically only for up to
5 seconds, so a customer who double-sent saw an error instead of their reply.

The lease is a safety bound for a process that dies mid-answer, not the expected wait.
`Retry-After` is now at most **2 seconds** (`BUSY_RETRY_AFTER_SECONDS` in
[app/services/conversation_service.py](../app/services/conversation_service.py)). The
lease itself is unchanged, so a crashed answer still blocks the conversation until its
lease expires; clients are now told to check back sooner. Verified end to end against
the real backend and the real page client: before, the page showed an error at once
(`Retry-After 180`); after, it waited 2 s, retried by itself and showed the reply
(`test_the_customer_page_waits_out_a_busy_conversation_by_itself`). If the conversation
stays busy (a stuck answer), the page still stops after its two automatic retries and
shows "still being answered" with a "Try again" button, as before; two integration tests
that pinned "no automatic retry" now expect those two bounded retries.

**Expected refusals, not failures.** The only non-2xx responses in both runs were these
409s: 3 (4) and 7 (8) per burst of messages to one conversation, and 3 (4) and 7 (8)
among the same message retried at once (the in-progress duplicate). Each is retryable
and was followed by exactly one stored reply.

**Not observed:** no `503 SERVICE_UNAVAILABLE` from a locked database, no timeouts, no
`500`, no connection errors, in any scenario at either level. SQLite's single writer
did not refuse a write at 8 concurrent requests.

## Latency

Client-observed over loopback HTTP, milliseconds, median / 90th / 99th percentile:

| Request | Concurrency 4 | Concurrency 8 |
|---|---|---|
| Chat turn, 16 conversations (scenario 1) | 240 / 295 / 1,631 | 512 / 1,762 / 2,007 |
| Chat turn while staff read analytics (scenario 5) | 253 / 317 / 430 | 522 / 610 / 799 |
| Chat turn while an FAQ is edited (scenario 4) | 332 / 396 / 405 | 647 / 810 / 890 |
| Chat turn under write contention (scenario 6) | 261 / 317 / 400 | 411 / 446 / 456 |
| Create a conversation (scenario 6) | 34 / 51 / 63 | 39 / 53 / 64 |
| Create a ticket (scenario 6) | 65 / 112 / 139 | 169 / 269 / 327 |
| Save an FAQ edit (scenario 6) | 53 / 81 / 81 | 96 / 161 / 161 |
| Chat analytics, 30 days (scenario 5) | 122 | 126 |
| Ticket analytics, 30 days (scenario 5) | 74 | 73 |

Each p99 of scenario 1 is a single slow request among 64; it was not investigated further.

What the numbers show:

- **More concurrency did not mean more throughput.** Scenario 1's 64 chat turns took
  5.2 s at concurrency 4 and 5.5 s at 8 (about 12 turns per second either way, with the
  mocked LLM), while the median latency doubled. On 4 CPUs the server is busy with query
  embedding and Python work at 4 concurrent turns; further requests queue.
- **Staff analytics did not slow customers measurably:** chat median 253 ms with staff
  reading analytics against 240 ms without (concurrency 4).
- **FAQ edits during chat cost about 90 ms per turn** at concurrency 4 (332 against
  240 ms): each saved edit reloads the catalogue and brings the semantic index up to
  date, while turns continue on the snapshot they pinned.
- **Writes queue but are not refused:** ticket creation's median grew from 65 to 169 ms
  between concurrency 4 and 8, with no `503`.

## Memory

Resident memory of the test process (server and client together), MB, after each step:

| | Concurrency 4 | Concurrency 8 |
|---|---:|---:|
| Before the model | 94.3 | 94.7 |
| After the model's first load | 593.7 | 592.9 |
| After scenarios 1-6 | 600.7 → 606.5 | 607.0 → 612.7 |
| After soak rounds 1, 2, 3 | 606.2, 606.6, 606.0 | 613.7, 613.2, 614.0 |

About 13 MB (21 MB at concurrency 8) were added during the first scenarios, then memory
stayed flat across the soak rounds, within ±1 MB: no growth trend in about 1,100
requests per run. This is a short run. Leaks that need hours or many more conversations
to show would not appear here.

## Safe operating assumptions for the single-server prototype

These follow from the design and from what was measured. They are assumptions to
operate within, not guarantees beyond it:

1. **One server, one backend worker.** The embedding model, the FAQ index, the FAQ
   catalogue snapshot, the rate limits and the metrics live in one process's memory
   (security audit SEC-09/SEC-10, [docs/embedding-model.md](embedding-model.md)). Several
   workers or servers were not tested, and their consistency is not claimed.
2. **SQLite on a local disk, one writer at a time.** Writes queued but were never refused
   at up to 8 concurrent requests. A locked database beyond the 5-second busy timeout
   would answer `503` with `Retry-After: 1`, which did not happen here
   ([docs/sqlite-review.md](sqlite-review.md)).
3. **Concurrency beyond about 4 in-flight requests adds latency, not throughput, on a
   4-CPU machine.** On other hardware, run `scripts/evaluate_concurrency.py` before
   relying on these figures.
4. **A real LLM changes the picture.** Each answered turn will wait for OpenAI (seconds,
   not measured here). During that wait the turn holds its conversation's processing
   lease and one of the worker threads that run blocking routes (AnyIO's default limit is
   40). It holds no database lock: no transaction is open while the LLM runs
   (`tests/test_sqlite_workload.py`). How many turns can wait on OpenAI at once without
   delaying others was not measured.
5. **One message per conversation at a time.** Further messages get `409` and a
   `Retry-After` of at most 2 s; the customer page retries by itself. Retries with the
   same ID never create a second message or ticket.
6. **The production rate limits apply** (off in these tests): 20 chat messages per
   minute per conversation, 20 new conversations and 120 requests per minute per IP,
   5 support requests per minute per conversation (`.env.example`). They are process-local, which
   assumption 1 makes sufficient.
7. **Memory:** about 0.6 GB for the backend after the model loads, stable under this
   load, inside the 2 GB container limit. Each Streamlit page adds its own (about
   50–70 MB idle, [docs/performance-profile.md](performance-profile.md)) plus its
   sessions.
8. **Before going beyond a prototype,** measure the real LLM's latency under load, run a
   longer soak, and test on the target server. Growth beyond one server needs a
   database with concurrent writers and shared state (a decision, not a tuning step).
