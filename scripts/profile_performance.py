"""Profile the running application: startup, latency, memory, CPU, image sizes (Stage 13).

Usage (from the project root, virtual environment active):

    python scripts/profile_performance.py --target local
    python scripts/profile_performance.py --target docker --docker "wsl.exe -e docker"
        --model-dir <the all-MiniLM-L6-v2 folder, as the Docker host sees it>

Unlike ``measure_performance.py`` (Stage 9, everything in one process through
TestClient), this starts the real servers, the way they run in production, and measures
them from outside over HTTP on 127.0.0.1:

- ``--target local``: uvicorn and Streamlit as local processes from this virtual
  environment. Memory and CPU are read for the whole process tree (psutil, a development
  tool: ``pip install psutil``).
- ``--target docker``: the production images (``support-chatbot-{backend,customer,admin}``
  with ``--tag``, built from ``deploy/Dockerfile``) with the production limits from
  ``deploy/compose.yaml``: backend 2 GB and 2 CPUs, pages 512 MB and 1 CPU, read-only
  root filesystem. Memory and CPU are read from the container's cgroup (memory as
  ``docker stats`` reports it: current usage minus reclaimable file cache). Image sizes
  are read with ``docker image inspect``.

What is measured, and how (all wall-clock ``perf_counter`` on this machine):

- **startup**: from launching the process or ``docker run`` until ``/api/v1/health``
  (live) and ``/api/v1/ready`` (ready) answer 200, or the Streamlit page's
  ``/_stcore/health`` does. Repeated ``--startup-runs`` times;
- **embedding model load**: (local) a fresh Python process that imports the embedding
  stack, loads the model and embeds one text, repeated ``--model-runs`` times; (both) the
  backend's own ``faq_index_build_duration`` (model load and FAQ index build, triggered by
  the first chat turn, ``SEMANTIC_INDEX_PRELOAD=false`` as by default);
- **first chat request**: the first ``POST /conversations/{id}/chat`` after startup,
  which loads the model and builds the index on demand;
- **warm chat requests**: ``--turns`` chat turns over fixed conversations afterwards;
- **retrieval**: ``--searches`` calls each of ``GET /faqs/search`` (keyword),
  ``POST /semantic-search`` and ``POST /hybrid-search``, measured by the client (HTTP
  included) and, for chat, inside the backend (``retrieval_duration``);
- **database**: the backend's own ``db_transaction_duration`` (read and write
  transactions, including commit) and a history read over HTTP;
- **memory and CPU**: resident memory after startup, after the model load and after the
  warm phase, the peak, and CPU seconds per warm chat turn;
- **LLM generation**: never called. ``LLM_ENABLED=false`` is forced, so no paid request
  can be made; live OpenAI latency is reported as not measured.

The backend's server-side figures come from its in-memory metrics
(``GET /api/v1/admin/monitoring``, docs/monitoring.md), read with a staff token created
for this run only. Rate limits are switched off for the run (one client sending many
requests quickly is the point here). The conversation database is a temporary file
(local) or an anonymous volume removed afterwards (docker).

Logs and output contain durations, sizes and counts only, never message text.
"""

import argparse
import json
import os
import shlex
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/profile_performance.py`
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402

from app.core.staff_auth import generate_staff_token, hash_staff_token  # noqa: E402
from scripts.measure_performance import CONVERSATIONS, percentile  # noqa: E402

API = "/api/v1"
SEARCH_QUERIES = [
    "How can I track my order?",
    "return policy",
    "I want my money back",
    "reset my password",
    "Do you ship internationally?",
    "payment declined",
]
BACKEND_PORT = 18000
PAGE_PORTS = {"customer": 18501, "admin": 18502}
PAGE_SCRIPTS = {"customer": "frontend/app.py", "admin": "frontend/admin_app.py"}
MEASURED_ROUTES = {
    f"{API}/conversations/{{session_id}}/chat",
    f"{API}/conversations/{{session_id}}/messages",
    f"{API}/faqs/search",
    f"{API}/semantic-search",
    f"{API}/hybrid-search",
}
STARTUP_TIMEOUT_S = 180.0
FIRST_CHAT_TIMEOUT_S = 600.0
MIB = 1024 * 1024
# Production limits (deploy/compose.yaml).
LIMITS = {"backend": ("2g", "2.0"), "customer": ("512m", "1.0"), "admin": ("512m", "1.0")}


def summary(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "p50_ms": round(statistics.median(values), 1),
        "p95_ms": round(percentile(values, 0.95), 1),
        "max_ms": round(max(values), 1),
    }


def wait_for(url: str, deadline_s: float) -> float:
    """Seconds until ``url`` answers 200 (polled every 50 ms)."""
    started = time.perf_counter()
    while time.perf_counter() - started < deadline_s:
        try:
            if httpx.get(url, timeout=2.0).status_code == 200:
                return time.perf_counter() - started
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    raise TimeoutError(f"{url} did not answer 200 within {deadline_s:.0f} s")


def port_free(port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


# --- Targets ---------------------------------------------------------------------------


class Service:
    """A started server: its URL, a way to read its resources, and a way to stop it."""

    def __init__(
        self,
        url: str,
        stop: Callable[[], None],
        resources: Callable[[], dict[str, float]],
        launched_at: float,
    ) -> None:
        self.url, self.stop, self.resources, self.launched_at = url, stop, resources, launched_at
        self.peak_mib = 0.0
        self._sampling = False
        self._thread: threading.Thread | None = None

    def wait(self, path: str) -> float:
        """Seconds from launch until ``path`` answers 200."""
        wait_for(self.url + path, STARTUP_TIMEOUT_S)
        return time.perf_counter() - self.launched_at

    def sample_peak(self, interval_s: float) -> None:
        """Track the highest memory reading in the background until ``stop_sampling``."""

        def loop() -> None:
            while self._sampling:
                try:
                    self.peak_mib = max(self.peak_mib, self.resources()["memory_mib"])
                except Exception:  # noqa: BLE001 - the process may be stopping
                    return
                time.sleep(interval_s)

        self._sampling = True
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop_sampling(self) -> None:
        self._sampling = False
        if self._thread is not None:
            self._thread.join(timeout=10)


# The local servers ignore the developer's .env (its tuning and keys would make the run
# irreproducible): only the settings this script passes, and the defaults, apply.
BACKEND_LAUNCHER = """
import sys
from app.core.config import Settings
Settings.model_config["env_file"] = None
import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port=int(sys.argv[1]), workers=1,
            access_log=False, server_header=False)
"""
PAGE_LAUNCHER = """
import sys
from frontend.config import FrontendSettings
FrontendSettings.model_config["env_file"] = None
from streamlit.web import cli
script, port = sys.argv[1], sys.argv[2]
sys.argv = ["streamlit", "run", script, "--server.address", "127.0.0.1", "--server.port", port]
sys.exit(cli.main())
"""


def clean_environ() -> dict[str, str]:
    """This process's environment without any application setting (from the shell)."""
    from app.core.config import Settings
    from frontend.config import FrontendSettings

    names = {name.upper() for name in (*Settings.model_fields, *FrontendSettings.model_fields)}
    return {key: value for key, value in os.environ.items() if key.upper() not in names}


class LocalTarget:
    """Servers as local processes from this virtual environment."""

    name = "local"

    def __init__(self, workdir: Path) -> None:
        import psutil  # a development tool; only this target needs it

        self.psutil = psutil
        self.workdir = workdir

    def _tree(self, pid: int) -> list[Any]:
        # On Windows the venv's python.exe is a launcher that starts the real interpreter
        # as a child: the tree is what runs.
        process = self.psutil.Process(pid)
        return [process, *process.children(recursive=True)]

    def _resources(self, pid: int) -> dict[str, float]:
        memory = cpu = 0.0
        for process in self._tree(pid):
            try:
                memory += process.memory_info().rss
                times = process.cpu_times()
                cpu += times.user + times.system
            except self.psutil.NoSuchProcess:
                continue
        return {"memory_mib": memory / MIB, "cpu_seconds": cpu}

    def _start(self, command: list[str], env: dict[str, str], url: str) -> Service:
        log = open(self.workdir / f"{uuid.uuid4().hex}.log", "wb")  # noqa: SIM115
        launched = time.perf_counter()
        process = subprocess.Popen(  # noqa: S603 - fixed command, no shell
            command, cwd=PROJECT_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT
        )

        def stop() -> None:
            try:
                tree = self._tree(process.pid)
            except self.psutil.NoSuchProcess:
                tree = []
            for member in reversed(tree):
                try:
                    member.kill()
                except self.psutil.NoSuchProcess:
                    pass
            # Every process of the tree, so the database file is released before the
            # next run (Windows keeps a file locked while any process has it open).
            self.psutil.wait_procs(tree, timeout=30)
            process.wait(timeout=30)
            log.close()

        return Service(url, stop, lambda: self._resources(process.pid), launched)

    def start_backend(self, env: dict[str, str]) -> Service:
        database = self.workdir / f"perf-{uuid.uuid4().hex}.db"
        environment = {
            **clean_environ(),
            **env,
            "DATABASE_URL": f"sqlite:///{database.as_posix()}",
            "HF_HUB_OFFLINE": "1",
        }
        command = [sys.executable, "-c", BACKEND_LAUNCHER, str(BACKEND_PORT)]
        return self._start(command, environment, f"http://127.0.0.1:{BACKEND_PORT}")

    def start_page(self, page: str, backend_url: str) -> Service:
        port = PAGE_PORTS[page]
        environment = {
            **clean_environ(),
            "BACKEND_API_URL": backend_url,
            "STREAMLIT_SERVER_HEADLESS": "true",
            "STREAMLIT_SERVER_FILE_WATCHER_TYPE": "none",
            "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
        }
        command = [sys.executable, "-c", PAGE_LAUNCHER, PAGE_SCRIPTS[page], str(port)]
        return self._start(command, environment, f"http://127.0.0.1:{port}")

    def images(self) -> dict[str, Any]:
        return {}

    def close(self) -> None:
        pass


CGROUP_READER = """
c=/sys/fs/cgroup
if [ -f $c/memory.current ]; then
  echo current $(cat $c/memory.current); cat $c/memory.stat $c/cpu.stat
else
  echo current $(cat $c/memory/memory.usage_in_bytes); cat $c/memory/memory.stat
  echo cpuacct_usage $(cat $c/cpuacct/cpuacct.usage)
fi
"""


def parse_cgroup(out: str) -> dict[str, float]:
    """Memory (MiB, minus reclaimable file cache) and CPU seconds from CGROUP_READER."""
    values = dict(line.split() for line in out.splitlines() if len(line.split()) == 2)
    if "usage_usec" in values:  # v2
        memory = int(values["current"]) - int(values.get("inactive_file", 0))
        cpu_seconds = int(values["usage_usec"]) / 1e6
    else:  # v1
        memory = int(values["current"]) - int(values.get("total_inactive_file", 0))
        cpu_seconds = int(values["cpuacct_usage"]) / 1e9
    return {"memory_mib": memory / MIB, "cpu_seconds": cpu_seconds}


class DockerTarget:
    """The production images, run with the production limits."""

    name = "docker"

    def __init__(self, docker: str, tag: str, model_dir: str | None) -> None:
        self.docker = shlex.split(docker)
        self.tag = tag
        self.model_dir = model_dir
        self.network = f"perf-{uuid.uuid4().hex[:8]}"
        self.run(["network", "create", self.network])

    def run(self, args: list[str], *, check: bool = True) -> str:
        result = subprocess.run(  # noqa: S603 - fixed command, no shell
            [*self.docker, *args], capture_output=True, text=True, check=False
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"docker {args[0]} failed: {result.stderr.strip()[:300]}")
        return result.stdout.replace("\0", "").strip()

    def _resources(self, name: str) -> dict[str, float]:
        # The container's cgroup: usage minus reclaimable file cache (what docker stats
        # shows), and total CPU time. cgroup v2, or v1 (e.g. Docker in WSL 2).
        return parse_cgroup(self.run(["exec", name, "sh", "-c", CGROUP_READER]))

    def _start(self, name: str, image: str, port: tuple[int, int], extra: list[str]) -> Service:
        memory, cpus = LIMITS[image]
        launched = time.perf_counter()
        self.run(["run", "-d", "--rm", "--name", name, "--network", self.network,
                  "-p", f"127.0.0.1:{port[0]}:{port[1]}", "--memory", memory, "--cpus", cpus,
                  "--read-only", "--tmpfs", "/tmp:size=256m", *extra,  # noqa: S108 - the container's /tmp
                  f"support-chatbot-{image}:{self.tag}"])  # fmt: skip

        def stop() -> None:
            self.run(["stop", "-t", "20", name], check=False)

        return Service(f"http://127.0.0.1:{port[0]}", stop, lambda: self._resources(name), launched)

    def start_backend(self, env: dict[str, str]) -> Service:
        extra = ["--mount", "type=volume,dst=/var/lib/chatbot"]  # anonymous, removed by --rm
        if self.model_dir:
            extra += ["-v", f"{self.model_dir}:/models/all-MiniLM-L6-v2:ro"]
        for key, value in env.items():
            if key != "EMBEDDING_MODEL_NAME":  # the image's own /models path
                extra += ["-e", f"{key}={value}"]
        return self._start("perf-backend", "backend", (BACKEND_PORT, 8000), extra)

    def start_page(self, page: str, backend_url: str) -> Service:
        port = 8501 if page == "customer" else 8502
        extra = ["-e", "BACKEND_API_URL=http://perf-backend:8000"]
        return self._start(f"perf-{page}", page, (PAGE_PORTS[page], port), extra)

    def images(self) -> dict[str, Any]:
        sizes = {}
        for image in ("backend", "customer", "admin"):
            ref = f"support-chatbot-{image}:{self.tag}"
            size = int(self.run(["image", "inspect", ref, "--format", "{{.Size}}"]))
            sizes[ref] = {"bytes": size, "mib": round(size / MIB, 1)}
        return sizes

    def close(self) -> None:
        self.run(["network", "rm", self.network], check=False)


# --- Measurements ----------------------------------------------------------------------


def backend_env(staff_hash: str) -> dict[str, str]:
    """Settings for the measured backend. LLM_ENABLED=false: no paid call is possible."""
    return {
        "APP_ENV": "development",
        "DEBUG": "false",
        "LLM_ENABLED": "false",
        "QUERY_REWRITE_USE_LLM": "false",
        "SEMANTIC_INDEX_PRELOAD": "false",
        "RATE_LIMIT_ENABLED": "false",
        "STAFF_TOKEN_HASHES": json.dumps([f"perf:admin:{staff_hash}"]),
        "LOG_LEVEL": "WARNING",
        "METRICS_LOG_INTERVAL_SECONDS": "0",
        "CORS_ORIGINS": "[]",
    }


def rounded(resources: dict[str, float]) -> dict[str, float]:
    return {key: round(value, 2 if key == "cpu_seconds" else 1) for key, value in resources.items()}


def measure_startup(start: Callable[[], Service], paths: list[str], runs: int) -> dict[str, Any]:
    """Seconds from launch to each path answering 200, over ``runs`` fresh starts."""
    seconds: dict[str, list[float]] = {path: [] for path in paths}
    memory: list[float] = []
    for _ in range(runs):
        service = start()
        try:
            for path in paths:
                seconds[path].append(service.wait(path))
            time.sleep(1.0)  # let start-up work settle before reading memory
            memory.append(service.resources()["memory_mib"])
        finally:
            service.stop()
    return {
        "runs": runs,
        "seconds": {path: [round(s, 2) for s in values] for path, values in seconds.items()},
        "median_seconds": {path: round(statistics.median(v), 2) for path, v in seconds.items()},
        "memory_mib_after_start": [round(m, 1) for m in memory],
    }


def timed(call: Callable[[], httpx.Response]) -> tuple[float, httpx.Response]:
    started = time.perf_counter()
    response = call()
    elapsed = (time.perf_counter() - started) * 1000
    response.raise_for_status()
    return elapsed, response


def measure_backend_workload(
    service: Service, client: httpx.Client, staff_token: str, turns: int, searches: int
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    result["ready_seconds"] = round(service.wait(f"{API}/ready"), 2)
    time.sleep(2.0)
    idle_before = service.resources()
    time.sleep(10.0)
    idle_after = service.resources()
    result["idle"] = {
        "memory_mib": round(idle_after["memory_mib"], 1),
        "cpu_percent_of_one_core": round(
            (idle_after["cpu_seconds"] - idle_before["cpu_seconds"]) / 10.0 * 100, 1
        ),
    }
    service.sample_peak(0.5)

    def new_conversation() -> tuple[str, dict[str, str]]:
        body = client.post(f"{API}/conversations").raise_for_status().json()
        return body["session_id"], {"Authorization": f"Bearer {body['access_token']}"}

    def send(sid: str, headers: dict[str, str], text: str, timeout: float = 60.0) -> float:
        ms, _ = timed(lambda: client.post(
            f"{API}/conversations/{sid}/chat",
            json={"message": text, "client_message_id": str(uuid.uuid4())},
            headers=headers, timeout=timeout,
        ))  # fmt: skip
        return ms

    # First chat request: loads the embedding model and builds the FAQ index.
    sid, headers = new_conversation()
    before = service.resources()
    result["first_chat_ms"] = round(
        send(sid, headers, CONVERSATIONS[0][0], timeout=FIRST_CHAT_TIMEOUT_S), 1
    )
    after_model = service.resources()
    result["first_chat_cpu_seconds"] = round(after_model["cpu_seconds"] - before["cpu_seconds"], 2)
    result["memory_mib_after_model_load"] = round(after_model["memory_mib"], 1)

    # Warm chat turns.
    warm: list[float] = []
    history: list[float] = []
    started_wall = time.perf_counter()
    index = 0
    while len(warm) < turns:
        sid, headers = new_conversation()
        for text in CONVERSATIONS[index % len(CONVERSATIONS)]:
            if len(warm) < turns:
                warm.append(send(sid, headers, text))
        ms, _ = timed(lambda: client.get(f"{API}/conversations/{sid}/messages", headers=headers))  # noqa: B023
        history.append(ms)
        index += 1
    wall = time.perf_counter() - started_wall
    after_warm = service.resources()
    cpu = after_warm["cpu_seconds"] - after_model["cpu_seconds"]
    result["warm_chat"] = summary(warm)
    result["warm_chat_cpu_seconds_per_turn"] = round(cpu / turns, 3)
    result["warm_phase_cpu_percent_of_one_core"] = round(cpu / wall * 100, 1)
    result["history_read"] = summary(history)
    result["memory_mib_after_warm_turns"] = round(after_warm["memory_mib"], 1)

    # Retrieval endpoints, measured by the client (HTTP and JSON included).
    calls: dict[str, Callable[[str], httpx.Response]] = {
        "keyword_search": lambda q: client.get(f"{API}/faqs/search", params={"q": q}),
        "semantic_search": lambda q: client.post(f"{API}/semantic-search", json={"query": q}),
        "hybrid_search": lambda q: client.post(f"{API}/hybrid-search", json={"query": q}),
    }
    for name, call in calls.items():
        times = []
        for i in range(searches):
            query = SEARCH_QUERIES[i % len(SEARCH_QUERIES)]
            times.append(timed(lambda call=call, query=query: call(query))[0])  # type: ignore[misc]
        result[name] = summary(times)

    service.stop_sampling()
    result["memory_mib_peak_sampled"] = round(service.peak_mib, 1)

    # The backend's own measurements.
    monitoring = (
        client.get(f"{API}/admin/monitoring", headers={"Authorization": f"Bearer {staff_token}"})
        .raise_for_status()
        .json()
    )
    server: dict[str, Any] = {}
    for series, rows in monitoring["latencies"].items():
        for row in rows:
            # Request time inside the backend, for the measured routes only: compared with
            # the client's time it shows the network path's share.
            if (
                series == "http_request_duration"
                and row["labels"].get("route") not in MEASURED_ROUTES
            ):
                continue
            labels = ",".join(f"{k}={v}" for k, v in sorted(row["labels"].items()))
            key = f"{series}{{{labels}}}" if labels else series
            server[key] = {k: row[k] for k in ("count", "p50_ms", "p95_ms", "max_ms", "average_ms")}
    result["server_side"] = server
    result["llm_calls"] = server.get("llm_call_duration", {}).get("count", 0)
    return result


MODEL_PROBE = """
import json, time
t0 = time.perf_counter()
from app.core.config import Settings
from app.services.embedding_service import EmbeddingService
settings = Settings(_env_file=None)
t1 = time.perf_counter()
service = EmbeddingService(settings.embedding_model_name)
service.encode_batch(["warm up"])
t2 = time.perf_counter()
service.encode_batch(["How can I track my order?"])
t3 = time.perf_counter()
print(json.dumps({"import_s": t1 - t0, "load_and_first_encode_s": t2 - t1,
                  "second_encode_ms": (t3 - t2) * 1000}))
"""


def measure_model_load(runs: int) -> dict[str, Any]:
    """Each run in a fresh Python process: import, model load and first embedding."""
    results = []
    for _ in range(runs):
        started = time.perf_counter()
        out = subprocess.run(  # noqa: S603 - fixed command, no shell
            [sys.executable, "-c", MODEL_PROBE],
            cwd=PROJECT_ROOT,
            env={**clean_environ(), "HF_HUB_OFFLINE": "1"},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        data = json.loads(out.strip().splitlines()[-1])
        data["process_total_s"] = time.perf_counter() - started
        results.append({k: round(v, 2) for k, v in data.items()})
    return {"runs": results}


def measure_page_script(backend_url: str) -> dict[str, Any]:
    """The customer page's first and second script run against the live backend
    (Streamlit's AppTest, in this process: page code and its HTTP calls, no browser)."""
    os.environ["BACKEND_API_URL"] = backend_url
    from streamlit.testing.v1 import AppTest

    from frontend.config import FrontendSettings

    FrontendSettings.model_config["env_file"] = None  # as the measured page servers

    page = AppTest.from_file(str(PROJECT_ROOT / "frontend" / "app.py"), default_timeout=120)
    started = time.perf_counter()
    page.run()
    first = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    page.chat_input[0].set_value(CONVERSATIONS[0][0]).run()
    send = (time.perf_counter() - started) * 1000
    return {"first_run_ms": round(first, 1), "send_message_run_ms": round(send, 1)}


# --- Main ------------------------------------------------------------------------------


def print_report(output: dict[str, Any]) -> None:
    backend = output["backend"]
    print(f"\nTarget: {output['target']}  ({output['environment']['note']})")
    for label, data in (("Backend startup", output["backend_startup"]),
                        *((f"{p.title()} page startup", d)
                          for p, d in output["page_startup"].items())):  # fmt: skip
        memory = data["memory_mib_after_start"]
        print(f"  {label:28} median {data['median_seconds']}  memory {memory} MiB")
    if "model_load" in output:
        for run in output["model_load"]["runs"]:
            print(f"  Model load (fresh process)   {run}")
    print(f"  First chat request           {backend['first_chat_ms']} ms")
    for key in ("warm_chat", "history_read", "keyword_search", "semantic_search", "hybrid_search"):
        print(f"  {key:28} {backend[key]}")
    print(f"  CPU per warm turn            {backend['warm_chat_cpu_seconds_per_turn']} s")
    print(f"  Memory idle / model / warm / peak: {backend['idle']['memory_mib']} / "
          f"{backend['memory_mib_after_model_load']} / {backend['memory_mib_after_warm_turns']} / "
          f"{backend['memory_mib_peak_sampled']} MiB")  # fmt: skip
    for key, row in backend["server_side"].items():
        print(f"  server {key:44} {row}")
    if "customer_page_script" in output:
        print(f"  Customer page script runs    {output['customer_page_script']}")
    if output.get("images"):
        print(f"  Images: {output['images']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--target", choices=["local", "docker"], default="local")
    parser.add_argument("--docker", default="docker", help='e.g. "wsl.exe -e docker"')
    parser.add_argument("--tag", default="profile", help="image tag for --target docker")
    parser.add_argument("--model-dir", help="docker: host folder of the model, mounted at /models")
    parser.add_argument("--turns", type=int, default=40)
    parser.add_argument("--searches", type=int, default=30)
    parser.add_argument("--startup-runs", type=int, default=3)
    parser.add_argument("--model-runs", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    ports = [BACKEND_PORT, *PAGE_PORTS.values()]
    if busy := [port for port in ports if not port_free(port)]:
        parser.error(f"ports in use: {busy}")
    staff_token = generate_staff_token()
    env = backend_env(hash_staff_token(staff_token))

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as workdir:
        target: Any = (
            LocalTarget(Path(workdir))
            if args.target == "local"
            else DockerTarget(args.docker, args.tag, args.model_dir)
        )
        output: dict[str, Any] = {
            "target": target.name,
            "environment": {
                "python": sys.version.split()[0],
                "platform": sys.platform,
                "cpu_count": os.cpu_count(),
                "note": "real servers over HTTP on 127.0.0.1; LLM disabled (no paid calls); "
                "rate limits off; SEMANTIC_INDEX_PRELOAD=false",
            },
            "measured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            output["images"] = target.images()
            output["backend_startup"] = measure_startup(
                lambda: target.start_backend(env),
                [f"{API}/health", f"{API}/ready"],
                args.startup_runs,
            )
            if args.target == "local":
                output["model_load"] = measure_model_load(args.model_runs)
            backend = target.start_backend(env)
            try:
                with httpx.Client(base_url=backend.url, timeout=60.0) as client:
                    output["backend"] = measure_backend_workload(
                        backend, client, staff_token, args.turns, args.searches
                    )
                output["page_startup"] = {
                    page: measure_startup(
                        lambda page=page: target.start_page(page, backend.url),  # type: ignore[misc]
                        ["/_stcore/health"],
                        args.startup_runs,
                    )
                    for page in PAGE_PORTS
                }
                if args.target == "local":
                    output["customer_page_script"] = measure_page_script(backend.url)
            finally:
                backend.stop()
        finally:
            target.close()

    print_report(output)
    if args.output:
        args.output.write_text(json.dumps(output, indent=2), encoding="utf-8")
        print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
