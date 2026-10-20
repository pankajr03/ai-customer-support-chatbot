"""Shutdown, crash and restart behaviour of the production containers (Stage 13).

Run where Docker runs, with Python 3.10+ and nothing else (standard library only):

    python3 scripts/evaluate_restarts.py --tag restart --output restart-results.json

Before: build the images (``docker build -f deploy/Dockerfile --target backend -t
support-chatbot-backend:<tag> .``, the same for ``customer``). The model is fetched once
into a test volume with the image's own ``scripts.fetch_embedding_model`` (about 92 MB
from Hugging Face, no account); ``--model-volume`` reuses one.

The backend runs as compose runs it: ``--init`` (tini as PID 1), ``--restart
unless-stopped``, read-only root filesystem, 2 GB / 2 CPUs, a named volume for the
database and the model volume read-only, ``SEMANTIC_INDEX_PRELOAD=background``, the
image's health check, and a 30 s stop grace period (compose's ``stop_grace_period``).
Requests go straight to the container's address on the Docker network (no port
publishing). No LLM is configured: ``LLM_ENABLED=false``, nothing paid.

Scenarios:

1. ``clean_start``: start to healthy, model files verified, semantic index ready.
2. ``graceful_stop_and_persistence``: a conversation, a ticket and an FAQ edit; ``docker
   stop``; the shutdown sequence and exit code; ``docker start``; everything still there,
   including the edited FAQ in semantic search.
3. ``in_flight_within_grace``: a chat that loads the model (preload off), ``docker stop``
   during it: does the client get its answer before the process exits?
4. ``in_flight_beyond_grace``: the same with the CPU limited so the request outlives the
   20 s graceful timeout: what does the client get, what is stored, and what happens
   when it retries with the same ``client_message_id`` after the restart?
5. ``crash_and_restart_policy``: the server process killed (SIGKILL) during write load;
   the restart policy restarts it; health recovers; nothing acknowledged is lost and no
   exchange is half-written.
6. ``page_crash_and_restart_policy``: the customer page's process killed; restarted.

Containers, the network and the database volume are removed at the end.
"""

# Runs the docker CLI on purpose, opens http:// URLs of its own test containers only, and
# passes container-side /tmp mounts: not the risks these rules look for.
# ruff: noqa: S108, S310, S603

import argparse
import json
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

PREFIX = "t21"
NETWORK = f"{PREFIX}-net"
DB_VOLUME = f"{PREFIX}-db"
BACKEND = f"{PREFIX}-backend"
PAGE = f"{PREFIX}-customer"
GRACE = 30  # compose's stop_grace_period for the backend
API = "/api/v1"
QUESTION = "What is your return policy?"
EDITED_FAQ = "faq_002"

DB_CHECK = r"""
import json, sqlite3
db = sqlite3.connect("file:/var/lib/chatbot/conversations.db?mode=ro", uri=True)
broken = 0
for (sid,) in db.execute("SELECT session_id FROM conversation_sessions"):
    rows = db.execute("SELECT sequence, role FROM conversation_messages WHERE session_id = ? "
                      "ORDER BY sequence", (sid,)).fetchall()
    if [s for s, _ in rows] != list(range(1, len(rows) + 1)) or any(
            r != ("user" if s % 2 else "assistant") for s, r in rows):
        broken += 1
print(json.dumps({
    "integrity_check": db.execute("PRAGMA integrity_check").fetchone()[0],
    "foreign_key_violations": len(db.execute("PRAGMA foreign_key_check").fetchall()),
    "conversations": db.execute("SELECT count(*) FROM conversation_sessions").fetchone()[0],
    "messages": db.execute("SELECT count(*) FROM conversation_messages").fetchone()[0],
    "half_written_or_misnumbered": broken,
    "leases_held": db.execute("SELECT count(*) FROM conversation_sessions "
                              "WHERE processing_key IS NOT NULL").fetchone()[0],
    "tickets": db.execute("SELECT count(*) FROM support_tickets").fetchone()[0],
}))
"""

KILL_SERVER = r"""
import os, signal
me = os.getpid()
for pid in os.listdir("/proc"):
    if pid.isdigit() and int(pid) not in (1, me):
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read()
        except OSError:
            continue
        if b"uvicorn" in cmd or b"streamlit" in cmd:
            os.kill(int(pid), signal.SIGKILL)
            print(pid)
"""


class Docker:
    def __init__(self, command: str, tag: str, model_volume: str) -> None:
        self.cmd = shlex.split(command)
        self.tag = tag
        self.model_volume = model_volume
        self.staff_token = "stf_" + uuid.uuid4().hex + uuid.uuid4().hex[:11]
        self._hash = ""  # set by staff_hash() once the image exists

    def run(self, *args: str, check: bool = True) -> str:
        result = subprocess.run([*self.cmd, *args], capture_output=True, text=True, check=False)
        if check and result.returncode != 0:
            raise RuntimeError(f"docker {args[0]}: {result.stderr.strip()[:400]}")
        return result.stdout.strip()

    def inspect(self, name: str, template: str) -> str:
        return self.run("inspect", name, "--format", template, check=False)

    def ip(self, name: str) -> str:
        return self.inspect(
            name, f'{{{{(index .NetworkSettings.Networks "{NETWORK}").IPAddress}}}}'
        )

    def staff_hash(self) -> str:
        image = f"support-chatbot-backend:{self.tag}"
        code = (
            f"from app.core.staff_auth import hash_staff_token as h; print(h({self.staff_token!r}))"
        )
        out = self.run("run", "--rm", "--entrypoint", "python", image, "-c", code)
        return out.splitlines()[-1]

    def start_backend(self, *, preload: str = "background", cpus: str = "2.0") -> float:
        self.run("rm", "-f", BACKEND, check=False)
        started = time.monotonic()
        self.run("run", "-d", "--name", BACKEND, "--network", NETWORK, "--init",
                 "--restart", "unless-stopped", "--read-only", "--tmpfs", "/tmp:size=256m",
                 "--memory", "2g", "--cpus", cpus, "--stop-timeout", str(GRACE),
                 "-v", f"{DB_VOLUME}:/var/lib/chatbot", "-v", f"{self.model_volume}:/models:ro",
                 "-e", "RATE_LIMIT_ENABLED=false", "-e", f"SEMANTIC_INDEX_PRELOAD={preload}",
                 "-e", f"STAFF_TOKEN_HASHES={json.dumps(['ops:admin:' + self._hash])}",
                 f"support-chatbot-backend:{self.tag}")  # fmt: skip
        return started

    def wait_healthy(self, name: str, started: float, timeout: float = 240) -> float | None:
        while time.monotonic() - started < timeout:
            if self.inspect(name, "{{.State.Health.Status}}") == "healthy":
                return round(time.monotonic() - started, 1)
            time.sleep(0.5)
        return None

    def db_check(self) -> dict[str, Any]:
        return json.loads(self.run("exec", BACKEND, "python", "-c", DB_CHECK).splitlines()[-1])


def lower(headers: Any) -> dict[str, str]:
    """Header names in lower case (servers may send either; HTTP ignores case)."""
    return {name.lower(): value for name, value in headers.items()}


def http(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None,
         timeout: float = 300) -> tuple[Any, Any, dict[str, str]]:  # fmt: skip
    """(status or error name, JSON body or None, headers)."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", **(headers or {})})  # fmt: skip
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None, lower(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        return exc.code, payload, lower(exc.headers)
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return type(reason).__name__, None, {}


class Api:
    def __init__(self, docker: Docker) -> None:
        self.docker = docker

    @property
    def base(self) -> str:
        return f"http://{self.docker.ip(BACKEND)}:8000{API}"

    def staff(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.docker.staff_token}"}

    def conversation(self) -> tuple[str, dict[str, str]]:
        status, body, _ = http("POST", f"{self.base}/conversations")
        if status != 201:
            raise RuntimeError(f"creating a conversation failed: {status}")
        return body["session_id"], {"Authorization": f"Bearer {body['access_token']}"}

    def chat(self, sid: str, auth: dict[str, str], key: str, timeout: float = 300) -> tuple:
        return http("POST", f"{self.base}/conversations/{sid}/chat",
                    {"message": QUESTION, "client_message_id": key}, auth, timeout)  # fmt: skip

    def history(self, sid: str, auth: dict[str, str]) -> list[dict[str, Any]]:
        status, body, _ = http("GET", f"{self.base}/conversations/{sid}/messages?limit=100",
                               headers=auth)  # fmt: skip
        return body["messages"] if status == 200 else []

    def semantic_ready(self, timeout: float = 240) -> float | None:
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            status, body, _ = http("GET", f"{self.base}/ready", timeout=5)
            if status == 200 and body["checks"]["semantic_search"] == "ready":
                return round(time.monotonic() - started, 1)
            time.sleep(0.5)
        return None


def shutdown_log(docker: Docker) -> list[str]:
    """uvicorn's shutdown messages, in order, from the backend's log."""
    logs = docker.run("logs", BACKEND, check=False)
    wanted = ("Shutting down", "Waiting for", "Cancel", "Application shutdown complete",
              "Finished server process", "timeout graceful")  # fmt: skip
    seen: list[str] = []
    for line in logs.splitlines():
        try:
            message = json.loads(line).get("message", "")
        except ValueError:
            message = line
        if any(w in message for w in wanted) and message not in seen:
            seen.append(message[:120])
    return seen


# --- Scenarios ---------------------------------------------------------------------------


def clean_start(docker: Docker, api: Api) -> dict[str, Any]:
    started = docker.start_backend()
    healthy = docker.wait_healthy(BACKEND, started)
    model = api.semantic_ready()
    logs = docker.run("logs", BACKEND)
    return {
        "seconds_to_healthy": healthy,
        "seconds_from_healthy_to_semantic_ready": model,
        "model_files_verified": "Embedding model files verified" in logs,
        "database": docker.db_check(),
    }


def graceful_stop_and_persistence(docker: Docker, api: Api) -> dict[str, Any]:
    sid, auth = api.conversation()
    first = api.chat(sid, auth, str(uuid.uuid4()))
    ticket_status, ticket, _ = http("POST", f"{api.base}/support/tickets", {
        "session_id": sid, "client_request_id": str(uuid.uuid4()), "ticket_type": "support",
        "subject": "Refund assistance", "description": "I need help with a refund request.",
        "contact": {"name": "Synthetic Customer", "email": "restart@example.com", "phone": None,
                    "contact_preference": "email", "consent_given": True},
        "confirmed": True}, auth)  # fmt: skip
    _, faq, _ = http("GET", f"{api.base}/admin/faqs/{EDITED_FAQ}", headers=api.staff())
    edited = faq["answer"].split(" [edited")[0] + f" [edited {uuid.uuid4().hex[:6]}]"
    edit_status, _, _ = http("PATCH", f"{api.base}/admin/faqs/{EDITED_FAQ}",
                             {"expected_version": faq["version"], "answer": edited},
                             api.staff())  # fmt: skip
    before = docker.db_check()

    stopping = time.monotonic()
    docker.run("stop", "-t", str(GRACE), BACKEND)
    stop_seconds = round(time.monotonic() - stopping, 1)
    exit_code = docker.inspect(BACKEND, "{{.State.ExitCode}}")
    sequence = shutdown_log(docker)

    starting = time.monotonic()
    docker.run("start", BACKEND)
    healthy = docker.wait_healthy(BACKEND, starting)
    semantic = api.semantic_ready()
    after = docker.db_check()
    _, public_faq, _ = http("GET", f"{api.base}/faqs/{EDITED_FAQ}")
    _, search, _ = http("POST", f"{api.base}/semantic-search",
                        {"query": public_faq["question"], "top_k": 1})  # fmt: skip
    ticket_after, _, _ = http("GET", f"{api.base}/support/tickets/{ticket['ticket_id']}",
                              headers=api.staff())  # fmt: skip
    return {
        "chat_before_stop": first[0],
        "ticket_created": ticket_status,
        "faq_edit_saved": edit_status,
        "stop_seconds": stop_seconds,
        "exit_code": exit_code,
        "shutdown_sequence": sequence,
        "seconds_to_healthy_after_start": healthy,
        "seconds_to_semantic_ready": semantic,
        "conversation_history_after_restart": len(api.history(sid, auth)),
        "ticket_after_restart": ticket_after,
        "edited_faq_served_after_restart": public_faq["answer"] == edited,
        "semantic_search_returns_edited_answer": bool(search and search["results"]
                                                      and search["results"][0]["answer"] == edited),
        "rows_before_stop": {k: before[k] for k in ("conversations", "messages", "tickets")},
        "rows_after_restart": {k: after[k] for k in ("conversations", "messages", "tickets")},
        "database_after_restart": after,
    }  # fmt: skip


def in_flight(docker: Docker, api: Api, *, cpus: str, stop_after: float) -> dict[str, Any]:
    """A chat whose turn loads the model (preload off), with ``docker stop`` during it."""
    started = docker.start_backend(preload="false", cpus=cpus)
    docker.wait_healthy(BACKEND, started)
    sid, auth = api.conversation()
    key = str(uuid.uuid4())
    outcome: dict[str, Any] = {}

    def send() -> None:
        sent = time.monotonic()
        status, body, _ = api.chat(sid, auth, key)
        decision = body.get("decision") if isinstance(body, dict) else None
        outcome.update(status=status, seconds=round(time.monotonic() - sent, 1), decision=decision)

    client = threading.Thread(target=send)
    client.start()
    time.sleep(stop_after)
    stopping = time.monotonic()
    docker.run("stop", "-t", str(GRACE), BACKEND)
    stop_seconds = round(time.monotonic() - stopping, 1)
    client.join(timeout=120)
    sequence = shutdown_log(docker)

    starting = time.monotonic()
    docker.run("update", "--cpus", "2.0", BACKEND)
    docker.run("start", BACKEND)
    docker.wait_healthy(BACKEND, starting)
    after_restart = docker.db_check()
    stored = sum(1 for m in api.history(sid, auth) if m["role"] == "user")
    # The client did not learn the outcome (or did): it retries with the same ID.
    retries: list[dict[str, Any]] = []
    retrying = time.monotonic()
    while time.monotonic() - retrying < 240:
        status, body, headers = api.chat(sid, auth, key)
        retries.append({"after_s": round(time.monotonic() - retrying, 1), "status": status,
                        "code": (body or {}).get("error", {}).get("code"),
                        "retry_after": headers.get("retry-after"),
                        "replayed": headers.get("idempotent-replayed") == "true"})  # fmt: skip
        if status == 200:
            break
        time.sleep(float(headers.get("retry-after") or 2))
    final = sum(1 for m in api.history(sid, auth) if m["role"] == "user")
    return {
        "cpus_during_request": cpus,
        "stop_sent_after_s": stop_after,
        "stop_seconds": stop_seconds,
        "client_outcome": outcome,
        "shutdown_sequence": sequence,
        "customer_messages_stored_before_retry": stored,
        "leases_held_after_restart": after_restart["leases_held"],
        "retries": retries if len(retries) <= 6 else retries[:3] + [{"...": len(retries) - 6}]
        + retries[-3:],
        "seconds_until_retry_answered": retries[-1]["after_s"] if retries[-1]["status"] == 200
        else None,
        "customer_messages_stored_after_retry": final,
    }  # fmt: skip


def crash_and_restart(docker: Docker, api: Api, conversations: int = 6) -> dict[str, Any]:
    started = docker.start_backend()
    docker.wait_healthy(BACKEND, started)
    api.semantic_ready()
    restarts_before = int(docker.inspect(BACKEND, "{{.RestartCount}}") or 0)
    sessions = [api.conversation() for _ in range(conversations)]
    acknowledged: list[tuple[str, str]] = []
    unknown: list[tuple[str, dict[str, str], str]] = []
    stop = threading.Event()

    def converse(sid: str, auth: dict[str, str]) -> None:
        while not stop.is_set():
            key = str(uuid.uuid4())
            status, body, _ = api.chat(sid, auth, key, timeout=30)
            if status == 200:
                acknowledged.append((sid, body["message_id"]))
            else:
                unknown.append((sid, auth, key))
                return

    threads = [threading.Thread(target=converse, args=s) for s in sessions]
    for thread in threads:
        thread.start()
    time.sleep(3)
    killed = docker.run("exec", BACKEND, "python", "-c", KILL_SERVER, check=False)
    crashed = time.monotonic()
    stop.set()
    for thread in threads:
        thread.join(timeout=60)
    healthy = docker.wait_healthy(BACKEND, crashed)
    restarts_after = int(docker.inspect(BACKEND, "{{.RestartCount}}") or 0)
    database = docker.db_check()
    stored = {m["message_id"] for sid, auth in sessions for m in api.history(sid, auth)}
    # Requests whose outcome the client never learned: retried with the same ID.
    retried: list[dict[str, Any]] = []
    for sid, auth, key in unknown:
        retrying = time.monotonic()
        while True:
            status, body, headers = api.chat(sid, auth, key)
            if status == 200 or time.monotonic() - retrying > 240:
                break
            time.sleep(float(headers.get("retry-after") or 2))
        retried.append({"status": status, "seconds": round(time.monotonic() - retrying, 1),
                        "replayed": headers.get("idempotent-replayed") == "true"})  # fmt: skip
    final = docker.db_check()
    return {
        "killed_pids": killed.split(),
        "restart_count": f"{restarts_before} -> {restarts_after}",
        "seconds_from_kill_to_healthy": healthy,
        "acknowledged_replies": len(acknowledged),
        "acknowledged_replies_lost": sum(1 for _, mid in acknowledged if mid not in stored),
        "requests_with_unknown_outcome": len(unknown),
        "database_after_restart": database,
        "retries_of_unknown_outcomes": retried,
        "database_after_retries": final,
    }


def page_crash(docker: Docker) -> dict[str, Any]:
    docker.run("rm", "-f", PAGE, check=False)
    started = time.monotonic()
    docker.run("run", "-d", "--name", PAGE, "--network", NETWORK, "--init", "--restart",
               "unless-stopped", "--read-only", "--tmpfs", "/tmp:size=64m", "--memory", "512m",
               "--cpus", "1.0", "-e", f"BACKEND_API_URL=http://{BACKEND}:8000",
               f"support-chatbot-customer:{docker.tag}")  # fmt: skip
    first = docker.wait_healthy(PAGE, started)
    killed = docker.run("exec", PAGE, "python", "-c", KILL_SERVER, check=False)
    crashed = time.monotonic()
    time.sleep(1)
    healthy = docker.wait_healthy(PAGE, crashed)
    return {"seconds_to_healthy": first, "killed_pids": killed.split(),
            "restart_count": docker.inspect(PAGE, "{{.RestartCount}}"),
            "seconds_from_kill_to_healthy": healthy}  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--tag", default="restart")
    parser.add_argument("--model-volume", default=f"{PREFIX}-models")
    parser.add_argument("--slow-cpus", default="0.3", help="CPU limit for scenario 4")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    docker = Docker(args.docker, args.tag, args.model_volume)

    docker.run("network", "create", NETWORK, check=False)
    docker.run("volume", "rm", "-f", DB_VOLUME, check=False)
    docker.run("volume", "create", DB_VOLUME)
    if args.model_volume not in docker.run("volume", "ls", "-q").split():
        docker.run("volume", "create", args.model_volume)
        docker.run("run", "--rm", "--read-only", "--tmpfs", "/tmp:size=1g", "-e", "HF_HOME=/tmp/hf",
                   "-e", "HF_HUB_OFFLINE=0", "-v", f"{args.model_volume}:/models",
                   f"support-chatbot-backend:{args.tag}", "python", "-m",
                   "scripts.fetch_embedding_model",
                   "--dest", "/models/all-MiniLM-L6-v2")  # fmt: skip
    docker._hash = docker.staff_hash()
    api = Api(docker)
    results: dict[str, Any] = {"image_tag": args.tag,
                               "measured_at": time.strftime("%Y-%m-%d %H:%M:%S")}  # fmt: skip
    try:
        steps = [
            ("clean_start", lambda: clean_start(docker, api)),
            ("graceful_stop_and_persistence", lambda: graceful_stop_and_persistence(docker, api)),
            ("in_flight_within_grace", lambda: in_flight(docker, api, cpus="2.0", stop_after=3)),
            ("in_flight_beyond_grace",
             lambda: in_flight(docker, api, cpus=args.slow_cpus, stop_after=3)),
            ("crash_and_restart_policy", lambda: crash_and_restart(docker, api)),
            ("page_crash_and_restart_policy", lambda: page_crash(docker)),
        ]  # fmt: skip
        for name, step in steps:
            print(f"== {name}", flush=True)
            try:
                results[name] = step()
            except Exception as exc:  # noqa: BLE001 - record and continue with the next
                results[name] = {"error": f"{type(exc).__name__}: {exc}"[:400]}
            print(json.dumps(results[name], indent=2), flush=True)
    finally:
        docker.run("rm", "-f", BACKEND, PAGE, check=False)
        docker.run("network", "rm", NETWORK, check=False)
        docker.run("volume", "rm", "-f", DB_VOLUME, check=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as file:
            json.dump(results, file, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
