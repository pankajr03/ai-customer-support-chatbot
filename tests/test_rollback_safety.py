"""Rollback safety of the database (Stage 13, Task 24). docs/deployment-rollback.md.

- An **older release never discards what a newer one added**: before this, starting
  older code against a database a newer release had upgraded rebuilt the changed tables
  to the older definition and silently dropped the new columns with their data. Now it
  refuses to start and changes nothing.
- **A verified backup is taken before any schema upgrade that changes existing tables**
  (adds a column, rebuilds a table). If it fails, nothing is upgraded.
- **The deployed release is recorded** in the database (``app_releases``), so every
  backup says which releases wrote it.
"""

# Test data built from constants in this file only.
# ruff: noqa: S608

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.database import backups
from app.database.backups import BackupError, create_backup, restore_to_new, verify_backup
from app.database.connection import Database, DatabaseError
from scripts import release_history

TICKET_CHECK = "'normal', 'high', 'urgent'"  # the priority CHECK constraint's tail


def make_database(tmp_path: Path, name: str = "app.db") -> Path:
    path = tmp_path / name
    database = Database(f"sqlite:///{path.as_posix()}")
    database.init_schema()
    database.dispose()
    return path


def start(path: Path, backup_dir: Path | None = None) -> Database:
    """A release starting against ``path``: what the backend does at startup."""
    database = Database(f"sqlite:///{path.as_posix()}", upgrade_backup_dir=backup_dir)
    try:
        database.init_schema()
    finally:
        database.dispose()
    return database


def sql(path: Path, *statements: str) -> list[Any]:
    connection = sqlite3.connect(path)
    try:
        rows: list[Any] = []
        for statement in statements:
            rows = connection.execute(statement).fetchall()
        connection.commit()
        return rows
    finally:
        connection.close()


def columns(path: Path, table: str) -> set[str]:
    return {row[1] for row in sql(path, f"PRAGMA table_info({table})")}


def add_ticket(path: Path, extra: dict[str, Any] | None = None) -> None:
    sql(path, "INSERT INTO customer_contacts (contact_id, name, email, phone, "
        "contact_preference, consent_given, consent_timestamp, created_at, updated_at, "
        "identity_hash, anonymized_at) VALUES ('c1', 'N', 'n@example.com', NULL, 'email', 1, "
        "'2026-01-01 00:00:00.000000', '2026-01-01 00:00:00.000000', "
        f"'2026-01-01 00:00:00.000000', '{'a' * 64}', NULL)")  # fmt: skip
    names = ["ticket_id", "reference_number", "contact_id", "ticket_type", "subject",
             "description", "status", "priority", "created_at", "updated_at"]  # fmt: skip
    moment = "'2026-01-01 00:00:00.000000'"
    values = ["'t1'", "'SUP-0000-0001'", "'c1'", "'support'", "'s'", "'d'", "'open'",
              "'normal'", moment, moment]  # fmt: skip
    for name, value in (extra or {}).items():
        names.append(name)
        values.append(str(value))
    sql(path, f"INSERT INTO support_tickets ({', '.join(names)}) VALUES ({', '.join(values)})")


# --- An older release against a newer database -------------------------------------------


def test_an_older_release_refuses_to_discard_columns_a_newer_one_added(tmp_path: Path) -> None:
    path = make_database(tmp_path)
    # What a newer release did: a new nullable column, with data in it.
    sql(path, "ALTER TABLE support_tickets ADD COLUMN satisfaction_score INTEGER")
    add_ticket(path, {"satisfaction_score": 5})
    backup_dir = tmp_path / "backups"

    with pytest.raises(DatabaseError) as refused:
        start(path, backup_dir)  # this (older) code, as in a rollback

    message = str(refused.value)
    assert "support_tickets.satisfaction_score" in message
    assert "upgraded by a newer release" in message and "Nothing was changed" in message
    assert "satisfaction_score" in columns(path, "support_tickets")  # still there
    assert sql(path, "SELECT satisfaction_score FROM support_tickets") == [(5,)]
    assert not backup_dir.exists()  # refused before anything, backups included


def test_extra_tables_and_indexes_of_a_newer_release_are_left_alone(tmp_path: Path) -> None:
    path = make_database(tmp_path)
    sql(path, "CREATE TABLE ticket_surveys (id INTEGER PRIMARY KEY, score INTEGER)",
        "CREATE INDEX ix_ticket_priority ON support_tickets (priority)",
        "ALTER TABLE conversation_messages ADD COLUMN sentiment TEXT")  # fmt: skip

    start(path)  # nothing this code would rebuild: it starts, and keeps them

    tables = {r[0] for r in sql(path, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "ticket_surveys" in tables
    assert "sentiment" in columns(path, "conversation_messages")


# --- Backups before upgrades --------------------------------------------------------------


def manifests(folder: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(folder.glob(f"*{backups.MANIFEST_SUFFIX}"))]  # fmt: skip


def test_adding_a_column_is_preceded_by_a_verified_backup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = make_database(tmp_path)
    sql(path, "ALTER TABLE conversation_messages DROP COLUMN generation_method")  # older schema
    backup_dir = tmp_path / "backups"

    with caplog.at_level(logging.WARNING):
        start(path, backup_dir)

    assert "generation_method" in columns(path, "conversation_messages")  # upgraded
    (manifest,) = manifests(backup_dir)
    assert manifest["reason"] == "before schema upgrade"
    backup = backup_dir / manifest["backup_file"]
    assert "generation_method" not in columns(backup, "conversation_messages")  # as it was
    assert verify_backup(backup) == []
    assert "Backed up the database before upgrading its schema" in caplog.text
    assert "add conversation_messages.generation_method" in caplog.text


def test_a_table_rebuild_is_preceded_by_a_backup(tmp_path: Path) -> None:
    path = make_database(tmp_path)
    add_ticket(path)
    # An older definition of the table: one more allowed priority in its CHECK constraint.
    sql(path, "PRAGMA writable_schema = ON",
        f"UPDATE sqlite_master SET sql = replace(sql, \"{TICKET_CHECK}\", "
        f"\"{TICKET_CHECK}, 'legacy'\") WHERE name = 'support_tickets'")  # fmt: skip
    backup_dir = tmp_path / "backups"

    start(path, backup_dir)

    (manifest,) = manifests(backup_dir)
    assert manifest["reason"] == "before schema upgrade"
    assert "'legacy'" not in sql(path, "SELECT sql FROM sqlite_master WHERE name = "
                                       "'support_tickets'")[0][0]  # fmt: skip
    assert sql(path, "SELECT count(*) FROM support_tickets") == [(1,)]  # rows kept


def test_no_backup_is_needed_for_a_new_database_tables_or_indexes(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    path = tmp_path / "new.db"
    start(path, backup_dir)  # a new database
    sql(path, "DROP INDEX ix_ticket_created", "DROP TABLE app_releases")
    start(path, backup_dir)  # only an index and a table to create: no existing row touched

    assert not backup_dir.exists() or not list(backup_dir.iterdir())


def test_if_the_backup_fails_nothing_is_upgraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_database(tmp_path)
    sql(path, "ALTER TABLE conversation_messages DROP COLUMN generation_method")

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise BackupError("the copy did not verify")

    monkeypatch.setattr(backups, "create_backup", broken)

    with pytest.raises(DatabaseError, match="needs a backup first"):
        start(path, tmp_path / "backups")

    assert "generation_method" not in columns(path, "conversation_messages")  # untouched


def test_a_pre_upgrade_backup_restores_with_the_newer_code(tmp_path: Path) -> None:
    path = make_database(tmp_path)
    sql(path, "DROP TABLE app_releases")  # a database from before the history existed
    backup = create_backup(path, tmp_path / "backups", as_found=True).path

    assert verify_backup(backup) == []  # judged by the tables it was made with
    target = tmp_path / "restored" / "app.db"
    restore_to_new(backup, target)
    start(target)  # the newer code upgrades the restored copy as usual
    assert "app_releases" in {r[0] for r in sql(target, "SELECT name FROM sqlite_master")}


# --- Release history ---------------------------------------------------------------------


def test_each_release_is_recorded_once(tmp_path: Path) -> None:
    path = make_database(tmp_path)
    database = Database(f"sqlite:///{path.as_posix()}")

    assert database.record_release("0.1.0", "aaa1111") is True
    assert database.record_release("0.1.0", "aaa1111") is False  # a restart
    assert database.record_release("0.1.0", "bbb2222") is True  # a new release
    sql(path, "CREATE INDEX ix_test ON faqs (question)")
    assert database.record_release("0.1.0", "bbb2222") is True  # its schema changed
    database.dispose()

    rows = sql(path, "SELECT app_version, revision FROM app_releases ORDER BY id")
    assert rows == [("0.1.0", "aaa1111"), ("0.1.0", "bbb2222"), ("0.1.0", "bbb2222")]


def test_the_application_records_its_revision_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_settings: Any, faq_service: Any
) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("APP_REVISION", "c0ffee1")
    path = tmp_path / "app.db"
    app = create_app(make_settings(), faq_service=faq_service,
                     database=Database(f"sqlite:///{path.as_posix()}"))  # fmt: skip
    with TestClient(app):
        pass

    assert sql(path, "SELECT revision FROM app_releases") == [("c0ffee1",)]


def test_release_history_shows_releases_and_pre_upgrade_backups(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = make_database(tmp_path)
    database = Database(f"sqlite:///{path.as_posix()}")
    database.record_release("0.1.0", "aaa1111")
    database.dispose()
    backup_dir = tmp_path / "backups"
    create_backup(path, backup_dir, reason="before schema upgrade", as_found=True)

    release_history.main(["--database-url", f"sqlite:///{path.as_posix()}",
                          "--backup-dir", str(backup_dir)])  # fmt: skip

    out = capsys.readouterr().out
    assert "revision aaa1111" in out and "<- current schema" in out
    assert "Backups taken before a schema upgrade" in out and ".db  schema" in out
