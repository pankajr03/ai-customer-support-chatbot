"""Stage 12 (Tasks 14 and 20): the grounding evaluation's judgments and metrics, and the
local load test, on tiny inputs (lexical encoder, loopback only, no model, no network).
"""

from typing import Any

import pytest

from scripts import evaluate_grounding, load_test_local

# --- Grounded answer evaluation ------------------------------------------------------------


def test_claim_support_is_lexical_per_sentence() -> None:
    evidence = ["Refunds are issued within 3 business days to the original payment method."]
    rows = evaluate_grounding.claim_support(
        "Refunds are issued within 3 business days. We also give free laptops.", evidence
    )
    assert [r["supported"] for r in rows] == [True, False]
    assert rows[0]["coverage"] == 1.0


def _result(**changes: Any) -> dict[str, Any]:
    result = {
        "case_id": "x", "outcome": "answer", "acceptable_outcomes": ["answer"],
        "grounded": True, "unsupported_claims": [], "attribution_correct": True,
        "outcome_correct": True, "problems": [], "latency_ms": 10.0, "model_called": True,
        "usage": None, "manual_review": False,
    }  # fmt: skip
    return {**result, **changes}


def test_grounding_metrics_use_the_stated_denominators() -> None:
    results = [
        _result(),
        _result(grounded=False, unsupported_claims=["grounding:unsupported_number"]),
        _result(outcome="fallback", acceptable_outcomes=["fallback"], grounded=False),
        _result(
            outcome="answer",
            acceptable_outcomes=["clarification"],
            grounded=False,
            outcome_correct=False,
        ),  # fmt: skip
    ]
    summary = evaluate_grounding.summarize(results, {"passed": 9, "regular_cases": 10})
    assert summary["grounded_answer_rate"] == {"n": 1, "of": 3, "rate": 1 / 3}
    assert summary["unsupported_claim_rate"] == {"n": 1, "of": 4, "rate": 0.25}
    assert summary["correct_fallback_rate"]["n"] == 1
    assert summary["clarification_correctness"] == {"n": 0, "of": 1, "rate": 0.0}
    assert summary["source_attribution_correctness"]["of"] == 2  # answers where acceptable
    assert summary["prompt_injection_resistance"]["rate"] == 0.9
    assert isinstance(summary["token_usage"], str)  # no real usage: not invented


def test_the_grounding_script_runs_end_to_end_without_a_model() -> None:
    report = evaluate_grounding.evaluate(encoder="lexical", with_injection=False, max_cases=3)
    assert report["summary"]["cases"] == 3
    assert report["meta"]["judgment"].startswith("deterministic")
    assert "live" not in report["meta"]["llm_model"]


def test_live_mode_requires_explicit_confirmation(capsys: pytest.CaptureFixture[str]) -> None:
    assert evaluate_grounding.main(["--llm", "live"]) == 2
    assert "--yes" in capsys.readouterr().out


# --- Local load test -----------------------------------------------------------------------


def test_latency_summary_percentiles() -> None:
    summary = load_test_local.latency_summary([float(n) for n in range(1, 101)])
    assert (summary["samples"], summary["p50_ms"], summary["p90_ms"], summary["max_ms"]) == (
        100, 51.0, 91.0, 100.0,
    )  # fmt: skip
    assert load_test_local.latency_summary([]) == {"samples": 0}


def test_load_is_bounded() -> None:
    with pytest.raises(ValueError):
        load_test_local.run(
            requests=load_test_local.MAX_REQUESTS + 1, concurrency=1, encoder="lexical"
        )
    with pytest.raises(ValueError):
        load_test_local.run(
            requests=1, concurrency=load_test_local.MAX_CONCURRENCY + 1, encoder="lexical"
        )


def test_a_tiny_load_test_runs_on_loopback_only() -> None:
    report = load_test_local.run(requests=4, concurrency=2, encoder="lexical")
    assert report["meta"]["host"] == "127.0.0.1"
    for name, row in report["scenarios"].items():
        assert row["error_rate"]["n"] == 0, (name, row["statuses"])
    assert report["model_calls"] >= 1  # the mocked model, never a real one
    assert report["memory"]["after"]["peak_mb"] is None or report["memory"]["after"]["peak_mb"] > 0
