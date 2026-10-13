"""The hybrid evaluation dataset and script (scripts/evaluate_hybrid_search.py).

Scoring logic is tested with hand-made inputs; the end-to-end runs use the fake encoder
and the fixed test FAQs, so no model is downloaded.
"""

import json
import os
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import PROJECT_ROOT, Settings
from app.database.faq_repository import FAQRepository
from app.services.embedding_service import EmbeddingService
from scripts.evaluate_hybrid_search import (
    DEFAULT_DATASET,
    Answer,
    AnswerOutcome,
    CaseResult,
    EvalCase,
    EvalDataset,
    ExpectedDecision,
    QueryType,
    Retrieval,
    baseline_answer,
    check_labels_against_faqs,
    evaluate,
    format_report,
    latency_stats,
    load_dataset,
    main,
    paired,
    score_answer,
    score_retrieval,
    sign_test_p,
    summarize,
)
from scripts.evaluate_semantic_search import DEFAULT_DATASET as STAGE3_DATASET
from tests.fakes import CountingLoader


def case(case_id: str = "eval_001", **overrides: Any) -> EvalCase:
    data = {
        "id": case_id,
        "type": "paraphrase",
        "query": f"query {case_id}",
        "expected_faq_ids": ["faq_101"],
        "expected_decision": "answered",
        "category": "orders",
        "explanation": "why",
    }
    return EvalCase.model_validate({**data, **overrides})


def ambiguous(case_id: str = "eval_002") -> EvalCase:
    return case(
        case_id,
        type="ambiguous",
        expected_faq_ids=["faq_101", "faq_102"],
        expected_decision="ambiguous",
    )


def out_of_scope(case_id: str = "eval_003") -> EvalCase:
    return case(
        case_id,
        type="out_of_scope",
        expected_faq_ids=[],
        expected_decision="no_match",
        category=None,
    )


def fake_factory(model_name: str) -> EmbeddingService:
    return EmbeddingService(model_name, loader=CountingLoader())


# --- The bundled dataset ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def dataset() -> EvalDataset:
    return load_dataset(DEFAULT_DATASET)


@pytest.mark.sample_data
def test_dataset_has_at_least_40_cases_covering_every_type(dataset: EvalDataset) -> None:
    types = Counter(c.type for c in dataset.cases)
    decisions = Counter(c.expected_decision for c in dataset.cases)

    assert len(dataset.cases) >= 40
    assert set(types) == set(QueryType)
    assert all(count >= 4 for count in types.values())
    assert decisions[ExpectedDecision.AMBIGUOUS] >= 5
    assert decisions[ExpectedDecision.NO_MATCH] >= 5


@pytest.mark.sample_data
def test_dataset_labels_reference_real_active_faqs_and_categories(dataset: EvalDataset) -> None:
    faqs = FAQRepository.from_json_file(PROJECT_ROOT / dataset.faq_data).list_all()

    assert check_labels_against_faqs(dataset, faqs) == []


@pytest.mark.sample_data
def test_stage3_cases_are_reused_unchanged(dataset: EvalDataset) -> None:
    stage3 = json.loads(STAGE3_DATASET.read_text(encoding="utf-8"))["cases"]
    current = {c.id: c for c in dataset.cases}

    for old in stage3:
        new = current[old["id"]]
        assert (new.type.value, new.query, new.expected_faq_ids) == (
            old["type"],
            old["query"],
            old["expected_faq_ids"],
        )


@pytest.mark.sample_data
def test_every_case_explains_the_expected_behaviour(dataset: EvalDataset) -> None:
    assert all(len(c.explanation) >= 10 for c in dataset.cases)


# --- Label validation ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_faq_ids": []},  # answered needs an FAQ
        {"expected_decision": "no_match"},  # no_match must have no FAQ
        {"type": "out_of_scope", "expected_faq_ids": [], "expected_decision": "answered"},
        {"type": "out_of_scope"},  # out-of-scope cases must expect no_match
        {"expected_decision": "ambiguous", "expected_faq_ids": ["faq_101", "faq_102"]},
        {"type": "ambiguous", "expected_faq_ids": ["faq_101"], "expected_decision": "ambiguous"},
        {"expected_faq_ids": ["faq_101", "faq_101"]},
        {"type": "category_specific"},  # needs a filter
        {"category_filter": "orders"},  # only category_specific cases filter
        {"category_filter": "billing", "type": "category_specific"},
        {"expected_decision": "retrieval_unavailable"},
        {"explanation": ""},
        {"id": "case_1"},
        {"unknown": 1},
    ],
)
def test_invalid_cases_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        case(**overrides)


def test_duplicate_ids_and_queries_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate case ids"):
        EvalDataset(cases=[case("eval_001"), case("eval_001", query="other")])
    with pytest.raises(ValidationError, match="duplicate query"):
        EvalDataset(cases=[case("eval_001", query="Q"), case("eval_002", query="q")])


def test_same_query_with_another_category_filter_is_allowed() -> None:
    filtered = case("eval_002", query="cancel", type="category_specific", category_filter="orders")

    assert len(EvalDataset(cases=[case("eval_001", query="cancel"), filtered]).cases) == 2


def test_labels_are_checked_against_the_faq_data(faq_repository: FAQRepository) -> None:
    data = EvalDataset(
        cases=[
            case("eval_001", expected_faq_ids=["faq_999"]),
            case("eval_002", expected_faq_ids=["faq_105"], category="shipping"),
            case("eval_003", expected_faq_ids=["faq_101"], category="shipping"),
            case(
                "eval_004",
                type="category_specific",
                category_filter="shipping",
                expected_faq_ids=["faq_101", "faq_103"],
                category=None,
            ),
            ambiguous("eval_005"),
            out_of_scope("eval_006"),
        ]
    )

    assert check_labels_against_faqs(data, faq_repository.list_all()) == [
        "eval_001: expected FAQ faq_999 does not exist",
        "eval_002: expected FAQ faq_105 is inactive",
        "eval_003: category should be orders",
        "eval_004: an expected FAQ is outside category_filter",
    ]


# --- Scoring ------------------------------------------------------------------------------------


def test_score_retrieval() -> None:
    hit = score_retrieval(ambiguous(), [("faq_103", 0.9), ("faq_102", 0.8)], 5.0)
    empty = score_retrieval(case(), [], 1.0)

    assert (hit.top1_correct, hit.in_top_k) == (False, True)
    assert (empty.top1_correct, empty.in_top_k) == (False, False)


@pytest.mark.parametrize(
    ("label", "decision", "faq_id", "outcome", "correct"),
    [
        (case(), "answered", "faq_101", AnswerOutcome.CORRECT_ANSWER, True),
        (case(), "answered", "faq_102", AnswerOutcome.WRONG_ANSWER, False),
        (case(), "ambiguous", None, AnswerOutcome.CLARIFIED, False),
        (case(), "no_match", None, AnswerOutcome.DECLINED, False),
        (ambiguous(), "ambiguous", None, AnswerOutcome.CLARIFIED, True),
        (ambiguous(), "answered", "faq_102", AnswerOutcome.CORRECT_ANSWER, False),
        (out_of_scope(), "no_match", None, AnswerOutcome.DECLINED, True),
        (out_of_scope(), "retrieval_unavailable", None, AnswerOutcome.DECLINED, True),
        (out_of_scope(), "answered", "faq_101", AnswerOutcome.OUT_OF_SCOPE_ANSWERED, False),
        (out_of_scope(), "ambiguous", None, AnswerOutcome.CLARIFIED, False),
    ],
)
def test_score_answer(
    label: EvalCase, decision: str, faq_id: str | None, outcome: AnswerOutcome, correct: bool
) -> None:
    answer = score_answer(label, decision, faq_id, 1.0)

    assert (answer.outcome, answer.decision_correct) == (outcome, correct)


def test_baseline_answers_top1_or_declines() -> None:
    ranked = Retrieval([("faq_101", 0.45)], 2.0, True, True)

    assert baseline_answer(case(), ranked).outcome is AnswerOutcome.CORRECT_ANSWER
    assert baseline_answer(case(), ranked, 0.45).outcome is AnswerOutcome.CORRECT_ANSWER
    assert baseline_answer(case(), ranked, 0.46).outcome is AnswerOutcome.DECLINED
    assert baseline_answer(case(), Retrieval([], 2.0, False, False)).decision == "no_match"
    assert baseline_answer(case(), ranked).latency_ms == 2.0


@pytest.mark.parametrize(
    ("wins", "losses", "p"),
    [(0, 0, 1.0), (3, 3, 1.0), (5, 0, 0.0625), (0, 5, 0.0625), (6, 0, 0.03125), (14, 2, 0.0042)],
)
def test_sign_test(wins: int, losses: int, p: float) -> None:
    assert sign_test_p(wins, losses) == pytest.approx(p, abs=1e-4)


def test_paired_counts_discordant_cases() -> None:
    result = paired([True, True, False, False, True], [True, False, True, False, False])

    assert (result["wins"], result["losses"]) == (2, 1)


def test_latency_stats() -> None:
    assert latency_stats([]) == {"mean": None, "median": None, "p95": None, "max": None}
    stats = latency_stats([float(i) for i in range(1, 21)])
    assert (stats["mean"], stats["median"], stats["p95"], stats["max"]) == (10.5, 10.5, 19, 20)


def _result(label: EvalCase, top1: dict[str, bool], answers: dict[str, Answer]) -> CaseResult:
    return CaseResult(
        case_id=label.id,
        type=label.type.value,
        query=label.query,
        category_filter=None,
        expected_faq_ids=label.expected_faq_ids,
        expected_decision=label.expected_decision.value,
        retrieval={m: Retrieval([("faq_x", 1.0)], 1.0, ok, ok) for m, ok in top1.items()},
        answers=answers,
    )


def test_summarize_uses_fixed_denominators() -> None:
    labels = [case("eval_001"), case("eval_002"), ambiguous("eval_003"), out_of_scope("eval_004")]
    answered = {
        "eval_001": ("answered", "faq_101"),
        "eval_002": ("ambiguous", None),
        "eval_003": ("ambiguous", None),
        "eval_004": ("answered", "faq_101"),
    }
    results = []
    for label in labels:
        decision, faq_id = answered[label.id]
        options = ["faq_102"] if decision == "ambiguous" else []
        answer = score_answer(label, decision, faq_id, 3.0, options)
        declined = score_answer(label, "no_match", None, 1.0)
        top1 = {"keyword": False, "semantic": label.id == "eval_001", "hybrid": True}
        results.append(
            _result(
                label,
                top1,
                {"keyword_answer": declined, "semantic_answer": declined, "decision": answer},
            )
        )

    metrics = summarize(results, 3)

    assert metrics["subsets"] == {
        "in_scope": 3,
        "expected_answered": 2,
        "expected_ambiguous": 1,
        "out_of_scope": 1,
    }
    retrieval = metrics["retrieval"]
    assert retrieval["hybrid"]["top1_accuracy"] == {"n": 3, "of": 3, "rate": 1.0}
    assert retrieval["semantic"]["top1_accuracy"]["n"] == 1
    assert retrieval["keyword"]["recall_at_3"] == {"n": 0, "of": 3, "rate": 0.0}

    decision = metrics["answers"]["decision"]
    assert decision["correct_answer_rate"] == {"n": 1, "of": 2, "rate": 0.5}
    assert decision["out_of_scope_rejection_rate"] == {"n": 0, "of": 1, "rate": 0.0}
    assert decision["false_answer_rate"] == {"n": 1, "of": 4, "rate": 0.25}
    assert decision["answer_precision"] == {"n": 1, "of": 2, "rate": 0.5}
    assert decision["decision_accuracy"] == {"n": 2, "of": 4, "rate": 0.5}
    assert decision["ambiguity"]["true_positives"] == 1
    assert decision["ambiguity"]["precision"] == 0.5
    assert decision["ambiguity"]["recall"] == 1.0
    assert decision["ambiguity"]["f1"] == pytest.approx(2 / 3)
    assert decision["ambiguity"]["clarifications_offering_an_acceptable_faq"] == 1
    assert decision["confusion"]["answered"]["ambiguous"] == 1
    assert decision["latency_ms"]["mean"] == 3.0

    keyword = metrics["answers"]["keyword_answer"]
    assert keyword["out_of_scope_rejection_rate"]["rate"] == 1.0
    assert keyword["answer_precision"]["rate"] is None  # no answers given
    assert keyword["ambiguity"]["precision"] is None

    comparison = metrics["comparisons"]["retrieval_top1"]["hybrid_vs_semantic"]
    assert (comparison["wins"], comparison["losses"]) == (2, 0)
    assert metrics["by_type"]["paraphrase"]["hybrid_top1"] == 2
    assert metrics["by_type"]["out_of_scope"]["decision_false_answers"] == 1


# --- End to end (fake model, test FAQs) -------------------------------------------------------


@pytest.fixture
def eval_file(tmp_path: Path) -> Path:
    rows = [
        ("exact", "How do I reset my password?", None, ["faq_107"], "answered", "account"),
        ("short", "track order", None, ["faq_101"], "answered", "orders"),
        ("category_specific", "delivery", "shipping", ["faq_103"], "answered", "shipping"),
        ("ambiguous", "order", None, ["faq_101", "faq_102"], "ambiguous", "orders"),
        ("out_of_scope", "xyzzy quantum", None, [], "no_match", None),
    ]
    cases = [
        {
            "id": f"eval_{number:03d}",
            "type": kind,
            "query": query,
            "category_filter": category_filter,
            "expected_faq_ids": ids,
            "expected_decision": decision,
            "category": category,
            "explanation": "test case",
        }
        for number, (kind, query, category_filter, ids, decision, category) in enumerate(rows, 1)
    ]
    path = tmp_path / "eval.json"
    path.write_text(json.dumps({"faq_data": "unused", "cases": cases}), encoding="utf-8")
    return path


def run(eval_file: Path, faq_file: Path, settings: Settings, k: int = 3) -> dict[str, Any]:
    return evaluate(eval_file, faq_file, settings, k, fake_factory)


def test_evaluate_produces_a_complete_report(
    eval_file: Path, faq_file: Path, settings: Settings
) -> None:
    report = run(eval_file, faq_file, settings)

    assert report["meta"]["active_faqs"] == 7
    assert report["meta"]["settings"]["answer_min_similarity"] == 0.4
    assert report["metrics"]["cases"] == 5
    first = report["results"][0]
    assert set(first["retrieval"]) == {"keyword", "semantic", "hybrid"}
    assert set(first["answers"]) == {"keyword_answer", "semantic_answer", "decision"}
    assert first["answers"]["decision"]["outcome"] == "correct_answer"  # exact question
    for method in ("keyword", "semantic", "hybrid"):
        assert len(report["results"][1]["retrieval"][method]["ranked"]) <= 3
    json.dumps(report)  # fully serialisable


def test_category_filter_is_applied_to_every_method(
    eval_file: Path, faq_file: Path, settings: Settings
) -> None:
    filtered = run(eval_file, faq_file, settings)["results"][2]
    shipping = {"faq_103", "faq_104"}

    for method in ("keyword", "semantic", "hybrid"):
        assert {faq_id for faq_id, _ in filtered["retrieval"][method]["ranked"]} <= shipping


def test_decision_settings_change_answers_not_rankings(
    eval_file: Path, faq_file: Path, make_settings: Callable[..., Settings]
) -> None:
    loose = run(eval_file, faq_file, make_settings(answer_min_similarity=-1.0))
    strict = run(eval_file, faq_file, make_settings(answer_min_similarity=1.0))

    def rankings(report: dict[str, Any]) -> list[Any]:
        return [{m: r["retrieval"][m]["ranked"] for m in r["retrieval"]} for r in report["results"]]

    assert rankings(loose) == rankings(strict)
    assert (
        strict["metrics"]["answers"]["semantic_answer"]["out_of_scope_rejection_rate"]["rate"]
        == 1.0
    )
    assert (
        loose["metrics"]["answers"]["semantic_answer"]["out_of_scope_rejection_rate"]["rate"] == 0.0
    )


def test_results_are_reproducible(eval_file: Path, faq_file: Path, settings: Settings) -> None:
    def comparable(report: dict[str, Any]) -> str:
        text = json.dumps({"metrics": report["metrics"], "results": report["results"]})
        data = json.loads(text)
        for row in data["results"]:
            for part in ("retrieval", "answers"):
                for value in row[part].values():
                    value.pop("latency_ms")
        for group in ("retrieval", "answers"):
            for value in data["metrics"][group].values():
                value.pop("latency_ms")
        return json.dumps(data, sort_keys=True)

    assert comparable(run(eval_file, faq_file, settings)) == comparable(
        run(eval_file, faq_file, settings)
    )


def test_report_text_has_every_section(eval_file: Path, faq_file: Path, settings: Settings) -> None:
    text = format_report(run(eval_file, faq_file, settings))

    for heading in (
        "1. Retrieval quality",
        "2. Answer quality",
        "Out-of-scope rejection",
        "False-answer rate",
        "3. Ambiguity detection",
        "4. Average latency",
        "5. Paired comparisons",
        "6. By question type",
        "7. Failure examples",
        "keyword retrieval: wrong FAQ ranked first",
        "Decision: false answers",
        "Decision: ambiguity missed",
    ):
        assert heading in text


def test_invalid_labels_stop_the_evaluation(
    tmp_path: Path, faq_file: Path, settings: Settings
) -> None:
    path = tmp_path / "bad.json"
    bad = case(expected_faq_ids=["faq_105"], category="shipping").model_dump(mode="json")
    path.write_text(json.dumps({"cases": [bad]}), encoding="utf-8")

    with pytest.raises(ValueError, match="faq_105 is inactive"):
        run(path, faq_file, settings)


@pytest.mark.parametrize("k", [0, 21])
def test_invalid_k_is_rejected(eval_file: Path, faq_file: Path, settings: Settings, k: int) -> None:
    with pytest.raises(ValueError, match="k must be between 1 and 20"):
        run(eval_file, faq_file, settings, k)


def test_main_prints_report_and_writes_json(
    eval_file: Path,
    faq_file: Path,
    tmp_path: Path,
    settings: Settings,
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
            "-k",
            "2",
            "--output",
            str(output),
            "--offline",
        ],
        embedding_factory=fake_factory,
        settings=settings,
    )

    assert exit_code == 0
    assert "Hybrid search evaluation" in capsys.readouterr().out
    assert json.loads(output.read_text(encoding="utf-8"))["meta"]["k"] == 2
    assert os.environ["HF_HUB_OFFLINE"] == "1"
