"""The single-server Docker Compose deployment (deploy/, Stage 13).

Docker is not needed: these tests read the Compose file, the Dockerfile, the Nginx
configuration and the example environment files, and check the properties the
architecture depends on (docs/deployment-architecture.md). They cannot show that the
images build or the containers start.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from dotenv import dotenv_values

from app.core.config import Settings
from frontend.config import FrontendSettings

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
ENVIRONMENTS = ("production", "staging")
APP_SERVICES = ("backend", "customer", "admin", "nginx")


def compose() -> dict[str, Any]:
    return yaml.safe_load((DEPLOY / "compose.yaml").read_text(encoding="utf-8"))


def services() -> dict[str, dict[str, Any]]:
    return compose()["services"]


def service_networks(name: str) -> set[str]:
    return set(services()[name].get("networks", []))


@pytest.mark.parametrize("name", [*APP_SERVICES, "model-init"])
def test_every_service_is_hardened(name: str) -> None:
    service = services()[name]

    assert service.get("read_only") is True
    assert service.get("cap_drop") == ["ALL"]
    assert "no-new-privileges:true" in service.get("security_opt", [])
    assert not service.get("privileged", False)
    assert "cap_add" not in service
    for key in ("network_mode", "pid", "ipc", "userns_mode"):
        assert service.get(key) != "host", key


def test_no_container_mounts_the_docker_socket_or_host_system_paths() -> None:
    for name, service in services().items():
        for volume in service.get("volumes", []):
            source = str(volume).split(":", 1)[0]
            assert "docker.sock" not in str(volume), name
            assert not source.startswith(("/var/run", "/run", "/proc", "/sys", "/etc")), name


def test_only_nginx_publishes_ports_and_the_dashboard_defaults_to_loopback() -> None:
    published = {name for name, service in services().items() if service.get("ports")}
    assert published == {"nginx"}

    ports = services()["nginx"]["ports"]
    admin = [port for port in ports if port.endswith(":9443")]
    assert admin == ["${ADMIN_BIND:-127.0.0.1}:${ADMIN_HTTPS_PORT:-8443}:9443"]


def test_backend_and_pages_are_on_internal_networks_only() -> None:
    networks = compose()["networks"]

    assert networks["customer_tier"]["internal"] is True
    assert networks["admin_tier"]["internal"] is True
    assert service_networks("nginx") == {"public", "customer_tier", "admin_tier"}
    assert "public" not in service_networks("backend")
    # The two pages cannot reach each other: no shared network.
    assert service_networks("customer") == {"customer_tier"}
    assert service_networks("admin") == {"admin_tier"}


def test_service_names_are_predictable_and_no_new_datastores_are_introduced() -> None:
    assert set(services()) == {*APP_SERVICES, "model-init"}
    images = " ".join(str(service.get("image", "")) for service in services().values())
    assert not re.search(r"redis|postgres|mysql|mariadb|mongo", images, re.IGNORECASE)


def test_backend_runs_exactly_one_worker() -> None:
    dockerfile = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    backend = services()["backend"]

    assert '"--workers", "1"' in dockerfile
    assert "command" not in backend  # the Dockerfile's single-worker command is used
    assert "deploy" not in backend and "scale" not in backend


def test_persistent_storage_uses_named_volumes_and_the_model_is_read_only() -> None:
    volumes = services()["backend"]["volumes"]

    assert set(compose()["volumes"]) == {"db", "backups", "models"}
    assert "db:/var/lib/chatbot" in volumes
    assert "backups:/app/data/backups" in volumes
    assert "models:/models:ro" in volumes
    environment = services()["backend"]["environment"]
    assert environment["DATABASE_URL"] == "sqlite:////var/lib/chatbot/conversations.db"
    assert environment["EMBEDDING_MODEL_NAME"].startswith("/models/")
    assert environment["HF_HUB_OFFLINE"] == "1"


def test_model_revision_is_pinned_consistently() -> None:
    defaults = dotenv_values(DEPLOY / ".env.example")
    command = services()["model-init"]["environment"]["EMBEDDING_MODEL_REVISION"]

    assert re.fullmatch(r"[0-9a-f]{40}", defaults["EMBEDDING_MODEL_REVISION"] or "")
    assert defaults["EMBEDDING_MODEL_REVISION"] in command
    assert services()["model-init"]["profiles"] == ["setup"]


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_backend_environment_files_are_valid_settings(
    environment: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = dotenv_values(DEPLOY / "environments" / environment / "backend.env.example")
    for key, value in values.items():
        monkeypatch.setenv(key, value or "")
    fixed = services()["backend"]["environment"]

    settings = Settings(
        _env_file=None,
        database_url=fixed["DATABASE_URL"],
        faq_data_path=Path(fixed["FAQ_DATA_PATH"]),
        embedding_model_name=fixed["EMBEDDING_MODEL_NAME"],
    )

    assert settings.app_env == environment
    assert settings.debug is False
    assert settings.llm_enabled is False
    assert settings.cors_origins == []
    assert settings.semantic_index_preload == "background"
    # Site-wide ceilings: well above the per-visitor defaults (see the file's comment).
    assert settings.rate_limit_new_conversations_per_minute > 20
    assert not set(values) & {"DATABASE_URL", "FAQ_DATA_PATH", "EMBEDDING_MODEL_NAME"}


@pytest.mark.parametrize("environment", ENVIRONMENTS)
@pytest.mark.parametrize("page", ["customer", "admin"])
def test_page_environment_files_are_valid_settings(
    environment: str, page: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = dotenv_values(DEPLOY / "environments" / environment / f"{page}.env.example")
    for key, value in values.items():
        monkeypatch.setenv(key, value or "")
    monkeypatch.setenv("BACKEND_API_URL", services()[page]["environment"]["BACKEND_API_URL"])

    settings = FrontendSettings(_env_file=None)

    assert settings.base_url == "http://backend:8000"
    assert "BACKEND_API_URL" not in values


def test_example_files_hold_no_secrets() -> None:
    for path in [DEPLOY / ".env.example", *DEPLOY.glob("environments/*/*.env.example")]:
        values = dotenv_values(path)
        assert values.get("OPENAI_API_KEY", "") in ("", None), path
        assert values.get("STAFF_TOKEN_HASHES", "[]") == "[]", path


def test_docker_build_context_is_an_allow_list() -> None:
    lines = [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]

    assert lines[0] == "*"
    allowed = {line[1:] for line in lines if line.startswith("!")}
    assert allowed == {
        "requirements.txt",
        "app/",
        "frontend/",
        "scripts/",
        "data/faqs.json",
        ".streamlit/config.toml",
    }


def test_real_deployment_files_are_git_ignored() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")

    patterns = ("deploy/environments/*/*.env", "deploy/nginx/certs/", "deploy/nginx/admin.htpasswd")
    for pattern in patterns:
        assert pattern in ignored


def test_nginx_exposes_no_backend_route_except_readiness() -> None:
    config = (DEPLOY / "nginx" / "nginx.conf").read_text(encoding="utf-8")

    assert "server_tokens off;" in config
    assert re.findall(r"proxy_pass http://backend_api[^;]*;", config) == [
        "proxy_pass http://backend_api/api/v1/ready;"
    ]
    assert "auth_basic_user_file /etc/nginx/admin.htpasswd;" in config
    # WebSocket locations repeat the headers they would otherwise stop inheriting.
    blocks = re.findall(r"location = /_stcore/stream \{(.*?)\n        \}", config, re.S)
    assert len(blocks) == 2  # customer and admin
    for block in blocks:
        assert "proxy_set_header Host $host;" in block
        assert "proxy_set_header Upgrade $http_upgrade;" in block
