"""Stage 12: importing ``app.main`` builds nothing; ``app.main:app`` still serves.

Before, the module built the default application at import time, from ``.env`` and the
database at ``DATABASE_URL``: any script that only wanted ``create_app`` (evaluations,
the load test) opened and upgraded the developer's database. Each check runs in a fresh
interpreter, with an in-memory database and no ``.env``.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(code: str) -> str:
    env = {**os.environ, "DATABASE_URL": "sqlite://", "HF_HUB_OFFLINE": "1"}
    result = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
        timeout=120, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout.strip().splitlines()[-1]


def test_importing_the_module_builds_no_application() -> None:
    assert run("import app.main as m; print(m._default_app is None)") == "True"


def test_uvicorn_still_finds_the_application() -> None:
    code = (
        "from uvicorn.importer import import_from_string; "
        "app = import_from_string('app.main:app'); print(type(app).__name__)"
    )
    assert run(code) == "FastAPI"


def test_the_application_is_built_once() -> None:
    assert run("import app.main as m; print(m.app is m.app)") == "True"
