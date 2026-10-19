"""Scheduled database backups for the Docker deployment (Stage 13).

Runs as the ``backup`` service in deploy/compose.yaml. Once a day at ``BACKUP_AT`` (UTC)
it makes the same verified backup as ``python -m scripts.backup_database`` and keeps the
newest ``BACKUP_KEEP``. See docs/backup-and-restore.md.

    python -m scripts.backup_scheduler            # run forever (the service's command)
    python -m scripts.backup_scheduler --once     # one backup now, then exit
    python -m scripts.backup_scheduler --check    # health check: exit 0 healthy, 1 not

Settings (environment):
    DATABASE_URL         the database to back up (fixed by compose.yaml)
    BACKUP_DIR           where backups go (default data/backups; compose: the volume)
    BACKUP_AT            daily time, HH:MM in UTC (default 03:00)
    BACKUP_KEEP          backups to keep (default 14)
    BACKUP_RUN_ON_START  "true": also back up right after starting (default false)

After every run it writes a status file (``BACKUP_STATUS_FILE``, default
/tmp/backup-status.json: time, result, file name; no content). The health check fails
when the last run failed, or when no backup has succeeded for 1.5 days after the first
was due, so ``docker compose ps`` and monitoring see a stopped schedule. Stops promptly
on SIGTERM. Logs one line per run: file, size, counts, never content.
"""

import argparse
import json
import logging
import os
import signal
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/backup_scheduler.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database.backups import BackupError, create_backup, prune  # noqa: E402
from app.database.connection import resolve_sqlite_url  # noqa: E402

logger = logging.getLogger("backup_scheduler")

DEFAULT_AT = "03:00"
DEFAULT_KEEP = 14
INTERVAL = timedelta(days=1)
GRACE = INTERVAL / 2  # unhealthy when the last success is older than interval + grace
DEFAULT_STATUS_FILE = Path("/tmp/backup-status.json")  # noqa: S108 - the container's tmpfs
DEFAULT_BACKUP_DIR = PROJECT_ROOT / "data" / "backups"


@dataclass(frozen=True)
class Schedule:
    database: Path
    folder: Path
    at_hour: int
    at_minute: int
    keep: int
    run_on_start: bool
    status_file: Path

    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None) -> "Schedule":
        env = dict(os.environ if env is None else env)
        url = resolve_sqlite_url(env.get("DATABASE_URL", "sqlite:///data/conversations.db"))
        if url.database in (None, "", ":memory:"):
            raise ValueError("DATABASE_URL must be a database file to back up")
        at = env.get("BACKUP_AT", DEFAULT_AT)
        try:
            hour, minute = (int(part) for part in at.split(":"))
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError
        except ValueError:
            raise ValueError(f"BACKUP_AT must be HH:MM (UTC), not {at!r}") from None
        keep = int(env.get("BACKUP_KEEP", DEFAULT_KEEP))
        if keep < 1:
            raise ValueError("BACKUP_KEEP must be at least 1")
        return cls(
            database=Path(url.database),
            folder=Path(env.get("BACKUP_DIR", str(DEFAULT_BACKUP_DIR))),
            at_hour=hour,
            at_minute=minute,
            keep=keep,
            run_on_start=env.get("BACKUP_RUN_ON_START", "false").strip().lower() == "true",
            status_file=Path(env.get("BACKUP_STATUS_FILE", str(DEFAULT_STATUS_FILE))),
        )

    def next_run(self, now: datetime) -> datetime:
        """The next BACKUP_AT strictly after ``now`` (UTC)."""
        today = now.replace(hour=self.at_hour, minute=self.at_minute, second=0, microsecond=0)
        return today if today > now else today + INTERVAL


def run_once(schedule: Schedule, now: datetime | None = None) -> dict:
    """One verified backup and pruning; returns (and records) the status."""
    now = now or datetime.now(UTC)
    status: dict = {"at": now.isoformat(timespec="seconds")}
    try:
        backup = create_backup(schedule.database, schedule.folder, now=now)
        removed = prune(schedule.folder, schedule.database.stem, schedule.keep)
        status.update(
            ok=True,
            file=backup.path.name,
            size_bytes=backup.manifest["size_bytes"],
            rows=sum(backup.manifest["tables"].values()),
            removed=len(removed),
        )
        logger.info(
            "event=backup ok file=%s size_bytes=%d rows=%d removed=%d keep=%d",
            backup.path.name,
            status["size_bytes"],
            status["rows"],
            len(removed),
            schedule.keep,
        )
    except (BackupError, OSError) as exc:
        status.update(ok=False, error=type(exc).__name__, message=str(exc)[:300])
        logger.error("event=backup failed error=%s %s", type(exc).__name__, status["message"])
    previous = read_status(schedule.status_file)
    status["last_success"] = (
        status["at"] if status.get("ok") else (previous or {}).get("last_success")
    )
    status["started"] = (previous or {}).get("started", status["at"])
    write_status(schedule.status_file, status)
    return status


def read_status(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_status(path: Path, status: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(status), encoding="utf-8")
    os.replace(temporary, path)


def healthy(schedule: Schedule, now: datetime | None = None) -> tuple[bool, str]:
    """Whether backups are happening: the last run succeeded, and the last success is
    recent (or the first backup is not overdue yet)."""
    now = now or datetime.now(UTC)
    status = read_status(schedule.status_file)
    if status is None:
        return False, "no status file: the scheduler is not running"
    if status.get("ok") is False:
        return False, f"the last backup failed ({status.get('error')})"
    started = datetime.fromisoformat(status["started"])
    reference = status.get("last_success")
    if reference is None:  # no backup yet: overdue once the first one is well past due
        first_due = schedule.next_run(started)
        if now > first_due + GRACE:
            return False, "no backup has succeeded since the scheduler started"
        return True, f"waiting for the first backup at {first_due.isoformat()}"
    age = now - datetime.fromisoformat(reference)
    if age > INTERVAL + GRACE:
        return False, f"the last successful backup is {age} old"
    return True, f"last successful backup at {reference}"


def serve(schedule: Schedule, stop: threading.Event) -> None:
    """Back up at every scheduled time until ``stop`` is set."""
    now = datetime.now(UTC)
    write_status(
        schedule.status_file,
        {"at": now.isoformat(timespec="seconds"), "started": now.isoformat(timespec="seconds")},
    )
    if schedule.run_on_start:
        run_once(schedule)
    while not stop.is_set():
        due = schedule.next_run(datetime.now(UTC))
        logger.info("event=backup_scheduled next=%s", due.isoformat(timespec="seconds"))
        while not stop.is_set() and datetime.now(UTC) < due:
            stop.wait(min(60.0, (due - datetime.now(UTC)).total_seconds()))
        if not stop.is_set():
            run_once(schedule)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="Back up once now, then exit.")
    group.add_argument("--check", action="store_true", help="Health check (exit 0 or 1).")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )
    try:
        schedule = Schedule.from_environment()
    except ValueError as exc:
        print(f"Cannot start: {exc}", file=sys.stderr)
        return 2
    if args.check:
        ok, reason = healthy(schedule)
        print(reason)
        return 0 if ok else 1
    if args.once:
        return 0 if run_once(schedule).get("ok") else 1

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    logger.info(
        "event=backup_scheduler_started database=%s folder=%s at=%02d:%02d UTC keep=%d",
        schedule.database.name,
        schedule.folder,
        schedule.at_hour,
        schedule.at_minute,
        schedule.keep,
    )
    serve(schedule, stop)
    logger.info("event=backup_scheduler_stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
