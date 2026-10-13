"""The semantic search evaluation dataset and script (scripts/evaluate_semantic_search.py).

Scoring logic is tested with hand-made rankings; the end-to-end runs use the fake
encoder and the fixed test FAQs, so no model is downloaded.
"""

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import PROJECT_ROOT
from app.database.faq_repository import FAQRepository
from app.services.embedding_service import EmbeddingService
from scripts.evaluate_semantic_search import (
    DEFAULT_DATASET,
    EvalCase,
    EvalDataset,
    Outcome,
    QueryType,
    check_labels_against_faqs,
    classify,
    evaluate,
    format_report,
    load_dataset,
    main,
    summarize,
)
from tests.fakes import CountingLoader


def case(case_id: str = "eval_001", **overrides: Any) -> EvalCase:
    data = {"id": case_id, "type": "paraphrase", "query": "q", "expected_faq_ids": ["faq_001"]}
    return EvalCase.model_validate({**data, **overrides})


def out_of_scope(case_id: str = "eval_900") -> EvalCase:
    return case(case_id, type="out_of_scope", expected_faq_ids=[])


def fake_factory(model_name: str) -> EmbeddingService:
    return EmbeddingService(model_name, loader=CountingLoader())


# --- The bundled dataset ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def dataset() -> EvalDataset:
    return load_dataset(DEFAULT_DATASET)


@pytest.mark.sample_data
def test_dataset_has_at_least_25_cases_covering_every_type(dataset: EvalDataset) -> None:
    types = Counter(c.type for c in dataset.cases)

    assert len(dataset.cases) >= 25
    assert set(types) == set(QueryType)
    assert types[QueryType.OUT_OF_SCOPE] >= 5


@pytest.mark.sample_data
def test_dataset_labels_reference_real_active_faqs(dataset: EvalDataset) -> None:
    faqs = FAQRepository.from_json_file(PROJECT_ROOT / dataset.faq_data).list_all()

    assert check_labels_against_faqs(dataset, faqs) == []


@pytest.mark.sample_data
def test_dataset_covers_every_active_category(dataset: EvalDataset) -> None:
    repository = FAQRepository.from_json_file(PROJECT_ROOT / dataset.faq_data)
    expected_categories = {
        repository.get_by_id(faq_id).category  # type: ignore[union-attr]
        for c in dataset.cases
        for faq_id in c.expected_faq_ids
    }

    assert expected_categories == {faq.category for faq in repository.list_active()}


@pytest.mark.sample_data
def test_dataset_queries_are_unique(dataset: EvalDataset) -> None:
    queries = [c.query.lower() for c in dataset.cases]

    assert len(queries) == len(set(queries))


# --- Dataset validation -----------------------------------------------------------------------


def test_in_scope_case_requires_expected_ids() -> None:
    with pytest.raises(ValidationError, match="needs at least one expected FAQ id"):
        case(expected_faq_ids=[])


def test_out_of_scope_case_must_not_list_expected_ids() -> None:
    with pytest.raises(ValidationError, match="must not list expected FAQ ids"):
        case(type="out_of_scope", expected_faq_ids=["faq_001"])


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_faq_ids": ["faq_001", "faq_001"]},
        {"id": "case-1"},
        {"type": "unknown_type"},
        {"query": ""},
        {"answer": "made up"},
    ],
)
def test_invalid_cases_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        case(**overrides)


def test_duplicate_case_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate case ids: eval_001"):
        EvalDataset(cases=[case(), case()])


def test_labels_are_checked_against_the_faq_data(faq_repository: FAQRepository) -> None:
    dataset = EvalDataset(
        cases=[
            case("eval_001", expected_faq_ids=["faq_101"]),  # active: fine
            case("eval_002", expected_faq_ids=["faq_105"]),  # inactive
            case("eval_003", expected_faq_ids=["faq_999"]),  # does not exist
        ]
    )

    assert check_labels_against_faqs(dataset, faq_repository.list_all()) == [
        "eval_002: expected FAQ faq_105 is inactive",
        "eval_003: expected FAQ faq_999 does not exist",
    ]


# --- Classification ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("evaluation_case", "ranked", "outcome", "top1", "in_top_k"),
    [
        (case(), [("faq_001", 0.8), ("faq_002", 0.3)], Outcome.CORRECT, True, True),
        (case(), [("faq_001", 0.3), ("faq_002", 0.2)], Outcome.THRESHOLD_REJECTED, True, True),
        (case(), [("faq_002", 0.7), ("faq_001", 0.6)], Outcome.WRONG_ANSWER_SHOWN, False, True),
        (case(), [("faq_002", 0.3), ("faq_003", 0.2)], Outcome.WRONG_ANSWER_HIDDEN, False, False),
        (out_of_scope(), [("faq_002", 0.2)], Outcome.CORRECTLY_REJECTED, False, False),
        (out_of_scope(), [("faq_002", 0.5)], Outcome.OUT_OF_SCOPE_ANSWERED, False, False),
    ],
)
def test_classify_outcomes(
    evaluation_case: EvalCase,
    ranked: list[tuple[str, float]],
    outcome: Outcome,
    top1: bool,
    in_top_k: bool,
) -> None:
    result = classify(evaluation_case, ranked, threshold=0.4, latency_ms=5.0)

    assert (result.outcome, result.top1_correct, result.in_top_k) == (outcome, top1, in_top_k)


def test_any_acceptable_id_counts_for_ambiguous_cases() -> None:
    ambiguous = case(type="ambiguous", expected_faq_ids=["faq_001", "faq_006", "faq_010"])

    result = classify(ambiguous, [("faq_010", 0.6), ("faq_001", 0.5)], 0.4, 1.0)

    assert result.outcome is Outcome.CORRECT


def test_score_equal_to_threshold_counts_as_shown() -> None:
    assert classify(case(), [("faq_001", 0.4)], 0.4, 1.0).outcome is Outcome.CORRECT


def test_empty_ranking_is_a_hidden_retrieval_failure() -> None:
    result = classify(case(), [], 0.4, 1.0)

    assert result.outcome is Outcome.WRONG_ANSWER_HIDDEN
    assert result.top_score is None


# --- Metrics ----------------------------------------------------------------------------------


def test_summarize_metrics() -> None:
    results = [
        classify(case("eval_001"), [("faq_001", 0.9)], 0.4, 10.0),  # correct
        classify(case("eval_002"), [("faq_001", 0.3)], 0.4, 20.0),  # threshold rejected
        classify(case("eval_003"), [("faq_002", 0.6), ("faq_001", 0.5)], 0.4, 30.0),  # top-3
        classify(case("eval_004"), [("faq_002", 0.6), ("faq_003", 0.5)], 0.4, 40.0),  # miss
        classify(out_of_scope("eval_005"), [("faq_002", 0.1)], 0.4, 50.0),  # rejected
        classify(out_of_scope("eval_006"), [("faq_002", 0.7)], 0.4, 60.0),  # answered
    ]

    metrics = summarize(results, k=3)

    assert (metrics["in_scope_cases"], metrics["out_of_scope_cases"]) == (4, 2)
    assert metrics["top1_accuracy"] == 2 / 4
    assert metrics["recall_at_3"] == 3 / 4
    assert metrics["answered_correctly_rate"] == 1 / 4
    assert metrics["no_match_rejection_rate"] == 1 / 2
    assert metrics["outcomes"] == {
        "correct": 1,
        "threshold_rejected": 1,
        "wrong_answer_shown": 2,
        "wrong_answer_hidden": 0,
        "correctly_rejected": 1,
        "out_of_scope_answered": 1,
    }
    assert metrics["latency_ms"]["mean"] == 35.0
    assert metrics["latency_ms"]["max"] == 60.0
    assert metrics["by_type"]["paraphrase"] == {
        "cases": 4,
        "top1_correct": 2,
        "in_top_3": 3,
        "answered_correctly": 1,
    }
    assert metrics["by_type"]["out_of_scope"] == {"cases": 2, "rejected": 1}


def test_rates_are_none_without_cases() -> None:
    metrics = summarize([classify(case(), [("faq_001", 0.9)], 0.4, 1.0)], k=3)

    assert metrics["no_match_rejection_rate"] is None
    assert summarize([], k=3)["top1_accuracy"] is None


# --- End to end (fake model, test FAQs) -------------------------------------------------------


@pytest.fixture
def eval_file(tmp_path: Path) -> Path:
    data = {
        "faq_data": "unused",
        "cases": [
            {
                "id": "eval_001",
                "type": "exact",
                "query": "How do I reset my password?",
                "expected_faq_ids": ["faq_107"],
            },
            {
                "id": "eval_002",
                "type": "short",
                "query": "track order",
                "expected_faq_ids": ["faq_101"],
            },
            {
                "id": "eval_003",
                "type": "ambiguous",
                "query": "shipping",
                "expected_faq_ids": ["faq_103", "faq_104"],
            },
            {
                "id": "eval_004",
                "type": "out_of_scope",
                "query": "xyzzy quantum",
                "expected_faq_ids": [],
            },
        ],
    }
    path = tmp_path / "eval.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def run(eval_file: Path, faq_file: Path, threshold: float = 0.4) -> dict[str, Any]:
    return evaluate(eval_file, faq_file, "fake/model", threshold, 3, fake_factory)


def test_evaluate_produces_a_complete_report(eval_file: Path, faq_file: Path) -> None:
    report = run(eval_file, faq_file)

    assert report["meta"]["model"] == "fake/model"
    assert report["meta"]["threshold"] == 0.4
    assert report["meta"]["active_faqs"] == 7
    assert len(report["meta"]["dataset_sha256"]) == 64
    assert report["metrics"]["cases"] == 4
    assert len(report["results"]) == 4
    first = report["results"][0]
    assert first["case_id"] == "eval_001"
    assert first["ranked"][0][0] == "faq_107"
    assert first["outcome"] == "correct"
    json.dumps(report)  # fully serialisable


def test_results_are_reproducible(eval_file: Path, faq_file: Path) -> None:
    def comparable(report: dict[str, Any]) -> Any:
        rows = [{k: v for k, v in r.items() if k != "latency_ms"} for r in report["results"]]
        metrics = {k: v for k, v in report["metrics"].items() if k != "latency_ms"}
        return rows, metrics

    assert comparable(run(eval_file, faq_file)) == comparable(run(eval_file, faq_file))


def test_threshold_changes_outcomes_not_rankings(eval_file: Path, faq_file: Path) -> None:
    loose, strict = run(eval_file, faq_file, threshold=-1.0), run(eval_file, faq_file, 0.99)

    assert [r["ranked"] for r in loose["results"]] == [r["ranked"] for r in strict["results"]]
    assert loose["metrics"]["top1_accuracy"] == strict["metrics"]["top1_accuracy"]
    for report in (loose, strict):
        outcomes = report["metrics"]["outcomes"]
        top1_hits = sum(r["top1_correct"] for r in report["results"])
        # Every correct top-1 is either answered or rejected by the threshold, never lost.
        assert outcomes["correct"] + outcomes["threshold_rejected"] == top1_hits
    assert strict["metrics"]["outcomes"]["correct"] <= loose["metrics"]["outcomes"]["correct"]
    assert loose["metrics"]["outcomes"]["threshold_rejected"] == 0  # -1 rejects nothing


def test_report_text_separates_failure_kinds(eval_file: Path, faq_file: Path) -> None:
    text = format_report(run(eval_file, faq_file, threshold=0.99))

    for heading in (
        "Top-1 accuracy:",
        "Recall@3:",
        "No-match rejection rate:",
        "Latency per query",
        "Retrieval failures: wrong FAQ ranked first",
        "Threshold rejections: right FAQ first, but below threshold",
        "Out-of-scope questions that were answered",
    ):
        assert heading in text


def test_invalid_labels_stop_the_evaluation(tmp_path: Path, faq_file: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "eval_001",
                        "type": "exact",
                        "query": "q",
                        "expected_faq_ids": ["faq_105"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="faq_105 is inactive"):
        run(path, faq_file)


@pytest.mark.parametrize(("threshold", "k"), [(1.5, 3), (-2.0, 3), (0.4, 0)])
def test_invalid_parameters_are_rejected(
    eval_file: Path, faq_file: Path, threshold: float, k: int
) -> None:
    with pytest.raises(ValueError):
        evaluate(eval_file, faq_file, "fake/model", threshold, k, fake_factory)


def test_main_prints_report_and_writes_json(
    eval_file: Path,
    faq_file: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    output = tmp_path / "results.json"

    exit_code = main(
        [
            "--dataset",
            str(eval_file),
            "--faq-data",
            str(faq_file),
            "--model",
            "fake/model",
            "--threshold",
            "0.5",
            "--output",
            str(output),
            "--offline",
        ],
        embedding_factory=fake_factory,
    )

    assert exit_code == 0
    assert "Semantic search evaluation" in capsys.readouterr().out
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["meta"]["threshold"] == 0.5
    assert os.environ["HF_HUB_OFFLINE"] == "1"
