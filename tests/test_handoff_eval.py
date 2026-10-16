"""The human handoff evaluation dataset and its summary (Stage 10, Task 16).

Running the scenarios needs the real embedding model (python scripts/evaluate_handoff.py);
these tests check the dataset and the metric summary offline.
"""

from typing import Any

from scripts.evaluate_handoff import (
    METRICS,
    MIN_SCENARIOS,
    REQUIRED_CATEGORIES,
    load_dataset,
    summarise,
    validate_dataset,
)


def test_the_dataset_is_valid_and_covers_every_required_case() -> None:
    dataset = load_dataset()

    assert validate_dataset(dataset) == []
    assert len(dataset.scenarios) >= MIN_SCENARIOS
    assert REQUIRED_CATEGORIES <= {s.category for s in dataset.scenarios}


def test_every_metric_is_exercised_by_some_step() -> None:
    actions = {step.action for s in load_dataset().scenarios for step in s.steps}

    assert {"chat", "decline", "submit", "retry", "api_conflict", "other_session",
            "staff_list", "staff_change", "check_status", "expire"} <= actions  # fmt: skip


def test_faults_cover_timeouts_and_database_failures() -> None:
    faults = {step.fault for s in load_dataset().scenarios for step in s.steps if step.fault}

    assert faults == {"timeout", "database"}


def test_invalid_datasets_are_reported() -> None:
    dataset = load_dataset()
    broken = dataset.model_copy(update={"scenarios": dataset.scenarios[:3]})

    problems = validate_dataset(broken)

    assert any("at least" in problem for problem in problems)
    assert any("missing categories" in problem for problem in problems)


def test_the_summary_counts_only_recorded_checks() -> None:
    results: list[dict[str, Any]] = [
        {"passed": True, "checks": [{"metric": "authorization", "passed": True}]},
        {
            "passed": False,
            "checks": [
                {"metric": "authorization", "passed": False},
                {"metric": "ticket_creation", "passed": True},
            ],
        },
    ]

    summary = summarise(results)

    assert "authorization            1/2 (50%)" in summary
    assert "ticket_creation          1/1 (100%)" in summary
    assert "contact_validation       0/0 (-)" in summary  # never invented
    assert "scenarios                1/2" in summary
    assert len(METRICS) == 9
