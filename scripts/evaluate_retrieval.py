"""Retrieval evaluation (Stage 12, Task 13): keyword-only, semantic-only and hybrid search
on the retrieval cases of the unified dataset, plus the answer decision built on them.

Usage (from the project root, virtual environment active):

    python scripts/evaluate_retrieval.py
    python scripts/evaluate_retrieval.py --output data/retrieval_eval_results.json
    python scripts/evaluate_retrieval.py --database data/conversations.db   # a COPY is read

FAQs are loaded through the real storage layer: by default a temporary SQLite database
seeded from ``data/faqs.json`` exactly as the API seeds an empty database; with
``--database`` a temporary *copy* of that database file (the original is never opened, so
a running server or customer data is never touched). Inactive FAQs are excluded, as in
production. Settings are the code defaults (``.env`` is not read), so runs are
reproducible; the embedding model must be in the local cache (no download is attempted).

Two separate questions
----------------------
1. **Ranking** (retrieval only, no answer is given): does a method rank an acceptable
   FAQ first, in the top 3, and how high?
2. **Answering**: what would the customer get under three policies?
   - ``keyword_answer``: answer the keyword top-1 whenever keyword search finds anything;
   - ``semantic_answer``: answer the semantic top-1 when its cosine similarity reaches
     ``ANSWER_MIN_SIMILARITY`` (the same threshold the decision layer uses);
   - ``decision``: the real answer decision (hybrid ranking, acceptance, ambiguity), as
     the chat API uses it.
   Hybrid ranking has no answer policy of its own: reciprocal-rank-fusion scores are
   rank-based and must not be thresholded, so declining is the decision layer's job.

Metric definitions and denominators
-----------------------------------
A case may list several acceptable FAQs (``expected_faq_ids``); any one counts.

- **Top-1 accuracy**: eligible cases whose highest-ranked FAQ is acceptable.
- **Recall@3**: eligible cases with an acceptable FAQ in the top 3.
- **MRR** (mean reciprocal rank, over the top 10): the mean of 1/rank of the first
  acceptable FAQ, 0 when none is in the top 10.
  *Eligible* = in-scope cases: an acceptable FAQ exists (expected decision answered or
  ambiguous). Every method shares this denominator.
- **Out-of-scope rejection rate**: of the cases expected ``no_match``, those the policy
  declined (gave no FAQ answer). A clarifying question does not count as a rejection.
- **False-answer rate**: of ALL retrieval cases, those where the policy gave an FAQ
  answer that is not acceptable: a wrong FAQ for an in-scope question, or any FAQ answer
  to an out-of-scope one. "Unsupported answer" means exactly this; whether the wording of
  an answer is correct is the grounding evaluation's job, not this one.
- **Correct-answer rate**: of the cases expected ``answered``, those answered with an
  acceptable FAQ.
- **Decision accuracy** (decision policy): of all cases, those whose decision is one of
  the expected decisions and, when answered, cites an acceptable FAQ.

Scores are reported in each method's own units and never as probabilities: cosine
similarity is a geometric measure between embeddings, and RRF scores only encode ranks.
Neither is a confidence. All numbers come from this run; nothing is estimated.
"""

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/evaluate_retrieval.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings  # noqa: E402
from app.database.connection import Database  # noqa: E402
from app.database.faq_repository import FAQRepository  # noqa: E402
from app.models.faq import FAQCategory  # noqa: E402
from app.repositories.faq_repository import DatabaseFAQRepository  # noqa: E402
from app.services.answer_decision_service import Decision, RetrievalMode  # noqa: E402
from app.services.embedding_service import EmbeddingService  # noqa: E402
from app.services.faq_service import FAQService  # noqa: E402
from app.services.search_pipeline import build_search_services  # noqa: E402
from app.services.semantic_search_service import (  # noqa: E402
    SemanticSearchService,
    meets_threshold,
)
from scripts.ai_eval_harness import DEFAULT_FAQ_DATA, embedding_service  # noqa: E402
from scripts.evaluate_hybrid_search import latency_stats, paired  # noqa: E402
from scripts.evaluate_semantic_search import environment_info, file_sha256  # noqa: E402

DEFAULT_DATASET = PROJECT_ROOT / "data" / "ai_evaluation.json"
METHODS = ("keyword", "semantic", "hybrid")
POLICIES = ("keyword_answer", "semantic_answer", "decision")
RANK_DEPTH = 10  # rankings are taken this deep (MRR is over the top 10)
RECALL_K = 3


@dataclass
class Ranking:
    ranked: list[tuple[str, float]]  # (FAQ ID, score in the method's own units)
    latency_ms: float
    rank: int | None  # 1-based rank of the first acceptable FAQ in the top 10


@dataclass
class PolicyAnswer:
    decision: str  # answered / ambiguous / no_match / retrieval_unavailable
    faq_id: str | None
    correct_answer: bool
    false_answer: bool
    declined: bool
    decision_correct: bool


@dataclass
class CaseResult:
    case_id: str
    subcategory: str
    query: str
    category_filter: str | None
    expected_decision: list[str]
    expected_faq_ids: list[str]
    rankings: dict[str, Ranking] = field(default_factory=dict)
    answers: dict[str, PolicyAnswer] = field(default_factory=dict)
    decision_latency_ms: float = 0.0

    @property
    def in_scope(self) -> bool:
        return bool(self.expected_faq_ids)


def load_retrieval_cases(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = [c for c in data["cases"] if c["evaluation_method"] == "retrieval_ranking"]
    if not cases:
        raise ValueError(f"{path.name} has no retrieval_ranking cases")
    return cases


def check_labels(cases: Sequence[dict[str, Any]], active_ids: set[str]) -> list[str]:
    """Labels must name active FAQs that exist: expected IDs are never invented."""
    return [
        f"{case['id']}: {faq_id} is not an active FAQ"
        for case in cases
        for faq_id in case["expected_faq_ids"]
        if faq_id not in active_ids
    ]


def _rank(ranked: Sequence[tuple[str, float]], acceptable: set[str]) -> int | None:
    return next((i for i, (faq_id, _) in enumerate(ranked, 1) if faq_id in acceptable), None)


def _answer(case: dict[str, Any], decision: str, faq_id: str | None) -> PolicyAnswer:
    acceptable = set(case["expected_faq_ids"])
    answered = decision == Decision.ANSWERED.value
    correct = answered and faq_id in acceptable
    return PolicyAnswer(
        decision=decision,
        faq_id=faq_id if answered else None,
        correct_answer=correct,
        false_answer=answered and not correct,
        declined=decision in (Decision.NO_MATCH.value, Decision.RETRIEVAL_UNAVAILABLE.value),
        decision_correct=decision in case["expected_decision"] and (not answered or correct),
    )


def _timed(call: Any) -> tuple[Any, float]:
    started = time.perf_counter()
    value = call()
    return value, (time.perf_counter() - started) * 1000


def run_case(
    case: dict[str, Any], semantic: SemanticSearchService, search: Any, threshold: float
) -> CaseResult:
    query = case["input"]
    category = FAQCategory(case["category_filter"]) if case["category_filter"] else None
    acceptable = set(case["expected_faq_ids"])
    keyword, keyword_ms = _timed(
        lambda: search.keyword.search(query, top_k=RANK_DEPTH, category=category)
    )
    semantic_hits, semantic_ms = _timed(
        lambda: semantic.search(query, top_k=RANK_DEPTH, category=category, min_score=-1.0)
    )
    hybrid, hybrid_ms = _timed(
        lambda: search.hybrid.search(query, top_k=RANK_DEPTH, category=category)
    )
    decision, decision_ms = _timed(lambda: search.decision.decide(query, category=category))
    if decision.retrieval_mode is not RetrievalMode.HYBRID:
        raise RuntimeError("semantic retrieval became unavailable; evaluation aborted")

    lists = {
        "keyword": [(c.faq_id, float(c.score)) for c in keyword],
        "semantic": [(m.faq.id, float(m.score)) for m in semantic_hits],
        "hybrid": [(h.faq_id, float(h.rrf_score)) for h in hybrid],
    }
    timings = {"keyword": keyword_ms, "semantic": semantic_ms, "hybrid": hybrid_ms}
    result = CaseResult(
        case_id=case["id"],
        subcategory=case["subcategory"],
        query=query,
        category_filter=case["category_filter"],
        expected_decision=case["expected_decision"],
        expected_faq_ids=case["expected_faq_ids"],
        decision_latency_ms=decision_ms,
    )
    for method in METHODS:
        result.rankings[method] = Ranking(
            lists[method], timings[method], _rank(lists[method], acceptable)
        )

    top_keyword = lists["keyword"][0][0] if lists["keyword"] else None
    result.answers["keyword_answer"] = _answer(
        case, Decision.ANSWERED.value if top_keyword else Decision.NO_MATCH.value, top_keyword
    )
    top_semantic = lists["semantic"][0] if lists["semantic"] else None
    semantic_answers = top_semantic is not None and meets_threshold(top_semantic[1], threshold)
    result.answers["semantic_answer"] = _answer(
        case,
        Decision.ANSWERED.value if semantic_answers else Decision.NO_MATCH.value,
        top_semantic[0] if semantic_answers and top_semantic else None,
    )
    result.answers["decision"] = _answer(
        case, decision.decision.value, decision.faq.id if decision.faq else None
    )
    return result


def _fraction(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "n": numerator,
        "of": denominator,
        "rate": numerator / denominator if denominator else None,
    }


def summarize(results: Sequence[CaseResult]) -> dict[str, Any]:
    eligible = [r for r in results if r.in_scope]
    out_of_scope = [r for r in results if "no_match" in r.expected_decision and not r.in_scope]
    expect_answer = [r for r in results if r.expected_decision == ["answered"]]

    ranking = {}
    for method in METHODS:
        rows = [r.rankings[method] for r in eligible]
        ranking[method] = {
            "top1_accuracy": _fraction(sum(x.rank == 1 for x in rows), len(rows)),
            f"recall_at_{RECALL_K}": _fraction(
                sum(x.rank is not None and x.rank <= RECALL_K for x in rows), len(rows)
            ),
            "mrr_at_10": {
                "value": sum(1 / x.rank for x in rows if x.rank) / len(rows) if rows else None,
                "of": len(rows),
            },
            "no_results": sum(not x.ranked for x in rows),
            "latency_ms": latency_stats([r.rankings[method].latency_ms for r in results]),
        }

    answering = {}
    for policy in POLICIES:
        answering[policy] = {
            "out_of_scope_rejection_rate": _fraction(
                sum(r.answers[policy].declined for r in out_of_scope), len(out_of_scope)
            ),
            "false_answer_rate": _fraction(
                sum(r.answers[policy].false_answer for r in results), len(results)
            ),
            "correct_answer_rate": _fraction(
                sum(r.answers[policy].correct_answer for r in expect_answer), len(expect_answer)
            ),
            "decision_accuracy": _fraction(
                sum(r.answers[policy].decision_correct for r in results), len(results)
            ),
        }
    answering["decision"]["latency_ms"] = latency_stats([r.decision_latency_ms for r in results])

    def top1(method: str) -> list[bool]:
        return [r.rankings[method].rank == 1 for r in eligible]

    by_subcategory: dict[str, dict[str, Any]] = {}
    for result in results:
        row = by_subcategory.setdefault(
            result.subcategory,
            {
                "cases": 0,
                **{f"{m}_top1": 0 for m in METHODS},
                "decision_correct": 0,
                "false_answers": 0,
            },
        )
        row["cases"] += 1
        for method in METHODS:
            row[f"{method}_top1"] += result.rankings[method].rank == 1
        row["decision_correct"] += result.answers["decision"].decision_correct
        row["false_answers"] += result.answers["decision"].false_answer

    return {
        "cases": len(results),
        "denominators": {
            "eligible_in_scope": len(eligible),
            "out_of_scope": len(out_of_scope),
            "expected_answered": len(expect_answer),
            "all": len(results),
        },
        "ranking": ranking,
        "answering": answering,
        "paired_top1": {
            "hybrid_vs_keyword": paired(top1("hybrid"), top1("keyword")),
            "hybrid_vs_semantic": paired(top1("hybrid"), top1("semantic")),
            "semantic_vs_keyword": paired(top1("semantic"), top1("keyword")),
        },
        "by_subcategory": dict(sorted(by_subcategory.items())),
    }


def _faq_service(
    faq_path: Path, database_path: Path | None, folder: Path
) -> tuple[FAQService, str, Database]:
    """FAQs through the real storage layer, from a seeded or copied temporary database."""
    target = folder / "faqs.db"
    if database_path is not None:
        shutil.copyfile(database_path, target)  # the original is never opened
        source = f"copy of {database_path.name}"
    else:
        source = f"temporary database seeded from {faq_path.name}"
    database = Database(f"sqlite:///{target.as_posix()}")
    database.init_schema()
    repository = DatabaseFAQRepository(database)
    if database_path is None:
        repository.import_faqs(FAQRepository.from_json_file(faq_path).list_all())
    repository.reload()
    return FAQService(repository), source, database


def evaluate(
    dataset_path: Path = DEFAULT_DATASET,
    *,
    faq_path: Path = DEFAULT_FAQ_DATA,
    database_path: Path | None = None,
    encoder: str = "model",
    embeddings: EmbeddingService | None = None,
) -> dict[str, Any]:
    settings = Settings(_env_file=None)
    cases = load_retrieval_cases(dataset_path)
    with tempfile.TemporaryDirectory(prefix="retrieval-eval-") as folder:
        faq_service, faq_source, database = _faq_service(faq_path, database_path, Path(folder))
        active = {faq.id for faq in faq_service.list_active_faqs()}
        if problems := check_labels(cases, active):
            raise ValueError("Invalid labels:\n  " + "\n  ".join(problems))
        embeddings = embeddings or embedding_service(encoder, settings.embedding_model_name)
        semantic = SemanticSearchService(faq_service.list_active_faqs, embeddings)
        search = build_search_services(
            settings, faq_service.list_active_faqs, semantic, catalog=faq_service
        )
        started = time.perf_counter()
        semantic.build_index()
        search.decision.decide("warm-up query")
        warmup = time.perf_counter() - started
        results = [run_case(c, semantic, search, settings.answer_min_similarity) for c in cases]
        catalogue = {"faqs": len(faq_service.repository), "active": len(active)}
        database.dispose()  # Windows cannot delete an open database file
    return {
        "meta": {
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "dataset": dataset_path.name,
            "dataset_sha256": file_sha256(dataset_path),
            "faq_source": faq_source,
            "faq_data_sha256": file_sha256(faq_path) if database_path is None else None,
            "catalogue": catalogue,
            "embedding_model": embeddings.model_name,
            "encoder": encoder,
            "rank_depth": RANK_DEPTH,
            "settings": {
                name: getattr(settings, name)
                for name in (
                    "answer_min_similarity",
                    "hybrid_rrf_k",
                    "hybrid_candidate_depth",
                    "answer_candidates",
                    "answer_ambiguity_margin",
                    "answer_accept_exact_question",
                )
            },  # fmt: skip
            "warmup_seconds": round(warmup, 2),
            "environment": environment_info(),
        },
        "metrics": summarize(results),
        "results": [asdict(r) for r in results],
    }


def _pct(fraction: dict[str, Any]) -> str:
    rate = fraction["rate"]
    return f"{fraction['n']}/{fraction['of']} ({rate:.1%})" if rate is not None else "n/a"


def format_report(report: dict[str, Any]) -> str:
    meta, metrics = report["meta"], report["metrics"]
    d = metrics["denominators"]
    lines = [
        f"Retrieval evaluation, {meta['created_at']}",
        f"Dataset {meta['dataset']} ({metrics['cases']} retrieval cases)",
        f"FAQs: {meta['faq_source']} ({meta['catalogue']['active']} of "
        f"{meta['catalogue']['faqs']} active); model {meta['embedding_model']}",
        f"Denominators: eligible (in scope) {d['eligible_in_scope']}, out of scope "
        f"{d['out_of_scope']}, expected answered {d['expected_answered']}, all {d['all']}",
        "",
        "Ranking (eligible cases)      Top-1               Recall@3            MRR@10",
    ]
    for method in METHODS:
        row = metrics["ranking"][method]
        lines.append(
            f"  {method:<27} {_pct(row['top1_accuracy']):<19} "
            f"{_pct(row['recall_at_3']):<19} {row['mrr_at_10']['value']:.3f}"
        )
    lines += [
        "",
        "Answering           OOS rejection       False answers       Correct answers     "
        "Decision accuracy",
    ]
    for policy in POLICIES:
        row = metrics["answering"][policy]
        lines.append(
            f"  {policy:<17} {_pct(row['out_of_scope_rejection_rate']):<19} "
            f"{_pct(row['false_answer_rate']):<19} {_pct(row['correct_answer_rate']):<19} "
            f"{_pct(row['decision_accuracy'])}"
        )
    lines += ["", "Paired Top-1 comparisons (exact sign test):"]
    for name, result in metrics["paired_top1"].items():
        lines.append(
            f"  {name:<22} first-only {result['wins']}, second-only "
            f"{result['losses']}, p = {result['p_value']:.3f}"
        )
    lines += [
        "",
        "By subcategory (cases; Top-1 keyword/semantic/hybrid; decision correct; false answers):",
    ]
    for name, row in metrics["by_subcategory"].items():
        lines.append(
            f"  {name:<20} {row['cases']:>3}; {row['keyword_top1']}/{row['semantic_top1']}/"
            f"{row['hybrid_top1']}; {row['decision_correct']}; {row['false_answers']}"
        )
    lines += [
        "",
        "Scores are each method's own units: cosine similarity and RRF scores are not "
        "probabilities or confidence. Small differences on this many cases are usually not "
        "significant (see the p-values).",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--faq-data", type=Path, default=DEFAULT_FAQ_DATA)
    parser.add_argument("--database", type=Path, help="evaluate a COPY of this SQLite file")
    parser.add_argument("--encoder", choices=("model", "lexical"), default="model")
    parser.add_argument("--output", type=Path, help="also write the full report as JSON")
    args = parser.parse_args(argv)
    report = evaluate(
        args.dataset, faq_path=args.faq_data, database_path=args.database, encoder=args.encoder
    )
    print(format_report(report))
    if args.output:
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nFull report written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
