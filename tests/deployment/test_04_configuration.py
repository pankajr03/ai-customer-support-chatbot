"""Configuration validation and missing secrets, with the built images.

One-off backend containers (``docker compose run --rm --no-deps``) with one setting
changed. Each gets its own throwaway database in /tmp: none of them opens the running
stack's database, and none of them is paid (the LLM is either off or refused).
"""

import subprocess

from tests.deployment.conftest import Stack

THROWAWAY = ["-e", "DATABASE_URL=sqlite:////tmp/config-test.db"]


def run_backend(stack: Stack, *settings: str, command: tuple[str, ...] = ()
                ) -> subprocess.CompletedProcess[str]:  # fmt: skip
    """The backend's own command (the server) unless ``command`` is given."""
    env = [arg for setting in settings for arg in ("-e", setting)]
    return stack.compose("run", "--rm", "--no-deps", "-T", *THROWAWAY, *env, "backend",
                         *command, check=False, timeout=300)  # fmt: skip


def output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def test_the_compose_configuration_is_valid(built: Stack) -> None:
    result = built.compose("config", "--quiet", check=False)

    assert result.returncode == 0, output(result)


def test_compose_refuses_to_run_without_a_deployment_environment(built: Stack) -> None:
    incomplete = built.deploy / "incomplete.env"
    incomplete.write_text(
        "\n".join(line for line in (built.deploy / ".env").read_text().splitlines()
                  if not line.startswith("DEPLOY_ENV=")) + "\n", newline="\n",
    )  # fmt: skip

    result = built.compose("config", "--quiet", env_file=incomplete, check=False)

    assert result.returncode != 0
    assert "set DEPLOY_ENV in deploy/.env" in output(result)


def test_an_invalid_setting_stops_the_backend_with_a_readable_message(built: Stack) -> None:
    result = run_backend(built, "DEBUG=true")  # APP_ENV=production in backend.env

    assert result.returncode != 0
    assert "Cannot start" in output(result) and "DEBUG must be false" in output(result)
    assert "Traceback" not in output(result)


def test_llm_enabled_without_an_api_key_refuses_to_start(built: Stack) -> None:
    result = run_backend(built, "LLM_ENABLED=true", "OPENAI_API_KEY=")

    assert result.returncode != 0
    assert "LLM_ENABLED=true requires OPENAI_API_KEY" in output(result)


def test_an_invalid_api_key_is_refused_without_being_printed(built: Stack) -> None:
    secret = "sk-deploytest-SHORT"  # noqa: S105 - a fake key
    result = run_backend(built, "LLM_ENABLED=true", f"OPENAI_API_KEY={secret}")

    assert result.returncode != 0
    assert "OPENAI_API_KEY looks incomplete" in output(result)
    assert secret not in output(result)


def test_without_staff_tokens_every_staff_operation_is_disabled(built: Stack) -> None:
    check = (
        "from fastapi.testclient import TestClient; from app.main import create_app; "
        "c = TestClient(create_app()); r = c.get('/api/v1/admin/faqs'); "
        "print(r.status_code, r.json()['error']['code'])"
    )
    result = run_backend(built, "STAFF_TOKEN_HASHES=[]", command=("python", "-c", check))

    assert result.returncode == 0, output(result)
    assert "403 STAFF_ACCESS_DISABLED" in output(result)
    assert "No staff configured" in output(result)  # a warning in production
