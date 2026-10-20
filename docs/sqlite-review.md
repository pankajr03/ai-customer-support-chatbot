# SQLite production suitability (Stage 13)

Should this deployment keep SQLite? This review looks at the application's actual
database workload, the configuration as applied, measured behaviour under concurrent
writes, migrations and backups.

**Date:** 2026-10-19. **Evidence:** code reading; tests (`tests/test_sqlite_workload.py`,
`tests/test_database_operations.py`); measurements inside the backend image on the real
Docker volume (Linux, ext4, SQLite 3.46.1).

> **Decision: keep SQLite** for the single-server deployment. Nothing measured here
> justifies a change: writes are short and never span an LLM call, and concurrent
> writers lost nothing. Throughput is far above what a support chat needs. It must
> stay on a local disk with one backend process, and it needs scheduled backups.
> [Section 9](#9-when-postgresql-becomes-appropriate) lists the conditions under which
> PostgreSQL becomes the right choice. **No migration was made.**

## Contents

1. [The workload](#1-the-workload)
2. [Configuration as applied](#2-configuration-as-applied)
3. [Transactions and the LLM call](#3-transactions-and-the-llm-call)
4. [Concurrent writes, measured](#4-concurrent-writes-measured)
5. [Storage: persistent, local, never on a network filesystem](#5-storage-persistent-local-never-on-a-network-filesystem)
6. [Migrations](#6-migrations)
7. [Backups](#7-backups)
8. [Single-server limitations](#8-single-server-limitations)
9. [When PostgreSQL becomes appropriate](#9-when-postgresql-becomes-appropriate)
10. [Changes made in this review](#10-changes-made-in-this-review)

---

## 1. The workload

Every write goes through a repository, in its own short transaction
(`Database.write()`: `BEGIN IMMEDIATE`, commit or roll back).

| Operation | Write transactions | Contents | Frequency |
|---|---|---|---|
| Start a conversation | 1 | Session row with the token hash | Once per customer visit |
| **Chat turn** | **2** (plus about 6 short reads) | 1: take the processing lease. 2: store the customer message **and** the reply, release the lease | Per customer message: **the dominant load** |
| Close or delete a conversation | 1 | Status change, or delete with its messages | Rare |
| Support request or sales lead | 1 | Contact, ticket, first event and audit entry together | Rare |
| Staff ticket change (status, assignment, note) | 1 | Change, ticket event and audit entry together | Staff actions |
| FAQ create, edit, (de)activate | 1 | FAQ, revision and audit entry together; the catalogue reload and index refresh run **after** the commit | Occasional staff actions |
| Refused staff request; contact-detail view | 1 | One audit entry | Staff actions |
| Retention cleanup (startup, `cleanup_conversations`) | Batches of 500 | Expired sessions and their messages | Startup, or scheduled |
| Ticket anonymization | 1 per ticket | Personal data replaced, event and audit recorded | Scheduled |

Reads (history, FAQ catalogue, analytics, tickets) never take the write lock; in WAL
mode they run alongside a writer.

Measured earlier on the development machine (Stage 9,
[docs/performance-and-evaluation.md](performance-and-evaluation.md)): a warm chat turn
spends about 20 ms in storage. Write transactions take 4.3 ms at the median, with rare
spikes to 0.3 s (a checkpoint or disk flush).

## 2. Configuration as applied

Read from the live database on the Docker volume:

| Setting | Value | Assessment |
|---|---|---|
| `foreign_keys` | `1` on every connection | **Enforced, not just declared:** inserting a message for a conversation that does not exist fails with `FOREIGN KEY constraint failed` (`test_foreign_keys_are_enforced_not_just_declared`) |
| `journal_mode` | `wal` | Right for this workload: readers never block the writer and vice versa; one writer at a time. Requires a local filesystem (section 5) |
| `busy_timeout` | `5000` ms | A writer waits up to 5 s for the lock, then fails with `database is locked`. The API answers `503 SERVICE_UNAVAILABLE` (with `Retry-After` for conversation requests), and clients retry with their idempotency keys. Under 8 continuously writing processes the slowest wait was 2.1 s (section 4). Kept |
| `synchronous` | `2` (FULL) | Every commit is on disk before it returns, so a power loss loses nothing committed. `NORMAL` would be faster in WAL mode but can lose the last transactions; at the measured rates the speed is not needed |
| `secure_delete` | `1` | Deleted customer data is overwritten, not left in free pages |
| `wal_autocheckpoint` | `1000` pages (4 MB) | The WAL is folded back into the database automatically; retention cleanup also truncates it explicitly |
| Write transactions | `BEGIN IMMEDIATE` | The write lock is taken at the start, so a read-then-write (next message number) cannot interleave with another writer |

All are set per connection in `app/database/connection.py`, and checked by
`test_every_connection_gets_the_documented_settings`.

## 3. Transactions and the LLM call

**No write transaction is open while the LLM is called.** A chat turn
(`CustomerChatService.chat`):

1. **Write:** take the conversation's processing lease (`claim_processing`), then commit.
2. **No transaction:** load history (short reads), rewrite the question, retrieve,
   decide, and call the LLM. This step takes seconds.
3. **Write:** store both messages and release the lease (`add_exchange`), then commit.
   If another request took the lease meanwhile, nothing is stored.

The lease is a row in the database, not a held lock, so it costs other customers
nothing. FAQ edits follow the same rule: commit first, then refresh the semantic index,
which re-embeds questions and can take seconds of CPU.

**Measured** (`test_no_write_transaction_is_open_during_the_slow_part_of_a_turn`): the
answer pipeline was slowed by 1 s to stand in for an LLM call. During that second,
another customer's write (starting a conversation) completed in well under 0.5 s, and
every write transaction of the turn stayed under 0.5 s. **Mutation check:** holding a
write transaction across the slow step made the test fail (the other write waited
1.0 s).

## 4. Concurrent writes, measured

Separate processes (each with its own engine and connections, as separate server
processes would have) performing the real write sequence of a chat turn, as fast as
they can, on one database file on the Docker volume (Linux, ext4):

| Writer processes | Chat turns | Write transactions | Throughput | Slowest write (lock wait included) | Mean | Errors | Afterwards |
|---|---|---|---|---|---|---|---|
| 1 | 100 | 300 | 159 writes/s (53 turns/s) | 54 ms | 5.9 ms | 0 | Integrity `ok`, no message-number gaps |
| 4 | 400 | 1,200 | 225 writes/s (75 turns/s) | 1,138 ms | 15.0 ms | 0 | Integrity `ok`, no gaps |
| 8 | 800 | 2,400 | 235 writes/s (78 turns/s) | 2,136 ms | 29.5 ms | 0 | Integrity `ok`, no gaps |

Also in the test suite (4 processes, Windows): 0 errors, all rows present, foreign keys
and integrity intact, the slowest write under half the busy timeout.

Reading these numbers:

- **SQLite serialises writers, as expected.** Throughput levels off around 235 writes/s
  (about 78 chat turns per second, or 4,700 per minute). That is far above a support
  chat's load, where every turn also spends seconds in the LLM.
- **Lock waits grow with continuous contention.** The slowest write took 2.1 s with 8
  processes writing nonstop. That is still below the 5 s busy timeout, but a growing
  share of it. Real traffic is not nonstop: a customer writes twice per turn and
  waits seconds in between.
- The Stage 12 load test (Windows, the whole application, 8 parallel chats) recorded a
  chat p99 of 7.1 s, attributed to waiting for the write lock. It was not repeated on
  Linux here; these Linux measurements isolate the database and show much shorter
  waits.
- **Not measured:** sustained production traffic, and a disk slower than this
  machine's.

## 5. Storage: persistent, local, never on a network filesystem

- **Persistent:** the database is in the named Docker volume `db`
  (`DATABASE_URL=sqlite:////var/lib/chatbot/conversations.db`, fixed in
  `compose.yaml`). It survives container recreation and `docker compose down` and
  `up` (verified in Task 6). An in-memory database is refused in production (Task 7).
- **Local disk only.** SQLite relies on file locks, and WAL on shared memory. On
  network filesystems (NFS, SMB/CIFS, sshfs, cluster filesystems) and on 9p, which
  carries Windows drives into WSL 2, both are unreliable: two writers can each believe
  they hold the lock, and the file can be corrupted.
- **New guard:** `Database` reads the mount table and identifies the filesystem holding
  the database folder. On a network filesystem, **production refuses to start**:
  > Cannot start: … the database folder is on a 9p filesystem, where SQLite's locking
  > is unreliable and the file can be corrupted. Put DATABASE_URL on a local disk (in
  > Docker: a named volume).

  Verified in the backend container against a real 9p bind mount from Windows, and in
  tests for NFS, CIFS and 9p. Development logs the same message as an error and
  continues. On the Docker volume the detected filesystem is `ext4`, which is accepted.
- **Do not** bind-mount the database folder from a NAS, a cloud file share, or (on
  Windows) a Windows drive into WSL or Docker. Use a named volume or a local disk.

## 6. Migrations

`Database.init_schema()` runs at every start, before the FAQ catalogue loads:

| Situation | What happens |
|---|---|
| New database | All tables, indexes and the audit table's append-only triggers are created |
| A table lacks a **nullable** column added later | `ALTER TABLE … ADD COLUMN`; rows kept |
| A support table's definition changed (for example a new allowed status) | Rebuilt in one transaction: rename, create, copy every row, drop, foreign-key check. Any failure rolls everything back and the app refuses to start; nothing is lost. Triggers are preserved (SEC-23) |
| A **required** column is missing | The app refuses to start and names the column; nothing is changed |
| Code older than the database | No downgrade. Since Stage 13 (Task 24) the older code refuses to start when a rebuild would discard columns it does not know, changing nothing; restore the backup taken before the upgrade ([docs/deployment-rollback.md](deployment-rollback.md)) |
| Any change to existing tables (a column, a rebuild) | Preceded by an automatic, verified backup into the backups folder; no upgrade without it |

Assessment: safe, and tested (upgrades from older files are in the repository tests;
an upgrade of a real older backup was verified in the readiness assessment). It is
also limited:

- **No version table:** the code compares definitions, so it cannot express a data
  migration (for example splitting a column), and it cannot downgrade.
- A table rebuild holds the write lock while it copies rows. That is fine at this size,
  but on a large table it lengthens startup, during which the app is not ready.
- It runs in whichever process starts. With the single backend process here that is
  fine; several processes starting at once were never tested.

**Procedure for every upgrade:** take a backup (`docker compose exec backend python -m
scripts.backup_database`) before deploying new code. The backup is the rollback.

## 7. Backups

| Requirement | How |
|---|---|
| Consistent while running | SQLite's online backup API, never `cp` (the newest transactions can still be in the `-wal` file) |
| Command | `python -m scripts.backup_database` (in the deployment: `docker compose exec backend python -m scripts.backup_database`): verified, timestamped and never overwriting, with a checksum manifest and owner-only permissions, keeping the newest 14 (`--keep`). Full guide: [docs/backup-and-restore.md](backup-and-restore.md) |
| Self-contained file | The copy is switched from WAL to an ordinary journal, so each backup is one `.db` file with no `-wal` or `-shm` beside it (this changed the FAQ import's backups too) |
| Taken under load | Verified on the Docker volume: a backup taken while 4 processes were writing completed and verified. The writers had 0 errors (their slowest write was 2.7 s while the backup ran), and the live file was intact afterwards |
| Schedule | **Not automatic.** Run it from the host's scheduler, for example daily: `0 3 * * * cd /srv/support-chatbot/deploy && docker compose exec -T backend python -m scripts.backup_database` |
| Off the server | **Required.** The `backups` volume is on the same disk. Copy the files to separate, encrypted storage: they hold customer contact details and conversations in clear |
| Restore | `python -m scripts.restore_database`: a test restore into a separate database by default; replacing the active one only with `--replace-active --confirm <file name>`, the backend stopped, after a safety backup. Procedure: [docs/backup-and-restore.md](backup-and-restore.md), section 3 |
| Retention conflict | Backups keep data that retention has already deleted from the live database. Keep backups no longer than the retention policy allows |

Point-in-time recovery (restoring to any minute) is **not** possible with SQLite file
copies: at most, the last backup is restored. If that is required, see section 9.

## 8. Single-server limitations

These follow from SQLite and are accepted for this deployment:

1. **One machine.** The file cannot be shared between servers. There is no failover:
   if the server is lost, service resumes only from the last off-server backup.
2. **One backend process.** Leases and idempotency keys work across processes, but the
   FAQ catalogue snapshot, the semantic index, rate limits and the LLM circuit breaker
   are per process (SEC-09, SEC-10). Run `--workers 1` and one backend container.
3. **One writer at a time.** About 235 writes/s on this machine, with lock waits
   growing under sustained contention (section 4).
4. **No encryption at rest.** The file and its backups are plain; rely on disk
   encryption and access to the host.
5. **Backups are whole-file copies**, with no point-in-time recovery (section 7).
6. **Schema changes** happen at startup, with no downgrade (section 6).

## 9. When PostgreSQL becomes appropriate

Move to PostgreSQL (and shared state for rate limits and the FAQ catalogue revision)
when **any** of these becomes true. Each is observable:

| Trigger | How you would notice |
|---|---|
| More than one backend process or server is needed (capacity, or availability during deployments) | Planning; CPU saturated on the single backend |
| Failover or high availability is required | A requirement: no downtime on server loss |
| Write-lock waits threaten the busy timeout under real traffic | `503 SERVICE_UNAVAILABLE` responses caused by `database is locked` in the logs; chat latency outside the LLM growing with traffic |
| Point-in-time recovery, or a recovery point measured in minutes, is required | Backup requirements |
| Managed encryption at rest, access auditing or replication for reporting | Compliance requirements |
| The database grows to where startup rebuilds or whole-file backups take too long | Backup duration; startup time after upgrades |

None of these holds today. The move would be contained: only the repositories talk to
the database. But it needs versioned migrations (for example Alembic), a data copy, a
load test of its own, and the per-process state made shared, so it is a project, not a
setting. **It was deliberately not started here.**

## 10. Changes made in this review

| Change | Why | Verified |
|---|---|---|
| Production refuses a database folder on a network filesystem (`app/database/connection.py`); development logs an error | SQLite on NFS, SMB or 9p can be corrupted | Real 9p mount in Docker: refused with the message above. Tests for NFS, CIFS, 9p and mount-table parsing |
| `scripts/backup_database.py`: online backup, verified, with retention | There was no standalone backup command | Tests; Docker: under load and on the live database |
| Backups are self-contained single files | Copies kept WAL mode, so `-wal` and `-shm` files appeared next to them | `test_backup_copies_verifies_and_keeps_the_newest` |
| `tests/test_sqlite_workload.py` | Connection settings, foreign-key enforcement, no write lock during the slow step (with a mutation check), 4 concurrent writer processes | 4 tests |

Not changed, deliberately: the busy timeout (5 s), `synchronous=FULL`, WAL, and the
database engine.
