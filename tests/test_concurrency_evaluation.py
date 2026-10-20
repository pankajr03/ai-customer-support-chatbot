"""The Stage 13 concurrency evaluation, at a tiny scale (Task 20).

Runs ``scripts/evaluate_concurrency.py`` end to end on loopback (lexical encoder, mocked
LLM, concurrency 2) and requires every correctness check to hold: isolation between
conversations, idempotent retries of messages and support requests, one winner when two
editors race, and a consistent database. The full-size run and its latencies are in
docs/concurrency-and-limits.md; this test keeps the guarantees, not the timings.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import evaluate_concurrency


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    output = tmp_path_factory.mktemp("concurrency") / "report.json"
    status = evaluate_concurrency.main([
        "--encoder", "lexical", "--concurrency", "2", "--sessions", "3", "--turns", "2",
        "--rounds", "1", "--soak-rounds", "0", "--output", str(output),
    ])  # fmt: skip
    assert status == 0
    return dict(json.loads(Path(output).read_text(encoding="utf-8")))


def test_the_database_is_consistent_after_concurrent_load(report: dict[str, Any]) -> None:
    checks = report["scenarios"]["consistency"]

    assert checks["integrity_check"] == "ok"
    assert {key: value for key, value in checks.items() if isinstance(value, int)} == {
        "foreign_key_violations": 0,
        "conversations_misnumbered_or_not_alternating": 0,
        "leases_left": 0,
        "acknowledged_replies": checks["acknowledged_replies"],
        "acknowledged_replies_missing": 0,
        "retry_keys_stored_twice": 0,
        "refused_requests_stored_anyway": 0,
        "tickets_acknowledged": checks["tickets_acknowledged"],
        "tickets_acknowledged_missing": 0,
        "tickets_stored_without_acknowledgement": 0,
        "duplicate_reference_numbers": 0,
        "tickets_without_creation_event": 0,
        "faq_revisions_vs_version": 0,
        "answered_replies_checked": checks["answered_replies_checked"],
        "answered_replies_not_an_approved_version": 0,
    }
    assert checks["acknowledged_replies"] > 0 and checks["tickets_acknowledged"] > 0


def test_conversations_stay_isolated(report: dict[str, Any]) -> None:
    isolation = report["scenarios"]["isolation"]

    assert isolation["conversations_checked"] >= 3 and isolation["problems"] == []


def test_concurrent_retries_are_idempotent(report: dict[str, Any]) -> None:
    chat = report["scenarios"]["concurrent_chat"]
    tickets = report["scenarios"]["simultaneous_tickets"]

    assert chat["distinct_replies_for_one_retried_message"] == 1
    assert chat["late_retry_replayed"] is True
    assert tickets["tickets_for_one_request_id"] == 1


def test_one_message_per_conversation_is_answered_at_a_time(report: dict[str, Any]) -> None:
    outcomes = report["scenarios"]["concurrent_chat"]["same_session_different_messages"]

    # Every request is answered or refused with a retryable 409, never anything else.
    assert set(outcomes["outcomes"]) <= {"200", "409 REQUEST_IN_PROGRESS"}


def test_racing_editors_get_one_winner_and_a_version_conflict(report: dict[str, Any]) -> None:
    race = report["scenarios"]["faq_updates_during_retrieval"]["editor_race"]

    assert race["winners"] == 1
    assert race["outcomes"] == {"200": 1, "409 FAQ_VERSION_CONFLICT": 1}
