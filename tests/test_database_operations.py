"""Network-filesystem guard and online backups (Stage 13, SQLite review)."""

import logging
import sqlite3
from pathlib import Path

import pytest

from app.core.config import Settings
from app.database import connection
from app.database.connection import Database, DatabaseError, filesystem_type
from app.main import create_app
from scripts import backup_database

MOUNTS = """\
overlay / overlay rw,relatime 0 0
/dev/sdc /var/lib/chatbot ext4 rw,relatime 0 0
server:/export /srv/nfs nfs4 rw,vers=4.2 0 0
//nas/share /srv/smb cifs rw 0 0
drvfs /mnt/e 9p rw,relatime 0 0
/dev/sdd /srv/nfs/local ext4 rw 0 0
/dev/sde /srv/with\\040space ext4 rw 0 0
"""


@pytest.fixture
def mounts(tmp_path: Path) -> Path:
    path = tmp_path / "mounts"
    path.write_text(MOUNTS, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("folder", "expected"),
    [
        ("/var/lib/chatbot", "ext4"),
        ("/var/lib/chatbot/sub", "ext4"),
        ("/srv/nfs/db", "nfs4"),
        ("/srv/nfs/local/db", "ext4"),  # the longest matching mount point wins
        ("/srv/smb", "cifs"),
        ("/mnt/e/tamara-ai/data", "9p"),
        ("/srv/with space/db", "ext4"),
        ("/srv/nfsx", "overlay"),  # a prefix of a name is not a mount point
    ],
)
def test_filesystem_type_finds_the_mount_holding_a_path(
    mounts: Path, folder: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "resolve", lambda self: self)  # keep the POSIX path as given
    assert filesystem_type(Path(folder), mounts) == expected


def test_filesystem_type_is_unknown_without_a_mount_table(tmp_path: Path) -> None:
    assert filesystem_type(tmp_path, tmp_path / "missing") is None


@pytest.mark.parametrize("kind", ["nfs4", "cifs", "9p"])
def test_production_refuses_a_database_on_a_network_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, kind: str
) -> None:
    monkeypatch.setattr(connection, "filesystem_type", lambda path, mounts_file=None: kind)
    url = f"sqlite:///{(tmp_path / 'app.db').as_posix()}"

    with pytest.raises(DatabaseError, match=f"on a {kind} filesystem"):
        Database(url, refuse_network_filesystem=True)
    with caplog.at_level(logging.ERROR, logger="app.database.connection"):
        Database(url).dispose()  # development: allowed, but logged as an error
    assert f"on a {kind} filesystem" in caplog.text

    production = Settings(_env_file=None, app_env="production", database_url=url)
    with pytest.raises(DatabaseError):
        create_app(production)


def test_a_local_filesystem_is_accepted_in_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connection, "filesystem_type", lambda path, mounts_file=None: "ext4")
    url = f"sqlite:///{(tmp_path / 'app.db').as_posix()}"

    Database(url, refuse_network_filesystem=True).dispose()


# --- scripts/backup_database.py ---------------------------------------------------------


def make_database(path: Path) -> str:
    url = f"sqlite:///{path.as_posix()}"
    db = Database(url)
    db.init_schema()
    db.dispose()
    return url


def test_backup_copies_verifies_and_keeps_the_newest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url = make_database(tmp_path / "conversations.db")
    out = tmp_path / "backups"
    out.mkdir()
    for stamp in ("20261001T000000Z", "20261002T000000Z", "20261003T000000Z"):
        (out / f"conversations-{stamp}.db").write_bytes(b"old")
    unrelated = out / "import-20261001T000000Z.db"  # another database's backup
    unrelated.write_bytes(b"keep me")

    code = backup_database.main(["--database-url", url, "--output-dir", str(out), "--keep", "2"])

    assert code == 0
    kept = sorted(p.name for p in out.glob("conversations-*.db"))
    assert len(kept) == 2 and "conversations-20261003T000000Z.db" in kept
    assert unrelated.exists()
    newest = max(out.glob("conversations-*.db"))
    # One self-contained file: no write-ahead log or shared-memory file beside it.
    assert not list(out.glob("*-wal")) and not list(out.glob("*-shm"))
    with sqlite3.connect(newest) as copy:
        assert copy.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert copy.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    assert "verified" in capsys.readouterr().out


def test_a_backup_that_fails_verification_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    url = make_database(tmp_path / "conversations.db")
    out = tmp_path / "backups"
    monkeypatch.setattr(backup_database, "verify", lambda path: ["integrity_check: 1 problem(s)"])

    assert backup_database.main(["--database-url", url, "--output-dir", str(out)]) == 1
    assert list(out.glob("*.db")) == []
    assert "FAILED verification" in capsys.readouterr().out


def test_nothing_to_back_up_for_an_in_memory_database(tmp_path: Path) -> None:
    code = backup_database.main(["--database-url", "sqlite://", "--output-dir", str(tmp_path)])

    assert code == 2
