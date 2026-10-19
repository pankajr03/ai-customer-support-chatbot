"""The single-server Docker Compose deployment (deploy/, Stage 13).

Docker is not needed: these tests read the Compose file, the Dockerfile, the Nginx
configuration and the example environment files, and check the properties the
architecture depends on (docs/deployment-architecture.md). They cannot show that the
images build or the containers start.
"""

import fnmatch
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


def dockerignore_rules() -> list[str]:
    return [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def test_docker_build_context_is_an_allow_list() -> None:
    lines = dockerignore_rules()

    assert lines[0] == "*"
    allowed = {line[1:] for line in lines if line.startswith("!")}
    assert allowed == {
        "deploy/requirements/",
        "app/",
        "frontend/",
        "scripts/",
        "data/faqs.json",
        ".streamlit/config.toml",
    }


def excluding_rules() -> list[str]:
    """The explicit exclusions: every rule after the last allow-list exception."""
    rules = dockerignore_rules()
    last_allow = max(i for i, rule in enumerate(rules) if rule.startswith("!"))
    return rules[last_allow + 1 :]


def test_secrets_databases_caches_and_reports_are_excluded_even_inside_allowed_folders() -> None:
    # In .dockerignore the last matching rule wins, so these come after the allow-list.
    rules = set(excluding_rules())
    required = {
        "**/.env", "**/.env.*", "**/secrets/", "**/secrets.toml", "**/*.pem", "**/*.key",
        "**/*.htpasswd", "**/.venv/", "**/venv/", "**/.git/", "**/.pytest_cache/",
        "**/__pycache__/", "**/*.db", "**/*.db-wal", "**/*.db-shm", "**/*.sqlite",
        "**/*.sqlite3", "**/.coverage", "**/coverage.xml", "**/htmlcov/", "**/junit*.xml",
        "**/*.tmp", "**/*.log", "**/.vscode/", "**/.idea/", "**/backups/", "**/*.bak",
        "**/*.zip", "**/*.tar.gz",
    }  # fmt: skip

    assert required <= rules, required - rules


def test_no_exclusion_removes_application_code_schema_code_or_faq_data() -> None:
    """Schema creation and upgrades live in app/database/ (there are no separate migration
    files); the maintenance scripts use them. None of the exclusions may match these."""
    required = [
        "app/main.py",
        "app/database/connection.py",
        "app/database/models.py",
        "app/database/faq_repository.py",
        "app/repositories/faq_repository.py",
        "frontend/app.py",
        "frontend/admin_app.py",
        "frontend/styles.css",
        "scripts/import_faqs.py",
        "scripts/cleanup_conversations.py",
        "scripts/anonymize_support_data.py",
        "scripts/create_staff_token.py",
        "data/faqs.json",
        ".streamlit/config.toml",
        "deploy/requirements/backend.txt",
    ]
    for path in required:
        assert (ROOT / path).is_file(), path
        parts = path.split("/")
        for rule in excluding_rules():
            # Every exclusion is "**/<name>" or "**/<name>/": match <name> against each
            # path component (directory rules against directories only).
            assert rule.startswith("**/"), rule
            name = rule[3:].rstrip("/")
            candidates = parts[:-1] if rule.endswith("/") else parts
            assert not any(fnmatch.fnmatchcase(part, name) for part in candidates), (rule, path)


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


# --- Images (deploy/Dockerfile, deploy/requirements/) ------------------------------------

LOCKS = DEPLOY / "requirements"


def dockerfile() -> str:
    return (DEPLOY / "Dockerfile").read_text(encoding="utf-8")


def locked(image: str) -> dict[str, str]:
    """``{name: version}`` from a lock file."""
    text = (LOCKS / f"{image}.txt").read_text(encoding="utf-8")
    return dict(re.findall(r"^([a-z0-9][a-z0-9._-]*)==(\S+)", text, re.MULTILINE))


def test_every_image_has_a_target_and_the_pages_share_one_stage() -> None:
    stages = re.findall(r"^FROM (\S+) AS (\S+)$", dockerfile(), re.MULTILINE)

    assert [name for _, name in stages] == ["base", "backend", "frontend", "customer", "admin"]
    assert dict((name, parent) for parent, name in stages) == {
        "base": "${PYTHON_IMAGE}",
        "backend": "base",
        "frontend": "base",
        "customer": "frontend",
        "admin": "frontend",
    }
    targets = {
        name: services()[name]["build"]["target"] for name in ("backend", "customer", "admin")
    }
    assert targets == {"backend": "backend", "customer": "customer", "admin": "admin"}


def test_base_and_proxy_images_are_pinned_by_version_and_digest() -> None:
    base = re.search(r"^ARG PYTHON_IMAGE=(\S+)$", dockerfile(), re.MULTILINE)
    nginx = services()["nginx"]["image"]

    assert base and re.fullmatch(r"python:3\.13\.\d+-slim-\w+@sha256:[0-9a-f]{64}", base[1])
    assert re.fullmatch(
        r"nginxinc/nginx-unprivileged:\d+\.\d+\.\d+-alpine@sha256:[0-9a-f]{64}", nginx
    )
    assert ":latest" not in dockerfile() and ":latest" not in nginx


def test_packages_install_only_from_hash_locked_files_without_resolving() -> None:
    installs = re.findall(r"RUN pip install([^&]*)", dockerfile())

    assert len(installs) == 2
    for install in installs:
        assert "--require-hashes" in install
        assert "--no-deps" in install
        assert "--only-binary=:all:" in install
        assert "-r /tmp/requirements.txt" in install
    assert "requirements.txt ." not in dockerfile()  # the unlocked list is never installed


@pytest.mark.parametrize("image", ["backend", "frontend"])
def test_every_locked_package_has_an_exact_version_and_a_hash(image: str) -> None:
    text = (LOCKS / f"{image}.txt").read_text(encoding="utf-8")
    entries = re.split(r"\n(?=[a-z0-9])", text.split("\n", 2)[2])

    assert entries and all("==" in entry.split()[0] for entry in entries)
    assert all("--hash=sha256:" in entry for entry in entries)


@pytest.mark.parametrize("image", ["backend", "frontend"])
def test_locks_agree_with_the_direct_pins_in_requirements_txt(image: str) -> None:
    from scripts.lock_deploy_requirements import direct_requirements, package_name, select

    requirements = direct_requirements((ROOT / "requirements.txt").read_text(encoding="utf-8"))
    lock = locked(image)

    for requirement in select(requirements, image):
        name, version = package_name(requirement), requirement.split("==", 1)[1]
        assert lock[name].split("+", 1)[0] == version, name


def test_backend_lock_has_cpu_only_torch_and_the_linux_server_extras() -> None:
    lock = locked("backend")

    assert lock["torch"].endswith("+cpu")
    assert not [name for name in lock if name.startswith(("nvidia", "triton"))]
    # Linux-only extras of uvicorn[standard]: present because the lock was resolved for
    # Linux, not for the Windows machine that generated it.
    assert {"uvloop", "httptools"} <= lock.keys()
    assert not {"colorama", "pywin32"} & lock.keys()


def test_development_tools_are_in_neither_image() -> None:
    for image in ("backend", "frontend"):
        assert not {"pytest", "pytest-cov", "ruff", "mypy"} & locked(image).keys(), image
    assert not {"torch", "sentence-transformers", "openai"} & locked("frontend").keys()
    assert "streamlit" not in locked("backend")


def test_images_run_as_a_non_root_user_with_exec_form_commands() -> None:
    text = dockerfile()

    for stage in ("backend", "frontend"):
        body = text.split(f" AS {stage}\n", 1)[1].split("\nFROM ", 1)[0]
        assert "USER app" in body, stage
    assert "WORKDIR /app" in text
    for command in re.findall(r"^CMD (.+)$", text, re.MULTILINE):
        assert command.startswith("["), command  # exec form: the process gets SIGTERM
    assert len(re.findall(r"^HEALTHCHECK ", text, re.MULTILINE)) == 3
    assert "--timeout-graceful-shutdown" in text


def test_images_never_download_the_embedding_model_at_startup() -> None:
    text = dockerfile()

    assert "HF_HUB_OFFLINE=1" in text
    assert "EMBEDDING_MODEL_NAME=/models/all-MiniLM-L6-v2" in text
    assert "snapshot_download" not in text  # only compose's one-off model-init downloads


def test_lock_script_selects_packages_per_image() -> None:
    from scripts.lock_deploy_requirements import pyproject_for, select

    requirements = ["fastapi==1", "uvicorn[standard]==2", "streamlit==3", "pytest==4", "httpx==5"]

    assert select(requirements, "backend") == ["fastapi==1", "uvicorn[standard]==2", "httpx==5"]
    assert select(requirements, "frontend") == ["streamlit==3", "httpx==5"]
    assert 'torch = { index = "pytorch-cpu" }' in pyproject_for(
        ["torch==1"], torch_from_cpu_index=True
    )
    assert "pytorch" not in pyproject_for(["httpx==5"], torch_from_cpu_index=False)
    with pytest.raises(ValueError):
        select(requirements, "other")
