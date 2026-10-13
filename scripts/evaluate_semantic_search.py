"""Evaluate semantic FAQ search against a labelled question set.

Usage (from the project root, virtual environment active):

    python scripts/evaluate_semantic_search.py
    python scripts/evaluate_semantic_search.py --threshold 0.5 --output eval_results.json
    python scripts/evaluate_semantic_search.py --offline     # never try to download the model

How it works
------------
1. Every question is retrieved WITHOUT a threshold (min_score=-1), keeping the top k.
2. The threshold is then applied as a separate step. This separates two failure kinds:
   - retrieval failure: the top-ranked FAQ is not an acceptable answer;
   - threshold rejection: the top-ranked FAQ is correct, but scored below the threshold
     so the API would return no result.
3. Out-of-scope questions (no acceptable FAQ) should score below the threshold.

Metrics
-------
- Top-1 accuracy (in-scope): the first result is an acceptable FAQ (threshold ignored).
- Recall@k (in-scope): an acceptable FAQ appears in the top k (threshold ignored).
- Answered correctly (in-scope): correct top-1 AND score >= threshold (what users get).
- No-match rejection rate (out-of-scope): top-1 score < threshold.
- Query latency: wall time of each search call after a warm-up query, so the one-off
  model load and index build are reported separately.

Reproducibility: the report records the model, threshold, k, SHA-256 hashes of the
dataset and FAQ files and library versions; ``--output`` saves everything as JSON.
Model inference on CPU is deterministic for a fixed model and inputs.
"""

import argparse
import hashlib
import json
import os
import platform
import statistics
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib import metadata
from pathlib import Path
from typing import Any, Self

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/evaluate_semantic_search.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import BaseModel, ConfigDict, Field, model_validator  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.database.faq_repository import FAQRepository, resolve_data_path  # noqa: E402
from app.models.faq import FAQ  # noqa: E402
from app.services.embedding_service import EmbeddingService  # noqa: E402
from app.services.faq_service import FAQService  # noqa: E402
from app.services.semantic_search_service import (  # noqa: E402
    SemanticSearchService,
    meets_threshold,
)

DEFAULT_DATASET = PROJECT_ROOT / "data" / "semantic_search_eval.json"
DEFAULT_K = 3
MAX_FAILURE_EXAMPLES = 10


# --- Dataset ----------------------------------------------------------------------------


class QueryType(StrEnum):
    EXACT = "exact"
    PARAPHRASE = "paraphrase"
    SYNONYM = "synonym"
    SHORT = "short"
    LONG = "long"
    TYPO = "typo"
    AMBIGUOUS = "ambiguous"
    OUT_OF_SCOPE = "out_of_scope"


class EvalCase(BaseModel):
    """One labelled question. ``expected_faq_ids`` lists every acceptable answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^eval_\d{3}$")
    type: QueryType
    query: str = Field(min_length=1)
    expected_faq_ids: list[str] = Field(default_factory=list)
    notes: str = ""

    @property
    def in_scope(self) -> bool:
        return self.type is not QueryType.OUT_OF_SCOPE

    @model_validator(mode="after")
    def _labels_match_type(self) -> Self:
        if self.in_scope and not self.expected_faq_ids:
            raise ValueError(f"{self.id}: in-scope case needs at least one expected FAQ id")
        if not self.in_scope and self.expected_faq_ids:
            raise ValueError(f"{self.id}: out_of_scope case must not list expected FAQ ids")
        if len(set(self.expected_faq_ids)) != len(self.expected_faq_ids):
            raise ValueError(f"{self.id}: expected_faq_ids contains duplicates")
        return self


class EvalDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = ""
    faq_data: str = "data/faqs.json"
    cases: list[EvalCase] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_case_ids(self) -> Self:
        ids = [case.id for case in self.cases]
        if duplicates := sorted({i for i in ids if ids.count(i) > 1}):
            raise ValueError(f"duplicate case ids: {', '.join(duplicates)}")
        return self


def load_dataset(path: Path) -> EvalDataset:
    return EvalDataset.model_validate_json(path.read_text(encoding="utf-8"))


def check_labels_against_faqs(dataset: EvalDataset, faqs: Iterable[FAQ]) -> list[str]:
    """Problems with expected IDs: unknown IDs, or IDs of inactive FAQs (never returned)."""
    by_id = {faq.id: faq for faq in faqs}
    problems = []
    for case in dataset.cases:
        for faq_id in case.expected_faq_ids:
            if faq_id not in by_id:
                problems.append(f"{case.id}: expected FAQ {faq_id} does not exist")
            elif not by_id[faq_id].is_active:
                problems.append(f"{case.id}: expected FAQ {faq_id} is inactive")
    return problems


# --- Scoring (pure functions) --------------------------------------------------------------


class Outcome(StrEnum):
    CORRECT = "correct"  # in-scope: acceptable top-1 at/above threshold
    THRESHOLD_REJECTED = "threshold_rejected"  # in-scope: acceptable top-1, below threshold
    WRONG_ANSWER_SHOWN = "wrong_answer_shown"  # retrieval failure, shown to the user
    WRONG_ANSWER_HIDDEN = "wrong_answer_hidden"  # retrieval failure, hidden by threshold
    CORRECTLY_REJECTED = "correctly_rejected"  # out-of-scope: nothing at/above threshold
    OUT_OF_SCOPE_ANSWERED = "out_of_scope_answered"  # out-of-scope: an FAQ was shown


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    type: str
    query: str
    expected_faq_ids: list[str]
    ranked: list[tuple[str, float]]  # top-k (faq_id, score), best first, no threshold
    latency_ms: float
    outcome: Outcome
    top1_correct: bool
    in_top_k: bool

    @property
    def top_score(self) -> float | None:
        return self.ranked[0][1] if self.ranked else None


def classify(
    case: EvalCase, ranked: Sequence[tuple[str, float]], threshold: float, latency_ms: float
) -> CaseResult:
    """Decide the outcome of one case from its unthresholded ranking."""
    top_id, top_score = ranked[0] if ranked else (None, float("-inf"))
    shown = meets_threshold(top_score, threshold)  # same rule as the API
    acceptable = set(case.expected_faq_ids)
    top1_correct = top_id in acceptable
    in_top_k = any(faq_id in acceptable for faq_id, _ in ranked)

    if not case.in_scope:
        outcome = Outcome.OUT_OF_SCOPE_ANSWERED if shown else Outcome.CORRECTLY_REJECTED
    elif top1_correct:
        outcome = Outcome.CORRECT if shown else Outcome.THRESHOLD_REJECTED
    else:
        outcome = Outcome.WRONG_ANSWER_SHOWN if shown else Outcome.WRONG_ANSWER_HIDDEN

    return CaseResult(
        case_id=case.id,
        type=case.type.value,
        query=case.query,
        expected_faq_ids=list(case.expected_faq_ids),
        ranked=[(faq_id, float(score)) for faq_id, score in ranked],
        latency_ms=latency_ms,
        outcome=outcome,
        top1_correct=top1_correct,
        in_top_k=in_top_k,
    )


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def summarize(results: Sequence[CaseResult], k: int) -> dict[str, Any]:
    """Aggregate metrics. Rates are fractions 0..1, or None when there are no cases."""
    in_scope = [r for r in results if r.type != QueryType.OUT_OF_SCOPE]
    out_of_scope = [r for r in results if r.type == QueryType.OUT_OF_SCOPE]
    count = {outcome: sum(r.outcome is outcome for r in results) for outcome in Outcome}
    latencies = sorted(r.latency_ms for r in results)

    by_type = {}
    for query_type in QueryType:
        rows = [r for r in results if r.type == query_type]
        if not rows:
            continue
        if query_type is QueryType.OUT_OF_SCOPE:
            by_type[query_type.value] = {
                "cases": len(rows),
                "rejected": sum(r.outcome is Outcome.CORRECTLY_REJECTED for r in rows),
            }
        else:
            by_type[query_type.value] = {
                "cases": len(rows),
                "top1_correct": sum(r.top1_correct for r in rows),
                f"in_top_{k}": sum(r.in_top_k for r in rows),
                "answered_correctly": sum(r.outcome is Outcome.CORRECT for r in rows),
            }

    return {
        "cases": len(results),
        "in_scope_cases": len(in_scope),
        "out_of_scope_cases": len(out_of_scope),
        "top1_accuracy": _rate(sum(r.top1_correct for r in in_scope), len(in_scope)),
        f"recall_at_{k}": _rate(sum(r.in_top_k for r in in_scope), len(in_scope)),
        "answered_correctly_rate": _rate(count[Outcome.CORRECT], len(in_scope)),
        "no_match_rejection_rate": _rate(count[Outcome.CORRECTLY_REJECTED], len(out_of_scope)),
        "outcomes": {outcome.value: n for outcome, n in count.items()},
        "latency_ms": {
            "mean": statistics.fmean(latencies) if latencies else None,
            "median": statistics.median(latencies) if latencies else None,
            "p95": latencies[max(0, round(0.95 * len(latencies)) - 1)] if latencies else None,
            "max": latencies[-1] if latencies else None,
        },
        "by_type": by_type,
    }


# --- Running -------------------------------------------------------------------------------------


def run_cases(
    semantic: SemanticSearchService, cases: Sequence[EvalCase], threshold: float, k: int
) -> list[CaseResult]:
    """Search every case (no threshold), time it, then classify against ``threshold``."""
    results = []
    for case in cases:
        started = time.perf_counter()
        matches = semantic.search(case.query, top_k=k, min_score=-1.0)
        latency_ms = (time.perf_counter() - started) * 1000
        ranked = [(match.faq.id, match.score) for match in matches]
        results.append(classify(case, ranked, threshold, latency_ms))
    return results


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


def environment_info() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": {name: _version(name) for name in ("sentence-transformers", "torch", "numpy")},
    }


# --- Report ------------------------------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:5.1f}%"


def _latency_line(latency: dict[str, float | None]) -> str:
    if latency["mean"] is None:
        return "  n/a"
    return (
        f"  mean {latency['mean']:.1f} ms | median {latency['median']:.1f} ms | "
        f"p95 {latency['p95']:.1f} ms | max {latency['max']:.1f} ms"
    )


def _ranking(result: CaseResult) -> str:
    return ", ".join(f"{faq_id} {score:.3f}" for faq_id, score in result.ranked)


def format_report(report: dict[str, Any]) -> str:
    meta, metrics, k = report["meta"], report["metrics"], report["meta"]["k"]
    out = metrics["outcomes"]
    results = [_result_from_dict(r) for r in report["results"]]
    lines = [
        "Semantic search evaluation",
        "=" * 26,
        f"Model:     {meta['model']}",
        f"Threshold: {meta['threshold']}   k: {k}",
        f"Dataset:   {meta['dataset']} ({metrics['cases']} cases: "
        f"{metrics['in_scope_cases']} in scope, {metrics['out_of_scope_cases']} out of scope)",
        f"           sha256 {meta['dataset_sha256'][:16]}...",
        f"FAQ data:  {meta['faq_data']} ({meta['active_faqs']} active FAQs), "
        f"sha256 {meta['faq_data_sha256'][:16]}...",
        f"Warm-up (model load + index build): {meta['warmup_seconds']:.1f} s",
        "",
        "Retrieval quality (in scope, threshold ignored)",
        f"  Top-1 accuracy:          {_pct(metrics['top1_accuracy'])}",
        f"  Recall@{k}:                {_pct(metrics[f'recall_at_{k}'])}",
        f"With threshold {meta['threshold']}",
        f"  Answered correctly:      {_pct(metrics['answered_correctly_rate'])}  "
        f"({out['correct']} of {metrics['in_scope_cases']})",
        f"  Threshold rejections:    {out['threshold_rejected']}  (right FAQ, score too low)",
        f"  Wrong answer shown:      {out['wrong_answer_shown']}  (retrieval failure, shown)",
        f"  Wrong answer hidden:     {out['wrong_answer_hidden']}  "
        "(retrieval failure, below threshold)",
        f"  No-match rejection rate: {_pct(metrics['no_match_rejection_rate'])}  "
        f"({out['correctly_rejected']} of {metrics['out_of_scope_cases']} out-of-scope)",
        "",
        "Latency per query (after warm-up)",
        _latency_line(metrics["latency_ms"]),
        "",
        "By question type",
        f"  {'type':<13}{'cases':>6}{'top-1':>8}{f'top-{k}':>8}{'answered':>10}{'rejected':>10}",
    ]
    for query_type, row in metrics["by_type"].items():
        if query_type == QueryType.OUT_OF_SCOPE:
            lines.append(
                f"  {query_type:<13}{row['cases']:>6}{'':>8}{'':>8}{'':>10}{row['rejected']:>10}"
            )
        else:
            lines.append(
                f"  {query_type:<13}{row['cases']:>6}{row['top1_correct']:>8}"
                f"{row[f'in_top_{k}']:>8}{row['answered_correctly']:>10}{'':>10}"
            )

    sections = [
        (
            "Retrieval failures: wrong FAQ ranked first",
            {Outcome.WRONG_ANSWER_SHOWN, Outcome.WRONG_ANSWER_HIDDEN},
        ),
        (
            "Threshold rejections: right FAQ first, but below threshold",
            {Outcome.THRESHOLD_REJECTED},
        ),
        ("Out-of-scope questions that were answered", {Outcome.OUT_OF_SCOPE_ANSWERED}),
    ]
    for title, outcomes in sections:
        failures = [r for r in results if r.outcome in outcomes]
        lines += ["", f"{title} ({len(failures)})"]
        for r in failures[:MAX_FAILURE_EXAMPLES]:
            expected = ", ".join(r.expected_faq_ids) or "none"
            tag = f" [{r.outcome.value}]" if len(outcomes) > 1 else ""
            hint = f"; acceptable FAQ in top {k}" if r.in_top_k and not r.top1_correct else ""
            lines.append(f"  {r.case_id} ({r.type}){tag} {r.query!r}")
            lines.append(f"      expected {expected}{hint} | got {_ranking(r)}")
        if len(failures) > MAX_FAILURE_EXAMPLES:
            lines.append(f"  ... and {len(failures) - MAX_FAILURE_EXAMPLES} more (see --output)")
    return "\n".join(lines)


def _result_from_dict(data: dict[str, Any]) -> CaseResult:
    return CaseResult(
        **{
            **data,
            "ranked": [tuple(pair) for pair in data["ranked"]],
            "outcome": Outcome(data["outcome"]),
        }
    )


# --- Entry point ----------------------------------------------------------------------------------


def evaluate(
    dataset_path: Path,
    faq_path: Path,
    model_name: str,
    threshold: float,
    k: int,
    embedding_factory: Callable[[str], EmbeddingService] = EmbeddingService,
) -> dict[str, Any]:
    """Run the full evaluation and return a JSON-serialisable report."""
    if not -1.0 <= threshold <= 1.0:
        raise ValueError(f"threshold must be between -1 and 1, got {threshold}")
    if k < 1:
        raise ValueError(f"k must be at least 1, got {k}")

    dataset = load_dataset(dataset_path)
    repository = FAQRepository.from_json_file(faq_path)
    if problems := check_labels_against_faqs(dataset, repository.list_all()):
        raise ValueError("Invalid evaluation labels:\n  " + "\n  ".join(problems))

    faq_service = FAQService(repository)
    semantic = SemanticSearchService(faq_service.list_active_faqs, embedding_factory(model_name))
    started = time.perf_counter()
    semantic.build_index()
    semantic.search("warm-up query", top_k=1, min_score=-1.0)  # load model, prime caches
    warmup_seconds = time.perf_counter() - started

    results = run_cases(semantic, dataset.cases, threshold, k)
    return {
        "meta": {
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "model": model_name,
            "threshold": threshold,
            "k": k,
            "dataset": _display_path(dataset_path),
            "dataset_sha256": file_sha256(dataset_path),
            "faq_data": _display_path(faq_path),
            "faq_data_sha256": file_sha256(faq_path),
            "active_faqs": len(faq_service.list_active_faqs()),
            "warmup_seconds": warmup_seconds,
            "environment": environment_info(),
        },
        "metrics": summarize(results, k),
        "results": [{**asdict(r), "outcome": r.outcome.value} for r in results],
    }


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    settings = Settings()
    parser = argparse.ArgumentParser(description="Evaluate semantic FAQ search.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--faq-data", type=Path, default=resolve_data_path(settings.faq_data_path))
    parser.add_argument("--model", default=settings.embedding_model_name)
    parser.add_argument(
        "--threshold",
        type=float,
        default=settings.semantic_min_similarity,
        help="similarity threshold to evaluate (default: SEMANTIC_MIN_SIMILARITY)",
    )
    parser.add_argument("-k", type=int, default=DEFAULT_K, help="k for Recall@k (default 3)")
    parser.add_argument("--output", type=Path, help="also write the full report as JSON")
    parser.add_argument(
        "--offline", action="store_true", help="use only the locally cached model (no downloads)"
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    embedding_factory: Callable[[str], EmbeddingService] = EmbeddingService,
) -> int:
    args = parse_args(argv)
    if args.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"  # read when huggingface_hub is first imported
    report = evaluate(
        args.dataset, args.faq_data, args.model, args.threshold, args.k, embedding_factory
    )
    print(format_report(report))
    if args.output:
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nFull results written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
