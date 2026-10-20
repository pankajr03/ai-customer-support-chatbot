"""Backup and restore, persistent storage across ``down``/``up``, and container restart.

Last in the run: these stop and restart the stack.
"""

import json
import time

from tests.deployment.conftest import HTTPS_PORT, Stack, chat, new_conversation

# A new file in the container's /tmp: a test restore never touches the active database.
RESTORED = "/tmp/restore-test/conversations.db"  # noqa: S108 - inside the container
KILL_SERVER = (
    "import os, signal\n"
    "me = os.getpid()\n"
    "for pid in os.listdir('/proc'):\n"
    "    if pid.isdigit() and int(pid) not in (1, me):\n"
    "        try: cmd = open(f'/proc/{pid}/cmdline', 'rb').read()\n"
    "        except OSError: continue\n"
    "        if b'uvicorn' in cmd: os.kill(int(pid), signal.SIGKILL)\n"
)


def test_backup_and_restore(stack: Stack) -> None:
    sid, auth = new_conversation(stack)
    assert chat(stack, sid, auth, "What is your return policy?").status_code == 200

    backend = ("exec", "-T", "backend", "python", "-m")
    backup = stack.compose(*backend, "scripts.backup_database")
    path = next(word for word in backup.stdout.split() if word.endswith(".db"))
    verified = stack.compose(*backend, "scripts.restore_database", path, "--verify-only",
                             check=False)  # fmt: skip
    restored = stack.compose(*backend, "scripts.restore_database", path, "--to", RESTORED,
                             check=False)  # fmt: skip
    count = (
        "import sqlite3, sys; db = sqlite3.connect(sys.argv[1]); print(db.execute("
        "'SELECT count(*) FROM conversation_messages WHERE session_id = ?', "
        "(sys.argv[2],)).fetchone()[0])"
    )
    rows = stack.compose("exec", "-T", "backend", "python", "-c", count, RESTORED,
                         sid.replace("-", "")).stdout.strip()  # fmt: skip

    assert path.startswith("/app/data/backups/")
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert rows == "2"  # the conversation is in the restored copy
    # The active database was never touched by the test restore.
    history = stack.get(HTTPS_PORT, f"/api/v1/conversations/{sid}/messages", headers=auth)
    assert history.json()["total_messages"] == 2


def test_data_survives_down_and_up(stack: Stack) -> None:
    sid, auth = new_conversation(stack)
    assert chat(stack, sid, auth, "How can I track my order?").status_code == 200

    stack.compose("down", timeout=300)  # containers removed, volumes kept
    stack.compose("up", "-d", timeout=600)
    stack.wait_healthy()

    history = stack.get(HTTPS_PORT, f"/api/v1/conversations/{sid}/messages", headers=auth)
    assert history.status_code == 200
    assert [m["role"] for m in history.json()["messages"]] == ["user", "assistant"]


def test_the_backend_restarts_after_its_process_dies(stack: Stack) -> None:
    container = stack.container_id("backend")
    before = int(stack.docker("inspect", container, "--format", "{{.RestartCount}}").stdout)

    stack.python_in("backend", KILL_SERVER, check=False)  # the server, not tini (PID 1)
    started = time.monotonic()
    while time.monotonic() - started < 120:
        after = int(stack.docker("inspect", container, "--format", "{{.RestartCount}}").stdout)
        if after > before:
            break
        time.sleep(1)
    recovered = stack.wait_healthy(("backend",))
    state = json.loads(stack.docker("inspect", container, "--format", "{{json .State}}").stdout)

    assert after == before + 1  # restarted by "restart: unless-stopped"
    assert state["Status"] == "running" and recovered < 120
    assert stack.get(HTTPS_PORT, "/healthz").json() == {"status": "ready"}
