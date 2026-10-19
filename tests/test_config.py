"""Configuration loading, validation and secret handling."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import PROJECT_ROOT, Settings
from app.main import create_app

FAKE_KEY = "sk-test-1234567890-not-a-real-key"


def test_defaults_are_safe_for_development() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_name == "AI Customer Support Chatbot API"
    assert settings.app_env == "development"
    assert settings.debug is False
    assert settings.log_level == "INFO"
    assert settings.api_v1_prefix == "/api/v1"
    assert settings.cors_origins == []
    assert settings.openai_api_key is None
    assert settings.embedding_model_name == "sentence-transformers/all-MiniLM-L6-v2"


def test_values_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_NAME", "Acme Support API")
    monkeypatch.setenv("APP_ENV", "Staging")
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("API_V1_PREFIX", "/api/v2")
    monkeypatch.setenv("CORS_ORIGINS", '["http://localhost:8501"]')

    settings = Settings(_env_file=None)

    assert settings.app_name == "Acme Support API"
    assert settings.app_env == "staging"
    assert settings.debug is True
    assert settings.log_level == "DEBUG"
    assert settings.api_v1_prefix == "/api/v2"
    assert settings.cors_origins == ["http://localhost:8501"]


def test_values_read_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("APP_ENV=staging\nLOG_LEVEL=WARNING\n", encoding="utf-8")

    settings = Settings(_env_file=env_file)

    assert settings.app_env == "staging"
    assert settings.log_level == "WARNING"


def test_environment_variable_overrides_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LOG_LEVEL=WARNING\n", encoding="utf-8")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")

    assert Settings(_env_file=env_file).log_level == "ERROR"


def test_env_example_is_valid_and_complete() -> None:
    """.env.example must load cleanly and document every required variable."""
    example = PROJECT_ROOT / ".env.example"
    keys = {
        line.split("=", 1)[0].strip()
        for line in example.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }

    assert {"APP_NAME", "APP_ENV", "DEBUG", "LOG_LEVEL", "API_V1_PREFIX"} <= keys
    assert Settings(_env_file=example) == Settings(_env_file=None)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("APP_ENV", "prod"),
        ("LOG_LEVEL", "VERBOSE"),
        ("DEBUG", "maybe"),
        ("APP_NAME", "   "),
        ("API_V1_PREFIX", "api/v1"),
        ("API_V1_PREFIX", "/api/v1/"),
        ("API_V1_PREFIX", "/"),
        ("API_V1_PREFIX", "/api v1"),
        ("CORS_ORIGINS", '["*"]'),
        ("CORS_ORIGINS", '["localhost:8501"]'),
        ("CORS_ORIGINS", '["http://localhost:8501/"]'),
        ("CORS_ORIGINS", "not-json"),
    ],
)
def test_invalid_values_fail_fast(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises((ValidationError, ValueError)):
        Settings(_env_file=None)


def test_debug_is_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="DEBUG must be false"):
        Settings(_env_file=None, app_env="production", debug=True)


def test_openai_key_is_optional_and_blank_means_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")

    assert Settings(_env_file=None).openai_api_key is None


def test_secret_is_masked_everywhere_it_could_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)

    settings = Settings(_env_file=None)

    assert settings.openai_api_key is not None
    assert settings.openai_api_key.get_secret_value() == FAKE_KEY
    assert FAKE_KEY not in repr(settings)
    assert FAKE_KEY not in str(settings)
    assert FAKE_KEY not in settings.model_dump_json()
    assert FAKE_KEY not in str(settings.safe_summary())
    assert settings.safe_summary()["openai_api_key_set"] is True


def test_logs_do_not_contain_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Check the real console output: configure_logging() installs its own stdout handler.
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)

    app = create_app(Settings(_env_file=None, log_level="DEBUG"))
    TestClient(app).get("/api/v1/health")
    output = capsys.readouterr().out

    assert "started" in output
    assert "openai_api_key_set': True" in output
    assert FAKE_KEY not in output


def test_custom_prefix_is_applied() -> None:
    client = TestClient(create_app(Settings(_env_file=None, api_v1_prefix="/api/v2")))

    assert client.get("/api/v2/health").status_code == 200
    assert client.get("/api/v1/health").status_code == 404


@pytest.mark.parametrize(
    "value", ["  sentence-transformers/all-mpnet-base-v2  ", "C:/models/my-local-model"]
)
def test_embedding_model_is_configurable(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("EMBEDDING_MODEL_NAME", value)

    settings = Settings(_env_file=None)

    assert settings.embedding_model_name == value.strip()
    assert settings.safe_summary()["embedding_model_name"] == value.strip()


@pytest.mark.parametrize("value", ["", "   ", "x" * 201])
def test_invalid_embedding_model_name_fails_fast(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("EMBEDDING_MODEL_NAME", value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_version_comes_from_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_VERSION", "9.9.9")

    assert Settings(_env_file=None).app_version == "0.1.0"


# --- Environment configuration (Stage 13) -------------------------------------------------

PRODUCTION_FILE_DB = "sqlite:///data/production-check.db"  # validated only, never opened


def test_env_example_documents_every_setting_and_nothing_else() -> None:
    from dotenv import dotenv_values

    from frontend.config import FrontendSettings

    documented = set(dotenv_values(PROJECT_ROOT / ".env.example"))
    settings = {name.upper() for name in [*Settings.model_fields, *FrontendSettings.model_fields]}

    assert settings - documented == set(), "settings missing from .env.example"
    assert documented - settings == set(), "variables in .env.example that nothing reads"


def test_env_example_explains_every_variable() -> None:
    """Each variable line is preceded by a comment (its documentation)."""
    lines = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    undocumented = []
    for index, line in enumerate(lines):
        if line and not line.startswith("#") and "=" in line:
            above = [previous for previous in lines[:index] if previous.strip()]
            if not above or not above[-1].startswith("#") and "=" not in above[-1]:
                undocumented.append(line.split("=", 1)[0])
    assert undocumented == []


@pytest.mark.parametrize("database_url", ["sqlite://", "sqlite:///:memory:"])
def test_production_refuses_an_in_memory_database(database_url: str) -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL must point to a database file"):
        Settings(_env_file=None, app_env="production", database_url=database_url)

    for environment in ("development", "staging"):  # fine for tests and local work
        Settings(_env_file=None, app_env=environment, database_url=database_url)


def test_production_refuses_disabled_rate_limits() -> None:
    with pytest.raises(ValidationError, match="RATE_LIMIT_ENABLED must be true"):
        Settings(
            _env_file=None,
            app_env="production",
            database_url=PRODUCTION_FILE_DB,
            rate_limit_enabled=False,
        )

    Settings(_env_file=None, app_env="staging", rate_limit_enabled=False)  # load tests


def test_production_defaults_are_accepted() -> None:
    settings = Settings(_env_file=None, app_env="production", database_url=PRODUCTION_FILE_DB)

    assert settings.docs_enabled is False
    assert settings.debug is False
    assert settings.rate_limit_enabled is True


def test_production_failures_name_variables_but_never_print_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import describe_settings_error

    fake_key = "sk-UNIQUE-KEY-MARKER"  # too short: rejected as truncated
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", fake_key)
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("STAFF_TOKEN_HASHES", '["alice:admin:not-a-hash-UNIQUE-STAFF-MARKER"]')

    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)
    message = describe_settings_error(caught.value)

    assert "STAFF_TOKEN_HASHES" in message
    assert fake_key not in message and fake_key not in str(caught.value)
    assert "UNIQUE-STAFF-MARKER" not in message and "UNIQUE-STAFF-MARKER" not in str(caught.value)


def test_production_warns_when_no_staff_are_configured(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'app.db').as_posix()}"
    for environment, level in (("production", "WARNING"), ("development", "INFO")):
        create_app(Settings(_env_file=None, app_env=environment, database_url=database_url))
        lines = [line for line in capsys.readouterr().out.splitlines() if "No staff" in line]
        assert len(lines) == 1 and f"| {level} " in lines[0], (environment, lines)


def test_startup_message_names_the_settings_file_only_when_it_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core import config

    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, log_level="VERBOSE")

    present = tmp_path / ".env"
    present.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", present)
    assert f"Settings file: {present}" in config.describe_settings_error(caught.value)

    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "missing" / ".env")
    message = config.describe_settings_error(caught.value)
    assert "Settings file:" not in message
    assert "the values come from the environment" in message
