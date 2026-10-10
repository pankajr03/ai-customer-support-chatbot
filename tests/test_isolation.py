"""The test suite must never depend on external services or internet access."""

import socket

import pytest
from fastapi.testclient import TestClient


def test_external_connections_are_blocked() -> None:
    with pytest.raises(RuntimeError, match="must not access the network"):
        socket.create_connection(("example.com", 443), timeout=1)


def test_external_dns_lookups_are_blocked() -> None:
    with pytest.raises(RuntimeError, match="must not access the network"):
        socket.getaddrinfo("api.openai.com", 443)


def test_test_client_works_without_network(client: TestClient) -> None:
    # TestClient calls the ASGI app in-process, so no real socket is opened.
    assert client.get("/api/v1/health").status_code == 200
