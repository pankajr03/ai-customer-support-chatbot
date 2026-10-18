"""Run the unified AI evaluation set (Stage 12, Task 12): ``data/ai_evaluation.json``.

Usage (from the project root, virtual environment active):

    python scripts/evaluate_ai.py
    python scripts/evaluate_ai.py --output data/ai_evaluation_results.json
    python scripts/evaluate_ai.py --category grounding --category safety
    python scripts/evaluate_ai.py --encoder lexical            # no PyTorch (tests)

Each case names its ``evaluation_method`` (see the dataset's ``evaluation_methods``):

- ``retrieval_ranking`` cases go to ``scripts/evaluate_retrieval.py`` (ranking metrics
  for keyword, semantic and hybrid search; a case passes when the answer decision is
  one of the expected decisions and, when answered, cites an acceptable FAQ);
- every other method runs the case through the real application
  (``scripts/ai_eval_harness.py``): chat API, conversation storage, rewriting, retrieval,
  prompt, parsing, grounding checks and fallbacks, with a scripted model. ``chat_flow``
  uses an honest model and deterministic follow-up rewriting; ``compromised_model`` a
  model that makes the case's unsupported claim; ``llm_failure`` a failing model;
  ``faq_change`` a staff change before or during the answer.

What a pass means
-----------------
Every check is deterministic: decisions, cited FAQ IDs (from the real catalogue; labels
naming a missing or inactive FAQ are rejected before the run), forbidden and required
phrases, and the invariants of ``ai_eval_harness.invariant_problems``. Phrase checks are
not a judgement of factual correctness: a reply can avoid every forbidden phrase and
still be wrong. Cases marked ``human_review`` (several FAQs combined, or generated text)
are listed for a person to read; with ``--llm live`` every generated answer should be.

Nothing here is a guarantee: the set is small, written by the developers, and the
scripted model shows what the application's controls do, not how often a real model
misbehaves.
"""

import argparse
import json
import sys
import time
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/evaluate_ai.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import BaseModel, ConfigDict, Field, model_validator  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.database.faq_repository import FAQRepository  # noqa: E402
from app.services.embedding_service import EmbeddingService  # noqa: E402
from scripts import evaluate_retrieval  # noqa: E402
from scripts.ai_eval_harness import (  # noqa: E402
    DECISIONS,
    DEFAULT_FAQ_DATA,
    GENERATION_METHODS,
    Expect,
    ScriptedCase,
    Setup,
    Turn,
    embedding_service,
    eval_app,
    run_scripted_case,
)
from scripts.evaluate_semantic_search import environment_info, file_sha256  # noqa: E402

DEFAULT_DATASET = PROJECT_ROOT / "data" / "ai_evaluation.json"
MIN_CASES = 100
Category = Literal["retrieval", "conversation", "grounding", "safety"]
Method = Literal["retrieval_ranking", "chat_flow", "compromised_model", "llm_failure", "faq_change"]
CAVEAT = (
    "Deterministic checks on a small, developer-written set with a scripted model. Phrase "
    "checks are not proof of factual correctness; read the human-review cases."
)


class AICase(BaseModel):
    """One case of the unified set (see the dataset's description for each field)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^(RET|CONV|GRD|SAF)-\d{3}$")
    category: Category
    subcategory: str = Field(min_length=1)
    input: str = Field(min_length=1)
    history: list[str] = Field(default_factory=list)
    category_filter: str | None = None
    expected_decision: list[str] = Field(min_length=1)
    expected_faq_ids: list[str] = Field(default_factory=list)
    excluded_faq_ids: list[str] = Field(default_factory=list)
    expected_generation: list[str] | None = None
    expected_safety: str = Field(min_length=1)
    evaluation_method: Method
    source: str | None = None
    notes: str | None = None
    model_reply: dict[str, Any] | str | None = None
    rewrite_reply: dict[str, Any] | None = None
    setup: dict[str, Any] | None = None
    during_answer: dict[str, Any] | None = None
    must_contain: list[str] = Field(default_factory=list)
    must_not_contain: list[str] = Field(default_factory=list)
    model_input_must_not_contain: list[str] = Field(default_factory=list)
    answer_input_must_not_contain: list[str] = Field(default_factory=list)
    human_review: bool = False
    known_limitation: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "AICase":
        if unknown := set(self.expected_decision) - set(DECISIONS):
            raise ValueError(f"{self.id}: unknown decisions {sorted(unknown)}")
        if self.expected_generation and set(self.expected_generation) - set(GENERATION_METHODS):
            raise ValueError(f"{self.id}: unknown generation methods")
        prefix = {"retrieval": "RET", "conversation": "CONV", "grounding": "GRD", "safety": "SAF"}
        if not self.id.startswith(prefix[self.category]):
            raise ValueError(f"{self.id}: id prefix does not match category {self.category}")
        if (self.evaluation_method == "retrieval_ranking") != (self.category == "retrieval"):
            raise ValueError(f"{self.id}: retrieval cases, and only they, use retrieval_ranking")
        if (
            self.evaluation_method == "compromised_model"
            and self.model_reply is None
            and not self.rewrite_reply
        ):
            raise ValueError(f"{self.id}: a compromised_model case needs a model reply")
        return self

    def scripted(self) -> ScriptedCase:
        """This case as a harness case: history turns, then the input."""
        model: dict[str, Any] = {}
        if self.model_reply is not None:
            model["answer"] = self.model_reply
        if self.rewrite_reply is not None:
            model["rewrite"] = self.rewrite_reply
        turns = [Turn(message=message) for message in self.history]
        turns.append(Turn(message=self.input, model=model, during_answer=self.during_answer))
        answer_only = set(self.expected_decision) <= {"no_match", "retrieval_unavailable"}
        expect = Expect(
            decision_in=self.expected_decision,
            generation_method_in=self.expected_generation or list(GENERATION_METHODS),
            source_faq_ids_any=self.expected_faq_ids or ([] if answer_only else None),
            source_faq_ids_none=self.excluded_faq_ids,
            must_contain=self.must_contain,
        )
        return ScriptedCase(
            id=self.id,
            turns=turns,
            setup=Setup(**(self.setup or {})),
            expect=expect,
            must_not_contain=self.must_not_contain,
            model_input_must_not_contain=self.model_input_must_not_contain,
            answer_input_must_not_contain=self.answer_input_must_not_contain,
            known_limitation=self.known_limitation,
        )


class AIDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    description: str
    faq_data: str
    evaluation_methods: dict[str, str]
    cases: list[AICase] = Field(min_length=MIN_CASES)

    @model_validator(mode="after")
    def _unique(self) -> "AIDataset":
        ids = [case.id for case in self.cases]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate case ids")
        keys = [
            json.dumps(
                case.model_dump(exclude={"id", "notes", "source", "expected_safety"}),
                sort_keys=True,
            )
            for case in self.cases
        ]
        if len(set(keys)) != len(keys):
            raise ValueError("two cases are identical apart from their IDs")
        return self


def load_dataset(path: Path = DEFAULT_DATASET) -> AIDataset:
    return AIDataset.model_validate_json(path.read_text(encoding="utf-8"))


def check_labels(dataset: AIDataset, faq_path: Path) -> list[str]:
    """Expected FAQ IDs must be real and active; excluded IDs real or deliberately fake."""
    faqs = {faq.id: faq for faq in FAQRepository.from_json_file(faq_path).list_all()}
    problems = []
    for case in dataset.cases:
        for faq_id in case.expected_faq_ids:
            if faq_id not in faqs:
                problems.append(f"{case.id}: expected FAQ {faq_id} does not exist")
            elif not faqs[faq_id].is_active:
                problems.append(f"{case.id}: expected FAQ {faq_id} is inactive")
    return problems


def _rewrite_mode(case: AICase) -> dict[str, Any]:
    # Compromised cases keep LLM rewriting on (the scripted model answers it, "unchanged"
    # unless the case says otherwise); the others use deterministic follow-up rewriting.
    return {"query_rewrite_use_llm": case.evaluation_method == "compromised_model"}


def evaluate(
    dataset_path: Path = DEFAULT_DATASET,
    *,
    faq_path: Path = DEFAULT_FAQ_DATA,
    encoder: str = "model",
    categories: Sequence[str] = (),
    embeddings: EmbeddingService | None = None,
) -> dict[str, Any]:
    dataset = load_dataset(dataset_path)
    if problems := check_labels(dataset, faq_path):
        raise ValueError("Invalid labels:\n  " + "\n  ".join(problems))
    cases = [c for c in dataset.cases if not categories or c.category in categories]
    embeddings = embeddings or embedding_service(
        encoder, Settings(_env_file=None).embedding_model_name
    )
    started = time.perf_counter()

    outcomes: list[dict[str, Any]] = []
    retrieval_report = None
    if any(c.category == "retrieval" for c in cases):
        retrieval_report = evaluate_retrieval.evaluate(
            dataset_path, faq_path=faq_path, encoder=encoder, embeddings=embeddings
        )
        for result in retrieval_report["results"]:
            answer = result["answers"]["decision"]
            outcomes.append(
                {
                    "case_id": result["case_id"],
                    "passed": answer["decision_correct"],
                    "problems": []
                    if answer["decision_correct"]
                    else [
                        f"decision {answer['decision']} ({answer['faq_id']}) not in "
                        f"{result['expected_decision']} with {result['expected_faq_ids']}"
                    ],
                    "decision": answer["decision"],
                    "sources": [answer["faq_id"]] if answer["faq_id"] else [],
                    "known_limitation": False,
                }
            )
    for case in cases:
        if case.category == "retrieval":
            continue
        with eval_app(embeddings, faq_path=faq_path, **_rewrite_mode(case)) as ev:
            outcomes.append(run_scripted_case(ev, case.scripted()).as_dict())

    by_id = {c.id: c for c in cases}
    regular = [o for o in outcomes if not o["known_limitation"]]

    def table(key: str) -> dict[str, dict[str, int]]:
        counts: dict[str, Counter[str]] = {}
        for outcome in regular:
            value = getattr(by_id[outcome["case_id"]], key)
            counts.setdefault(value, Counter())["cases"] += 1
            counts[value]["passed"] += outcome["passed"]
        return {name: dict(c) for name, c in sorted(counts.items())}

    return {
        "meta": {
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "dataset": dataset_path.name,
            "dataset_version": dataset.version,
            "dataset_sha256": file_sha256(dataset_path),
            "faq_data": faq_path.name,
            "faq_data_sha256": file_sha256(faq_path),
            "encoder": encoder,
            "embedding_model": embeddings.model_name,
            "seconds": round(time.perf_counter() - started, 1),
            "environment": environment_info(),
            "caveat": CAVEAT,
        },
        "summary": {
            "cases": len(outcomes),
            "passed": sum(o["passed"] for o in regular),
            "regular_cases": len(regular),
            "by_category": table("category"),
            "by_method": table("evaluation_method"),
            "failed": [o["case_id"] for o in regular if not o["passed"]],
            "known_limitations": {
                "held": [o["case_id"] for o in outcomes if o["known_limitation"] and o["passed"]],
                "confirmed": [
                    o["case_id"] for o in outcomes if o["known_limitation"] and not o["passed"]
                ],
            },
            "human_review": [c.id for c in cases if c.human_review],
        },
        "retrieval_metrics": retrieval_report["metrics"] if retrieval_report else None,
        "results": outcomes,
    }


def format_report(report: dict[str, Any]) -> str:
    meta, summary = report["meta"], report["summary"]
    lines = [
        f"AI evaluation ({meta['dataset']} v{meta['dataset_version']}, encoder {meta['encoder']}), "
        f"{meta['created_at']}",
        f"Passed {summary['passed']}/{summary['regular_cases']} regular cases "
        f"({summary['cases']} in total)",
        "",
        "By category:",
    ]
    for name, row in summary["by_category"].items():
        lines.append(f"  {name:<14} {row.get('passed', 0)}/{row['cases']}")
    lines.append("By evaluation method:")
    for name, row in summary["by_method"].items():
        lines.append(f"  {name:<18} {row.get('passed', 0)}/{row['cases']}")
    limits = summary["known_limitations"]
    lines += [
        f"Known limitations: held {limits['held'] or 'none'}, "
        f"confirmed {limits['confirmed'] or 'none'}",
        f"For human review: {', '.join(summary['human_review']) or 'none'}",
    ]
    if report["retrieval_metrics"]:
        ranking = report["retrieval_metrics"]["ranking"]
        lines += ["", "Retrieval ranking (details: scripts/evaluate_retrieval.py):"]
        for method, row in ranking.items():
            lines.append(
                f"  {method:<9} Top-1 {row['top1_accuracy']['rate']:.1%}, "
                f"Recall@3 {row['recall_at_3']['rate']:.1%}, MRR@10 {row['mrr_at_10']['value']:.3f}"
            )
    failed = [r for r in report["results"] if r["problems"]]
    if failed:
        lines += ["", "Cases with problems:"]
        for result in failed:
            tag = " (known limitation)" if result["known_limitation"] else ""
            lines.append(f"  {result['case_id']}{tag}: {'; '.join(result['problems'])}")
    lines += ["", meta["caveat"]]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--faq-data", type=Path, default=DEFAULT_FAQ_DATA)
    parser.add_argument("--encoder", choices=("model", "lexical"), default="model")
    parser.add_argument(
        "--category",
        action="append",
        default=[],
        choices=("retrieval", "conversation", "grounding", "safety"),
    )
    parser.add_argument("--output", type=Path, help="also write the full report as JSON")
    args = parser.parse_args(argv)
    report = evaluate(
        args.dataset, faq_path=args.faq_data, encoder=args.encoder, categories=args.category
    )
    print(format_report(report))
    if args.output:
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nFull report written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
