"""Compare keyword, semantic and hybrid retrieval, and evaluate the answer decision.

Usage (from the project root, virtual environment active):

    python scripts/evaluate_hybrid_search.py
    python scripts/evaluate_hybrid_search.py --offline --output hybrid_eval_results.json

Decision settings (ANSWER_MIN_SIMILARITY, ANSWER_AMBIGUITY_MARGIN, ...) come from the
environment / .env exactly as the API reads them, so set those variables to evaluate
other values.

Two separate questions
----------------------
1. Retrieval quality: does a method RANK an acceptable FAQ first (top-1) or in the top k
   (Recall@k)? Measured for keyword-only, semantic-only and hybrid (RRF) retrieval on the
   same in-scope cases (expected_faq_ids not empty), so all three share one denominator.
   Retrieval never decides whether to answer.
2. Answer quality: what would the customer get? Measured for three answer policies:
   - keyword_answer:  answer the keyword top-1 if keyword search finds anything
                      (the Stage 2 behaviour);
   - semantic_answer: answer the semantic top-1 if its similarity reaches
                      ANSWER_MIN_SIMILARITY (the Stage 3 behaviour, same threshold as the
                      decision layer);
   - decision:        the real answer decision layer (hybrid retrieval + acceptance +
                      ambiguity detection), as used by POST /api/v1/hybrid-search.
   Hybrid retrieval alone has no answer policy: RRF scores are rank-based and must not
   be thresholded, so rejecting out-of-scope questions is the decision layer's job.

Metric denominators (fixed for every method or policy)
------------------------------------------------------
- Top-1 accuracy, Recall@k:        in-scope cases (expected answered or ambiguous).
- Correct-answer rate:             cases expected to be answered.
- Out-of-scope rejection rate:     out-of-scope cases (expected no_match); only a
                                   no_match decision counts as a rejection.
- False-answer rate:               ALL cases; a false answer is an FAQ answer that is not
                                   acceptable (wrong FAQ, or any answer when out of scope).
- Answer precision:                answers given by that policy (varies; reported with it).
- Decision accuracy:               ALL cases; the decision equals expected_decision (and,
                                   when answered, the FAQ is acceptable).
- Ambiguity detection:             expected ambiguous = positive, decision ambiguous =
                                   predicted positive; precision, recall and F1.
- Latency:                         per call after warm-up, for each retrieval method and
                                   for the full decision (which includes hybrid retrieval).

Paired comparisons count the cases where one method is right and the other wrong, with an
exact two-sided sign test. On a set this small, differences of a few cases are usually not
statistically significant; the report says so instead of declaring a winner.
"""

import argparse
import json
import math
import os
import statistics
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/evaluate_hybrid_search.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import BaseModel, ConfigDict, Field, model_validator  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.database.faq_repository import FAQRepository, resolve_data_path  # noqa: E402
from app.models.faq import FAQ, Category  # noqa: E402
from app.services.answer_decision_service import Decision, RetrievalMode  # noqa: E402
from app.services.embedding_service import EmbeddingService  # noqa: E402
from app.services.faq_service import FAQService  # noqa: E402
from app.services.search_pipeline import SearchServices, build_search_services  # noqa: E402
from app.services.semantic_search_service import (  # noqa: E402
    SemanticSearchService,
    meets_threshold,
)
from scripts.evaluate_semantic_search import environment_info, file_sha256  # noqa: E402

DEFAULT_DATASET = PROJECT_ROOT / "data" / "hybrid_search_eval.json"
DEFAULT_K = 3
MAX_FAILURE_EXAMPLES = 8
SIGNIFICANCE_LEVEL = 0.05


# --- Dataset ----------------------------------------------------------------------------------


class QueryType(StrEnum):
    EXACT = "exact"
    PARAPHRASE = "paraphrase"
    SYNONYM = "synonym"
    KEYWORD_HEAVY = "keyword_heavy"
    SHORT = "short"
    LONG = "long"
    TYPO = "typo"
    AMBIGUOUS = "ambiguous"
    MISLEADING_OVERLAP = "misleading_overlap"
    CATEGORY_SPECIFIC = "category_specific"
    OUT_OF_SCOPE = "out_of_scope"


class ExpectedDecision(StrEnum):
    ANSWERED = Decision.ANSWERED.value
    AMBIGUOUS = Decision.AMBIGUOUS.value
    NO_MATCH = Decision.NO_MATCH.value


class EvalCase(BaseModel):
    """One labelled question. ``expected_faq_ids`` lists every acceptable FAQ."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^eval_\d{3}$")
    type: QueryType
    query: str = Field(min_length=1)
    category_filter: Category | None = None
    expected_faq_ids: list[str] = Field(default_factory=list)
    expected_decision: ExpectedDecision
    category: Category | None = None
    explanation: str = Field(min_length=1)

    @property
    def in_scope(self) -> bool:
        return bool(self.expected_faq_ids)

    @model_validator(mode="after")
    def _labels_are_consistent(self) -> Self:
        ids, decision = self.expected_faq_ids, self.expected_decision
        if len(set(ids)) != len(ids):
            raise ValueError(f"{self.id}: expected_faq_ids contains duplicates")
        if (decision is ExpectedDecision.NO_MATCH) != (not ids):
            raise ValueError(f"{self.id}: no_match cases, and only they, have no expected FAQ")
        if (self.type is QueryType.OUT_OF_SCOPE) != (decision is ExpectedDecision.NO_MATCH):
            raise ValueError(f"{self.id}: out_of_scope cases, and only they, expect no_match")
        if (self.type is QueryType.AMBIGUOUS) != (decision is ExpectedDecision.AMBIGUOUS):
            raise ValueError(f"{self.id}: ambiguous cases, and only they, expect ambiguous")
        if decision is ExpectedDecision.AMBIGUOUS and len(ids) < 2:
            raise ValueError(f"{self.id}: an ambiguous case needs at least two expected FAQs")
        if (self.type is QueryType.CATEGORY_SPECIFIC) != (self.category_filter is not None):
            raise ValueError(f"{self.id}: category_specific cases, and only they, set a filter")
        return self


class EvalDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = ""
    faq_data: str = "data/faqs.json"
    cases: list[EvalCase] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_cases(self) -> Self:
        ids = [case.id for case in self.cases]
        if duplicates := sorted({i for i in ids if ids.count(i) > 1}):
            raise ValueError(f"duplicate case ids: {', '.join(duplicates)}")
        keys = [(case.query.casefold(), case.category_filter) for case in self.cases]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate query (with the same category filter)")
        return self


def load_dataset(path: Path) -> EvalDataset:
    return EvalDataset.model_validate_json(path.read_text(encoding="utf-8"))


def check_labels_against_faqs(dataset: EvalDataset, faqs: Iterable[FAQ]) -> list[str]:
    """Label problems: unknown or inactive FAQs, and categories that do not match them."""
    by_id = {faq.id: faq for faq in faqs}
    problems = []
    for case in dataset.cases:
        known = []
        for faq_id in case.expected_faq_ids:
            if faq_id not in by_id:
                problems.append(f"{case.id}: expected FAQ {faq_id} does not exist")
            elif not by_id[faq_id].is_active:
                problems.append(f"{case.id}: expected FAQ {faq_id} is inactive")
            else:
                known.append(by_id[faq_id])
        if len(known) != len(case.expected_faq_ids):
            continue
        categories = {faq.category for faq in known}
        expected_category = categories.pop() if len(categories) == 1 else None
        if case.category != expected_category:
            problems.append(
                f"{case.id}: category should be "
                f"{expected_category.value if expected_category else None}"
            )
        if case.category_filter is not None and any(
            faq.category is not case.category_filter for faq in known
        ):
            problems.append(f"{case.id}: an expected FAQ is outside category_filter")
    return problems


# --- Per-case results ----------------------------------------------------------------------


class Method(StrEnum):
    KEYWORD = "keyword"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


class Policy(StrEnum):
    KEYWORD_ANSWER = "keyword_answer"
    SEMANTIC_ANSWER = "semantic_answer"
    DECISION = "decision"


class AnswerOutcome(StrEnum):
    CORRECT_ANSWER = "correct_answer"  # answered with an acceptable FAQ
    WRONG_ANSWER = "wrong_answer"  # in scope, answered with an unacceptable FAQ
    OUT_OF_SCOPE_ANSWERED = "out_of_scope_answered"  # out of scope, but an FAQ was answered
    CLARIFIED = "clarified"  # asked a clarifying question
    DECLINED = "declined"  # no answer (no_match / retrieval_unavailable)


FALSE_ANSWERS = frozenset({AnswerOutcome.WRONG_ANSWER, AnswerOutcome.OUT_OF_SCOPE_ANSWERED})


@dataclass(frozen=True)
class Retrieval:
    """A method's top-k ranking for one case. Scores are in that method's own units."""

    ranked: list[tuple[str, float]]
    latency_ms: float
    top1_correct: bool
    in_top_k: bool


@dataclass(frozen=True)
class Answer:
    """What one answer policy would have shown for one case."""

    decision: str
    faq_id: str | None
    options: list[str]  # FAQs offered in a clarifying question
    outcome: AnswerOutcome
    decision_correct: bool
    latency_ms: float


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    type: str
    query: str
    category_filter: str | None
    expected_faq_ids: list[str]
    expected_decision: str
    retrieval: dict[str, Retrieval] = field(default_factory=dict)
    answers: dict[str, Answer] = field(default_factory=dict)


def score_retrieval(
    case: EvalCase, ranked: Sequence[tuple[str, float]], latency_ms: float
) -> Retrieval:
    acceptable = set(case.expected_faq_ids)
    return Retrieval(
        ranked=[(faq_id, float(score)) for faq_id, score in ranked],
        latency_ms=latency_ms,
        top1_correct=bool(ranked) and ranked[0][0] in acceptable,
        in_top_k=any(faq_id in acceptable for faq_id, _ in ranked),
    )


def score_answer(
    case: EvalCase,
    decision: str,
    faq_id: str | None,
    latency_ms: float,
    options: Sequence[str] = (),
) -> Answer:
    """Classify what a policy showed, and whether its decision matches the label."""
    acceptable = set(case.expected_faq_ids)
    if decision == Decision.ANSWERED:
        if faq_id in acceptable:
            outcome = AnswerOutcome.CORRECT_ANSWER
        elif case.in_scope:
            outcome = AnswerOutcome.WRONG_ANSWER
        else:
            outcome = AnswerOutcome.OUT_OF_SCOPE_ANSWERED
    elif decision == Decision.AMBIGUOUS:
        outcome = AnswerOutcome.CLARIFIED
    else:
        outcome = AnswerOutcome.DECLINED

    expected_outcome = {
        ExpectedDecision.ANSWERED: AnswerOutcome.CORRECT_ANSWER,
        ExpectedDecision.AMBIGUOUS: AnswerOutcome.CLARIFIED,
        ExpectedDecision.NO_MATCH: AnswerOutcome.DECLINED,
    }[case.expected_decision]
    return Answer(
        decision=str(decision),
        faq_id=faq_id,
        options=list(options),
        outcome=outcome,
        decision_correct=outcome is expected_outcome,
        latency_ms=latency_ms,
    )


def baseline_answer(
    case: EvalCase, retrieval: Retrieval, min_similarity: float | None = None
) -> Answer:
    """keyword_answer (min_similarity None) or semantic_answer: answer the top-1 or decline."""
    top = retrieval.ranked[0] if retrieval.ranked else None
    answers = top is not None and (
        min_similarity is None or meets_threshold(top[1], min_similarity)
    )
    if answers:
        return score_answer(case, Decision.ANSWERED, top[0], retrieval.latency_ms)  # type: ignore[index]
    return score_answer(case, Decision.NO_MATCH, None, retrieval.latency_ms)


# --- Metrics (pure functions) ---------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _fraction(numerator: int, denominator: int) -> dict[str, Any]:
    return {"n": numerator, "of": denominator, "rate": _rate(numerator, denominator)}


def latency_stats(values: Sequence[float]) -> dict[str, float | None]:
    ordered = sorted(values)
    if not ordered:
        return {"mean": None, "median": None, "p95": None, "max": None}
    return {
        "mean": statistics.fmean(ordered),
        "median": statistics.median(ordered),
        "p95": ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)],
        "max": ordered[-1],
    }


def sign_test_p(wins: int, losses: int) -> float:
    """Exact two-sided sign test on discordant pairs (ties ignored)."""
    n = wins + losses
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(wins, losses) + 1)) / 2**n
    return min(1.0, 2 * tail)


def paired(first: Sequence[bool], second: Sequence[bool]) -> dict[str, Any]:
    """How often ``first`` is right where ``second`` is wrong, and vice versa."""
    wins = sum(a and not b for a, b in zip(first, second, strict=True))
    losses = sum(b and not a for a, b in zip(first, second, strict=True))
    return {"wins": wins, "losses": losses, "p_value": sign_test_p(wins, losses)}


def summarize(results: Sequence[CaseResult], k: int) -> dict[str, Any]:
    in_scope = [r for r in results if r.expected_faq_ids]
    expect_answer = [r for r in results if r.expected_decision == ExpectedDecision.ANSWERED]
    expect_ambiguous = [r for r in results if r.expected_decision == ExpectedDecision.AMBIGUOUS]
    out_of_scope = [r for r in results if r.expected_decision == ExpectedDecision.NO_MATCH]

    retrieval = {}
    for method in Method:
        rows = [r.retrieval[method] for r in in_scope]
        retrieval[method.value] = {
            "top1_accuracy": _fraction(sum(x.top1_correct for x in rows), len(rows)),
            f"recall_at_{k}": _fraction(sum(x.in_top_k for x in rows), len(rows)),
            "no_results": sum(not x.ranked for x in rows),
            "latency_ms": latency_stats([r.retrieval[method].latency_ms for r in results]),
        }

    answers = {}
    for policy in Policy:
        rows = [r.answers[policy] for r in results]
        given = [a for a in rows if a.outcome in FALSE_ANSWERS | {AnswerOutcome.CORRECT_ANSWER}]
        clarified = [r for r in results if r.answers[policy].outcome is AnswerOutcome.CLARIFIED]
        true_ambiguous = sum(r.expected_decision == ExpectedDecision.AMBIGUOUS for r in clarified)
        recall = _rate(true_ambiguous, len(expect_ambiguous))
        precision = _rate(true_ambiguous, len(clarified))
        answers[policy.value] = {
            "correct_answer_rate": _fraction(
                sum(
                    r.answers[policy].outcome is AnswerOutcome.CORRECT_ANSWER for r in expect_answer
                ),
                len(expect_answer),
            ),
            "out_of_scope_rejection_rate": _fraction(
                sum(r.answers[policy].outcome is AnswerOutcome.DECLINED for r in out_of_scope),
                len(out_of_scope),
            ),
            "false_answer_rate": _fraction(
                sum(a.outcome in FALSE_ANSWERS for a in rows), len(rows)
            ),
            "answer_precision": _fraction(
                sum(a.outcome is AnswerOutcome.CORRECT_ANSWER for a in given), len(given)
            ),
            "decision_accuracy": _fraction(sum(a.decision_correct for a in rows), len(rows)),
            "ambiguity": {
                "expected": len(expect_ambiguous),
                "clarified": len(clarified),
                "true_positives": true_ambiguous,
                "precision": precision,
                "recall": recall,
                "f1": (
                    2 * precision * recall / (precision + recall)
                    if precision and recall
                    else (0.0 if precision is not None and recall is not None else None)
                ),
                "clarifications_offering_an_acceptable_faq": sum(
                    bool(set(r.answers[policy].options) & set(r.expected_faq_ids))
                    for r in clarified
                    if r.expected_faq_ids
                ),
            },
            "outcomes": {o.value: sum(a.outcome is o for a in rows) for o in AnswerOutcome},
            "confusion": {
                expected.value: {
                    actual: sum(
                        r.expected_decision == expected and r.answers[policy].decision == actual
                        for r in results
                    )
                    for actual in (d.value for d in Decision)
                }
                for expected in ExpectedDecision
            },
            "latency_ms": latency_stats([a.latency_ms for a in rows]),
        }

    def top1(method: Method) -> list[bool]:
        return [r.retrieval[method].top1_correct for r in in_scope]

    def recall_k(method: Method) -> list[bool]:
        return [r.retrieval[method].in_top_k for r in in_scope]

    def not_false(policy: Policy) -> list[bool]:
        return [r.answers[policy].outcome not in FALSE_ANSWERS for r in results]

    comparisons = {
        "retrieval_top1": {
            f"{a.value}_vs_{b.value}": paired(top1(a), top1(b)) for a, b in _method_pairs()
        },
        f"retrieval_recall_at_{k}": {
            f"{a.value}_vs_{b.value}": paired(recall_k(a), recall_k(b)) for a, b in _method_pairs()
        },
        "avoided_false_answers": {
            f"{Policy.DECISION.value}_vs_{p.value}": paired(
                not_false(Policy.DECISION), not_false(p)
            )
            for p in (Policy.SEMANTIC_ANSWER, Policy.KEYWORD_ANSWER)
        },
    }

    by_type = {}
    for query_type in QueryType:
        rows = [r for r in results if r.type == query_type]
        if not rows:
            continue
        scoped = [r for r in rows if r.expected_faq_ids]
        by_type[query_type.value] = {
            "cases": len(rows),
            **{f"{m.value}_top1": sum(r.retrieval[m].top1_correct for r in scoped) for m in Method},
            "decision_correct": sum(r.answers[Policy.DECISION].decision_correct for r in rows),
            "decision_false_answers": sum(
                r.answers[Policy.DECISION].outcome in FALSE_ANSWERS for r in rows
            ),
        }

    return {
        "cases": len(results),
        "subsets": {
            "in_scope": len(in_scope),
            "expected_answered": len(expect_answer),
            "expected_ambiguous": len(expect_ambiguous),
            "out_of_scope": len(out_of_scope),
        },
        "retrieval": retrieval,
        "answers": answers,
        "comparisons": comparisons,
        "by_type": by_type,
    }


def _method_pairs() -> list[tuple[Method, Method]]:
    return [
        (Method.HYBRID, Method.SEMANTIC),
        (Method.HYBRID, Method.KEYWORD),
        (Method.SEMANTIC, Method.KEYWORD),
    ]


# --- Running -------------------------------------------------------------------------------------


def _timed(call: Callable[[], Any]) -> tuple[Any, float]:
    started = time.perf_counter()
    value = call()
    return value, (time.perf_counter() - started) * 1000


def run_case(
    case: EvalCase,
    semantic: SemanticSearchService,
    search: SearchServices,
    min_similarity: float,
    k: int,
) -> CaseResult:
    """Run every method and policy on one case. The same category filter is used by all."""
    query, category = case.query, case.category_filter
    keyword_hits, keyword_ms = _timed(
        lambda: search.keyword.search(query, top_k=k, category=category)
    )
    semantic_hits, semantic_ms = _timed(
        lambda: semantic.search(query, top_k=k, category=category, min_score=-1.0)
    )
    hybrid_hits, hybrid_ms = _timed(lambda: search.hybrid.search(query, top_k=k, category=category))
    decision, decision_ms = _timed(lambda: search.decision.decide(query, category=category))
    if decision.retrieval_mode is not RetrievalMode.HYBRID:
        raise RuntimeError("semantic retrieval became unavailable; evaluation aborted")

    retrieval = {
        Method.KEYWORD.value: score_retrieval(
            case, [(c.faq_id, c.score) for c in keyword_hits], keyword_ms
        ),
        Method.SEMANTIC.value: score_retrieval(
            case, [(m.faq.id, m.score) for m in semantic_hits], semantic_ms
        ),
        Method.HYBRID.value: score_retrieval(
            case, [(h.faq_id, h.rrf_score) for h in hybrid_hits], hybrid_ms
        ),
    }
    answers = {
        Policy.KEYWORD_ANSWER.value: baseline_answer(case, retrieval[Method.KEYWORD]),
        Policy.SEMANTIC_ANSWER.value: baseline_answer(
            case, retrieval[Method.SEMANTIC], min_similarity
        ),
        Policy.DECISION.value: score_answer(
            case,
            decision.decision.value,
            decision.faq.id if decision.faq else None,
            decision_ms,
            [option.faq.id for option in decision.options]
            if decision.decision is Decision.AMBIGUOUS
            else (),
        ),
    }
    return CaseResult(
        case_id=case.id,
        type=case.type.value,
        query=case.query,
        category_filter=None if category is None else category.value,
        expected_faq_ids=list(case.expected_faq_ids),
        expected_decision=case.expected_decision.value,
        retrieval=retrieval,
        answers=answers,
    )


def evaluate(
    dataset_path: Path,
    faq_path: Path,
    settings: Settings,
    k: int = DEFAULT_K,
    embedding_factory: Callable[[str], EmbeddingService] = EmbeddingService,
) -> dict[str, Any]:
    """Run the full evaluation and return a JSON-serialisable report."""
    dataset = load_dataset(dataset_path)
    repository = FAQRepository.from_json_file(faq_path)
    if problems := check_labels_against_faqs(dataset, repository.list_all()):
        raise ValueError("Invalid evaluation labels:\n  " + "\n  ".join(problems))

    faq_service = FAQService(repository)
    semantic = SemanticSearchService(
        faq_service.list_active_faqs, embedding_factory(settings.embedding_model_name)
    )
    search = build_search_services(settings, faq_service.list_active_faqs, semantic)
    if not 1 <= k <= search.decision.max_top_k:
        raise ValueError(f"k must be between 1 and {search.decision.max_top_k}, got {k}")

    started = time.perf_counter()
    semantic.build_index()  # load model, embed FAQs
    search.decision.decide("warm-up query")  # prime every code path once
    warmup_seconds = time.perf_counter() - started

    min_similarity = settings.answer_min_similarity
    results = [run_case(case, semantic, search, min_similarity, k) for case in dataset.cases]
    return {
        "meta": {
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "model": settings.embedding_model_name,
            "k": k,
            "settings": {
                name: getattr(settings, name)
                for name in (
                    "hybrid_rrf_k",
                    "hybrid_candidate_depth",
                    "answer_candidates",
                    "answer_min_similarity",
                    "answer_accept_exact_question",
                    "answer_ambiguity_margin",
                    "answer_max_clarification_options",
                )
            },
            "dataset": _display_path(dataset_path),
            "dataset_sha256": file_sha256(dataset_path),
            "faq_data": _display_path(faq_path),
            "faq_data_sha256": file_sha256(faq_path),
            "active_faqs": len(faq_service.list_active_faqs()),
            "warmup_seconds": warmup_seconds,
            "environment": environment_info(),
        },
        "metrics": summarize(results, k),
        "results": [asdict(r) for r in results],
    }


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


# --- Report ------------------------------------------------------------------------------------


def _frac(value: dict[str, Any]) -> str:
    if value["rate"] is None:
        return "n/a"
    return f"{value['rate'] * 100:5.1f}% ({value['n']}/{value['of']})"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _ms(latency: dict[str, float | None]) -> str:
    if latency["mean"] is None:
        return "n/a"
    return (
        f"mean {latency['mean']:6.1f} | median {latency['median']:6.1f} | p95 {latency['p95']:6.1f}"
    )


def _comparison_line(name: str, result: dict[str, Any]) -> str:
    first, second = name.split("_vs_")
    wins, losses, p = result["wins"], result["losses"], result["p_value"]
    if wins == losses:
        verdict = "no difference"
    elif p < SIGNIFICANCE_LEVEL:
        verdict = f"{first if wins > losses else second} ahead (p < {SIGNIFICANCE_LEVEL})"
    else:
        verdict = "difference not statistically significant on this set"
    return (
        f"  {first} vs {second}: {first} right/{second} wrong {wins}, "
        f"{first} wrong/{second} right {losses}, sign test p={p:.3f} -> {verdict}"
    )


def _ranking(retrieval: dict[str, Any]) -> str:
    return ", ".join(f"{faq_id} {score:.3g}" for faq_id, score in retrieval["ranked"]) or "none"


def format_report(report: dict[str, Any]) -> str:
    meta, metrics, k = report["meta"], report["metrics"], report["meta"]["k"]
    subsets, results = metrics["subsets"], report["results"]
    lines = [
        "Hybrid search evaluation",
        "=" * 24,
        f"Model:    {meta['model']}   k: {k}",
        "Settings: " + ", ".join(f"{name}={value}" for name, value in meta["settings"].items()),
        f"Dataset:  {meta['dataset']} ({metrics['cases']} cases: {subsets['expected_answered']} "
        f"expect an answer, {subsets['expected_ambiguous']} ambiguous, "
        f"{subsets['out_of_scope']} out of scope), sha256 {meta['dataset_sha256'][:16]}...",
        f"FAQ data: {meta['faq_data']} ({meta['active_faqs']} active FAQs), "
        f"sha256 {meta['faq_data_sha256'][:16]}...",
        f"Warm-up (model load + index build): {meta['warmup_seconds']:.1f} s",
        "",
        f"1. Retrieval quality ({subsets['in_scope']} in-scope cases; ranking only, no "
        "answer decision)",
        f"  {'method':<10}{'top-1 accuracy':>22}{f'Recall@{k}':>22}{'no results':>12}",
    ]
    for method, row in metrics["retrieval"].items():
        lines.append(
            f"  {method:<10}{_frac(row['top1_accuracy']):>22}"
            f"{_frac(row[f'recall_at_{k}']):>22}{row['no_results']:>12}"
        )

    lines += [
        "",
        f"2. Answer quality ({metrics['cases']} cases; what the customer would get)",
        "  keyword_answer = keyword top-1 if any match; semantic_answer = semantic top-1 if "
        f"similarity >= {meta['settings']['answer_min_similarity']}; decision = answer "
        "decision layer",
    ]
    rows = [
        (
            "Correct-answer rate",
            "correct_answer_rate",
            f"of {subsets['expected_answered']} expected answered",
        ),
        (
            "Out-of-scope rejection",
            "out_of_scope_rejection_rate",
            f"of {subsets['out_of_scope']} out of scope",
        ),
        ("False-answer rate", "false_answer_rate", f"of all {metrics['cases']} cases"),
        ("Answer precision", "answer_precision", "of answers given"),
        ("Decision accuracy", "decision_accuracy", f"of all {metrics['cases']} cases"),
    ]
    policies = list(metrics["answers"])
    lines.append(f"  {'metric':<24}" + "".join(f"{p:>22}" for p in policies) + "   denominator")
    for label, key, denominator in rows:
        lines.append(
            f"  {label:<24}"
            + "".join(f"{_frac(metrics['answers'][p][key]):>22}" for p in policies)
            + f"   {denominator}"
        )

    decision = metrics["answers"][Policy.DECISION]
    amb = decision["ambiguity"]
    out = decision["outcomes"]
    lines += [
        "",
        "3. Ambiguity detection (decision layer; the baselines never ask)",
        f"  Clarifying questions asked: {amb['clarified']}  "
        f"(expected-ambiguous cases: {amb['expected']})",
        f"  Precision {_pct(amb['precision'])} ({amb['true_positives']}/{amb['clarified']})   "
        f"Recall {_pct(amb['recall'])} ({amb['true_positives']}/{amb['expected']})   "
        f"F1 {_pct(amb['f1'])}",
        f"  Clarifications offering an acceptable FAQ: "
        f"{amb['clarifications_offering_an_acceptable_faq']} of {amb['clarified']}",
        "",
        "  Decision outcomes: " + ", ".join(f"{name} {count}" for name, count in out.items()),
        "  Confusion (expected -> actual decision):",
    ]
    for expected, actual in decision["confusion"].items():
        lines.append(f"    {expected:<10} " + "  ".join(f"{a} {n}" for a, n in actual.items()))

    lines += ["", "4. Average latency per call after warm-up (ms)"]
    for method, row in metrics["retrieval"].items():
        lines.append(f"  {method + ' retrieval':<20}{_ms(row['latency_ms'])}")
    lines.append(f"  {'decision (total)':<20}{_ms(decision['latency_ms'])}")

    lines += ["", "5. Paired comparisons (same cases; exact two-sided sign test)"]
    for group, pairs in metrics["comparisons"].items():
        lines.append(f"  {group}:")
        lines += ["  " + _comparison_line(name, value) for name, value in pairs.items()]

    lines += [
        "",
        "6. By question type (top-1 counts over in-scope cases; decision counts over all)",
        f"  {'type':<20}{'cases':>6}{'kw top1':>9}{'sem top1':>9}{'hyb top1':>9}"
        f"{'decision ok':>13}{'false ans':>11}",
    ]
    for query_type, row in metrics["by_type"].items():
        lines.append(
            f"  {query_type:<20}{row['cases']:>6}{row['keyword_top1']:>9}"
            f"{row['semantic_top1']:>9}{row['hybrid_top1']:>9}"
            f"{row['decision_correct']:>13}{row['decision_false_answers']:>11}"
        )

    lines += ["", "7. Failure examples"]
    for method in Method:
        failures = [
            r
            for r in results
            if r["expected_faq_ids"] and not r["retrieval"][method]["top1_correct"]
        ]
        lines += _examples(
            f"{method.value} retrieval: wrong FAQ ranked first",
            failures,
            lambda r, m=method.value: f"got {_ranking(r['retrieval'][m])}",
        )

    def decision_of(r: dict[str, Any]) -> dict[str, Any]:
        return r["answers"][Policy.DECISION]

    def described(r: dict[str, Any]) -> str:
        d = decision_of(r)
        detail = d["faq_id"] or ", ".join(d["options"]) or "-"
        return (
            f"decision {d['decision']} ({detail}); hybrid {_ranking(r['retrieval'][Method.HYBRID])}"
        )

    groups = [
        ("Decision: false answers", lambda r: decision_of(r)["outcome"] in FALSE_ANSWERS),
        (
            "Decision: answer expected, declined",
            lambda r: (
                r["expected_decision"] == ExpectedDecision.ANSWERED
                and decision_of(r)["outcome"] == AnswerOutcome.DECLINED
            ),
        ),
        (
            "Decision: answer expected, asked for clarification",
            lambda r: (
                r["expected_decision"] == ExpectedDecision.ANSWERED
                and decision_of(r)["outcome"] == AnswerOutcome.CLARIFIED
            ),
        ),
        (
            "Decision: ambiguity missed",
            lambda r: (
                r["expected_decision"] == ExpectedDecision.AMBIGUOUS
                and decision_of(r)["outcome"] != AnswerOutcome.CLARIFIED
            ),
        ),
        (
            "Decision: out of scope, asked for clarification",
            lambda r: (
                r["expected_decision"] == ExpectedDecision.NO_MATCH
                and decision_of(r)["outcome"] == AnswerOutcome.CLARIFIED
            ),
        ),
    ]
    for title, predicate in groups:
        lines += _examples(title, [r for r in results if predicate(r)], described)
    return "\n".join(lines)


def _examples(
    title: str, failures: list[dict[str, Any]], describe: Callable[[dict[str, Any]], str]
) -> list[str]:
    lines = ["", f"  {title} ({len(failures)})"]
    for r in failures[:MAX_FAILURE_EXAMPLES]:
        expected = ", ".join(r["expected_faq_ids"]) or "none"
        where = f" [category {r['category_filter']}]" if r["category_filter"] else ""
        lines.append(f"    {r['case_id']} ({r['type']}) {r['query']!r}{where}")
        lines.append(f"        expected {r['expected_decision']}: {expected} | {describe(r)}")
    if len(failures) > MAX_FAILURE_EXAMPLES:
        lines.append(f"    ... and {len(failures) - MAX_FAILURE_EXAMPLES} more (see --output)")
    return lines


# --- Entry point ----------------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None, settings: Settings) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare keyword, semantic and hybrid retrieval and the answer decision."
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--faq-data", type=Path, default=resolve_data_path(settings.faq_data_path))
    parser.add_argument("-k", type=int, default=DEFAULT_K, help="k for Recall@k (default 3)")
    parser.add_argument("--output", type=Path, help="also write the full report as JSON")
    parser.add_argument(
        "--offline", action="store_true", help="use only the locally cached model (no downloads)"
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    embedding_factory: Callable[[str], EmbeddingService] = EmbeddingService,
    settings: Settings | None = None,
) -> int:
    settings = settings or Settings()
    args = parse_args(argv, settings)
    if args.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"  # read when huggingface_hub is first imported
    report = evaluate(args.dataset, args.faq_data, settings, args.k, embedding_factory)
    print(format_report(report))
    if args.output:
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nFull results written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
