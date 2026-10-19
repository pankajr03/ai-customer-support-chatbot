"""Trusted proxy behaviour (Stage 13): the backend takes the client address from
X-Forwarded-For only when the request comes from the configured reverse proxy.

The application itself never reads forwarding headers: it uses ``request.client``,
which uvicorn's ProxyHeadersMiddleware rewrites for trusted peers only
(``FORWARDED_ALLOW_IPS``, set in deploy/compose.yaml to Nginx's fixed address). These
tests run the real application behind that middleware with the deployment's value and
use the per-IP limit on new conversations (1 per minute here) to see which address the
backend used.
"""

import asyncio
from pathlib import Path

import httpx
import yaml
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.core.config import Settings
from app.main import create_app

COMPOSE = Path(__file__).resolve().parents[1] / "deploy" / "compose.yaml"
TRUSTED_PROXY = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["backend"][
    "environment"
]["FORWARDED_ALLOW_IPS"]
UNTRUSTED_PEER = "172.30.1.20"  # e.g. the customer page container: not a proxy


def start_conversations(requests: list[tuple[str, str | None]]) -> list[int]:
    """POST /conversations once per (peer address, X-Forwarded-For) pair, in order."""
    settings = Settings(_env_file=None, rate_limit_new_conversations_per_minute=1)
    app = ProxyHeadersMiddleware(create_app(settings), trusted_hosts=TRUSTED_PROXY)

    async def run() -> list[int]:
        statuses = []
        for peer, forwarded_for in requests:
            transport = httpx.ASGITransport(app=app, client=(peer, 50_000))
            headers = {"X-Forwarded-For": forwarded_for} if forwarded_for else {}
            async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
                response = await client.post("/api/v1/conversations", headers=headers)
                statuses.append(response.status_code)
        return statuses

    return asyncio.run(run())


def test_the_trusted_proxy_is_a_single_fixed_address() -> None:
    assert TRUSTED_PROXY == "172.30.1.10"
    assert "*" not in TRUSTED_PROXY and "/" not in TRUSTED_PROXY


def test_through_the_proxy_each_client_is_counted_by_its_own_address() -> None:
    statuses = start_conversations(
        [
            (TRUSTED_PROXY, "203.0.113.1"),
            (TRUSTED_PROXY, "203.0.113.2"),  # another visitor: own allowance
            (TRUSTED_PROXY, "203.0.113.1"),  # the first visitor again: over the limit
        ]
    )

    assert statuses == [201, 201, 429]


def test_forwarding_headers_from_anyone_else_are_ignored() -> None:
    """A forged X-Forwarded-For from an untrusted peer does not buy a fresh allowance."""
    statuses = start_conversations(
        [
            (UNTRUSTED_PEER, "203.0.113.1"),
            (UNTRUSTED_PEER, "203.0.113.2"),  # forged: still the same peer
            (UNTRUSTED_PEER, None),
        ]
    )

    assert statuses == [201, 429, 429]


def test_the_proxy_itself_is_not_mistaken_for_a_client() -> None:
    """Requests forwarded for different visitors are not lumped together under the
    proxy's address, and a request from the proxy without a header counts as the proxy."""
    statuses = start_conversations(
        [
            (TRUSTED_PROXY, "203.0.113.7"),
            (TRUSTED_PROXY, None),  # no header: the proxy's own address, a fresh key
            (TRUSTED_PROXY, None),
        ]
    )

    assert statuses == [201, 201, 429]
