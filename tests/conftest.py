"""Shared pytest fixtures.

Every test runs fully offline and in-process: TestClient calls the ASGI app
directly (no server, no real HTTP), local .env files and shell variables are
ignored, and any attempt to reach a non-local host fails the test.
"""

import socket
from collections.abc import Callable, Iterable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.database.faq_repository import FAQRepository
from app.main import create_app
from app.models.faq import FAQ
from app.services.faq_service import FAQService

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
def make_faq_client(make_settings: Callable[..., Settings]) -> Callable[..., TestClient]:
    """Build a client whose FAQ service holds only the given in-memory FAQs.

    No JSON file is read or written, so tests never touch data/faqs.json.
    """

    def _make(faqs: Iterable[FAQ], **setting_overrides: object) -> TestClient:
        service = FAQService(FAQRepository(faqs))
        return TestClient(create_app(make_settings(**setting_overrides), faq_service=service))

    return _make
