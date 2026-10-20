# Backup and recovery (Stage 13)

What is protected, how, and what to do in each kind of data loss, for the single-server
Docker Compose deployment. This is the overview and the decision guide. The detailed
commands and their verification are in
[docs/backup-and-restore.md](backup-and-restore.md); the operator's steps are in the
runbook ([docs/deployment-runbook.md](deployment-runbook.md), sections 3.3, 3.6 and 4);
recovering from a failed update is in [docs/deployment-rollback.md](deployment-rollback.md).

Commands use the runbook's conventions: bash on the server, run from `deploy/`.
**✏️ SERVER-SPECIFIC** values must be replaced; **⚠️ DESTRUCTIVE** steps replace or
delete data. Nothing here deletes data automatically.

## Contents

1. [What needs protecting](#1-what-needs-protecting)
2. [How it is protected](#2-how-it-is-protected)
3. [Recovery objectives](#3-recovery-objectives)
4. [Choosing a recovery](#4-choosing-a-recovery)
5. [Recovering onto a new server](#5-recovering-onto-a-new-server)
6. [After any restore](#6-after-any-restore)
7. [Drills](#7-drills)
8. [What has and has not been verified](#8-what-has-and-has-not-been-verified)

---

## 1. What needs protecting

| Item | Where | Can it be recreated? | Protection |
|---|---|---|---|
| **The database**: conversations, support tickets and contact details, FAQs and their revisions, staff assignments and notes, the audit log, the release history | `db` volume, `/var/lib/chatbot/conversations.db` | **No** | Backups (section 2) |
| **Deployment configuration and secrets**: `deploy/.env`, `deploy/environments/<env>/*.env` (staff token hashes, OpenAI key, off-server credentials), `deploy/nginx/admin.htpasswd` | `deploy/` on the server, git-ignored | Partly: secrets can be rotated, but the restic password cannot be recovered | **Your secrets store**, by hand (runbook 2.2). Not in the database backups |
| **The restic password** for off-server copies | `offsite.env`, and a password manager | **No**: without it the off-server copies cannot be decrypted | A password manager or secrets store, held by more than one person |
| TLS certificate and key | `deploy/nginx/certs/`, `/etc/letsencrypt` | Yes: issue a new certificate (runbook 2.6) | None needed |
| The embedding model | `models` volume | Yes: `docker compose --profile setup run --rm model-init` (92 MB, checksums verified) | None needed |
| The images | Docker on the server | Yes: built from the release's commit (`IMAGE_TAG`) | Keep the previous release's images for rollback (runbook 3.4) |
| The FAQ seed file | `data/faqs.json` in the image | Yes: in the repository | Git. Only used to fill an empty FAQ table; the live FAQs are in the database |
| Logs and in-memory metrics | Docker's `json-file` logs (5 × 10 MB per container); backend memory | No, and not backed up | Ship logs to a collector if you need them longer ([logging.md](logging.md)) |

## 2. How it is protected

| Layer | When | Where it is kept | Protects against |
|---|---|---|---|
| **Scheduled backup** (`backup` service) | Daily at `BACKUP_AT` (UTC, default 03:00); the newest `BACKUP_KEEP` (14) are kept | `backups` volume, same disk | Mistakes, corruption, a bad update. **Not** the loss of the server or its disk |
| **Automatic pre-upgrade backup** | Whenever a new release adds a column or rebuilds a table, before changing anything; no upgrade without it | `backups` volume | A schema change you need to undo ([deployment-rollback.md](deployment-rollback.md#2-backups-before-migrations)) |
| **Manual backup** | Before every update, restore, import or risky change (runbook 3.3) | `backups` volume | The same, at a moment you choose |
| **Safety backup** | Automatically, before a restore replaces the active database | `backups` volume | A restore of the wrong backup: restore the safety backup to undo it |
| **Off-server copies** (`backup-offsite`, opt-in, restic) | Daily at `OFFSITE_AT` (default 03:30), once configured; retention 14 daily, 8 weekly, 6 monthly | Storage you control, encrypted before it leaves the server | **The loss of the server**, its disk or its volumes |

Every backup is a consistent online copy (SQLite's backup API, taken while the
application runs), a single file, verified before it is kept (integrity, foreign keys,
every table), with a manifest of checksums and row counts, readable only by its owner
(`0600`). See [backup-and-restore.md §2](backup-and-restore.md#2-what-a-backup-is).

> **Until off-server copies are configured, every backup is on the same server as the
> database.** A lost disk or a deleted volume then loses the backups too. Configuring
> them needs your storage, credentials and an encryption password
> ([backup-and-restore.md §5](backup-and-restore.md#5-off-server-copies)). This is the
> main open item for recovery ([deployment-readiness.md](deployment-readiness.md)).

## 3. Recovery objectives

Proposed from what was measured; **confirm them with the business** before relying on
them.

| Objective | Proposed | Based on |
|---|---|---|
| **RPO** (data you can lose) | Up to **24 hours** | Daily backups. Take a manual backup before risky changes to shorten it for that moment. For a shorter RPO, run the backup more often (it takes well under a second at this size) |
| **RTO** (time to recover on the same server) | Under **15 minutes** | Verifying and restoring took seconds; the stack becomes healthy in about a minute (51–67 s measured) |
| RTO on a new server | Not measured | Server preparation, image build (about 11 minutes on a cold cache here), model fetch, fetching the copy from off-server storage, DNS and a certificate. Measure it in a drill (section 7) |

## 4. Choosing a recovery

Start from what happened. **Take a backup before changing anything** (runbook 3.3),
unless the database cannot be read at all.

| What happened | Is a restore the answer? | What to do |
|---|---|---|
| **A wrong FAQ edit** | **No.** A restore would also discard every conversation and ticket since the backup | Edit it again on the dashboard. The FAQ's **History** lists every earlier revision, so the previous text can be copied back ([admin-dashboard.md](admin-dashboard.md#faq-revision-history)) |
| **A ticket changed by mistake** | No | Correct it on the dashboard; the ticket's events show what changed |
| **The backend will not start after an update** | Only if the update changed the schema and rolling forward is not possible | [deployment-rollback.md §5](deployment-rollback.md#5-rollback-steps); runbook 4.6, 4.7 |
| **The disk is full** | **No.** The data is intact | Free space (runbook 4.4). The backend becomes ready again by itself |
| **`database is locked`** | No | Something else is writing the file (runbook 4.2) |
| **The database is corrupt** (`integrity_check` not `ok`) | **Yes** | Keep the corrupt file for analysis, then restore the newest backup that verifies (runbook 4.2, then 3.6) |
| **Data deleted or damaged in bulk** (a wrong script, an operator error) | **Yes**, if correcting it by hand is not practical | Restore the newest backup from before the damage (runbook 3.6). Decide what to do about what was written since (section 6) |
| **The `db` volume was deleted** (`docker compose down -v`, an operator error) | **Yes** | Restore the newest backup. If the `backups` volume went with it, use the off-server copy (section 5, steps 4–7) |
| **The server, its disk or all volumes are lost** | **Yes**, from the off-server copy | Section 5. Without off-server copies, the data is lost |
| **A secret leaked** | No | Rotate it (runbook 3.7, incident steps in 4.5) |
| **The `models` volume is lost or corrupt** | No | `docker compose --profile setup run --rm model-init`. Meanwhile the backend answers from keyword evidence and reports `semantic_search: unavailable` |
| **The certificate files are lost** | No | Issue a new certificate (runbook 2.6) |

### Restoring on the same server

The runbook's procedure ([3.6](deployment-runbook.md#36-restore-data)), in short:

```bash
docker compose exec backend ls -l /app/data/backups
BACKUP=/app/data/backups/conversations-20261019T105622Z.db   # ✏️ SERVER-SPECIFIC: from the list
docker compose exec backend python -m scripts.restore_database "$BACKUP" --verify-only
docker compose run --rm --no-deps backend python -m scripts.restore_database \
  "$BACKUP" --to /tmp/restore-test/conversations.db          # a test restore first: changes nothing
```

```bash
# ⚠️ DESTRUCTIVE: everything written after the backup is lost (a safety backup is taken first)
docker compose stop backend
docker compose run --rm --no-deps backend python -m scripts.restore_database \
  "$BACKUP" --replace-active --confirm conversations.db
docker compose start backend
docker compose ps
```

Which backup: the newest one that verifies and predates the problem. A pre-upgrade
backup is restored with the **release it was taken from**, not the newer one
([deployment-rollback.md](deployment-rollback.md)). `scripts.release_history` lists the
releases and the automatic backups:

```bash
docker compose exec backend python -m scripts.release_history
```

## 5. Recovering onto a new server

For when the original server, its disk or its volumes are gone. It needs the off-server
copies (restic), the restic password, and the deployment configuration from your secrets
store. **This procedure has not been run end to end** (section 8): rehearse it in a drill
before you need it.

1. **Prepare the server** as in runbook 1 (requirements, Docker, firewall) and 2.1,
   checking out the **same release** that made the backup (its manifest records
   `application_version`; the release history in the database records the revision).
   A newer release also works: it upgrades the restored database at startup, after its
   own pre-upgrade backup. An older one refuses a newer schema.
2. **Restore the configuration** from your secrets store into `deploy/`: `.env`,
   `environments/production/*.env` (including `offsite.env`) and `nginx/admin.htpasswd`.
   `chmod 600` the `.env` files (runbook 2.2). Create a placeholder certificate (runbook
   2.2) until DNS points at the new server.
3. **Build the images and fetch the model** (runbook 2.3):

   ```bash
   docker compose build
   docker compose --profile setup run --rm model-init
   ```

4. **Fetch the backups from the off-server copy** into a separate volume. Contacts your
   off-server storage:

   ```bash
   docker compose --profile offsite run --rm --no-deps --entrypoint restic backup-offsite snapshots
   docker volume create restored-backups
   docker run --rm -v restored-backups:/restore alpine chown 10001:10001 /restore   # pulls alpine
   docker compose --profile offsite run --rm --no-deps -v restored-backups:/restore \
     --entrypoint restic backup-offsite restore latest --target /restore
   docker run --rm -v restored-backups:/restore:ro alpine ls -l /restore/backups
   ```

5. **Verify the backup you choose** (normally the newest):

   ```bash
   FILE=conversations-20261019T105622Z.db       # ✏️ SERVER-SPECIFIC: from the list above
   docker compose run --rm --no-deps -v restored-backups:/restore:ro backend \
     python -m scripts.restore_database "/restore/backups/$FILE" --verify-only
   ```

6. **Restore it as the active database.** On a new server the `db` volume is empty, so
   nothing is overwritten; on a server that already has a database, this is
   ⚠️ DESTRUCTIVE as in section 4 (a safety backup is taken first):

   ```bash
   docker compose run --rm --no-deps -v restored-backups:/restore:ro backend \
     python -m scripts.restore_database "/restore/backups/$FILE" --replace-active --confirm conversations.db
   ```

7. **Start and check** (runbook 2.5, 2.7):

   ```bash
   docker compose up -d
   docker compose ps
   docker compose exec backend python -m scripts.release_history
   ```

8. **Point DNS at the new server**, then replace the placeholder certificate (runbook
   2.6). Customers' browsers keep HSTS for the hostname, so the site must serve a trusted
   certificate before they return.
9. **Start the off-server copies again** from the new server
   (`docker compose --profile offsite up -d backup-offsite`), and check the first one
   succeeds.
10. Complete [section 6](#6-after-any-restore). Remove the `restored-backups` volume once
    the new server's own backups and off-server copies are running.

## 6. After any restore

- **Data written after the backup is gone** from the database: conversations, support
  requests, FAQ edits, audit events. Note the time window. Support requests in it may
  have reached the team by other means (mail, phone); tell the people affected as your
  policy requires.
- **Customers** whose conversations were created after the backup get `404` for them and
  start a new one. Conversation tokens from before the backup still work until they
  expire.
- **Data deleted since the backup comes back** with it. Apply retention again:

  ```bash
  docker compose exec backend python -m scripts.cleanup_conversations
  docker compose exec backend python -m scripts.anonymize_support_data --retention
  ```

- **Undo:** the restore printed the path of its safety backup. Restoring that one the
  same way returns to the state before the restore.
- **Staff tokens** are configuration, not data: they are unaffected.

## 7. Drills

A backup that has never been restored is a hope, not a backup.

| How often | What | Command |
|---|---|---|
| Daily, automatic | The `backup` service is healthy (it turns unhealthy when backups stop succeeding); so is `backup-offsite` once configured | `docker compose --profile offsite ps backup backup-offsite` |
| Monthly | Verify the newest backup, and test-restore it into a separate file | Section 4, the first block |
| Monthly, once off-server copies run | List the snapshots, and verify one backup fetched from them | Section 5, steps 4–5, on any machine with Docker |
| Twice a year, and after major changes | A timed recovery onto another machine (section 5, all steps), with the restic password from the password manager, not from the server | Record the time taken: it is your measured RTO |

Record each drill: date, backup used, result, time taken, problems found.

## 8. What has and has not been verified

**Verified** (tests and Docker runs, synthetic data; details in
[backup-and-restore.md §8](backup-and-restore.md#8-verification) and
[deployment-rollback.md §7](deployment-rollback.md#7-tested-locally)):

- Backups taken while the application runs are consistent, complete and verified; an
  uncommitted transaction is never captured.
- A damaged backup (one byte changed) is refused; a restore without the right
  confirmation is refused and changes nothing.
- Test restores leave the active database untouched; a confirmed restore replaces it,
  takes a safety backup first, and the application serves the restored conversations
  with their original tokens.
- The scheduled `backup` service backs up, reports its health and stops promptly.
- Off-server copies against a **local stand-in** repository: encrypted (no database or
  schema text readable in the repository, a wrong password refused), and a backup
  fetched back from it verified against its manifest and test-restored.
- A pre-upgrade backup taken automatically, an older release refusing the newer schema,
  and recovery by restoring the pre-upgrade backup with the older release (Docker).
- The deployment tests back up and restore on the running stack
  ([deployment-tests.md](deployment-tests.md)).

**Not verified:**

- A real remote restic repository (it needs your storage and credentials).
- Recovery onto a **different** server (section 5, end to end), and therefore its RTO.
- Restoring the deployment configuration from a secrets store.
- A timed drill against agreed objectives.
