"""scripts/backup_scheduler.py: scheduled, verified backups with a health check."""

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.database import backups
from app.database.connection import Database
from scripts import backup_scheduler as scheduler

NOON = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def environment(tmp_path: Path, **extra: str) -> dict[str, str]:
    database = tmp_path / "data" / "conversations.db"
    db = Database(f"sqlite:///{database.as_posix()}")
    db.init_schema()
    db.dispose()
    return {
        "DATABASE_URL": f"sqlite:///{database.as_posix()}",
        "BACKUP_DIR": str(tmp_path / "backups"),
        "BACKUP_STATUS_FILE": str(tmp_path / "status.json"),
        **extra,
    }


def schedule(tmp_path: Path, **extra: str) -> scheduler.Schedule:
    return scheduler.Schedule.from_environment(environment(tmp_path, **extra))


def test_settings_are_validated(tmp_path: Path) -> None:
    for bad in ({"BACKUP_AT": "25:00"}, {"BACKUP_AT": "3am"}, {"BACKUP_KEEP": "0"}):
        with pytest.raises(ValueError):
            schedule(tmp_path, **bad)
    with pytest.raises(ValueError, match="database file"):
        scheduler.Schedule.from_environment({"DATABASE_URL": "sqlite://"})
    defaults = schedule(tmp_path)
    assert (defaults.at_hour, defaults.at_minute, defaults.keep) == (3, 0, 14)
    assert defaults.run_on_start is False


@pytest.mark.parametrize(
    ("at", "now", "expected"),
    [
        ("03:00", NOON, datetime(2026, 10, 20, 3, 0, tzinfo=UTC)),  # today's time passed
        ("13:30", NOON, datetime(2026, 10, 19, 13, 30, tzinfo=UTC)),  # later today
        ("12:00", NOON, datetime(2026, 10, 20, 12, 0, tzinfo=UTC)),  # exactly now: tomorrow
    ],
)
def test_next_run_is_the_next_daily_time(
    tmp_path: Path, at: str, now: datetime, expected: datetime
) -> None:
    assert schedule(tmp_path, BACKUP_AT=at).next_run(now) == expected


def test_a_run_makes_a_verified_backup_and_records_success(tmp_path: Path) -> None:
    plan = schedule(tmp_path, BACKUP_KEEP="2")
    for hour in (1, 2, 3):
        status = scheduler.run_once(plan, now=NOON.replace(hour=hour))
        assert status["ok"] is True

    kept = sorted((tmp_path / "backups").glob("conversations-*.db"))
    assert [p.name for p in kept] == [
        "conversations-20261019T020000Z.db",
        "conversations-20261019T030000Z.db",
    ]
    assert all(backups.verify_backup(p) == [] for p in kept)
    recorded = scheduler.read_status(plan.status_file)
    assert recorded["last_success"] == "2026-10-19T03:00:00+00:00" and recorded["removed"] == 1


def test_a_failed_run_is_recorded_and_makes_the_service_unhealthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = schedule(tmp_path)
    scheduler.run_once(plan, now=NOON)

    def broken(*args: object, **kwargs: object) -> None:
        raise backups.BackupError("the copy did not verify: integrity_check")

    monkeypatch.setattr(scheduler, "create_backup", broken)
    status = scheduler.run_once(plan, now=NOON + timedelta(days=1))

    assert status["ok"] is False and status["error"] == "BackupError"
    assert status["last_success"] == NOON.isoformat()  # the last good one is remembered
    ok, reason = scheduler.healthy(plan, now=NOON + timedelta(days=1, minutes=1))
    assert not ok and "failed" in reason


def test_health_follows_the_schedule(tmp_path: Path) -> None:
    plan = schedule(tmp_path)  # daily at 03:00
    assert scheduler.healthy(plan, now=NOON) == (
        False,
        "no status file: the scheduler is not running",
    )

    started = {"at": NOON.isoformat(), "started": NOON.isoformat()}
    scheduler.write_status(plan.status_file, started)
    ok, reason = scheduler.healthy(plan, now=NOON + timedelta(hours=20))
    assert ok and "waiting for the first backup" in reason
    ok, reason = scheduler.healthy(plan, now=NOON + timedelta(days=1, hours=4))
    assert not ok and "no backup has succeeded" in reason  # first one well overdue

    scheduler.run_once(plan, now=NOON + timedelta(hours=15))  # 03:00 the next day
    assert scheduler.healthy(plan, now=NOON + timedelta(hours=16))[0]
    ok, reason = scheduler.healthy(plan, now=NOON + timedelta(days=2, hours=4))
    assert not ok and "old" in reason  # the schedule stopped


def test_the_service_backs_up_on_start_and_stops_when_asked(tmp_path: Path) -> None:
    plan = schedule(tmp_path, BACKUP_RUN_ON_START="true")
    stop = threading.Event()
    thread = threading.Thread(target=scheduler.serve, args=(plan, stop))
    thread.start()
    for _ in range(100):  # the first backup appears within a few seconds
        if list((tmp_path / "backups").glob("conversations-*.db")):
            break
        stop.wait(0.05)
    stop.set()
    thread.join(5)

    assert not thread.is_alive()  # stopped promptly, not at the next scheduled time
    assert len(list((tmp_path / "backups").glob("conversations-*.db"))) == 1
    assert scheduler.healthy(plan)[0]


def test_command_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    for key, value in environment(tmp_path).items():
        monkeypatch.setenv(key, value)

    assert scheduler.main(["--check"]) == 1  # nothing has run yet
    assert scheduler.main(["--once"]) == 0
    assert scheduler.main(["--check"]) == 0
    assert "last successful backup" in capsys.readouterr().out
    monkeypatch.setenv("BACKUP_AT", "noon")
    assert scheduler.main(["--once"]) == 2
