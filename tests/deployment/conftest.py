"""Local deployment verification: the real Docker stack, built and run (Stage 13, Task 22).

These tests build the three images from the working tree, start the production compose
stack (backend, customer page, staff dashboard, Nginx, backup scheduler) under its own
project name, and check it from outside, the way it is used.

They run **only when asked**: set ``RUN_DEPLOYMENT_TESTS=1``. They also need a working
Docker Engine with Compose v2. Otherwise every test here is **skipped** with the reason
(shown in pytest's summary, ``-ra``); a skipped test is never counted as passed.

    RUN_DEPLOYMENT_TESTS=1 pytest tests/deployment -v
    # Docker inside WSL, tests on Windows:
    $env:RUN_DEPLOYMENT_TESTS="1"; $env:DEPLOYMENT_DOCKER="wsl.exe -e docker"
    pytest tests/deployment -v

What it creates, and removes again:

- a copy of the working tree (tracked and new files, no ``.env``, no database) in a
  temporary folder, with test-only configuration: the production environment examples
  as ``*.env`` (``LLM_ENABLED=false``: no OpenAI request, nothing paid), two staff
  tokens, a self-signed certificate for ``localhost`` (generated with the backend
  image's openssl) and a basic-auth file;
- the compose project ``chatbot-deploytest``: containers, networks, and the ``db`` and
  ``backups`` volumes are removed at the end. The ``models`` volume is kept, so the
  embedding model (about 92 MB from Hugging Face) is fetched once, not on every run;
- host ports 18080, 18443 and 18444 on ``127.0.0.1``.

The stack uses compose's fixed subnets (172.30.1.0/24, 172.30.2.0/24): it cannot run
next to another stack started from ``deploy/compose.yaml`` on the same Docker host.
"""

import base64
import hashlib
import json
import os
import secrets
import shlex
import shutil
import ssl
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.staff_auth import generate_staff_token, hash_staff_token

ROOT = Path(__file__).resolve().parents[2]
RUN_ENV = "RUN_DEPLOYMENT_TESTS"
DOCKER_ENV = "DEPLOYMENT_DOCKER"  # the docker command, e.g. "wsl.exe -e docker"
PROJECT = "chatbot-deploytest"
TAG = "deploytest"
HTTP_PORT, HTTPS_PORT, ADMIN_PORT = 18080, 18443, 18444
HOSTNAME = "localhost"  # Nginx refuses requests by IP address: names only
SERVICES = ("backend", "customer", "admin", "nginx")
BUILD_TIMEOUT = 1800
HEALTH_TIMEOUT = 300


def docker_command() -> list[str]:
    return shlex.split(os.environ.get(DOCKER_ENV, "docker"))


def docker_unavailable() -> str | None:
    """Why Docker cannot be used here, or None when the engine and Compose v2 answer."""
    for args in (["version", "--format", "{{.Server.Version}}"], ["compose", "version"]):
        try:
            result = subprocess.run(  # noqa: S603 - the configured docker command
                [*docker_command(), *args], capture_output=True, text=True, timeout=60
            )
        except FileNotFoundError:
            return f"the docker command {docker_command()[0]!r} was not found"
        except subprocess.TimeoutExpired:
            return f"'docker {' '.join(args)}' did not answer within 60 s"
        if result.returncode != 0:
            message = (result.stderr or result.stdout).replace("\0", "").strip().splitlines()
            return f"'docker {' '.join(args)}' failed: {message[-1] if message else '?'}"
    return None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    here = [i for i in items if "tests/deployment/" in i.nodeid.replace("\\", "/")]
    if not here:
        return
    for item in here:
        item.add_marker(pytest.mark.deployment)
    if os.environ.get(RUN_ENV) != "1":
        reason = (f"deployment test: builds and runs the Docker stack (minutes); "
                  f"set {RUN_ENV}=1 to run it")  # fmt: skip
    else:
        problem = docker_unavailable()
        reason = f"deployment test: Docker is not available ({problem})" if problem else ""
    if reason:
        for item in here:
            item.add_marker(pytest.mark.skip(reason=reason))


def host_path(path: Path) -> str:
    """``path`` as the Docker CLI sees it (a WSL path when docker runs in WSL)."""
    if os.name == "nt" and Path(docker_command()[0]).name.lower().startswith("wsl"):
        posix = path.resolve().as_posix()
        return f"/mnt/{posix[0].lower()}{posix[2:]}"
    return str(path)


# --- The stack ---------------------------------------------------------------------------

# Run inside a container (standard library only): one HTTP request described by argv[1].
REQUEST_SCRIPT = """
import json, sys, urllib.error, urllib.request
r = json.loads(sys.argv[1])
data = json.dumps(r["body"]).encode() if r["body"] is not None else None
headers = {"Content-Type": "application/json", **r["headers"]}
request = urllib.request.Request(r["url"], data=data, method=r["method"], headers=headers)
try:
    response = urllib.request.urlopen(request, timeout=60)
    status, raw, received = response.status, response.read(), response.headers
except urllib.error.HTTPError as error:
    status, raw, received = error.code, error.read(), error.headers
print(json.dumps({"status": status, "body": raw.decode("utf-8", "replace"),
                  "headers": {k.lower(): v for k, v in received.items()}}))
"""


@dataclass
class Stack:
    root: Path
    admin_token: str
    support_token: str
    basic_auth: tuple[str, str]
    build_seconds: float = 0.0
    build_output: str = ""
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def deploy(self) -> Path:
        return self.root / "deploy"

    @property
    def certificate(self) -> Path:
        return self.deploy / "nginx" / "certs" / "fullchain.pem"

    def docker(self, *args: str, check: bool = True, timeout: float = 600,
               ) -> subprocess.CompletedProcess[str]:  # fmt: skip
        result = subprocess.run(  # noqa: S603 - the configured docker command
            [*docker_command(), *args], capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )  # fmt: skip
        result.stdout, result.stderr = (
            result.stdout.replace("\0", ""),
            result.stderr.replace("\0", ""),
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"docker {' '.join(args[:4])} … exited {result.returncode}:\n"
                f"{(result.stderr or result.stdout)[-2000:]}"
            )
        return result

    def compose(self, *args: str, env_file: Path | None = None, check: bool = True,
                timeout: float = 600) -> subprocess.CompletedProcess[str]:  # fmt: skip
        return self.docker(
            "compose", "-p", PROJECT, "--project-directory", host_path(self.deploy),
            "-f", host_path(self.deploy / "compose.yaml"),
            "--env-file", host_path(env_file or self.deploy / ".env"),
            *args, check=check, timeout=timeout,
        )  # fmt: skip

    def services(self) -> dict[str, dict[str, Any]]:
        """``docker compose ps`` by service: State, Health, Name, ..."""
        out = self.compose("ps", "--all", "--format", "json").stdout.strip()
        rows = json.loads(out) if out.startswith("[") else [
            json.loads(line) for line in out.splitlines() if line.strip()]  # fmt: skip
        return {row["Service"]: row for row in rows}

    def wait_healthy(self, services: tuple[str, ...] = SERVICES,
                     timeout: float = HEALTH_TIMEOUT) -> float:  # fmt: skip
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            rows = self.services()
            if all(rows.get(s, {}).get("Health") == "healthy" for s in services):
                return time.monotonic() - started
            time.sleep(2)
        health = {s: rows.get(s, {}).get("Health") for s in services}
        raise AssertionError(f"not healthy within {timeout:.0f} s: {health}")

    def python_in(self, service: str, code: str, *, check: bool = True,
                  ) -> subprocess.CompletedProcess[str]:  # fmt: skip
        return self.compose("exec", "-T", service, "python", "-c", code, check=check)

    def request_in(self, service: str, method: str, url: str, body: Any = None,
                   headers: dict[str, str] | None = None,
                   ) -> tuple[int, Any, dict[str, str]]:  # fmt: skip
        """An HTTP request made from inside ``service``'s container (its networks)."""
        request = json.dumps({"url": url, "method": method, "body": body,
                              "headers": headers or {}})  # fmt: skip
        result = self.compose("exec", "-T", service, "python", "-c", REQUEST_SCRIPT, request)
        out = json.loads(result.stdout.strip().splitlines()[-1])
        try:
            body_json = json.loads(out["body"])
        except ValueError:
            body_json = out["body"]
        return out["status"], body_json, out["headers"]

    def client(self, port: int, **kwargs: Any) -> httpx.Client:
        """HTTPS to the published Nginx port, verifying the test certificate for
        ``localhost`` (connected through 127.0.0.1, the name sent in SNI and Host)."""
        trust = ssl.create_default_context(cafile=str(self.certificate))
        return httpx.Client(
            base_url=f"https://127.0.0.1:{port}", verify=trust,
            headers={"Host": HOSTNAME}, timeout=60, follow_redirects=False, **kwargs,
        )  # fmt: skip

    def get(self, port: int, path: str, **kwargs: Any) -> httpx.Response:
        with self.client(port) as client:
            return client.get(path, extensions={"sni_hostname": HOSTNAME}, **kwargs)

    def send(self, port: int, method: str, path: str, **kwargs: Any) -> httpx.Response:
        with self.client(port) as client:
            return client.request(method, path, extensions={"sni_hostname": HOSTNAME}, **kwargs)

    def container_id(self, service: str) -> str:
        return self.compose("ps", "-q", service).stdout.strip()


def export_working_tree(target: Path) -> None:
    """Tracked and new (not ignored) files: no .env, database, models or virtualenv."""
    command = ["git", "ls-files", "-co", "--exclude-standard", "-z"]
    listed = (
        subprocess.run(  # noqa: S603 - git from PATH, fixed arguments
            command, cwd=ROOT, capture_output=True, check=True
        )
        .stdout.decode()
        .split("\0")
    )
    for name in filter(None, listed):
        source = ROOT / name
        if source.is_file():
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def write_env(path: Path, example: Path, overrides: dict[str, str]) -> None:
    lines, seen = [], set()
    for line in example.read_text(encoding="utf-8").splitlines():
        key = line.split("=", 1)[0].strip()
        if not line.lstrip().startswith("#") and "=" in line and key in overrides:
            lines.append(f"{key}={overrides[key]}")
            seen.add(key)
        else:
            lines.append(line)
    lines += [f"{k}={v}" for k, v in overrides.items() if k not in seen]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def ssha(password: str) -> str:
    """An Nginx-compatible {SSHA} password hash (salted SHA-1; test credentials only)."""
    salt = secrets.token_bytes(8)
    digest = hashlib.sha1(password.encode() + salt).digest()  # noqa: S324 - Nginx's format
    return "{SSHA}" + base64.b64encode(digest + salt).decode()


@pytest.fixture(scope="session")
def built(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    """The working tree exported, configured for a test deployment, and built."""
    root = tmp_path_factory.mktemp("deploytest")
    export_working_tree(root)
    deploy = root / "deploy"
    admin_token, support_token = generate_staff_token(), generate_staff_token()
    stack = Stack(root, admin_token, support_token, ("staff", secrets.token_urlsafe(18)))

    write_env(deploy / ".env", deploy / ".env.example", {
        "DEPLOY_ENV": "production", "IMAGE_TAG": TAG, "PUBLIC_HOSTNAME": HOSTNAME,
        "PUBLIC_BIND": "127.0.0.1", "PUBLIC_HTTP_PORT": str(HTTP_PORT),
        "PUBLIC_HTTPS_PORT": str(HTTPS_PORT), "ADMIN_BIND": "127.0.0.1",
        "ADMIN_HTTPS_PORT": str(ADMIN_PORT), "ADMIN_HOSTNAME": HOSTNAME,
        "BACKUP_RUN_ON_START": "false",
    })  # fmt: skip
    environments = deploy / "environments" / "production"
    hashes = [f"alex:admin:{hash_staff_token(admin_token)}",
              f"maria:support:{hash_staff_token(support_token)}"]  # fmt: skip
    write_env(environments / "backend.env", environments / "backend.env.example",
              {"STAFF_TOKEN_HASHES": json.dumps(hashes), "LLM_ENABLED": "false",
               "OPENAI_API_KEY": ""})  # fmt: skip
    for page in ("customer", "admin"):
        write_env(environments / f"{page}.env", environments / f"{page}.env.example", {})
    (deploy / "nginx" / "admin.htpasswd").write_text(
        f"{stack.basic_auth[0]}:{ssha(stack.basic_auth[1])}\n", encoding="utf-8", newline="\n"
    )

    started = time.monotonic()
    result = stack.compose("build", check=False, timeout=BUILD_TIMEOUT)
    stack.build_seconds = time.monotonic() - started
    stack.build_output = (result.stdout + result.stderr)[-4000:]
    if result.returncode != 0:
        pytest.fail(f"docker compose build failed:\n{stack.build_output}")

    certs = deploy / "nginx" / "certs"
    certs.mkdir(parents=True, exist_ok=True)
    stack.docker("run", "--rm", "--user", "0", "--entrypoint", "sh",
                 "-v", f"{host_path(certs)}:/out", f"support-chatbot-backend:{TAG}", "-c",
                 "openssl req -x509 -newkey rsa:2048 -nodes -days 2 "
                 "-subj '/O=DEPLOYMENT TEST ONLY/CN=localhost' "
                 "-addext 'subjectAltName=DNS:localhost' "
                 "-keyout /out/privkey.pem -out /out/fullchain.pem 2>/dev/null "
                 "&& chmod 644 /out/privkey.pem /out/fullchain.pem")  # fmt: skip
    yield stack


@pytest.fixture(scope="session")
def stack(built: Stack) -> Iterator[Stack]:
    """The built stack, with the model fetched and every service started and healthy."""
    started = time.monotonic()
    built.compose("--profile", "setup", "run", "--rm", "model-init", timeout=900)
    built.timings["model_init_seconds"] = time.monotonic() - started
    started = time.monotonic()
    built.compose("up", "-d", timeout=900)
    built.timings["up_to_healthy_seconds"] = built.wait_healthy() + (time.monotonic() - started)
    try:
        yield built
    finally:
        built.compose("down", "--remove-orphans", check=False, timeout=300)
        for volume in ("db", "backups"):
            built.docker("volume", "rm", "-f", f"{PROJECT}_{volume}", check=False)


def new_conversation(stack: Stack) -> tuple[str, dict[str, str]]:
    """A conversation created through Nginx, as the customer's browser would."""
    response = stack.send(HTTPS_PORT, "POST", "/api/v1/conversations")
    assert response.status_code == 201, response.text
    body = response.json()
    return body["session_id"], {"Authorization": f"Bearer {body['access_token']}"}


def chat(stack: Stack, sid: str, auth: dict[str, str], message: str) -> httpx.Response:
    body = {"message": message, "client_message_id": str(uuid.uuid4())}
    return stack.send(HTTPS_PORT, "POST", f"/api/v1/conversations/{sid}/chat", headers=auth,
                      json=body)  # fmt: skip
