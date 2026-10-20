"""Service startup, health, readiness, both frontends and API connectivity."""

import httpx

from tests.deployment.conftest import (
    ADMIN_PORT,
    HOSTNAME,
    HTTP_PORT,
    HTTPS_PORT,
    SERVICES,
    Stack,
    chat,
    new_conversation,
)


def test_every_service_starts_and_becomes_healthy(stack: Stack) -> None:
    services = stack.services()

    for name in SERVICES:
        assert services[name]["State"] == "running", name
        assert services[name]["Health"] == "healthy", name
    assert services["backup"]["State"] == "running"  # the scheduled backups
    assert "model-init" not in services or services["model-init"]["State"] != "running"


def test_backend_health(stack: Stack) -> None:
    status, body, _ = stack.request_in("backend", "GET", "http://127.0.0.1:8000/api/v1/health")
    public = stack.get(HTTPS_PORT, "/api/v1/health")

    assert status == 200 and body["status"] == "healthy"
    assert public.status_code == 200 and public.json()["status"] == "healthy"


def test_backend_readiness(stack: Stack) -> None:
    status, body, _ = stack.request_in("backend", "GET", "http://127.0.0.1:8000/api/v1/ready")
    public = stack.get(HTTPS_PORT, "/healthz")

    assert status == 200 and body["status"] == "ready"
    assert body["checks"]["knowledge_base"] == body["checks"]["conversation_storage"] == "ok"
    assert body["checks"]["llm"] == "disabled"  # no OpenAI request can be made
    # The public status: the verdict only, never the checks.
    assert public.status_code == 200 and public.json() == {"status": "ready"}
    assert stack.get(HTTPS_PORT, "/api/v1/ready").status_code == 404


def test_customer_frontend_is_served_over_https(stack: Stack) -> None:
    page = stack.get(HTTPS_PORT, "/")
    health = stack.get(HTTPS_PORT, "/_stcore/health")
    with httpx.Client(base_url=f"http://127.0.0.1:{HTTP_PORT}", headers={"Host": HOSTNAME},
                      follow_redirects=False, timeout=30) as http:  # fmt: skip
        redirect = http.get("/")  # plain HTTP: only a redirect to HTTPS

    assert page.status_code == 200 and "<noscript>" in page.text  # Streamlit's page
    assert health.status_code == 200
    assert redirect.status_code == 308
    assert redirect.headers["Location"] == f"https://localhost:{HTTPS_PORT}/"
    assert "max-age" in page.headers["Strict-Transport-Security"]


def test_admin_frontend_is_served_with_credentials(stack: Stack) -> None:
    page = stack.get(ADMIN_PORT, "/", auth=stack.basic_auth)

    assert page.status_code == 200 and "<noscript>" in page.text


def test_api_connectivity_through_nginx(stack: Stack) -> None:
    sid, auth = new_conversation(stack)

    reply = chat(stack, sid, auth, "How can I track my order?")
    history = stack.get(HTTPS_PORT, f"/api/v1/conversations/{sid}/messages", headers=auth)

    assert reply.status_code == 200, reply.text
    assert reply.json()["decision"] == "answered"
    assert reply.headers["X-Request-ID"]
    assert history.status_code == 200 and history.json()["total_messages"] == 2


def test_the_pages_reach_the_backend_over_the_private_networks(stack: Stack) -> None:
    for page in ("customer", "admin"):
        status, body, _ = stack.request_in(page, "GET", "http://backend:8000/api/v1/ready")
        assert status == 200 and body["status"] == "ready", page
    # The two pages are on separate networks: neither can resolve the other.
    for page, other in (("customer", "admin"), ("admin", "customer")):
        result = stack.python_in(
            page, f"import socket; socket.gethostbyname({other!r})", check=False
        )
        assert result.returncode != 0, f"{page} resolved {other}"
