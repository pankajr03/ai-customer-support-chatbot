# Database backup and restore (Stage 13)

How to back up the SQLite database safely while the application runs, check a backup,
test a restore without touching the live data, and replace the live database when you
really must. Written for whoever operates the server.

| Tool | Purpose |
|---|---|
| [scripts/backup_database.py](../scripts/backup_database.py) | Make a verified, timestamped backup with a manifest; keep the newest N |
| [scripts/backup_scheduler.py](../scripts/backup_scheduler.py) | The `backup` service: the same backup daily at `BACKUP_AT`, with a health check |
| [deploy/offsite/offsite.sh](../deploy/offsite/offsite.sh) | The opt-in `backup-offsite` service: encrypted off-server copies with restic |
| [scripts/restore_database.py](../scripts/restore_database.py) | Verify a backup; restore it into a **separate** database (default); or, with explicit confirmation, replace the active one |
| [app/database/backups.py](../app/database/backups.py) | The shared implementation (also used by `scripts.import_faqs --backup`) |

Background: [docs/sqlite-review.md](sqlite-review.md) (why SQLite, and the backup
requirements).

## Contents

1. [Commands](#1-commands)
2. [What a backup is](#2-what-a-backup-is)
3. [Restoring](#3-restoring)
4. [Schedule and retention](#4-schedule-and-retention)
5. [Off-server copies](#5-off-server-copies)
6. [Encryption](#6-encryption)
7. [Permissions and Git](#7-permissions-and-git)
8. [Verification](#8-verification)

---

## 1. Commands

In the Docker deployment, run from `deploy/`. Outside Docker, run from the project root
without the `docker compose …` prefix.

```bash
# Back up (the application keeps running)
docker compose exec backend python -m scripts.backup_database             # keep the newest 14
docker compose exec backend python -m scripts.backup_database --keep 30

# List the backups
docker compose exec backend ls -l /app/data/backups

# Check a backup (checksum against its manifest, integrity, foreign keys, every table)
docker compose exec backend python -m scripts.restore_database \
  /app/data/backups/conversations-20261019T105622Z.db --verify-only

# Test restore into a separate database (never the active one). The default target is
# a new temporary folder; or name a NEW file with --to
docker compose run --rm --no-deps backend python -m scripts.restore_database \
  /app/data/backups/conversations-20261019T105622Z.db --to /tmp/restore-test/conversations.db

# Replace the active database: DESTRUCTIVE (section 3)
docker compose stop backend
docker compose run --rm --no-deps backend python -m scripts.restore_database \
  /app/data/backups/conversations-20261019T105622Z.db --replace-active --confirm conversations.db
docker compose start backend
```

Exit codes. Backup: `0` backed up and verified; `1` the copy failed verification and
was deleted; `2` nothing to back up. Restore: `0` done; `1` the backup or the restored
copy failed verification, and nothing was replaced; `2` refused (missing or wrong
confirmation, target exists, no active database). Both print paths, sizes and counts,
never content.

## 2. What a backup is

| Property | How |
|---|---|
| **Consistent** | SQLite's **online backup API**, from a normal connection: a transactionally consistent snapshot, including whatever is still in the write-ahead log. An uncommitted transaction is never captured half-way (tested). The live file is never copied with `cp`: in WAL mode the newest commits can still be in `conversations.db-wal` |
| **Taken while running** | No downtime. Readers and writers continue; a backup of this size takes well under a second |
| **Complete** | Every table: conversations and messages, support tickets, contacts, ticket events and notes, FAQs and their revisions, the audit log. The FAQ seed file, configuration and secrets are **not** in it: the seed is in the repository, and the configuration lives with the deployment (`deploy/.env`, `deploy/environments/*`, `deploy/nginx/`), which you back up separately and keep secret |
| **One file** | The copy is switched from WAL to an ordinary journal, so a backup is a single `.db` file with no `-wal` or `-shm` belonging to it |
| **Timestamped, never overwritten** | `<database>-<UTC time>.db`, for example `conversations-20261019T105622Z.db`. Written as a hidden `.partial` file, verified, then published under a name that is not yet used; a second backup in the same second gets `-1`, `-2`, … |
| **Verified before it is kept** | `integrity_check`, `foreign_key_check`, and every table this version of the application expects. A copy that fails is deleted (exit 1) |
| **Described** | A manifest beside it, `<backup>.manifest.json`: SHA-256 of the file, size, row count per table, a fingerprint of the schema, the SQLite and application versions, the source path and journal mode. Counts and hashes only, no customer data |

Example manifest (from the verification run):

```json
{
  "format": 1,
  "created_at": "2026-10-19T10:56:22+00:00",
  "backup_file": "conversations-20261019T105622Z.db",
  "source_database": "/var/lib/chatbot/conversations.db",
  "source_journal_mode": "wal",
  "method": "sqlite3 online backup API",
  "size_bytes": 237568,
  "sha256": "65b84960…",
  "schema_sha256": "f3b4c4a7…",
  "tables": {"admin_audit_events": 1, "conversation_messages": 4, "conversation_sessions": 2, "…": "…"},
  "integrity_check": "ok",
  "sqlite_version": "3.46.1",
  "application_version": "0.1.0"
}
```

## 3. Restoring

Every restore first checks the backup: its SHA-256 must match the manifest (a damaged or
altered file is refused), and it must pass the same integrity checks as a new backup.

### Test restore (the default, safe)

Restores into a database file that **must not exist yet**: by default a new temporary
folder, or the `--to` path. It verifies the copy and compares every table's row count
with the manifest. The active database is never opened for writing. To look at the
result with the application, start a separate instance with
`DATABASE_URL=sqlite:///<that path>`. Do this regularly: a backup that has never been
restored is a hope, not a backup.

### Replacing the active database (destructive)

Only with **both** `--replace-active` and `--confirm <file name of the active
database>` (normally `conversations.db`). Without them the script refuses and explains
what to type. Nothing is replaced automatically.

1. **Stop the backend** (`docker compose stop backend`). The script cannot tell whether
   the application still has the file open; replacing it under a running backend would
   mix two databases.
2. Run the restore in a one-off container (`docker compose run --rm --no-deps backend …`).
   It:
   - checks the backup (checksum, integrity);
   - **backs up the current database first** (a safety backup, in `--safety-dir`,
     default `data/backups`), so the restore itself can be undone;
   - restores into a staging file beside the active one and verifies it;
   - removes the active file's `-wal` and `-shm` (they belong to the old file and
     must not be applied to the new one);
   - swaps the staging file in with one rename.
3. **Start the backend.** Customers whose conversations were created after the backup
   get `404` for them; their tokens in the backup still work.

Undo a restore: restore the safety backup the same way.

**After a restore,** remember what the backup does not contain: anything written after
it. Support requests in that gap are lost from the database; check the audit log and
mail or other channels for them.

## 4. Schedule and retention

| Item | Default | Notes |
|---|---|---|
| Backups kept on the server | The newest **14** per database (`--keep`) | Counted in backups, not days: with one backup a day that is two weeks. Safety backups from restores and the FAQ import's backups are in the same folder; only files named `<database>-<time>.db` of the same database are counted and pruned, each with its manifest |
| Schedule | **Daily at `BACKUP_AT`** (UTC, default 03:00) by the `backup` service | Started with the stack (`docker compose up -d`). `BACKUP_KEEP` (default 14) and `BACKUP_RUN_ON_START` in `deploy/.env`. No network at all. Its health check turns **unhealthy** if the last run failed or no backup has succeeded for 1.5 days: watch `docker compose ps` or your monitoring. Log lines: `event=backup ok file=… rows=…` |
| Off-server copies | **Daily at `OFFSITE_AT`** (default 03:30) by the opt-in `backup-offsite` service, **once configured** | The `backups` volume is on the same disk as the database: it protects against mistakes, not against losing the server. Section 5 |
| Restore tests | Monthly suggested | `--verify-only` on the newest backup, and a test restore of a recent one |

**Retention and personal data.** Backups contain data that the live database has since
deleted: conversations removed by cleanup or by the customer, and tickets anonymized
after `SUPPORT_TICKET_RETENTION_DAYS`. A person's data therefore remains in backups until
the oldest backup holding it is deleted. Decide a maximum backup age that fits your
retention policy and privacy obligations (for example 30 days), and apply it to the
off-server copies as well. Restoring an old backup brings back data that was deleted
after it was made: re-run the retention scripts after a restore
(`cleanup_conversations`, `anonymize_support_data --retention`).

## 5. Off-server copies

The `backup-offsite` service sends the `backups` volume to a **restic** repository on
storage you control (object storage, another server over SFTP, …), once a day after the
local backup. restic encrypts on this server, so the storage never sees customer data
in clear. It is **not started** until you configure it: a repository name, storage
credentials and an encryption password are yours to choose.

**Set up (production):**

```bash
cd /srv/support-chatbot/deploy
cp environments/production/offsite.env.example environments/production/offsite.env
chmod 600 environments/production/offsite.env
# Fill in RESTIC_REPOSITORY, RESTIC_PASSWORD (openssl rand -base64 32) and the storage
# credentials. Store the password in a password manager too: without it the copies
# cannot be decrypted. OFFSITE_RUN_ON_START=true makes a first copy at once.
docker compose --profile offsite up -d backup-offsite
docker compose --profile offsite logs backup-offsite        # event=offsite ok snapshots=1
docker compose --profile offsite ps backup-offsite          # healthy
```

| Setting (`offsite.env`) | Default | |
|---|---|---|
| `RESTIC_REPOSITORY` | (required) | For example `s3:https://s3.eu-central-1.amazonaws.com/bucket/support-chatbot`, `b2:bucket:support-chatbot`, `sftp:backup@host:/srv/restic/support-chatbot` |
| `RESTIC_PASSWORD` (or `RESTIC_PASSWORD_FILE`) | (required) | The encryption key's password |
| Storage credentials | | Only what the repository type needs, for example `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. Use a key limited to this one location |
| `OFFSITE_AT` | `03:30` | Daily, UTC |
| `OFFSITE_KEEP_DAILY` / `_WEEKLY` / `_MONTHLY` | `14` / `8` / `6` | Snapshots kept (`restic forget --prune`) |

The repository is initialised on first use. The service reads the backups read-only, runs
as the backups' owner (not root), has outbound network access only, and turns unhealthy
when copies stop succeeding.

**Restore from the off-server copy** (for example onto a new server; the whole
procedure is in [docs/backup-and-recovery.md §5](backup-and-recovery.md#5-recovering-onto-a-new-server)):

```bash
docker compose --profile offsite run --rm --no-deps --entrypoint restic backup-offsite snapshots
docker volume create restored-backups
docker run --rm -v restored-backups:/restore alpine chown 10001:10001 /restore
docker compose --profile offsite run --rm --no-deps -v restored-backups:/restore \
  --entrypoint restic backup-offsite restore latest --target /restore
# The files are in /restore/backups/: check one, then restore it as in section 3
TAG=$(sed -n 's/^IMAGE_TAG=//p' .env)          # the backend image's tag
FILE=conversations-20261019T105622Z.db         # ✏️ SERVER-SPECIFIC: one of the restored files
docker run --rm --user 10001 -v restored-backups:/restore:ro --entrypoint python \
  "support-chatbot-backend:$TAG" -m scripts.restore_database "/restore/backups/$FILE" --verify-only
```

## 6. Encryption

Backups contain customer conversations and contact details **in clear**. The
application does not encrypt them. Choose at least one of these, in this order of
preference:

| Option | What it protects | How |
|---|---|---|
| **Encrypted off-server backup tool** (built in: section 5) | Copies at rest and in transit, with retention | The `backup-offsite` service uses [restic](https://restic.net/): encrypted, deduplicated repositories on another server or object storage, with retention (`restic forget --keep-daily 14 --keep-weekly 8 --keep-monthly 6 --prune`). [BorgBackup](https://www.borgbackup.org/) is an alternative |
| **Encrypt each file before it leaves the server** | Copies at rest elsewhere | [age](https://age-encryption.org/) with a public key, keeping the private key **off** the server: `age -r age1… -o conversations-….db.age conversations-….db`. Or `gpg --symmetric --cipher-algo AES256`. Keep the manifest alongside (it holds no personal data) to check the decrypted file |
| **Disk encryption on the server** | The live database and local backups if the disk is taken | LUKS, or the cloud provider's volume encryption. It does **not** protect copies made from the running server |
| **Storage-side encryption** (object storage) | Copies at rest at the provider | Server-side encryption with your own keys. Prefer client-side (above) as well, so the provider never sees plaintext |

Not adopted: SQLCipher (encrypting the live database file itself). It needs a different
SQLite build in the image and key handling at startup; disk encryption covers the same
risk more simply on a single server.

Key management: keep decryption keys off the server and out of the repository, record
who holds them, and include decryption in the restore tests. A backup you cannot
decrypt is not a backup.

## 7. Permissions and Git

- **Files `0600`, folder `0700`** (owner only), set by the scripts on POSIX systems.
  In the container the owner is the `app` user (uid 10001). Verified on Docker:
  `-rw------- app app` for backups and manifests, `drwx------` for the folder. On
  Windows (development) POSIX permissions do not apply; rely on the user profile's
  permissions.
- **Never in Git:** `.gitignore` ignores `*.db`, `data/backups/` and
  `*.db.manifest.json` (checked by `test_backups_and_manifests_cannot_enter_git`).
- **Never in images:** `.dockerignore` excludes databases and `backups/` folders.
- **Never in logs:** the scripts print paths, sizes, table counts and checksums only.

## 8. Verification

**Tests** (`tests/test_backup_restore.py`, synthetic data written through the real
application: conversations with messages, a support request with contact, events and
audit entry, an admin-created FAQ with its revision):

| Check | Result |
|---|---|
| Backup | Verified; manifest row counts equal the source's; every data table non-empty; one file, no `-wal`/`-shm`; no customer text in the manifest |
| Same second twice | Two different files (`…Z.db`, `…Z-1.db`), both verified, no leftover `.partial` |
| Permissions (POSIX) | Files `0600`, folder `0700` (runs on Linux, including CI; skipped on Windows) |
| Git | Backup and manifest names are git-ignored |
| Test restore | Row counts equal; the source is untouched; the **application** serves a restored conversation with its **original access token** |
| Existing target | Refused; the file is untouched |
| Damaged backup (one byte flipped) | `checksum mismatch`: refused. Without a manifest: refused |
| Replace without or with a wrong confirmation | Refused (exit 2); nothing changed; the message names the `--confirm` value to use |
| Replace with confirmation | The active database equals the backup again; the safety backup holds the newer data and verifies; no staging files left |
| Backup while a write transaction is open | The uncommitted change is not in the backup |
| Failed verification | The copy is deleted; nothing kept |

**On Docker** (2026-10-19, the production configuration, synthetic data only):

| Step | Result |
|---|---|
| Synthetic data through Nginx | Two conversations with chat turns (`200`), a support request (`201`) |
| `backup_database` in the running backend | `conversations-20261019T105622Z.db` (0.2 MB, 9 tables, 70 rows, verified) and its manifest; both `-rw------- app app`, folder `drwx------`; personal data in the manifest: 0 matches |
| Test restore in a one-off container | Into `/tmp/restore-test/…` and, by default, a new temporary folder: row counts match; the active database untouched |
| A conversation created after the backup | `200` before the restore |
| Destructive restore without `--confirm` | Refused, exit 2 |
| With `--confirm conversations.db`, backend stopped | Verified, replaced; the previous database saved as `conversations-20261019T105641Z.db` |
| After starting the backend | The two original conversations `200`, with their original tokens and messages; the post-backup conversation `404` (rolled back; it is in the safety backup); readiness `200` |

Not verified here: off-server copies, encryption tooling, and restoring on a different
server.

**Scheduled and off-server copies on Docker** (2026-10-19, synthetic data; the
off-server repository was a **local stand-in folder**, not remote storage):

| Check | Result |
|---|---|
| Configuration | Valid without `offsite.env` (the profile off); `backup-offsite` appears only with `--profile offsite` |
| `backup` service | `event=backup ok file=conversations-…Z.db … rows=62` on start, then `event=backup_scheduled next=2026-10-20T03:00:00+00:00`; health check `last successful backup at …`; network mode `none`; files `-rw------- app app` |
| Stop | `SIGTERM`: stopped in 0.85 s (`event=backup_scheduler_stopped`), not at the next scheduled time |
| `backup-offsite` | Repository initialised on first use; `event=offsite ok snapshots=1`, then 2; all services healthy |
| Encryption | 7 repository files, **none** containing SQLite headers, schema text or table names; a wrong password: `Fatal: wrong password or no key found` |
| Restore from the off-server copy | `restic restore latest`: 9 files; a restored backup **verified against its manifest** with `restore_database --verify-only`, and test-restored with matching row counts |
| Unit tests | `tests/test_backup_scheduler.py` (9: settings, timing, success, failure, health states, start and stop); compose checks in `tests/test_deployment_config.py` |

Not verified: a real remote repository (it needs your storage and credentials), and
recovery onto a different server.
