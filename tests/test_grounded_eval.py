"""The grounded-answer evaluation dataset and script (scripts/evaluate_grounded_answers.py).

All checks are deterministic: scoring uses hand-made inputs, and the end-to-end runs use
the fake encoder, the fixed test FAQs and scripted fake LLM clients. No network.
"""

import json
import os
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import PROJECT_ROOT, Settings
from app.database.faq_repository import FAQRepository
from app.models.answer import GenerationMethod
from app.models.usage import ModelPricing
from app.services.embedding_service import EmbeddingService
from app.services.llm_service import LLMService
from app.services.usage_tracker import UsageTracker
from scripts.evaluate_grounded_answers import (
    CAVEAT,
    DEFAULT_DATASET,
    CaseType,
    GroundedCase,
    GroundedDataset,
    Outcome,
    check_labels,
    contains_phrase,
    evaluate,
    format_report,
    load_dataset,
    main,
    score_case,
    summarize,
)
from tests.fakes import CountingLoader

KEY = SecretStr("sk-test-0123456789abcdefghij")


def fake_factory(model_name: str) -> EmbeddingService:
    return EmbeddingService(model_name, loader=CountingLoader())


def case(**overrides: Any) -> GroundedCase:
    data = {
        "id": "ga_001",
        "type": "straightforward",
        "query": "q",
        "acceptable_outcomes": ["answer"],
        "acceptable_source_ids": ["faq_101"],
        "must_include": [["my orders"]],
        "must_not_include": ["90 days"],
        "explanation": "why",
    }
    data.update(overrides)
    return GroundedCase.model_validate(data)


# --- The bundled dataset ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def dataset() -> GroundedDataset:
    return load_dataset(DEFAULT_DATASET)


@pytest.mark.sample_data
def test_dataset_has_at_least_30_cases_covering_every_type(dataset: GroundedDataset) -> None:
    types = Counter(c.type for c in dataset.cases)

    assert len(dataset.cases) >= 30
    assert set(types) == set(CaseType)
    assert all(count >= 3 for count in types.values())


@pytest.mark.sample_data
def test_dataset_labels_match_the_real_faqs(dataset: GroundedDataset) -> None:
    """IDs are real and active; required facts exist and forbidden claims are absent."""
    faqs = FAQRepository.from_json_file(PROJECT_ROOT / dataset.faq_data).list_all()

    assert check_labels(dataset, faqs) == []


@pytest.mark.sample_data
def test_dataset_covers_every_outcome(dataset: GroundedDataset) -> None:
    only = Counter(
        tuple(c.acceptable_outcomes) for c in dataset.cases if len(c.acceptable_outcomes) == 1
    )

    assert only[("answer",)] >= 10 and only[("clarification",)] >= 3 and only[("fallback",)] >= 5


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "case_1"},
        {"acceptable_outcomes": []},
        {"acceptable_outcomes": ["answer", "answer"]},
        {"acceptable_source_ids": []},  # answer without sources
        {"acceptable_outcomes": ["fallback"], "acceptable_source_ids": []},  # must_include left
        {"must_include": [[]]},
        {"must_include": [[" "]]},
        {"must_not_include": [""]},
        {"type": "unknown"},
        {"explanation": ""},
        {"extra": 1},
    ],
)
def test_invalid_cases_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        case(**overrides)


def test_duplicate_ids_and_queries_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate case ids"):
        GroundedDataset(cases=[case(), case(query="other")])
    with pytest.raises(ValidationError, match="duplicate queries"):
        GroundedDataset(cases=[case(query="Q"), case(id="ga_002", query="q")])


def test_labels_are_checked_against_the_faqs(faq_repository: FAQRepository) -> None:
    data = GroundedDataset(
        cases=[
            case(id="ga_001", acceptable_source_ids=["faq_999"], must_include=[]),
            case(id="ga_002", query="b", acceptable_source_ids=["faq_105"], must_include=[]),
            case(id="ga_003", query="c", must_include=[["not in the faq"]]),
            case(id="ga_004", query="d", must_include=[], must_not_include=["my orders"]),
        ]
    )

    assert check_labels(data, faq_repository.list_all()) == [
        "ga_001: FAQ faq_999 does not exist",
        "ga_002: FAQ faq_105 is inactive",
        "ga_003: no acceptable FAQ contains any of ['not in the faq']",
        "ga_004: forbidden phrase 'my orders' is in an acceptable FAQ",
    ]


# --- Matching and scoring --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "phrase", "found"),
    [
        ("Returns within 30 Days.", "30 days", True),
        ("We arrive soon", "arr", False),  # whole words only
        ("Arr, matey", "arr", True),
        ("You can’t add items", "can't", True),
        ("free  of\ncharge", "free of charge", True),
        ("cards", "card", False),
    ],
)
def test_phrase_matching(text: str, phrase: str, found: bool) -> None:
    assert contains_phrase(text, phrase) is found


def diagnostics(**fields: Any) -> SimpleNamespace:
    data = {
        "llm_called": False,
        "latency_ms": None,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "estimated_cost": None,
        "fallback_reason": None,
        "llm_error_category": None,
    }
    data.update(fields)
    return SimpleNamespace(**data)


def scored(
    label: GroundedCase, method: GenerationMethod, answer: str, sources: list[str], **d: Any
) -> Any:
    primary = "Track it in My Orders within 30 days." if sources else None
    return score_case(
        label,
        generation_method=method,
        decision="answered",
        answer=answer,
        source_faq_ids=sources,
        evidence=[primary] if primary else [],
        primary=primary,
        latency_ms=12.0,
        diagnostics=diagnostics(**d),
    )


def test_score_a_good_answer() -> None:
    result = scored(
        case(), GenerationMethod.LLM_GROUNDED, "Check My Orders within 30 days.", ["faq_101"]
    )

    assert (result.outcome, result.outcome_ok, result.source_ok, result.include_ok) == (
        "answer",
        True,
        True,
        True,
    )
    assert (result.forbidden_hits, result.grounding_issues) == ([], [])


def test_score_an_answer_with_problems() -> None:
    result = scored(case(), GenerationMethod.LLM_GROUNDED, "Returns take 90 days.", ["faq_102"])

    assert (result.source_ok, result.include_ok) == (False, False)
    assert result.forbidden_hits == ["90 days"]
    assert "unsupported_number" in result.grounding_issues


@pytest.mark.parametrize(
    ("method", "outcome"),
    [(GenerationMethod.CLARIFICATION, "clarification"), (GenerationMethod.FALLBACK, "fallback")],
)
def test_non_answers_are_not_graded_for_relevance(method: GenerationMethod, outcome: str) -> None:
    result = scored(case(), method, "Could you clarify?", [])

    assert (result.outcome, result.outcome_ok) == (outcome, False)
    assert (result.source_ok, result.include_ok, result.grounding_issues) == (None, None, [])


def test_forbidden_claims_count_even_in_fallbacks() -> None:
    label = case(acceptable_outcomes=["fallback"], acceptable_source_ids=[], must_include=[])

    result = scored(label, GenerationMethod.FALLBACK, "Returns take 90 days.", [])

    assert result.outcome_ok and result.forbidden_hits == ["90 days"]


def test_summary_uses_fixed_denominators() -> None:
    fallback_case = case(
        id="ga_002", acceptable_outcomes=["fallback"], acceptable_source_ids=[], must_include=[]
    )
    clarify_case = case(id="ga_003", acceptable_outcomes=["clarification"], must_include=[])
    results = [
        scored(
            case(),
            GenerationMethod.LLM_GROUNDED,
            "My Orders within 30 days.",
            ["faq_101"],
            llm_called=True,
            latency_ms=900.0,
            input_tokens=800,
            output_tokens=60,
            total_tokens=860,
            estimated_cost=0.002,
        ),  # fmt: skip
        scored(
            case(id="ga_004"),
            GenerationMethod.APPROVED_FAQ,
            "Track it in My Orders within 30 days.",
            ["faq_101"],
            llm_called=True,
            latency_ms=700.0,
            input_tokens=700,
            output_tokens=40,
            total_tokens=740,
            fallback_reason="grounding_check_failed",
        ),  # fmt: skip
        scored(
            fallback_case, GenerationMethod.APPROVED_FAQ, "My Orders within 30 days.", ["faq_101"]
        ),
        scored(clarify_case, GenerationMethod.CLARIFICATION, "Could you clarify?", []),
    ]

    m = summarize(results)

    assert m["outcome_correctness"] == {"n": 3, "of": 4, "rate": 0.75}
    assert m["fallback_correctness"] == {"n": 0, "of": 1, "rate": 0.0}
    assert m["false_answers_on_fallback_cases"] == 1
    assert m["clarification_correctness"] == {"n": 1, "of": 1, "rate": 1.0}
    assert m["missed_answers"] == {"n": 0, "of": 2, "rate": 0.0}
    assert m["answer_relevance"]["of"] == 2 and m["source_correctness"]["of"] == 2
    assert m["grounding_compliance"]["of"] == 3
    assert m["grounding_compliance_generated"] == {"n": 1, "of": 1, "rate": 1.0}
    assert m["tokens"] == {
        "llm_calls": 2,
        "calls_with_usage": 2,
        "input": 1500,
        "output": 100,
        "total": 1600,
        "mean_total_per_call": 800.0,
    }
    assert m["estimated_cost"] == {"priced_calls": 1, "total": 0.002, "mean_per_call": 0.002}
    assert m["fallback_reasons"] == {"grounding_check_failed": 1}
    assert m["llm_latency_ms"]["mean"] == 800.0


def test_summary_without_llm_calls_reports_no_tokens_or_cost() -> None:
    m = summarize(
        [scored(case(), GenerationMethod.APPROVED_FAQ, "My Orders within 30 days.", ["faq_101"])]
    )

    assert m["tokens"]["llm_calls"] == 0 and m["tokens"]["total"] is None
    assert m["estimated_cost"]["total"] is None


# --- End to end (fake encoder, test FAQs, scripted LLMs) --------------------------------------


@pytest.fixture
def eval_file(tmp_path: Path) -> Path:
    rows = [
        (
            "ga_001",
            "straightforward",
            "How do I reset my password?",
            ["answer"],
            ["faq_107"],
            [["forgot password"]],
            ["30 days"],
        ),  # fmt: skip
        ("ga_002", "out_of_scope", "xyzzy quantum", ["fallback"], [], [], ["paris"]),
        ("ga_003", "ambiguous", "order", ["clarification"], ["faq_101", "faq_102"], [], []),
    ]
    cases = [
        {
            "id": cid,
            "type": kind,
            "query": query,
            "acceptable_outcomes": outcomes,
            "acceptable_source_ids": sources,
            "must_include": include,
            "must_not_include": exclude,
            "explanation": "test case",
        }
        for cid, kind, query, outcomes, sources, include, exclude in rows
    ]
    path = tmp_path / "grounded_eval.json"
    path.write_text(json.dumps({"faq_data": "unused", "cases": cases}), encoding="utf-8")
    return path


def scripted_llm(answer: str, tracker: UsageTracker | None = None) -> LLMService:
    reply = json.dumps(
        {"status": "answered", "answer": answer, "source_faq_ids": ["faq_107"], "reason": "r"}
    )

    def create(**kwargs: Any) -> Any:
        return SimpleNamespace(
            status="completed",
            output_text=reply,
            output=[],
            model="gpt-5.4-mini",
            id="resp_1",
            _request_id="req_1",
            usage=SimpleNamespace(input_tokens=500, output_tokens=50, total_tokens=550),
        )

    client = SimpleNamespace(responses=SimpleNamespace(create=create), close=lambda: None)
    return LLMService(
        KEY, model="gpt-5.4-mini", usage_tracker=tracker, client_factory=lambda key: client
    )


@pytest.fixture
def settings(make_settings: Callable[..., Settings]) -> Settings:
    """A high similarity bar makes fake-encoder outcomes predictable: only the exact
    question is answered (exact match); everything else falls back."""
    return make_settings(answer_min_similarity=0.99)


def run(eval_file: Path, faq_file: Path, settings: Settings, **kwargs: Any) -> dict[str, Any]:
    return evaluate(eval_file, faq_file, settings, embedding_factory=fake_factory, **kwargs)


def test_llm_off_report_is_complete_and_deterministic(
    eval_file: Path, faq_file: Path, settings: Settings
) -> None:
    first = run(eval_file, faq_file, settings)
    second = run(eval_file, faq_file, settings)

    def stable(report: dict[str, Any]) -> list[Any]:
        return [{k: v for k, v in r.items() if "latency" not in k} for r in report["results"]]

    assert stable(first) == stable(second)
    meta = first["meta"]
    assert (meta["mode"], meta["sample_size"], meta["caveat"]) == ("off", 3, CAVEAT)
    assert meta["llm"]["enabled"] is False and meta["llm"]["model"] is None
    password = first["results"][0]
    assert (password["generation_method"], password["outcome_ok"]) == ("approved_faq", True)
    assert first["results"][1]["outcome"] == "fallback"
    assert first["metrics"]["tokens"]["llm_calls"] == 0
    json.dumps(first)


def test_a_well_behaved_llm_is_measured_with_tokens_and_cost(
    eval_file: Path, faq_file: Path, make_settings: Callable[..., Settings]
) -> None:
    settings = make_settings(
        answer_min_similarity=0.99,
        llm_pricing={
            "gpt-5.4-mini": ModelPricing(input_per_million_tokens=2, output_per_million_tokens=8)
        },
    )
    tracker = UsageTracker.from_settings(settings)
    llm = scripted_llm(
        "Choose Forgot Password on the sign-in page and follow the emailed link.", tracker
    )

    report = run(eval_file, faq_file, settings, mode="live", llm=llm)

    password = report["results"][0]
    assert password["generation_method"] == "llm_grounded"
    assert (password["include_ok"], password["source_ok"], password["grounding_issues"]) == (
        True,
        True,
        [],
    )
    assert password["total_tokens"] == 550
    m = report["metrics"]
    assert m["grounding_compliance_generated"] == {"n": 1, "of": 1, "rate": 1.0}
    assert m["estimated_cost"]["total"] == pytest.approx((500 * 2 + 50 * 8) / 1e6)
    assert report["meta"]["llm"]["model"] == settings.openai_model


def test_a_misbehaving_llm_is_caught_and_reported(
    eval_file: Path, faq_file: Path, settings: Settings
) -> None:
    llm = scripted_llm("Reset it within 30 days for a $5 fee, guaranteed.")

    report = run(eval_file, faq_file, settings, mode="live", llm=llm)

    password = report["results"][0]
    # The grounding check rejected the invented claims: the approved answer was used.
    assert password["generation_method"] == "approved_faq"
    assert password["fallback_reason"] == "grounding_check_failed"
    assert password["forbidden_hits"] == []  # nothing invented reached the customer
    assert report["metrics"]["unsupported_claims"]["n"] == 0


def test_report_text_has_every_section(eval_file: Path, faq_file: Path, settings: Settings) -> None:
    text = format_report(run(eval_file, faq_file, settings))

    for heading in (
        "Grounded answer evaluation",
        "sample size: 3 cases",
        CAVEAT,
        "Outcome correctness (all cases)",
        "Fallback correctness",
        "Answer relevance",
        "Source correctness",
        "Grounding compliance",
        "Unsupported claims",
        "Latency",
        "Tokens (as reported by the API)",
        "Estimated cost: not configured",
        "By case type",
        "Failure examples",
    ):
        assert heading in text


def test_invalid_labels_stop_the_evaluation(
    tmp_path: Path, faq_file: Path, settings: Settings
) -> None:
    path = tmp_path / "bad.json"
    bad = case(acceptable_source_ids=["faq_105"], must_include=[]).model_dump(mode="json")
    path.write_text(json.dumps({"cases": [bad]}), encoding="utf-8")

    with pytest.raises(ValueError, match="faq_105 is inactive"):
        run(path, faq_file, settings)


def test_max_cases_limits_the_sample(eval_file: Path, faq_file: Path, settings: Settings) -> None:
    assert run(eval_file, faq_file, settings, max_cases=2)["meta"]["sample_size"] == 2


# --- Command line ---------------------------------------------------------------------------


def test_main_writes_the_report(
    eval_file: Path,
    faq_file: Path,
    tmp_path: Path,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    output = tmp_path / "out.json"

    code = main(
        [
            "--dataset",
            str(eval_file),
            "--faq-data",
            str(faq_file),
            "--output",
            str(output),
            "--offline",
        ],
        embedding_factory=fake_factory,
        settings=settings,
    )

    assert code == 0
    assert "Grounded answer evaluation" in capsys.readouterr().out
    assert json.loads(output.read_text(encoding="utf-8"))["meta"]["sample_size"] == 3
    assert os.environ["HF_HUB_OFFLINE"] == "1"


def test_live_mode_requires_explicit_confirmation(
    eval_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--dataset", str(eval_file), "--llm", "live"], embedding_factory=fake_factory)

    assert code == 2
    assert "PAID OpenAI request" in capsys.readouterr().err


def test_live_mode_without_a_key_fails_clearly(
    eval_file: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.core.config.ENV_FILE", Path("does-not-exist.env"))
    monkeypatch.setitem(Settings.model_config, "env_file", None)

    code = main(
        ["--dataset", str(eval_file), "--llm", "live", "--yes"], embedding_factory=fake_factory
    )

    assert code == 2
    assert "requires OPENAI_API_KEY" in capsys.readouterr().err


def test_invalid_max_cases_is_rejected(eval_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--dataset", str(eval_file), "--max-cases", "0"]) == 2
    assert "--max-cases" in capsys.readouterr().err


def test_outcome_enum_values() -> None:
    assert {o.value for o in Outcome} == {"answer", "clarification", "fallback"}
