# Retrieval and database operations: review and optimization (Stage 13)

Measured on 2026-10-20 on the development machine (Windows 10, 4 logical CPUs, Python
3.13.3, SQLite 3.49.1; the Docker image has SQLite 3.46.1). Every figure below was
measured; nothing is estimated. Raw results:
[data/retrieval_db_profile_before.json](../data/retrieval_db_profile_before.json) (the
code before this task) and
[data/retrieval_db_profile_after.json](../data/retrieval_db_profile_after.json) (after),
on identical data. Regression tests:
[tests/test_retrieval_db_optimizations.py](../tests/test_retrieval_db_optimizations.py).

## How it was measured

```powershell
python scripts/measure_retrieval_db.py --output data/retrieval_db_profile_after.json
```

[scripts/measure_retrieval_db.py](../scripts/measure_retrieval_db.py) runs in one process
on a temporary SQLite file, with no model and no network:

- **Volume:** 20,000 conversations (140,098 messages) and 20,000 support tickets (with
  20,000 contacts, 39,679 events, 23,853 notes) spread over 365 days, the ticket
  retention period, plus 20,000 audit events. Tickets older than two weeks are mostly
  resolved or closed, as in a working queue. `ANALYZE` is run, as a long-lived database
  would have statistics. Fixed random seed, so both runs see the same rows.
- **Operations:** each through the real repositories and services, warmed once, then
  timed 7 times (median reported). Every SQL statement they run is captured and
  explained with `EXPLAIN QUERY PLAN`.
- **Per chat turn and per FAQ edit:** the real application (TestClient) with a
  deterministic stand-in encoder that counts what it embeds.
- **Retrieval scaling:** keyword search and the semantic matrix product over the 30
  real FAQs and a synthetic catalogue of 2,030.

To compare fairly, the "before" run used this script against the original application
code (`git stash` of the two changed files), then the "after" run against the changes.

## Results

| Operation | Before | After | Statements before → after |
|---|---:|---:|---:|
| Staff analytics, chat view, 30 days | 638 ms | **208 ms** | 29 → 12 |
| Staff analytics, chat view, 7 days | 403 ms | **51 ms** | 29 → 12 |
| Staff analytics, ticket view, 30 days | 253 ms | **76 ms** | 16 → 6 |
| Dashboard overview | 56 ms | **34 ms** | 9 → 9 |
| Ticket list, first page (50) | 30 ms | **9 ms** | 3 → 3 |
| Ticket list, last page (offset 19,950) | 739 ms | **13 ms** | 3 → 3 |
| Ticket list, filtered by assignee | 50 ms | **6 ms** | 3 → 3 |
| Ticket list, created in the last 7 days | 34 ms | **4.5 ms** | 3 → 3 |
| Ticket list, open queue | 5.9 ms | 5.2 ms | unchanged plan |
| A ticket's events / notes | 0.9 / 1.3 ms | 1.3 / 1.2 ms | unchanged |
| Conversation history page (50) | 2.4 ms | 2.5 ms | unchanged |
| Recent history for a chat turn (10) | 1.6 ms | 2.5 ms | unchanged |
| Audit log, first page | 6.2 ms | 3.1 ms | unchanged |

Operations whose queries did not change moved by under a millisecond either way: run-to-run
variation, not an effect.

**Cost of the new indexes:** +3.4 MB (+3.0%) on this 112 MB database, measured after
`VACUUM`. Each new conversation and ticket now updates one, respectively four, more
index entries; chat turns write messages, whose indexes did not change.

## What changed

### 1. Staff analytics: the same results with fewer, cheaper statements

[app/repositories/analytics_repository.py](../app/repositories/analytics_repository.py):

- **The chat view ran the whole ticket report for three numbers.** `ticket_activity`
  (support tickets, sales leads, tickets after a chat offer) called `ticket_metrics`:
  14 statements, including two percentile computations. It is now one grouped query.
- **Duration statistics in one pass.** The median and 90th percentile used to re-run
  their whole query, the message self-join for latency, once per statistic (3 times in
  all). One statement now numbers the sorted values with window functions and reads
  every statistic from them. Same definition: the values at positions `int(0.5 × n)`
  and `int(0.9 × n)` of the ascending order. On the 90-day experiment database the
  latency statistics took 922 → 391 ms, with all five values identical.
- **Ticket breakdowns from one pass.** Type, status, priority, lead category, "after
  an offer" and per-day counts of the tickets created in the range came from six
  queries; now one grouped query. The backlog (by status, unassigned, oldest) came from
  three; now one. `ticket_metrics`: 14 → 5 statements.
- **Chat replies by decision and by offer from one pass**, and customer messages
  counted with `role = 'user'`, so SQLite seeks the `(role, created_at)` index instead
  of scanning it.

### 2. Indexes, each justified by a measured query

[app/database/models.py](../app/database/models.py). Created automatically on an
existing database at the next start (`Database.init_schema` creates missing indexes),
which a test checks.

| Index | Serves | Measured effect |
|---|---|---|
| `ix_session_created_at` (`conversation_sessions.created_at`) | Conversations started in a range (analytics, dashboard) | 10.9 → 1.0 ms; was a full scan |
| `ix_ticket_created` (`support_tickets.created_at, ticket_id`) | The staff ticket list's order; tickets created in a range | Last page 676 → 3.7 ms, first page 22 → 0.7 ms (raw query); no sort of the whole table per page |
| `ix_ticket_assignee_created` (`assigned_to, created_at`) | The list filtered by assignee (`?assigned_to=`) | 23 → 0.7 ms (raw query) |
| `ix_ticket_resolved_at`, `ix_ticket_closed_at` | Tickets resolved / closed in a range (analytics) | 20 → 0.2 ms each; were full scans |

**Rejected:** an index on `conversation_messages.created_at`. SQLite did not use it: it
already skip-scans the existing `(role, created_at)` index, and querying each role
explicitly makes that a direct seek. It would have cost 0.4 s to build and the largest
size increase.

**Caveat found while measuring:** an index is not always faster. On a first, less
realistic dataset (90 days, statuses uniformly random, so two thirds of all tickets
looked "active"), counting through an index that matches a large share of the table was
slower than scanning it (backlog: 141 ms through the status index against 32 ms
scanning). With a realistic queue, where the backlog is a small fraction, the index
wins. The figures above are from the realistic dataset; a queue where most tickets stay
open would see less benefit from them.

## Reviewed and left as they are

| Area | Measured | Decision |
|---|---|---|
| **FAQ keyword search** | 0.25 ms over the 30 FAQs; 23–27 ms over 2,030 synthetic FAQs (the two runs) | Kept. Normalised FAQ text is already cached per FAQ object, so a search processes only the query. It is linear in the catalogue size, which matters only at thousands of FAQs |
| **Semantic retrieval** | Matrix product 0.01–0.02 ms (30 FAQs), 0.50–0.53 ms (2,030); the query embedding (17–28 ms, [docs/performance-profile.md](performance-profile.md)) dominates | Kept. One embedding call per chat turn, measured |
| **Hybrid retrieval** | One query embedding and one keyword search per turn | Kept |
| **Embedding cache** | An answer edit embeds 0 texts; a question edit embeds 1. The index is then current: no rebuild in the next 12 chat turns | Kept ([docs/embedding-model.md](embedding-model.md)) |
| **FAQ version synchronisation and stale evidence** | Each retrieval pins one catalogue snapshot; the index is rebuilt only for a different revision; evidence changed during an answer is never served | Kept. Covered by `tests/test_faq_update_consistency.py` and `tests/test_semantic_search_service.py` |
| **Conversation history** | A chat turn reads only the last 10 messages (1.6–2.5 ms, an index seek on `(session_id, sequence)`); the history endpoint pages (≤ 100 per page); the page loads at most its newest messages | Kept. Nothing at runtime loads a whole conversation. The page asks for the first page once to learn the total before fetching the newest: one extra request for long conversations, left as it is |
| **Ticket pagination** | `OFFSET` pages, at most 100 per page. With the new index a deep page walks the index: 13 ms at offset 19,950 | Kept. Keyset pagination (after the last `created_at, ticket_id`) would make every page constant-time; not needed at this volume |
| **Per chat turn** | 17 SQL statements in 6 short transactions (8 SELECT, 2 UPDATE, 1 INSERT, plus BEGINs) | Kept. Each step re-checks the session and token in its own short transaction, so no transaction is open while retrieval or the LLM runs; merging them would trade that away |
| **Ticket events and notes** | 0.9–1.3 ms for a ticket with 1–3 of each | Not bounded: a ticket returns all of its events and notes. They grow only through staff actions on one ticket, so this was left; a cap would change the API |

## Regression tests

[tests/test_retrieval_db_optimizations.py](../tests/test_retrieval_db_optimizations.py),
17 tests. On the original code, the 12 result tests pass and the 5 optimization tests
fail, so the tests check the optimizations without changing what is computed:

- Analytics equal a plain Python computation over the same seeded rows: conversations,
  replies by decision and offer, every ticket breakdown, resolutions, closures, the
  backlog, the daily series, and the chat view's ticket counts.
- The one-pass duration statistics follow the definition, on seeded data and on edge
  cases (no values, one, two, ties, negative values dropped).
- Statement counts: ticket report 5, chat view's ticket counts 1, chat analytics ≤ 12.
- The new indexes are created on an existing database at startup, and SQLite uses them
  for the ticket list (no sort), the assignee filter and the time-range counts.
- The ticket list still returns every ticket exactly once, in creation order, across
  pages.
