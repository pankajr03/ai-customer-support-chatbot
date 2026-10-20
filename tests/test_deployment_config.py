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
# One-off and operational services: model download, scheduled and off-server backups.
SUPPORT_SERVICES = ("model-init", "backup", "backup-offsite")


def compose() -> dict[str, Any]:
    return yaml.safe_load((DEPLOY / "compose.yaml").read_text(encoding="utf-8"))


def services() -> dict[str, dict[str, Any]]:
    return compose()["services"]


def service_networks(name: str) -> set[str]:
    return set(services()[name].get("networks", []))


@pytest.mark.parametrize("name", [*APP_SERVICES, *SUPPORT_SERVICES])
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
            # Short syntax "source:target[:mode]" or long syntax {"source": ...}.
            source = volume["source"] if isinstance(volume, dict) else volume.split(":", 1)[0]
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
    assert set(services()) == {*APP_SERVICES, *SUPPORT_SERVICES}
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


def nginx_config() -> str:
    return (DEPLOY / "nginx" / "nginx.conf.template").read_text(encoding="utf-8")


def nginx_snippet(name: str) -> str:
    return (DEPLOY / "nginx" / "snippets" / name).read_text(encoding="utf-8")


def server_section(marker: str) -> str:
    """The text of one server block, from its "# --- <marker>" comment to the next."""
    # Server markers are indented 4 spaces; headings inside a server block, 8.
    text = nginx_config()
    return text.split(f"\n    # --- {marker}", 1)[1].split("\n    # --- ", 1)[0]


def public_api_block() -> str:
    public = server_section("8443: customer page")
    return public.split("location /api/ {", 1)[1].split("\n        }\n", 1)[0]


# Customer routes deliberately reachable through the public site (method, OpenAPI path).
PUBLIC_API = {
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/status"),  # not /ready: its checks describe the deployment
    ("GET", "/api/v1/faqs"),
    ("GET", "/api/v1/faqs/categories"),
    ("GET", "/api/v1/faqs/search"),
    ("GET", "/api/v1/faqs/{faq_id}"),
    ("POST", "/api/v1/conversations"),
    ("GET", "/api/v1/conversations/{session_id}"),
    ("DELETE", "/api/v1/conversations/{session_id}"),
    ("GET", "/api/v1/conversations/{session_id}/messages"),
    ("POST", "/api/v1/conversations/{session_id}/chat"),
    ("POST", "/api/v1/conversations/{session_id}/close"),
    ("POST", "/api/v1/support/tickets"),
    ("GET", "/api/v1/support/tickets/{ticket_id}"),
}


def proxied_by_public_api(method: str, path: str) -> bool:
    """Whether the public site's /api/ allow-list forwards this request to the backend.

    Follows Nginx's order: exact locations first, then regular expressions in order; a
    location's "if ($request_method ...)" refusal is applied; no match means 404.
    """
    block = public_api_block()
    exact, regex = [], []
    locations = re.findall(r"location (=|~) (\S+) \{(.*?)\n            \}", block, re.S)
    # Every nested location must be understood here, or a new one could slip past.
    assert len(locations) == block.count("location "), "unparsed location in /api/"
    for kind, quoted, body in locations:
        pattern = quoted.strip('"')
        allowed = None  # every method
        if only := re.search(r"\$request_method != (\w+)\)", body):
            allowed = {only[1]}
        elif some := re.search(r"\$request_method !~ \^\(([A-Z|]+)\)\$\)", body):
            allowed = set(some[1].split("|"))
        assert "proxy_pass http://backend_api;" in body, pattern
        (exact if kind == "=" else regex).append((pattern, allowed))
    for pattern, allowed in exact:
        if path == pattern:
            return allowed is None or method in allowed
    for pattern, allowed in regex:
        if re.search(pattern, path):
            return allowed is None or method in allowed
    return False


def test_public_site_proxies_exactly_the_customer_api_routes() -> None:
    """Every backend route, checked against the Nginx allow-list: staff, admin, docs and
    token-free compute routes are never forwarded, whatever routes are added later."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    spec = TestClient(create_app(Settings(_env_file=None))).get("/openapi.json").json()
    sample = "3f2b8c1e-7a4d-4e9b-9c61-2d5f0a8e4b17"  # a UUID for every path parameter
    exposed = set()
    for path, operations in spec["paths"].items():
        concrete = re.sub(r"\{[^}]+\}", sample, path)
        for method in operations:
            if proxied_by_public_api(method.upper(), concrete):
                exposed.add((method.upper(), path))

    assert exposed == PUBLIC_API
    assert public_api_block().rstrip().endswith("return 404;")
    # Hidden legacy routes (not in the schema) are under /admin, which nothing matches.
    assert not proxied_by_public_api("GET", "/api/v1/admin/overview")
    assert not proxied_by_public_api("GET", "/")
    assert not proxied_by_public_api("GET", "/docs")


def test_uptime_monitors_get_the_public_status_not_detailed_readiness() -> None:
    """/healthz shows the verdict only: detailed readiness (whether an LLM is configured
    or paused, the model's state) is for the private networks."""
    public = server_section("8443: customer page")
    healthz = public.split("location = /healthz {", 1)[1].split("\n        }\n", 1)[0]

    assert "proxy_pass http://backend_api/api/v1/status;" in healthz
    assert "/api/v1/ready" not in healthz
    assert not proxied_by_public_api("GET", "/api/v1/ready")
    assert proxied_by_public_api("HEAD", "/api/v1/status")


def test_location_patterns_with_braces_are_quoted() -> None:
    """Nginx ends an unquoted pattern at "{", so "^/x/[a-z]{36}$ {" would fail to load
    (found when the configuration was first run on Docker)."""
    patterns = re.findall(r"location (?:=|~\*?|\^~)? ?(\S+) \{", nginx_config())

    assert patterns
    for pattern in patterns:
        if "{" in pattern.strip('"'):
            assert pattern.startswith('"') and pattern.endswith('"'), pattern


def test_staff_tokens_are_refused_and_the_dashboard_listener_proxies_no_api() -> None:
    public = server_section("8443: customer page")
    dashboard = server_section("9443: staff dashboard")

    assert 'if ($http_authorization ~* "^\\s*bearer\\s+stf_")' in public
    assert "location /api/ {\n            return 404;\n        }" in dashboard
    assert "backend_api" not in dashboard
    assert "auth_basic_user_file /etc/nginx/admin.htpasswd;" in dashboard


def test_forwarded_headers_are_replaced_never_appended() -> None:
    headers = nginx_snippet("proxy-headers.conf")
    everything = nginx_config() + "".join(
        nginx_snippet(name) for name in ("websocket.conf", "proxy-headers.conf")
    )

    assert "proxy_set_header X-Forwarded-For   $remote_addr;" in headers
    assert "proxy_set_header X-Real-IP         $remote_addr;" in headers
    for removed in ("X-Forwarded-Host", "X-Forwarded-Port", "Forwarded "):
        assert f"proxy_set_header {removed}".strip() in headers, removed
    directives = "\n".join(
        line for line in everything.splitlines() if not line.lstrip().startswith("#")
    )
    assert "$proxy_add_x_forwarded_for" not in directives  # would keep a forged value
    assert "include /etc/nginx/snippets/proxy-headers.conf;" in nginx_config()
    assert "include /etc/nginx/snippets/proxy-headers.conf;" in nginx_snippet("websocket.conf")


def test_backend_trusts_forwarded_headers_only_from_nginx() -> None:
    import ipaddress

    trusted = services()["backend"]["environment"]["FORWARDED_ALLOW_IPS"]
    nginx_address = services()["nginx"]["networks"]["customer_tier"]["ipv4_address"]
    subnet = compose()["networks"]["customer_tier"]["ipam"]["config"][0]["subnet"]

    assert trusted == nginx_address  # one address, no "*", no ranges
    assert ipaddress.ip_address(nginx_address) in ipaddress.ip_network(subnet)
    assert f"from {nginx_address} (FORWARDED_ALLOW_IPS)" in nginx_config()
    assert "FORWARDED_ALLOW_IPS=127.0.0.1" in dockerfile()  # image default: loopback


def test_streamlit_websockets_are_proxied_with_upgrade_headers() -> None:
    websocket = nginx_snippet("websocket.conf")
    blocks = re.findall(r"location = /_stcore/stream \{(.*?)\n        \}", nginx_config(), re.S)

    assert len(blocks) == 2  # customer and dashboard
    for block in blocks:
        assert "include /etc/nginx/snippets/websocket.conf;" in block
    assert "proxy_set_header Upgrade    $http_upgrade;" in websocket
    assert "proxy_set_header Connection $connection_upgrade;" in websocket
    assert "proxy_read_timeout 1h;" in websocket and "proxy_buffering off;" in websocket


def test_request_sizes_and_timeouts_are_bounded() -> None:
    config = nginx_config()

    assert "client_max_body_size 256k;" in config  # pages
    assert "client_max_body_size 64k;" in public_api_block()  # API (backend: 32 KB)
    for directive in (
        "client_header_timeout",
        "client_body_timeout",
        "send_timeout",
        "keepalive_timeout",
        "proxy_connect_timeout",
        "proxy_read_timeout",
    ):
        assert re.search(rf"^\s+{directive} \d+[sh];", config, re.M), directive
    # The chat route outlasts the backend's slowest LLM turn; nothing waits for ever.
    assert "proxy_read_timeout 150s;" in public_api_block()
    assert "proxy_next_upstream off;" in config


def test_proxy_errors_are_answered_safely() -> None:
    import json

    from app.models.errors import ErrorCode

    config = nginx_config()
    errors = nginx_snippet("api-errors.conf") + nginx_snippet("ui-errors.conf")
    # Every location that proxies to an upstream maps upstream failures to a safe page.
    for block in re.findall(r"location [^{]+\{(.*?)\n        \}", config, re.S):
        if "proxy_pass" in block and "location" not in block:
            assert "error_page 502 504 =" in block
    assert "error_page 502 504 = @api_unavailable;" in public_api_block()
    # Nginx's own API errors use the API's JSON shape and codes, and name no server.
    bodies = re.findall(r"return \d{3} '(\{.*?\})\\n';", errors)
    assert len(bodies) == 4
    for body in bodies:
        error = json.loads(body)["error"]
        assert error["code"] in ErrorCode.__members__ and error["request_id"] is None
    # What clients receive (every response body, the HTML page included) names no server.
    sent = re.findall(r"return \d{3} '(.*?)';", errors + config)
    assert len(sent) == 6
    assert not [body for body in sent if re.search(r"nginx|uvicorn|streamlit", body, re.I)]
    assert "server_tokens off;" in config


def test_security_headers_apply_to_both_sites_and_to_error_responses() -> None:
    headers = nginx_snippet("security-headers.conf")

    for name in (
        "Strict-Transport-Security",
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Content-Security-Policy",
        "Permissions-Policy",
    ):
        assert re.search(rf'add_header {name} ".+" always;', headers), name
    for section in ("8443: customer page", "9443: staff dashboard"):
        assert "include /etc/nginx/snippets/security-headers.conf;" in server_section(section)
    # Error locations set add_header themselves, so they include the set again.
    for snippet in ("api-errors.conf", "ui-errors.conf"):
        text = nginx_snippet(snippet)
        assert text.count("include /etc/nginx/snippets/security-headers.conf;") == text.count(
            "location @"
        )


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
        assert not {"pytest", "pytest-cov", "ruff", "mypy", "psutil"} & locked(image).keys(), image
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


# --- Development override (deploy/compose.dev.yaml) ----------------------------------------


def dev_override() -> dict[str, Any]:
    return yaml.safe_load((DEPLOY / "compose.dev.yaml").read_text(encoding="utf-8"))


def test_development_override_publishes_only_on_loopback() -> None:
    for name, service in dev_override()["services"].items():
        for port in service.get("ports", []):
            assert port.startswith("127.0.0.1:"), (name, port)


def test_development_override_never_weakens_hardening() -> None:
    weakening = {"privileged", "cap_add", "read_only", "security_opt", "user", "network_mode"}
    for name, service in dev_override()["services"].items():
        assert not weakening & service.keys(), name
        for volume in service.get("volumes", []):
            assert volume.endswith(":ro"), (name, volume)  # source mounts are read-only
            assert "docker.sock" not in volume


def test_development_override_is_separate_from_production() -> None:
    override = dev_override()

    assert override["services"]["nginx"] == {"profiles": ["proxy"]}
    assert override["services"]["backend"]["environment"]["APP_ENV"] == "development"
    # Production publishes nothing but Nginx and never loads the override.
    assert "include" not in compose()
    assert not [name for name, service in services().items() if "extends" in service]
    assert {n for n, s in services().items() if s.get("ports")} == {"nginx"}


# --- Deployment variables (deploy/.env, Stage 13 Task 7) -----------------------------------


def nginx_template() -> str:
    return (DEPLOY / "nginx" / "nginx.conf.template").read_text(encoding="utf-8")


def test_every_compose_variable_is_documented_and_every_documented_one_is_used() -> None:
    text = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
    used = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", text))
    documented = set(dotenv_values(DEPLOY / ".env.example"))

    assert used - documented == set(), "used by compose.yaml but not in deploy/.env.example"
    assert documented - used == set(), "in deploy/.env.example but unused"


def test_public_hostname_and_environment_are_required() -> None:
    text = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")

    assert "${PUBLIC_HOSTNAME:?" in text
    assert "${DEPLOY_ENV:?" in text
    nginx = services()["nginx"]["environment"]
    assert nginx["ADMIN_HOSTNAME"] == "${ADMIN_HOSTNAME:-localhost}"


def test_nginx_template_substitutes_only_the_deployment_names() -> None:
    nginx = services()["nginx"]
    # "$$" is Compose's escape for a literal "$".
    assert nginx["environment"]["NGINX_ENVSUBST_FILTER"] == (
        "^(PUBLIC_HOSTNAME|ADMIN_HOSTNAME|PUBLIC_HTTPS_PORT)$$"
    )
    # Paths inside the container (its private tmpfs), not temporary files on this machine.
    container_tmp, rendered = "/tmp", "/tmp/nginx.conf"  # noqa: S108
    assert nginx["environment"]["NGINX_ENVSUBST_OUTPUT_DIR"] == container_tmp
    assert nginx["command"] == ["nginx", "-c", rendered, "-g", "daemon off;"]
    names = set(re.findall(r"\$\{([A-Z_]+)\}", nginx_template()))
    assert names == {"PUBLIC_HOSTNAME", "ADMIN_HOSTNAME", "PUBLIC_HTTPS_PORT"}


def test_nginx_answers_only_the_configured_hostnames() -> None:
    template = nginx_template()

    assert "server_name ${PUBLIC_HOSTNAME};" in template
    assert "server_name ${ADMIN_HOSTNAME};" in template
    # Catch-all for both HTTPS listeners: no TLS for unknown names, 444 otherwise.
    catch_all = template.split("listen 8443 ssl default_server;", 1)[1].split("}", 1)[0]
    assert "listen 9443 ssl default_server;" in catch_all
    assert "ssl_reject_handshake on;" in catch_all and "return 444;" in catch_all
    # Redirects use the configured name, never the client's Host header.
    redirects = re.findall(r"return 30[18] (\S+);", template)
    assert redirects == ["https://${PUBLIC_HOSTNAME}:${PUBLIC_HTTPS_PORT}$request_uri"]


def test_missing_proxy_files_stop_startup_instead_of_being_created() -> None:
    mounts = services()["nginx"]["volumes"]

    assert {m["source"] for m in mounts} == {
        "./nginx/nginx.conf.template",
        "./nginx/certs",
        "./nginx/admin.htpasswd",
        "./nginx/snippets",
        "./nginx/acme",
    }
    for mount in mounts:
        assert mount["type"] == "bind" and mount["read_only"] is True
        assert mount["bind"]["create_host_path"] is False, mount["source"]


def test_nginx_reaches_the_api_only_over_the_trusted_network() -> None:
    """Nginx is on both internal networks; the backend trusts only its customer-network
    address. The upstream name must therefore resolve on the customer network only."""
    upstream = re.search(r"upstream backend_api \{.*?server (\S+):8000", nginx_config(), re.S)
    backend_networks = services()["backend"]["networks"]

    assert upstream and upstream[1] == "backend-api"
    assert backend_networks["customer_tier"]["aliases"] == ["backend-api"]
    for name, network in backend_networks.items():
        if name != "customer_tier":
            assert "backend-api" not in (network or {}).get("aliases", []), name


# --- HTTPS (Stage 13 Task 9) ---------------------------------------------------------------


def http_servers() -> list[str]:
    """The two port-80 server blocks: the catch-all and the public hostname's."""
    section = server_section("8080: HTTP -> HTTPS")
    return section.split("    server {")[1:]


def test_acme_challenges_are_served_on_port_80_before_any_redirect() -> None:
    catch_all, public = http_servers()
    acme = nginx_snippet("acme-challenge.conf")

    for server in (catch_all, public):
        assert "include /etc/nginx/snippets/acme-challenge.conf;" in server
    # The redirect lives in "location /", so the challenge location is reachable; a
    # server-level return would answer every path, challenges included.
    assert "location / {" in public and "return 308" in public.split("location / {", 1)[1]
    assert not re.search(r"^        return ", public, re.M)
    assert "location ^~ /.well-known/acme-challenge/ {" in acme
    assert "root /var/www/acme;" in acme and "autoindex off;" in acme
    assert "try_files $uri =404;" in acme


def test_the_acme_webroot_is_mounted_read_only_and_its_tokens_are_not_committed() -> None:
    mount = next(m for m in services()["nginx"]["volumes"] if m["target"] == "/var/www/acme")

    assert mount["source"] == "./nginx/acme" and mount["read_only"] is True
    assert (DEPLOY / "nginx" / "acme" / ".gitkeep").is_file()
    assert "deploy/nginx/acme/.well-known/" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_cookies_get_the_secure_flag_on_both_https_listeners() -> None:
    assert re.search(r"^proxy_cookie_flags ~ secure;$", nginx_snippet("secure-cookies.conf"), re.M)
    for section in ("8443: customer page", "9443: staff dashboard"):
        assert "include /etc/nginx/snippets/secure-cookies.conf;" in server_section(section)


def test_hsts_is_sent_only_over_https() -> None:
    assert "Strict-Transport-Security" in nginx_snippet("security-headers.conf")
    for server in http_servers():
        assert "security-headers.conf" not in server and "Strict-Transport" not in server


def test_the_deploy_hook_checks_before_reloading_and_can_roll_back() -> None:
    hook = (DEPLOY / "certbot" / "deploy-hook.sh").read_text(encoding="utf-8")

    assert hook.startswith("#!/bin/sh") and "set -eu" in hook
    assert "RENEWED_LINEAGE" in hook and "cp -L" in hook  # live/ holds symlinks
    assert "-checkend 0" in hook  # never installs an expired certificate
    assert "openssl pkey" in hook  # the key must match the certificate
    test_at = hook.index("nginx -t -q -c /tmp/nginx.conf")
    reload_at = hook.index("nginx -c /tmp/nginx.conf -s reload")
    assert test_at < reload_at  # a failing configuration is never reloaded
    assert "previous/fullchain.pem" in hook  # and the previous pair is restored


def test_the_development_certificate_script_says_what_it_is() -> None:
    script = (DEPLOY / "dev" / "make-dev-certificate.sh").read_text(encoding="utf-8")

    assert "DEVELOPMENT ONLY" in script
    assert "NOT evidence that public HTTPS works" in script
    assert "/O=DEVELOPMENT ONLY - not trusted" in script  # visible in the certificate
    assert "-days 7" in script


# --- Authentication review (Stage 13 Task 10) ----------------------------------------------


def test_upstream_host_keeps_the_port_for_streamlit_origin_checks() -> None:
    """Streamlit refuses a WebSocket whose Origin does not match Host. Without the port,
    every WebSocket on a non-default port (the dashboard's 8443) was refused (403 on
    Docker with real hostnames)."""
    headers = nginx_snippet("proxy-headers.conf")

    assert re.search(r"^proxy_set_header Host\s+\$http_host;$", headers, re.M)
    assert not re.search(r"^proxy_set_header Host\s+\$host;$", headers, re.M)


def test_the_dashboard_accepts_private_networks_only() -> None:
    import ipaddress

    allowlist = nginx_snippet("admin-network-allowlist.conf")
    rules = re.findall(r"^(allow|deny) (\S+);", allowlist, re.M)

    assert rules[-1] == ("deny", "all")
    for action, network in rules[:-1]:
        assert action == "allow"
        net = ipaddress.ip_network(network)
        shared_address_space = ipaddress.ip_network("100.64.0.0/10")  # CGNAT, VPNs
        assert net.is_private or net.subnet_of(shared_address_space), network
    dashboard = server_section("9443: staff dashboard")
    assert "include /etc/nginx/snippets/admin-network-allowlist.conf;" in dashboard
    assert "admin-network-allowlist" not in server_section("8443: customer page")
    # The bind address stays the first barrier.
    admin_port = next(p for p in services()["nginx"]["ports"] if p.endswith(":9443"))
    assert admin_port.startswith("${ADMIN_BIND:-127.0.0.1}")


# --- Scheduled and off-server backups ----------------------------------------------------


def test_the_backup_service_runs_the_scheduler_without_any_network() -> None:
    backup = services()["backup"]

    assert backup["command"] == ["python", "-m", "scripts.backup_scheduler"]
    assert backup["network_mode"] == "none" and "networks" not in backup
    assert "ports" not in backup
    env = backup["environment"]
    assert env["DATABASE_URL"] == services()["backend"]["environment"]["DATABASE_URL"]
    assert env["BACKUP_DIR"] == "/app/data/backups"
    assert set(backup["volumes"]) == {"db:/var/lib/chatbot", "backups:/app/data/backups"}
    assert backup["healthcheck"]["test"][-1] == "--check"
    assert backup["depends_on"]["backend"]["condition"] == "service_healthy"


def test_off_server_copies_are_opt_in_encrypted_and_read_only() -> None:
    offsite = services()["backup-offsite"]
    script = (DEPLOY / "offsite" / "offsite.sh").read_text(encoding="utf-8")

    assert offsite["profiles"] == ["offsite"]  # never started by a plain "up"
    assert re.fullmatch(r"restic/restic:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", offsite["image"])
    assert offsite["user"] == "10001:10001"  # not root; the owner of the 0600 backups
    assert "backups:/backups:ro" in offsite["volumes"]
    assert offsite["networks"] == ["egress"]  # outbound only, nothing published
    assert offsite["env_file"][0]["required"] is False
    assert offsite["env_file"][0]["path"].endswith("/offsite.env")
    assert script.startswith("#!/bin/sh") and "set -eu" in script
    assert "restic backup /backups" in script and "--exclude '.*.partial'" in script
    assert "restic forget" in script and "--prune" in script
    assert "RESTIC_PASSWORD" in script  # restic refuses to run unencrypted


def test_off_server_settings_hold_no_secrets_and_are_git_ignored() -> None:
    for path in DEPLOY.glob("environments/*/offsite.env.example"):
        values = dotenv_values(path)
        for secret in ("RESTIC_PASSWORD", "AWS_SECRET_ACCESS_KEY", "RESTIC_REPOSITORY"):
            assert values.get(secret, "") in ("", None), (path, secret)
    assert "deploy/environments/*/*.env" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_every_application_container_logs_json_lines_and_development_reads_text() -> None:
    """docs/logging.md: Docker gets one JSON object per line; the override keeps text."""
    services = compose()["services"]
    for name in ("backend", "customer", "admin", "backup"):
        assert services[name]["environment"]["LOG_FORMAT"] == "json", name
    development = dev_override()["services"]
    for name in ("backend", "customer", "admin"):
        assert development[name]["environment"]["LOG_FORMAT"] == "text", name
    for env in ("production", "staging"):
        for page in ("customer", "admin"):
            example = DEPLOY / "environments" / env / f"{page}.env.example"
            assert "LOG_LEVEL=INFO" in example.read_text(encoding="utf-8").splitlines()


def test_every_nginx_json_error_follows_the_api_error_contract() -> None:
    """Stage 13 (Task 22): errors Nginx produces itself carry the same fields as the
    backend's, ``retryable`` included, with the backend's value for the code."""
    import json

    from app.models.errors import ErrorCode, ErrorResponse, is_retryable

    sources = [
        DEPLOY / "nginx" / "nginx.conf.template",
        *(DEPLOY / "nginx" / "snippets").glob("*.conf"),
    ]
    bodies = [
        json.loads(body)
        for path in sources
        for body in re.findall(r"return \d{3} '(\{.*?\})\\n';", path.read_text(encoding="utf-8"))
    ]

    assert len(bodies) == 5  # four in api-errors.conf, STAFF_FORBIDDEN in the template
    for body in bodies:
        ErrorResponse.model_validate(body)
        error = body["error"]
        assert error["retryable"] is is_retryable(ErrorCode(error["code"])), error["code"]
