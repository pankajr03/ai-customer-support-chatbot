"""Rollback, tested on the running stack (Stage 13, Task 24). docs/deployment-rollback.md.

Two more backend images are built from copies of the working tree:

- ``deploytest-v1b``: the same code under another revision (a release without a schema
  change);
- ``deploytest-v2``: the code with one more column in ``support_tickets`` (a release with
  a schema change).

Then: a rollback without a schema change (v1b back to v1: data intact); an upgrade to v2
(a verified backup first, then the new column); a rollback from v2 to v1, which the older
release refuses instead of discarding the column; and the documented manual recovery
(restore the pre-upgrade backup with the older release), which loses what v2 wrote.
"""

import re
import shutil
import time
from collections.abc import Iterator

import pytest

from tests.deployment.conftest import TAG, Stack, chat, new_conversation

V1B, V2 = f"{TAG}-v1b", f"{TAG}-v2"
NEW_COLUMN = "satisfaction_score"


def set_image_tag(stack: Stack, tag: str) -> None:
    env = stack.deploy / ".env"
    text = re.sub(r"^IMAGE_TAG=.*$", f"IMAGE_TAG={tag}", env.read_text(), flags=re.M)
    env.write_text(text, newline="\n")


def build_backend(stack: Stack, tag: str, *, add_column: bool) -> None:
    """The backend image of another release, from a copy of the working tree."""
    root = stack.root.parent / f"release-{tag}"
    shutil.rmtree(root, ignore_errors=True)
    shutil.copytree(stack.root, root)
    if add_column:
        models = root / "app" / "database" / "models.py"
        text = models.read_text(encoding="utf-8")
        last = "    anonymized_at: Mapped[datetime | None] = mapped_column(UTCDateTime)\n"
        following = "\n\nclass TicketEventRow"
        assert last + following in text
        added = f"    {NEW_COLUMN}: Mapped[int | None] = mapped_column(Integer)\n"
        models.write_text(text.replace(last + following, last + added + following),
                          encoding="utf-8")  # fmt: skip
    release = Stack(root, stack.admin_token, stack.support_token, stack.basic_auth)
    set_image_tag(release, tag)
    release.compose("build", "backend", timeout=1800)


def switch_backend(stack: Stack, tag: str) -> None:
    set_image_tag(stack, tag)
    stack.compose("up", "-d", "--no-deps", "backend", timeout=300)


def database_has_column(stack: Stack) -> bool:
    code = (
        "import sqlite3; c = sqlite3.connect('file:/var/lib/chatbot/conversations.db?mode=ro', "
        "uri=True); print([r[1] for r in c.execute('PRAGMA table_info(support_tickets)')])"
    )
    out = stack.compose("run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "backend",
                        "-c", code).stdout  # fmt: skip
    return NEW_COLUMN in out


@pytest.fixture(scope="module")
def releases(stack: Stack) -> Iterator[Stack]:
    build_backend(stack, V1B, add_column=False)
    build_backend(stack, V2, add_column=True)
    try:
        yield stack
    finally:
        set_image_tag(stack, TAG)
        for tag in (V1B, V2):
            stack.docker("image", "rm", "-f", f"support-chatbot-backend:{tag}", check=False)


def test_a_rollback_without_a_schema_change_keeps_everything(releases: Stack) -> None:
    stack = releases
    sid, auth = new_conversation(stack)
    assert chat(stack, sid, auth, "How can I track my order?").status_code == 200

    switch_backend(stack, V1B)
    stack.wait_healthy(("backend",))
    switch_backend(stack, TAG)  # the rollback
    stack.wait_healthy(("backend",))

    history = stack.get(18443, f"/api/v1/conversations/{sid}/messages", headers=auth)
    assert history.json()["total_messages"] == 2
    releases_out = stack.compose("exec", "-T", "backend", "python", "-m",
                                 "scripts.release_history").stdout  # fmt: skip
    assert f"revision {V1B}" in releases_out and f"revision {TAG}" in releases_out


def test_an_upgrade_with_a_schema_change_backs_up_first(releases: Stack) -> None:
    stack = releases
    switch_backend(stack, V2)
    stack.wait_healthy(("backend",))

    logs = stack.compose("logs", "--no-log-prefix", "backend").stdout
    assert "Backed up the database before upgrading its schema" in logs
    assert "support_tickets" in logs  # what was changed is named
    assert database_has_column(stack)
    history = stack.compose("exec", "-T", "backend", "python", "-m",
                            "scripts.release_history").stdout  # fmt: skip
    assert "Backups taken before a schema upgrade" in history and "  none" not in history


def test_rolling_back_past_the_schema_change_is_refused_then_recovered_by_hand(
    releases: Stack,
) -> None:
    stack = releases
    # Written by v2, after its upgrade: lost by the recovery, as documented.
    sid, auth = new_conversation(stack)
    assert chat(stack, sid, auth, "What is your return policy?").status_code == 200

    switch_backend(stack, TAG)  # the older release
    container = stack.container_id("backend")
    started = time.monotonic()
    refused = ""
    while time.monotonic() - started < 120 and "does not know" not in refused:
        refused = stack.docker("logs", container, check=False).stdout + stack.docker(
            "logs", container, check=False).stderr  # fmt: skip
        time.sleep(2)

    assert f"support_tickets.{NEW_COLUMN}" in refused and "Nothing was changed" in refused
    assert stack.services()["backend"].get("Health") != "healthy"
    assert database_has_column(stack)  # untouched: nothing discarded

    # The manual recovery (docs/deployment-rollback.md): stop, restore the pre-upgrade
    # backup with the older release, start.
    stack.compose("stop", "backend", timeout=120)
    listing = stack.compose("run", "--rm", "--no-deps", "-T", "backend", "python", "-m",
                            "scripts.release_history").stdout  # fmt: skip
    backup = re.findall(r"(conversations-\d{8}T\d{6}Z(?:-\d+)?\.db)  schema", listing)[-1]
    restored = stack.compose("run", "--rm", "--no-deps", "-T", "backend", "python", "-m",
                             "scripts.restore_database", f"/app/data/backups/{backup}",
                             "--replace-active", "--confirm", "conversations.db",
                             check=False)  # fmt: skip
    assert restored.returncode == 0, restored.stdout + restored.stderr
    stack.compose("up", "-d", "--no-deps", "backend", timeout=300)
    stack.wait_healthy(("backend",))

    assert not database_has_column(stack)
    gone = stack.get(18443, f"/api/v1/conversations/{sid}/messages", headers=auth)
    assert gone.status_code == 404  # written after the upgrade: lost with the rollback
    assert stack.get(18443, "/healthz").json() == {"status": "ready"}
