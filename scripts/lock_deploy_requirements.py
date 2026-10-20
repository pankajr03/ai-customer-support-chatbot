"""Generate the fully pinned, hash-locked requirements for the Docker images (Stage 13).

Usage (from the project root; needs network access and uv, https://docs.astral.sh/uv/):

    python -m scripts.lock_deploy_requirements             # writes deploy/requirements/*.txt
    python -m scripts.lock_deploy_requirements --uv PATH   # a uv executable not on PATH

``requirements.txt`` pins the direct dependencies only. The images install from
``deploy/requirements/backend.txt`` and ``frontend.txt`` instead: every package,
transitive ones included, at an exact version with SHA-256 hashes, resolved for the
images' platform (Linux x86-64, CPython 3.13), not for the machine running this script.
pip cannot do that from Windows (it evaluates environment markers such as
``sys_platform != "win32"`` against the running machine and would drop ``uvloop``), so
uv's ``--python-platform`` is used. uv is needed only to regenerate the locks, never to
build or run the images.

What each image gets is chosen from ``requirements.txt`` here, so versions have one
source: change a pin there, then re-run this script and commit both files.

- backend: everything except the frontend and the development tools. ``torch`` comes
  from PyTorch's CPU-only index (only ``torch``: every other package from PyPI).
- frontend: only what the Streamlit pages import.

Re-running later can pick newer *transitive* versions (new releases); the committed lock
files, not this script, are what makes a build reproducible.
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
OUTPUT_DIR = PROJECT_ROOT / "deploy" / "requirements"

PYTHON_VERSION = "3.13"
PYTHON_PLATFORM = "x86_64-manylinux_2_28"
PYTORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"

# Not in the backend image: the Streamlit frontend and the development tools.
BACKEND_EXCLUDED = frozenset({"streamlit", "pytest", "pytest-cov", "ruff", "mypy", "psutil"})
# The only packages the Streamlit pages import (checked by the Dockerfile at build time).
FRONTEND_INCLUDED = frozenset(
    {"pydantic", "pydantic-settings", "python-dotenv", "streamlit", "httpx"}
)


def direct_requirements(text: str) -> list[str]:
    """The ``name==version`` lines of requirements.txt (comments and blanks dropped)."""
    lines = (line.split("#", 1)[0].strip() for line in text.splitlines())
    return [line for line in lines if line]


def package_name(requirement: str) -> str:
    """``uvicorn[standard]==0.54.0`` -> ``uvicorn``."""
    return requirement.split("==", 1)[0].split("[", 1)[0].strip().lower()


def select(requirements: Sequence[str], image: str) -> list[str]:
    if image == "backend":
        return [r for r in requirements if package_name(r) not in BACKEND_EXCLUDED]
    if image == "frontend":
        return [r for r in requirements if package_name(r) in FRONTEND_INCLUDED]
    raise ValueError(f"unknown image {image!r}")


def pyproject_for(requirements: Sequence[str], *, torch_from_cpu_index: bool) -> str:
    """A throwaway pyproject.toml: uv routes a single package to an index only this way."""
    dependencies = "".join(f'  "{r}",\n' for r in requirements)
    text = (
        '[project]\nname = "deploy-lock"\nversion = "0"\n'
        f'requires-python = ">={PYTHON_VERSION}"\ndependencies = [\n{dependencies}]\n'
    )
    if torch_from_cpu_index:
        text += (
            '\n[[tool.uv.index]]\nname = "pytorch-cpu"\n'
            f'url = "{PYTORCH_CPU_INDEX}"\nexplicit = true\n'
            '\n[tool.uv.sources]\ntorch = { index = "pytorch-cpu" }\n'
        )
    return text


def lock(image: str, requirements: Sequence[str], uv: str, output: Path) -> None:
    selected = select(requirements, image)
    with tempfile.TemporaryDirectory() as folder:
        pyproject = Path(folder) / "pyproject.toml"
        pyproject.write_text(
            pyproject_for(selected, torch_from_cpu_index=image == "backend"), encoding="utf-8"
        )
        subprocess.run(  # noqa: S603 - fixed arguments, no shell; uv chosen by the operator
            [
                uv, "pip", "compile", str(pyproject),
                "--python-platform", PYTHON_PLATFORM,
                "--python-version", PYTHON_VERSION,
                "--generate-hashes",
                "--custom-compile-command", "python -m scripts.lock_deploy_requirements",
                "--output-file", str(output),
                "--quiet",
            ],
            check=True,
        )  # fmt: skip


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--uv", default=shutil.which("uv"), help="Path to the uv executable.")
    args = parser.parse_args(argv)
    if not args.uv:
        print("uv not found: install it (pip install uv) or pass --uv PATH.", file=sys.stderr)
        return 1
    requirements = direct_requirements(REQUIREMENTS.read_text(encoding="utf-8"))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for image in ("backend", "frontend"):
        output = OUTPUT_DIR / f"{image}.txt"
        lock(image, requirements, args.uv, output)
        count = sum(1 for line in output.read_text(encoding="utf-8").splitlines() if "==" in line)
        print(f"{output.relative_to(PROJECT_ROOT).as_posix()}: {count} packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
