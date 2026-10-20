# Deployment rollback strategy

How to recover from a failed application update, and what the application does to make
that safe. The commands are in the runbook ([docs/deployment-runbook.md](deployment-runbook.md),
sections 3.4 and 4.7); this document explains the strategy behind them and what was
tested.

**The rule:** a code rollback never reverses a database change. Going back to an older
release is safe only while the database is still one that release understands. When an
update changed the schema, the way back is a **restore of the backup taken before the
update**, a deliberate, manual and destructive step. The application never does it on
its own.

## 1. Recording the deployed version

| Where | What | How to read it |
|---|---|---|
| `deploy/.env` | `IMAGE_TAG`: the release's Git commit (runbook 2.2), so every release has its own images | `grep IMAGE_TAG deploy/.env` |
| The images | `APP_REVISION` (environment) and the label `org.opencontainers.image.revision`, both set from `IMAGE_TAG` at build time | `docker image inspect support-chatbot-backend:<tag> --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'` |
| The backend's log | At startup: *AI Customer Support Chatbot 0.1.0 (revision 1a2b3c4) started …* | `docker compose logs backend \| grep started` |
| **The database** | Table `app_releases`: a row each time a release with a new version, revision or schema starts against it (time, version, revision, schema fingerprint). Kept with the data, so **every backup records which releases wrote it** | `docker compose exec backend python -m scripts.release_history` |

`scripts/release_history.py` prints that history with the current schema, and the backups
taken before schema upgrades with their schema fingerprint. The fingerprint is the same
as the `schema_sha256` in backup manifests, so a backup can be matched to the release that
wrote it.

## 2. Backups before migrations

Two layers, both verified (integrity, foreign keys, every table it had) and kept in the
`backups` volume:

1. **Manual, before every update** (runbook 3.4, step 1):
   `docker compose exec backend python -m scripts.backup_database`. This is the
   rollback point for the whole release, schema change or not.
2. **Automatic, before every schema change to existing tables.** When the backend starts
   and is about to add a column or rebuild a table, it first takes a verified backup of
   the database as it is. Its manifest says `"reason": "before schema upgrade"`, and the
   log says *Backed up the database before upgrading its schema (add …; rebuild …)*.
   **If that backup fails, nothing is upgraded** and the backend does not start. Creating
   new tables or indexes changes no existing row and needs no backup.

Backups are never deleted by an upgrade or a rollback. The usual retention applies
(`BACKUP_KEEP` newest, docs/backup-and-restore.md).

## 3. Previous images and release references

- Each release is built under its own `IMAGE_TAG`, so the previous release's images stay
  on the server until removed (runbook 3.4 removes only releases **older** than the
  previous one).
- The release's Git tag or commit stays in the repository (`git checkout <previous>`),
  so the compose files match the images, and images can be rebuilt if they were deleted.
- `docker image ls support-chatbot-backend` lists the releases still available.

## 4. Database compatibility between releases

What an **older** release does when it starts against a database a **newer** release has
upgraded. The first four rows are tested in `tests/test_rollback_safety.py`, the first
and fourth also on Docker (section 7). The constraint rows rest on the table rebuild's
own tests: a rebuild that rows would violate changes nothing
(`tests/test_ticket_repository.py::test_a_rebuild_that_would_break_data_changes_nothing`),
and a rebuild to another definition keeps every row
(`test_a_table_rebuild_is_preceded_by_a_backup`).

| The newer release … | The older release on rollback | Data |
|---|---|---|
| changed no schema | Starts normally | Intact |
| added a table or an index | Starts; ignores it | Intact; used again after a new upgrade |
| added a nullable column to a conversation table | Starts; ignores it (rows it writes leave it empty) | Intact |
| added a column to a support, FAQ or audit table (the tables rebuilt on a definition change) | **Refuses to start**: *the database has column(s) this release does not know (support_tickets.satisfaction_score) … Nothing was changed* | Intact. Recovery: section 5 |
| changed a constraint, and rows use a value the older release does not allow | Refuses to start: the table rebuild fails and rolls back | Intact |
| changed a constraint, and no row uses a new value | Starts; rebuilds the table to its older definition, every row copied | Intact |
| removed or renamed a column, or transformed data | Not supported by the schema upgrade at all: needs a hand-written migration and its own rollback plan | — |

**Before this was in place**, the fourth row silently **dropped the new column and its
data**: an older release rebuilt the table to its own definition and copied only the
columns it knew. It was reproduced before the fix: a ticket's `satisfaction_score`
column disappeared when the older code started. Now the startup plans its changes first,
and refuses a rebuild that would discard any column.

There is still no schema version number and no downgrade: the database is compared with
the code's table definitions. This applies to rollbacks *to* a release that includes this
protection. Going back to a release from before it (before Stage 13, Task 24) has the old
behaviour, so after a schema change, restore the backup instead (section 5).

## 5. Rollback steps

First decide which case you are in. The update's backend log tells you: *Added column*,
*Upgraded table* or *Backed up the database before upgrading its schema* mean the schema
changed; no such line means it did not.

```bash
docker compose logs --no-log-prefix backend | grep -E 'Added column|Upgraded table|before upgrading its schema'
docker compose exec backend python -m scripts.release_history     # or with "run --rm --no-deps" if it is down
```

### A. The update changed no schema: switch the images back

Data is untouched. Runbook 4.7, "Release only": set `IMAGE_TAG` to the previous tag, check
out the previous release, `docker compose up -d`. Tested on Docker (section 7).

### B. The update changed the schema: restore, then switch back

**⚠️ Destructive: everything written after the pre-update backup is lost** (conversations,
support requests, FAQ edits, audit events). If the older release starts and refuses, as
in section 4, it has changed nothing, so there is time to decide.

1. **Decide whether to roll forward instead.** Fixing the newer release and deploying it
   again keeps all data. Prefer it when what was written since the update matters.
2. Note what will be lost: the time of the pre-update backup, and the support requests
   created since (the dashboard's ticket list, or the audit log), so they can be followed
   up by other means.
3. Stop the backend: `docker compose stop backend`.
4. Switch to the previous release (runbook 4.7: `IMAGE_TAG`, `git checkout`), **without
   starting it yet**.
5. Find the backup: the manual one from runbook 3.4 step 1, or the automatic one
   (`scripts.release_history` lists them; its schema matches the previous release's row).
6. Restore it with the **previous** release's image (runbook 3.6, replace the active
   database). It first saves the current, newer database as a safety backup, so this step
   can be undone by restoring that safety backup with the newer release.
7. Start: `docker compose up -d`. Check `docker compose ps`, `/healthz`, and the release
   history.
8. Apply retention again (runbook 3.6), and tell the people whose data was lost.

Tested end to end on Docker (section 7).

## 6. Deliberately not automatic

- **No automatic schema downgrade.** Code cannot know what a newer release meant by its
  changes. It refuses instead of guessing.
- **No automatic restore.** A restore loses data written after the backup, so a person
  decides, with confirmation (`--replace-active --confirm conversations.db`).
- **No automatic rollback of a deployment.** A failed update leaves the backend down or
  unhealthy, visibly. `restart: unless-stopped` restarts a refusing backend in a loop,
  harmlessly (each start refuses again, changing nothing). Stop it while you work.
- **Nothing deletes data or backups** as part of an update or a rollback.

## 7. Tested locally

**Unit tests** ([tests/test_rollback_safety.py](../tests/test_rollback_safety.py), 10, in
the normal suite):

- an older release refuses to discard a newer column, changes nothing and takes no backup;
- extra tables, indexes and conversation-table columns are left alone;
- adding a column and rebuilding a table are each preceded by a verified backup of the
  database as it was;
- no backup for a new database, new tables or new indexes;
- a failed backup stops the upgrade with nothing changed;
- a pre-upgrade backup of an older schema verifies and restores with the newer code;
- releases are recorded once each, the application records its `APP_REVISION`, and
  `release_history` shows them.

**On Docker** ([tests/deployment/test_06_rollback.py](../tests/deployment/test_06_rollback.py),
opt-in, [docs/deployment-tests.md](deployment-tests.md)), with the production images, on
2026-10-20. Two more backend images were built from copies of the working tree: `v1b`
(the same code under another revision) and `v2` (one more column in `support_tickets`).

| Step | Result |
|---|---|
| v1 → v1b → back to v1 (no schema change) | Healthy each time; the conversation from before is intact; both revisions in `release_history` |
| v1 → v2 (schema change) | A verified backup first, logged with what was changed; then the column was added |
| A conversation written by v2, then rollback to v1 | v1 **refused to start**, naming `support_tickets.satisfaction_score`; the database still had the column |
| Manual recovery: stop, restore the pre-upgrade backup with v1, start | Healthy; the column gone; the conversation written by v2 **gone** (`404`), as documented; `/healthz` ready |

All 28 deployment tests passed in 6 min 25 s (the three rollback tests took 22 s of
setup and about 90 s). The extra images were removed afterwards.

## 8. Limits

- No schema version number and no downgrade migrations: compatibility comes from comparing
  definitions (section 4). A real migration tool (for example Alembic) becomes necessary
  for renames, removals and data transformations, and before moving off SQLite.
- The release history starts with the first release that records it; earlier deployments
  are not in it.
- Rolling back to a release from before this protection after a schema change can still
  drop columns silently: restore the pre-update backup instead (section 5, B).
- Only one backend process upgrades the schema (the deployment runs one). Several
  processes starting different releases against one database were not tested.
