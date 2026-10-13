"""Semantic-search dependencies: installed, pinned, and never loaded at import time.

These checks deliberately avoid importing sentence_transformers/torch in the test
process: a cold import takes tens of seconds and no ordinary test should pay that.
"""

import importlib.metadata
import importlib.util
import json
import re
import subprocess
import sys

import pytest

from app.core.config import PROJECT_ROOT

HEAVY_MODULES = ("sentence_transformers", "torch", "transformers", "huggingface_hub")
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"


def pinned_requirements() -> dict[str, str]:
    """Package name -> pinned version from requirements.txt."""
    pins = {}
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        if match := re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?==([^\s#]+)", line.strip()):
            pins[match.group(1).lower()] = match.group(2)
    return pins


@pytest.mark.parametrize("package", ["sentence-transformers", "numpy", "torch"])
def test_embedding_packages_are_pinned_and_installed(package: str) -> None:
    pins = pinned_requirements()

    assert package in pins, f"{package} is not pinned in requirements.txt"
    assert importlib.metadata.version(package).split("+")[0] == pins[package]


@pytest.mark.parametrize("module", ["sentence_transformers", "numpy", "torch"])
def test_embedding_modules_are_importable(module: str) -> None:
    # find_spec locates the module without executing (importing) it.
    assert importlib.util.find_spec(module) is not None


@pytest.mark.parametrize(
    "package",
    ["langchain", "langgraph", "llama-index", "chromadb", "faiss-cpu", "qdrant-client", "pinecone"],
)
def test_no_unwanted_ai_frameworks_are_required(package: str) -> None:
    assert package not in pinned_requirements()


def test_openai_sdk_is_pinned_and_installed() -> None:
    """Stage 5 uses the official OpenAI SDK directly (no framework on top)."""
    pins = pinned_requirements()

    assert "openai" in pins, "openai is not pinned in requirements.txt"
    assert importlib.metadata.version("openai") == pins["openai"]


def test_importing_the_app_does_not_import_the_openai_sdk() -> None:
    """The SDK takes seconds to import; it is loaded only when the first LLM call is made."""
    script = """
import importlib, pkgutil, sys
import app
for module in pkgutil.walk_packages(app.__path__, "app."):
    importlib.import_module(module.name)
from app.main import create_app
create_app()
print("openai" in sys.modules)
"""
    result = subprocess.run(  # noqa: S603 - fixed interpreter and script, no user input
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "False"


def test_importing_the_app_does_not_load_embedding_libraries() -> None:
    """Importing every app module (and building the app) must not import torch & co."""
    script = f"""
import importlib, json, pkgutil, sys
import app
for module in pkgutil.walk_packages(app.__path__, "app."):
    importlib.import_module(module.name)
from app.main import create_app
create_app()
print(json.dumps({{name: name in sys.modules for name in {list(HEAVY_MODULES)!r}}}))
"""
    result = subprocess.run(  # noqa: S603 - fixed interpreter and script, no user input
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    loaded = json.loads(result.stdout.strip().splitlines()[-1])
    assert loaded == dict.fromkeys(HEAVY_MODULES, False), loaded
