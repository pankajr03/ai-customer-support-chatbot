"""Network-filesystem guard (Stage 13, SQLite review). Backups: tests/test_backup_restore.py."""

import logging
from pathlib import Path

import pytest

from app.core.config import Settings
from app.database import connection
from app.database.connection import Database, DatabaseError, filesystem_type
from app.main import create_app

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
