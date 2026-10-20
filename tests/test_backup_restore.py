"""Backup and restore with synthetic data (Stage 13): app/database/backups.py,
scripts/backup_database.py and scripts/restore_database.py.

A synthetic support history (conversations with chat messages, a support request with
its contact, events and audit entry, an FAQ created by an admin with its revision) is
written through the real application and repositories, backed up, restored into a
separate database, and checked there with the application itself.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.staff_auth import generate_staff_token, hash_staff_token
from app.database import backups
from app.database.backups import BackupError, create_backup, manifest_path_for, verify_backup
from app.database.connection import Database
from app.main import create_app
from app.models.conversation import utc_now
from app.models.faq_admin import FAQDraft
from app.models.support_api import SupportTicketCreateRequest
from app.repositories.conversation_repository import ConversationRepository
from app.services.answer_decision_service import Decision
from scripts import backup_database, restore_database

ROOT = Path(__file__).resolve().parents[1]
STAFF_TOKEN = generate_staff_token()


def url_of(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def app_for(path: Path) -> TestClient:
    settings = Settings(
        _env_file=None,
        database_url=url_of(path),
        rate_limit_enabled=False,
        staff_token_hashes=[f"admin1:admin:{hash_staff_token(STAFF_TOKEN)}"],
    )
    return TestClient(create_app(settings))


@pytest.fixture
def populated(tmp_path: Path) -> dict:
    """A database file holding a small synthetic support history."""
    db_path = tmp_path / "active" / "conversations.db"
    with app_for(db_path) as api:
        conversations = []
        for _ in range(3):
            body = api.post("/api/v1/conversations").json()
            conversations.append(body)
        # Chat exchanges, written as a turn writes them.
        database = Database(url_of(db_path))
        repository = ConversationRepository(database)
        for number, conversation in enumerate(conversations):
            repository.add_exchange(
                uuid.UUID(conversation["session_id"]),
                f"Synthetic question {number}?",
                f"Synthetic answer {number}.",
                Decision.ANSWERED,
                ["faq_001"],
                received_at=utc_now(),
            )
        database.dispose()
        # A support request: contact, ticket, events, audit entry.
        first = conversations[0]
        ticket = SupportTicketCreateRequest.model_config["json_schema_extra"]["examples"][0]
        response = api.post(
            "/api/v1/support/tickets",
            json={**ticket, "session_id": first["session_id"]},
            headers={"Authorization": f"Bearer {first['access_token']}"},
        )
        assert response.status_code in (200, 201), response.text
        # An FAQ created by an admin: FAQ, revision, audit entry.
        draft = FAQDraft.model_config["json_schema_extra"]["examples"][0]
        response = api.post(
            "/api/v1/admin/faqs",
            json={**draft, "question": "Is this a synthetic test question?"},
            headers={"Authorization": f"Bearer {STAFF_TOKEN}"},
        )
        assert response.status_code == 201, response.text
    return {"path": db_path, "conversations": conversations}


def counts(path: Path) -> dict[str, int]:
    return backups.table_counts(path)


def test_a_backup_is_consistent_verified_and_described(populated: dict, tmp_path: Path) -> None:
    source = populated["path"]
    backup = create_backup(source, tmp_path / "backups")

    assert backup.path.name.startswith("conversations-") and backup.path.suffix == ".db"
    assert verify_backup(backup.path) == []
    manifest = json.loads(backup.manifest_path.read_text(encoding="utf-8"))
    assert manifest["tables"] == counts(source)  # everything the source holds
    for table in ("conversation_messages", "support_tickets", "customer_contacts",
                  "ticket_events", "admin_audit_events", "faqs", "faq_revisions"):  # fmt: skip
        assert manifest["tables"][table] > 0, table
    assert manifest["sha256"] == backups.sha256_of(backup.path)
    assert manifest["method"] == "sqlite3 online backup API"
    assert manifest["integrity_check"] == "ok"
    # One self-contained file; nothing personal in the manifest.
    assert not list(backup.path.parent.glob("*-wal")) and not list(backup.path.parent.glob("*-shm"))
    text = backup.manifest_path.read_text(encoding="utf-8")
    assert "Synthetic question" not in text and "customer@example.com" not in text


def test_backups_never_overwrite_each_other(populated: dict, tmp_path: Path) -> None:
    folder = tmp_path / "backups"
    moment = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)

    first = create_backup(populated["path"], folder, now=moment)
    second = create_backup(populated["path"], folder, now=moment)  # the same second

    assert first.path != second.path
    assert second.path.name == "conversations-20261019T120000Z-1.db"
    assert verify_backup(first.path) == [] and verify_backup(second.path) == []
    assert not list(folder.glob(".*.partial"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_backup_files_are_readable_by_their_owner_only(populated: dict, tmp_path: Path) -> None:
    backup = create_backup(populated["path"], tmp_path / "backups")

    assert backup.path.stat().st_mode & 0o777 == 0o600
    assert backup.manifest_path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "backups").stat().st_mode & 0o777 == 0o700


def test_backups_and_manifests_cannot_enter_git() -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    names = [
        "data/backups/conversations-20261019T120000Z.db",
        "data/backups/conversations-20261019T120000Z.db.manifest.json",
        "elsewhere/conversations-20261019T120000Z-1.db.manifest.json",
    ]
    for name in names:
        result = subprocess.run(  # noqa: S603 - fixed arguments, no shell
            ["git", "check-ignore", "-q", name],  # noqa: S607 - git from PATH
            cwd=ROOT,
            capture_output=True,
        )
        assert result.returncode == 0, f"{name} is not git-ignored"


def test_restore_into_a_separate_database_works_with_the_application(
    populated: dict, tmp_path: Path
) -> None:
    source = populated["path"]
    backup = create_backup(source, tmp_path / "backups")
    before = counts(source)
    target = tmp_path / "restore-test" / "conversations.db"

    restored = backups.restore_to_new(backup.path, target)

    assert restored == before
    assert counts(source) == before  # the active database was not touched
    # The restored database serves the original conversations, tokens included.
    conversation = populated["conversations"][1]
    with app_for(target) as api:
        assert api.get("/api/v1/ready").status_code == 200
        messages = api.get(
            f"/api/v1/conversations/{conversation['session_id']}/messages",
            headers={"Authorization": f"Bearer {conversation['access_token']}"},
        ).json()["messages"]
    assert [m["content"] for m in messages] == ["Synthetic question 1?", "Synthetic answer 1."]


def test_a_test_restore_never_reuses_an_existing_file(populated: dict, tmp_path: Path) -> None:
    backup = create_backup(populated["path"], tmp_path / "backups")
    existing = tmp_path / "existing.db"
    existing.write_bytes(b"do not touch")

    with pytest.raises(BackupError, match="already exists"):
        backups.restore_to_new(backup.path, existing)
    assert existing.read_bytes() == b"do not touch"


def test_a_changed_backup_is_refused(populated: dict, tmp_path: Path) -> None:
    backup = create_backup(populated["path"], tmp_path / "backups")
    with backup.path.open("r+b") as handle:  # flip one byte in the middle
        handle.seek(backup.path.stat().st_size // 2)
        byte = handle.read(1)
        handle.seek(-1, 1)
        handle.write(bytes([byte[0] ^ 0xFF]))

    assert any("checksum mismatch" in p for p in verify_backup(backup.path))
    with pytest.raises(BackupError, match="checksum mismatch"):
        backups.restore_to_new(backup.path, tmp_path / "restored.db")

    manifest_path_for(backup.path).unlink()
    assert any("no manifest" in p for p in verify_backup(backup.path))


def test_replacing_the_active_database_needs_explicit_confirmation(
    populated: dict, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    active = populated["path"]
    backup = create_backup(active, tmp_path / "backups")
    # Newer activity after the backup, which the restore will roll back.
    with app_for(active) as api:
        api.post("/api/v1/conversations")
    after_backup = counts(active)
    common = [str(backup.path), "--replace-active", "--database-url", url_of(active),
              "--safety-dir", str(tmp_path / "safety")]  # fmt: skip

    assert restore_database.main(common) == restore_database.REFUSED  # no confirmation
    assert restore_database.main([*common, "--confirm", "other.db"]) == restore_database.REFUSED
    assert counts(active) == after_backup  # nothing replaced
    assert "--confirm conversations.db" in capsys.readouterr().out

    assert restore_database.main([*common, "--confirm", "conversations.db"]) == restore_database.OK
    assert counts(active) == backup.manifest["tables"]  # back to the backup
    safety = list((tmp_path / "safety").glob("conversations-*.db"))
    assert len(safety) == 1 and counts(safety[0]) == after_backup  # nothing lost
    assert verify_backup(safety[0]) == []
    assert not list(active.parent.glob(".*restoring*"))


def test_the_scripts_end_to_end(populated: dict, tmp_path: Path, capsys) -> None:
    folder = tmp_path / "backups"
    source = url_of(populated["path"])
    old = folder / "conversations-20200101T000000Z.db"
    folder.mkdir()
    old.write_bytes(b"x")
    manifest_path_for(old).write_text("{}", encoding="utf-8")
    unrelated = folder / "import-20200101T000000Z.db"  # another database's backup
    unrelated.write_bytes(b"x")

    code = backup_database.main(
        ["--database-url", source, "--output-dir", str(folder), "--keep", "1"]
    )
    assert code == 0
    made = [p for p in folder.glob("conversations-*.db")]
    assert len(made) == 1 and made[0] != old and not manifest_path_for(old).exists()
    assert unrelated.exists()

    assert restore_database.main([str(made[0]), "--verify-only"]) == restore_database.OK
    target = tmp_path / "check" / "restored.db"
    assert restore_database.main([str(made[0]), "--to", str(target)]) == restore_database.OK
    assert counts(target) == counts(populated["path"])
    out = capsys.readouterr().out
    assert "the active one was not touched" in out and "Synthetic" not in out

    assert backup_database.main(["--database-url", "sqlite://", "--output-dir", str(folder)]) == 2


def test_a_copy_that_fails_verification_is_not_kept(
    populated: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        backups, "verify_database", lambda path, required=None: ["integrity_check: 1 problem"]
    )
    folder = tmp_path / "backups"

    with pytest.raises(BackupError, match="did not verify"):
        create_backup(populated["path"], folder, now=datetime.now(UTC) - timedelta(days=1))
    assert list(folder.iterdir()) == []


def test_a_backup_taken_during_writes_is_consistent(populated: dict, tmp_path: Path) -> None:
    """Writes in progress on another connection are either fully in the backup or not
    at all: an uncommitted transaction is never captured half-way."""
    source = populated["path"]
    writer = sqlite3.connect(source, timeout=5)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("DELETE FROM conversation_messages")  # not committed during the backup
    backup = create_backup(source, tmp_path / "backups")
    writer.rollback()
    writer.close()

    assert (
        backup.manifest["tables"]["conversation_messages"]
        == counts(source)["conversation_messages"]
    )
