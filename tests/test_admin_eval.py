"""The Stage 11 admin evaluation (Task 20): the scenario file and its runner."""

import json
from pathlib import Path

from scripts import evaluate_admin

REQUIRED_FIELDS = {"id", "area", "description", "preconditions", "action", "expected"}
REQUIRED_AREAS = {
    "FAQ creation",
    "FAQ editing",
    "FAQ deactivation",
    "FAQ reactivation",
    "Concurrent edits",
    "Retrieval synchronization",
    "Stale embedding prevention",
    "Invalid FAQ content",
    "Unauthorized modification",
    "Support ticket review",
    "Ticket status updates",
    "Invalid status transitions",
    "Analytics accuracy",
    "Empty database",
    "Database failure",
    "Staff authorization failure",
}


def test_the_scenarios_are_complete_and_each_has_an_implementation() -> None:
    scenarios = evaluate_admin.load_scenarios()
    assert len(scenarios) >= 25
    assert all(REQUIRED_FIELDS <= set(s) for s in scenarios)
    assert REQUIRED_AREAS <= {s["area"] for s in scenarios}
    ids = [s["id"] for s in scenarios]
    assert len(set(ids)) == len(ids)
    assert set(ids) == set(evaluate_admin.IMPLEMENTATIONS)


def test_every_scenario_passes_when_run() -> None:
    results = evaluate_admin.evaluate()
    failed = {r["id"]: r["actual"] for r in results if r["status"] != "pass"}
    assert failed == {}
    assert all(r["actual"] for r in results)


def test_the_recorded_results_match_the_scenarios() -> None:
    recorded = json.loads(
        (Path(evaluate_admin.PROJECT_ROOT) / "data" / "admin_eval_results.json").read_text(
            encoding="utf-8"
        )
    )
    assert [r["id"] for r in recorded] == [s["id"] for s in evaluate_admin.load_scenarios()]
    assert all({"actual", "status"} <= set(r) for r in recorded)


def test_a_failed_check_is_recorded_not_hidden(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def broken(env: evaluate_admin.Env) -> str:
        evaluate_admin.check(False, "observed something else")
        return "unreachable"

    monkeypatch.setitem(evaluate_admin.IMPLEMENTATIONS, "AE-01", broken)
    status, actual = evaluate_admin.run_scenario("AE-01", tmp_path)
    assert (status, actual) == ("fail", "observed something else")
