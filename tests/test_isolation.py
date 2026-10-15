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


def test_the_openai_api_cannot_be_reached_from_ordinary_tests() -> None:
    with pytest.raises(RuntimeError, match="must not access the network"):
        socket.getaddrinfo("api.openai.com", 443)


def test_a_real_llm_client_in_an_ordinary_test_fails_without_a_request() -> None:
    """Even an unmocked OpenAI client cannot make a (paid) request during normal runs."""
    from pydantic import SecretStr

    from app.services.llm_service import ErrorCategory, LLMError, LLMService

    llm = LLMService(SecretStr("sk-test-0123456789abcdefghij"), model="gpt-5.4-mini", max_retries=0)
    try:
        with pytest.raises(LLMError) as caught:
            llm.generate(instructions="i", input="q")
    finally:
        llm.close()

    assert caught.value.category in (ErrorCategory.NETWORK, ErrorCategory.UNEXPECTED)
