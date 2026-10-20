"""The Stage 13 profiler's own logic (scripts/profile_performance.py). The measurements
themselves need running servers and are not part of the test suite."""

import json

import pytest

from scripts.profile_performance import (
    MIB,
    backend_env,
    clean_environ,
    parse_cgroup,
    summary,
)


def test_cgroup_v2_memory_excludes_reclaimable_cache_and_cpu_is_in_seconds() -> None:
    out = f"current {600 * MIB}\nanon 1\ninactive_file {100 * MIB}\nusage_usec 2500000\n"

    assert parse_cgroup(out) == {"memory_mib": 500.0, "cpu_seconds": 2.5}


def test_cgroup_v1_is_read_the_way_docker_stats_reads_it() -> None:
    out = f"current {300 * MIB}\ntotal_inactive_file {44 * MIB}\ncpuacct_usage 1500000000\n"

    assert parse_cgroup(out) == {"memory_mib": 256.0, "cpu_seconds": 1.5}


def test_the_measured_backend_can_never_call_a_paid_llm() -> None:
    env = backend_env("hash")

    assert env["LLM_ENABLED"] == "false" and env["QUERY_REWRITE_USE_LLM"] == "false"
    assert "OPENAI_API_KEY" not in env
    assert json.loads(env["STAFF_TOKEN_HASHES"]) == ["perf:admin:hash"]


def test_application_settings_from_the_shell_are_not_passed_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-the-shell-0123456789")
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("BACKEND_READ_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("PATH_FOR_THE_TEST", "kept")

    env = clean_environ()

    assert {"OPENAI_API_KEY", "LLM_ENABLED", "BACKEND_READ_TIMEOUT_SECONDS"}.isdisjoint(env)
    assert env["PATH_FOR_THE_TEST"] == "kept"


def test_summary_reports_median_p95_and_max() -> None:
    assert summary([float(v) for v in range(1, 101)]) == {
        "n": 100,
        "p50_ms": 50.5,
        "p95_ms": 95.0,
        "max_ms": 100.0,
    }
