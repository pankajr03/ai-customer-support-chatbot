"""Shared pytest fixtures and hooks.

Every test runs fully offline and in-process: TestClient calls the ASGI app
directly (no server, no real HTTP), local .env files and shell variables are
ignored, and any attempt to reach a non-local host fails the test.

Embedding models: ordinary tests use fake encoders. Hugging Face is forced into
offline mode for the whole session, so no model can be downloaded, and the session
fails if PyTorch or sentence-transformers were imported by ordinary tests. Tests that
load the real model are marked ``embedding_model``; they run only when
RUN_EMBEDDING_MODEL_TESTS=1 *and* the model is already in the local cache.
"""

import copy
import json
import os
import socket
import sys
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

# Must be set before anything imports huggingface_hub (it reads these once at import).
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.database.faq_repository import FAQRepository  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.faq import FAQ  # noqa: E402
from app.services.faq_service import FAQService  # noqa: E402
from tests.faq_test_data import TEST_FAQ_RECORDS  # noqa: E402

REAL_MODEL_ENV = "RUN_EMBEDDING_MODEL_TESTS"
DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
HEAVY_MODULES = ("torch", "sentence_transformers")


def hf_hub_cache_dir() -> Path:
    """Where huggingface_hub stores models (computed without importing it)."""
    if cache := os.environ.get("HF_HUB_CACHE"):
        return Path(cache)
    hf_home = os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface"
    return Path(hf_home) / "hub"


def model_is_cached(model_name: str = DEFAULT_MODEL) -> bool:
    snapshots = hf_hub_cache_dir() / f"models--{model_name.replace('/', '--')}" / "snapshots"
    return snapshots.is_dir() and any(snapshots.iterdir())


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip real-model tests unless explicitly requested and the model is available offline."""
    requested = os.environ.get(REAL_MODEL_ENV) == "1"
    cached = model_is_cached()
    for item in items:
        if "embedding_model" not in item.keywords:
            continue
        if not requested:
            item.add_marker(pytest.mark.skip(reason=f"real model test; set {REAL_MODEL_ENV}=1"))
        elif not cached:
            item.add_marker(
                pytest.mark.skip(reason=f"{DEFAULT_MODEL} is not cached locally (no downloads)")
            )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the run if ordinary tests loaded the real embedding stack."""
    if os.environ.get(REAL_MODEL_ENV) == "1":
        return
    loaded = [name for name in HEAVY_MODULES if name in sys.modules]
    if loaded:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(
                f"ERROR: ordinary tests imported {', '.join(loaded)}; use the fake encoders "
                "in tests/fakes.py or mark the test 'embedding_model'.",
                red=True,
            )
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


# Loopback is needed internally (e.g. the event loop's self-pipe on Windows).
LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testserver"})


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail fast if any test tries to resolve or connect to an external host."""
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(sock: socket.socket, address: object) -> None:
        if isinstance(address, tuple) and address[0] not in LOCAL_HOSTS:
            raise RuntimeError(f"Tests must not access the network (connect to {address!r})")
        return real_connect(sock, address)

    def guarded_getaddrinfo(host: object, *args: object, **kwargs: object) -> object:
        if host not in LOCAL_HOSTS and host is not None:
            raise RuntimeError(f"Tests must not access the network (DNS lookup of {host!r})")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the developer's shell environment from leaking into tests."""
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    """Build Settings that ignore any local .env file, with optional overrides."""

    def _make(**overrides: object) -> Settings:
        return Settings(_env_file=None, **overrides)

    return _make


@pytest.fixture
def settings(make_settings: Callable[..., Settings]) -> Settings:
    """Default settings, exactly as a fresh checkout without a .env would use."""
    return make_settings()


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def faq_records() -> list[dict[str, Any]]:
    """A fresh copy of the test FAQ records; tests may modify it before writing."""
    return copy.deepcopy(TEST_FAQ_RECORDS)


@pytest.fixture
def faq_file(tmp_path: Path, faq_records: list[dict[str, Any]]) -> Path:
    """The test FAQ records written to a temporary JSON file."""
    path = tmp_path / "faqs.json"
    path.write_text(json.dumps(faq_records, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def faq_repository(faq_file: Path) -> FAQRepository:
    """A repository loaded from the temporary file through the real loading code."""
    return FAQRepository.from_json_file(faq_file)


@pytest.fixture
def faq_service(faq_repository: FAQRepository) -> FAQService:
    return FAQService(faq_repository)


@pytest.fixture
def faq_client(make_settings: Callable[..., Settings], faq_file: Path) -> Iterator[TestClient]:
    """A full app serving the test FAQ file (via FAQ_DATA_PATH), not data/faqs.json."""
    with TestClient(create_app(make_settings(faq_data_path=faq_file))) as test_client:
        yield test_client


@pytest.fixture
def make_faq_client(make_settings: Callable[..., Settings]) -> Callable[..., TestClient]:
    """Build a client whose FAQ service holds only the given in-memory FAQs.

    No JSON file is read or written, so tests never touch data/faqs.json.
    """

    def _make(faqs: Iterable[FAQ], **setting_overrides: object) -> TestClient:
        service = FAQService(FAQRepository(faqs))
        return TestClient(create_app(make_settings(**setting_overrides), faq_service=service))

    return _make
