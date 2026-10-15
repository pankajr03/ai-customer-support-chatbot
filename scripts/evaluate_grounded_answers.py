"""Evaluate grounded answers (POST /api/v1/answer behaviour) on a labelled question set.

Usage (from the project root, virtual environment active):

    python scripts/evaluate_grounded_answers.py                     # LLM off: free, deterministic
    python scripts/evaluate_grounded_answers.py --offline --output grounded_eval.json
    python scripts/evaluate_grounded_answers.py --llm live --yes    # PAID: one request per answer

Modes
-----
- ``--llm off`` (default): the full pipeline with LLM generation disabled, exactly what
  customers get with ``LLM_ENABLED=false``. Answered questions return the approved FAQ
  answer verbatim. Deterministic and free; it measures retrieval, decisions, fallbacks
  and the safety of the approved answers.
- ``--llm live``: the same pipeline with your OpenAI settings (environment / .env),
  ``LLM_ENABLED`` forced on. Makes one paid request per question that Stage 4 answers;
  requires ``--yes``. Results describe this run of this model and configuration only:
  a handful of good answers does not prove the model is reliable, and repeated runs can
  differ.

Metrics (denominators are printed with every value)
---------------------------------------------------
- Outcome correctness (all cases): the outcome (answer / clarification / fallback) is
  one of the case's acceptable outcomes. This is the refusal/fallback correctness.
- Fallback correctness: cases whose only acceptable outcome is a fallback.
- Clarification correctness: cases whose only acceptable outcome is a clarification.
- Answer relevance (answers given where an answer is acceptable): every ``must_include``
  group has a phrase in the answer. A coarse, keyword-based proxy for relevance.
- Source correctness (same subset): the sources include an acceptable FAQ.
- Grounding compliance (all answers given): the final answer passes the grounding
  checks against its cited FAQs (``app/services/grounding_validator.py``).
- Unsupported claims (all cases): the reply contains a ``must_not_include`` phrase.
- Latency: end-to-end per question, and the LLM call where one was made.
- Tokens and estimated cost: from the API's usage metadata, and only when a price is
  configured (``LLM_PRICING``); otherwise reported as not configured.

These are automated, partly keyword-based checks. They catch clear failures; they do not
replace reading the answers (all answers are in the ``--output`` JSON).
"""

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/evaluate_grounded_answers.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import BaseModel, ConfigDict, Field, model_validator  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.database.faq_repository import FAQRepository, resolve_data_path  # noqa: E402
from app.models.answer import GenerationMethod  # noqa: E402
from app.models.faq import FAQ, Category  # noqa: E402
from app.prompts.customer_support import PROMPT_VERSION  # noqa: E402
from app.services.context_builder import ContextBuilder  # noqa: E402
from app.services.embedding_service import EmbeddingService  # noqa: E402
from app.services.faq_service import FAQService  # noqa: E402
from app.services.grounded_answer_service import GroundedAnswerService  # noqa: E402
from app.services.grounding_validator import check_grounding  # noqa: E402
from app.services.llm_service import LLMService  # noqa: E402
from app.services.search_pipeline import build_search_services  # noqa: E402
from app.services.semantic_search_service import SemanticSearchService  # noqa: E402
from app.services.usage_tracker import UsageTracker  # noqa: E402
from scripts.evaluate_hybrid_search import latency_stats  # noqa: E402
from scripts.evaluate_semantic_search import environment_info, file_sha256  # noqa: E402

DEFAULT_DATASET = PROJECT_ROOT / "data" / "grounded_answer_eval.json"
MAX_FAILURE_EXAMPLES = 8
CAVEAT = (
    "Results describe one run of one model and configuration on a small, hand-written set. "
    "They do not prove that the model is reliable; repeated runs can differ."
)


# --- Dataset ----------------------------------------------------------------------------------


class Outcome(StrEnum):
    ANSWER = "answer"
    CLARIFICATION = "clarification"
    FALLBACK = "fallback"


class CaseType(StrEnum):
    STRAIGHTFORWARD = "straightforward"
    PARAPHRASE = "paraphrase"
    MULTI_FAQ = "multi_faq"
    AMBIGUOUS = "ambiguous"
    OUT_OF_SCOPE = "out_of_scope"
    UNSUPPORTED_POLICY = "unsupported_policy"
    INSTRUCTION_OVERRIDE = "instruction_override"
    MISSING_INFORMATION = "missing_information"
    MISLEADING_NUMBERS = "misleading_numbers"
    RESTRICTION = "restriction"


class GroundedCase(BaseModel):
    """One labelled question and the behaviour that counts as correct."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^ga_\d{3}$")
    type: CaseType
    query: str = Field(min_length=1)
    category_filter: Category | None = None
    acceptable_outcomes: list[Outcome] = Field(min_length=1)
    acceptable_source_ids: list[str] = Field(default_factory=list)
    must_include: list[list[str]] = Field(default_factory=list)
    must_not_include: list[str] = Field(default_factory=list)
    explanation: str = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if len(set(self.acceptable_outcomes)) != len(self.acceptable_outcomes):
            raise ValueError(f"{self.id}: duplicate acceptable outcomes")
        if Outcome.ANSWER in self.acceptable_outcomes and not self.acceptable_source_ids:
            raise ValueError(f"{self.id}: an answer needs acceptable_source_ids")
        if self.must_include and Outcome.ANSWER not in self.acceptable_outcomes:
            raise ValueError(f"{self.id}: must_include only applies when an answer is acceptable")
        for group in self.must_include:
            if not group or any(not phrase.strip() for phrase in group):
                raise ValueError(f"{self.id}: must_include groups need non-empty phrases")
        if any(not phrase.strip() for phrase in self.must_not_include):
            raise ValueError(f"{self.id}: must_not_include phrases must not be empty")
        return self

    @property
    def answer_expected(self) -> bool:
        return Outcome.ANSWER in self.acceptable_outcomes


class GroundedDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = ""
    faq_data: str = "data/faqs.json"
    cases: list[GroundedCase] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ids = [case.id for case in self.cases]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate case ids")
        queries = [case.query.casefold() for case in self.cases]
        if len(set(queries)) != len(queries):
            raise ValueError("duplicate queries")
        return self


def load_dataset(path: Path) -> GroundedDataset:
    return GroundedDataset.model_validate_json(path.read_text(encoding="utf-8"))


def contains_phrase(text: str, phrase: str) -> bool:
    """Case-insensitive, whole-word phrase match ("arr" does not match "arrive")."""
    normalised = " ".join(text.replace("’", "'").casefold().split())
    pattern = rf"(?<![\w]){re.escape(' '.join(phrase.casefold().split()))}(?![\w])"
    return re.search(pattern, normalised) is not None


def check_labels(dataset: GroundedDataset, faqs: Iterable[FAQ]) -> list[str]:
    """Label problems: unknown or inactive FAQs, and phrases that contradict the FAQs."""
    by_id = {faq.id: faq for faq in faqs}
    problems = []
    for case in dataset.cases:
        known = []
        for faq_id in case.acceptable_source_ids:
            faq = by_id.get(faq_id)
            if faq is None:
                problems.append(f"{case.id}: FAQ {faq_id} does not exist")
            elif not faq.is_active:
                problems.append(f"{case.id}: FAQ {faq_id} is inactive")
            else:
                known.append(faq.answer)
        for group in case.must_include:
            if known and not any(contains_phrase(t, p) for t in known for p in group):
                problems.append(f"{case.id}: no acceptable FAQ contains any of {group}")
        for phrase in case.must_not_include:
            if any(contains_phrase(text, phrase) for text in known):
                problems.append(f"{case.id}: forbidden phrase {phrase!r} is in an acceptable FAQ")
    return problems


# --- Per-case results -------------------------------------------------------------------------

_OUTCOMES = {
    GenerationMethod.LLM_GROUNDED: Outcome.ANSWER,
    GenerationMethod.APPROVED_FAQ: Outcome.ANSWER,
    GenerationMethod.CLARIFICATION: Outcome.CLARIFICATION,
    GenerationMethod.FALLBACK: Outcome.FALLBACK,
}


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    type: str
    query: str
    acceptable_outcomes: list[str]
    outcome: str
    generation_method: str
    decision: str
    source_faq_ids: list[str]
    answer: str
    outcome_ok: bool
    source_ok: bool | None  # None when no answer was given or none was acceptable
    include_ok: bool | None
    forbidden_hits: list[str]
    grounding_issues: list[str]  # issue kinds for answers given
    latency_ms: float
    llm_called: bool
    llm_latency_ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    estimated_cost: float | None
    fallback_reason: str | None
    llm_error_category: str | None


def score_case(
    case: GroundedCase,
    *,
    generation_method: GenerationMethod,
    decision: str,
    answer: str,
    source_faq_ids: Sequence[str],
    evidence: Sequence[str],
    primary: str | None,
    latency_ms: float,
    diagnostics: Any,
) -> CaseResult:
    """Score one answer against its labels. Pure: no I/O."""
    outcome = _OUTCOMES[generation_method]
    answered = outcome is Outcome.ANSWER
    gradable = answered and case.answer_expected
    acceptable = set(case.acceptable_source_ids)
    issues: list[str] = []
    if answered and primary is not None:
        issues = list(check_grounding(answer, evidence, primary).kinds)
    return CaseResult(
        case_id=case.id,
        type=case.type.value,
        query=case.query,
        acceptable_outcomes=[o.value for o in case.acceptable_outcomes],
        outcome=outcome.value,
        generation_method=generation_method.value,
        decision=decision,
        source_faq_ids=list(source_faq_ids),
        answer=answer,
        outcome_ok=outcome in case.acceptable_outcomes,
        source_ok=bool(acceptable & set(source_faq_ids)) if gradable else None,
        include_ok=(
            all(any(contains_phrase(answer, p) for p in group) for group in case.must_include)
            if gradable and case.must_include
            else None
        ),
        forbidden_hits=[p for p in case.must_not_include if contains_phrase(answer, p)],
        grounding_issues=issues,
        latency_ms=latency_ms,
        llm_called=bool(diagnostics.llm_called),
        llm_latency_ms=diagnostics.latency_ms,
        input_tokens=diagnostics.input_tokens,
        output_tokens=diagnostics.output_tokens,
        total_tokens=diagnostics.total_tokens,
        estimated_cost=diagnostics.estimated_cost,
        fallback_reason=(
            str(getattr(diagnostics.fallback_reason, "value", diagnostics.fallback_reason))
            if diagnostics.fallback_reason
            else None
        ),
        llm_error_category=diagnostics.llm_error_category,
    )


def run_case(case: GroundedCase, service: GroundedAnswerService) -> CaseResult:
    from app.models.answer import AnswerResponse

    started = time.perf_counter()
    grounded = service.answer(case.query, category=case.category_filter)
    latency_ms = (time.perf_counter() - started) * 1000
    response = AnswerResponse.from_result(case.query, grounded.decision.decision, grounded.result)
    records = {r.faq_id: r for r in grounded.context.records}
    cited = [records[i] for i in response.source_faq_ids if i in records]
    return score_case(
        case,
        generation_method=response.generation_method,
        decision=response.decision.value,
        answer=response.answer,
        source_faq_ids=response.source_faq_ids,
        evidence=[text for r in cited for text in (r.question, r.answer)],
        primary=grounded.decision.faq.answer if grounded.decision.faq else None,
        latency_ms=latency_ms,
        diagnostics=grounded.result.diagnostics,
    )


# --- Metrics -----------------------------------------------------------------------------------


def _fraction(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "n": numerator,
        "of": denominator,
        "rate": numerator / denominator if denominator else None,
    }


def summarize(results: Sequence[CaseResult]) -> dict[str, Any]:
    answers = [r for r in results if r.outcome == Outcome.ANSWER]
    gradable = [r for r in answers if Outcome.ANSWER.value in r.acceptable_outcomes]
    relevance = [r for r in gradable if r.include_ok is not None]
    fallback_only = [r for r in results if r.acceptable_outcomes == [Outcome.FALLBACK.value]]
    clarify_only = [r for r in results if r.acceptable_outcomes == [Outcome.CLARIFICATION.value]]
    answer_only = [r for r in results if r.acceptable_outcomes == [Outcome.ANSWER.value]]
    generated = [r for r in results if r.generation_method == GenerationMethod.LLM_GROUNDED]
    llm_calls = [r for r in results if r.llm_called]
    reported = [r for r in llm_calls if r.total_tokens is not None or r.input_tokens is not None]
    priced = [r for r in llm_calls if r.estimated_cost is not None]

    def token_sum(field: str) -> int | None:
        values = [getattr(r, field) for r in llm_calls if getattr(r, field) is not None]
        return sum(values) if values else None

    return {
        "cases": len(results),
        "outcome_correctness": _fraction(sum(r.outcome_ok for r in results), len(results)),
        "fallback_correctness": _fraction(
            sum(r.outcome == Outcome.FALLBACK for r in fallback_only), len(fallback_only)
        ),
        "false_answers_on_fallback_cases": sum(r.outcome == Outcome.ANSWER for r in fallback_only),
        "clarification_correctness": _fraction(
            sum(r.outcome == Outcome.CLARIFICATION for r in clarify_only), len(clarify_only)
        ),
        "missed_answers": _fraction(
            sum(r.outcome != Outcome.ANSWER for r in answer_only), len(answer_only)
        ),
        "answer_relevance": _fraction(sum(bool(r.include_ok) for r in relevance), len(relevance)),
        "source_correctness": _fraction(sum(bool(r.source_ok) for r in gradable), len(gradable)),
        "grounding_compliance": _fraction(
            sum(not r.grounding_issues for r in answers), len(answers)
        ),
        "grounding_compliance_generated": _fraction(
            sum(not r.grounding_issues for r in generated), len(generated)
        ),
        "unsupported_claims": _fraction(sum(bool(r.forbidden_hits) for r in results), len(results)),
        "generation_methods": dict(Counter(r.generation_method for r in results)),
        "fallback_reasons": dict(Counter(r.fallback_reason for r in results if r.fallback_reason)),
        "llm_error_categories": dict(
            Counter(r.llm_error_category for r in results if r.llm_error_category)
        ),
        "latency_ms": latency_stats([r.latency_ms for r in results]),
        "llm_latency_ms": latency_stats(
            [r.llm_latency_ms for r in llm_calls if r.llm_latency_ms is not None]
        ),
        "tokens": {
            "llm_calls": len(llm_calls),
            "calls_with_usage": len(reported),
            "input": token_sum("input_tokens"),
            "output": token_sum("output_tokens"),
            "total": token_sum("total_tokens"),
            "mean_total_per_call": (
                token_sum("total_tokens") / len(reported)  # type: ignore[operator]
                if reported and token_sum("total_tokens") is not None
                else None
            ),
        },
        "estimated_cost": {
            "priced_calls": len(priced),
            "total": sum(r.estimated_cost for r in priced) if priced else None,  # type: ignore[misc]
            "mean_per_call": (
                sum(r.estimated_cost for r in priced) / len(priced) if priced else None  # type: ignore[misc]
            ),
        },
        "by_type": {
            case_type.value: {
                "cases": len(rows),
                "outcome_ok": sum(r.outcome_ok for r in rows),
                "unsupported_claims": sum(bool(r.forbidden_hits) for r in rows),
                "generated": sum(
                    r.generation_method == GenerationMethod.LLM_GROUNDED for r in rows
                ),
            }
            for case_type in CaseType
            if (rows := [r for r in results if r.type == case_type.value])
        },
    }


# --- Running ----------------------------------------------------------------------------------


def build_service(
    settings: Settings,
    faq_service: FAQService,
    embedding_factory: Callable[[str], EmbeddingService],
    llm: LLMService | None = None,
) -> tuple[GroundedAnswerService, SemanticSearchService, UsageTracker]:
    semantic = SemanticSearchService(
        faq_service.list_active_faqs, embedding_factory(settings.embedding_model_name)
    )
    search = build_search_services(settings, faq_service.list_active_faqs, semantic)
    tracker = UsageTracker.from_settings(settings)
    llm = llm or LLMService.from_settings(settings, usage_tracker=tracker)
    service = GroundedAnswerService(search.decision, ContextBuilder.from_settings(settings), llm)
    return service, semantic, tracker


def evaluate(
    dataset_path: Path,
    faq_path: Path,
    settings: Settings,
    *,
    mode: str = "off",
    max_cases: int | None = None,
    embedding_factory: Callable[[str], EmbeddingService] = EmbeddingService,
    llm: LLMService | None = None,
) -> dict[str, Any]:
    """Run the evaluation and return a JSON-serialisable report."""
    dataset = load_dataset(dataset_path)
    repository = FAQRepository.from_json_file(faq_path)
    if problems := check_labels(dataset, repository.list_all()):
        raise ValueError("Invalid evaluation labels:\n  " + "\n  ".join(problems))
    cases = dataset.cases[:max_cases] if max_cases else dataset.cases

    faq_service = FAQService(repository)
    service, semantic, _ = build_service(settings, faq_service, embedding_factory, llm)
    started = time.perf_counter()
    semantic.build_index()  # load the embedding model before timing questions
    warmup_seconds = time.perf_counter() - started

    results = [run_case(case, service) for case in cases]
    return {
        "meta": {
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "mode": mode,
            "sample_size": len(results),
            "caveat": CAVEAT,
            "llm": {
                "enabled": mode == "live",
                "model": settings.openai_model if mode == "live" else None,
                "prompt_version": PROMPT_VERSION,
                "reasoning_effort": settings.openai_reasoning_effort,
                "temperature": settings.llm_temperature,
                "max_output_tokens": settings.openai_max_output_tokens,
                "timeout_seconds": settings.openai_timeout_seconds,
                "max_retries": settings.openai_max_retries,
                "context_max_faqs": settings.llm_context_max_faqs,
                "context_max_chars": settings.llm_context_max_chars,
                "priced_models": sorted(settings.llm_pricing),
                "pricing_currency": settings.llm_pricing_currency,
            },
            "decision": {
                "answer_min_similarity": settings.answer_min_similarity,
                "answer_ambiguity_margin": settings.answer_ambiguity_margin,
                "answer_candidates": settings.answer_candidates,
            },
            "embedding_model": settings.embedding_model_name,
            "dataset": _display_path(dataset_path),
            "dataset_sha256": file_sha256(dataset_path),
            "faq_data": _display_path(faq_path),
            "faq_data_sha256": file_sha256(faq_path),
            "warmup_seconds": warmup_seconds,
            "environment": environment_info(),
        },
        "metrics": summarize(results),
        "results": [asdict(r) for r in results],
    }


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


# --- Report ----------------------------------------------------------------------------------


def _frac(value: dict[str, Any]) -> str:
    if value["rate"] is None:
        return "n/a (0 cases)"
    return f"{value['rate'] * 100:5.1f}% ({value['n']}/{value['of']})"


def _ms(latency: dict[str, float | None]) -> str:
    if latency["mean"] is None:
        return "n/a"
    return (
        f"mean {latency['mean']:.0f} ms | median {latency['median']:.0f} ms | "
        f"p95 {latency['p95']:.0f} ms"
    )


def format_report(report: dict[str, Any]) -> str:
    meta, m = report["meta"], report["metrics"]
    llm, tokens, cost = meta["llm"], m["tokens"], m["estimated_cost"]
    currency = llm["pricing_currency"]
    lines = [
        "Grounded answer evaluation",
        "=" * 26,
        f"Mode: LLM {'LIVE (paid)' if llm['enabled'] else 'off (approved FAQ answers)'}   "
        f"sample size: {meta['sample_size']} cases",
        f"Model: {llm['model'] or '-'}   prompt: {llm['prompt_version']}   reasoning effort: "
        f"{llm['reasoning_effort']}   temperature: {llm['temperature']}   max output tokens: "
        f"{llm['max_output_tokens']}",
        f"Embedding model: {meta['embedding_model']}   decision: min similarity "
        f"{meta['decision']['answer_min_similarity']}, ambiguity margin "
        f"{meta['decision']['answer_ambiguity_margin']}",
        f"Dataset: {meta['dataset']} (sha256 {meta['dataset_sha256'][:16]}...)",
        f"Note: {meta['caveat']}",
        "",
        "Behaviour",
        f"  Outcome correctness (all cases):           {_frac(m['outcome_correctness'])}",
        f"  Fallback correctness (fallback-only cases): {_frac(m['fallback_correctness'])}"
        f"   answers given there: {m['false_answers_on_fallback_cases']}",
        f"  Clarification correctness (clarify-only):  {_frac(m['clarification_correctness'])}",
        f"  Missed answers (answer-only cases):        {_frac(m['missed_answers'])}",
        "Answer quality (answers given where an answer is acceptable)",
        f"  Answer relevance (must_include):           {_frac(m['answer_relevance'])}",
        f"  Source correctness:                        {_frac(m['source_correctness'])}",
        f"  Grounding compliance (all answers):        {_frac(m['grounding_compliance'])}",
        "  Grounding compliance (LLM-written):        "
        f"{_frac(m['grounding_compliance_generated'])}",
        f"  Unsupported claims (all cases):            {_frac(m['unsupported_claims'])}",
        "",
        "Generation methods: "
        + (", ".join(f"{k} {v}" for k, v in sorted(m["generation_methods"].items())) or "-"),
        "Fallback reasons:   "
        + (", ".join(f"{k} {v}" for k, v in sorted(m["fallback_reasons"].items())) or "-"),
        "LLM error categories: "
        + (", ".join(f"{k} {v}" for k, v in sorted(m["llm_error_categories"].items())) or "-"),
        "",
        "Latency",
        f"  End to end per question: {_ms(m['latency_ms'])}",
        f"  LLM call ({tokens['llm_calls']} calls):     {_ms(m['llm_latency_ms'])}",
        "Tokens (as reported by the API)",
        f"  calls with usage: {tokens['calls_with_usage']}/{tokens['llm_calls']}   input "
        f"{tokens['input'] or 0}   output {tokens['output'] or 0}   total {tokens['total'] or 0}"
        + (
            f"   mean per call {tokens['mean_total_per_call']:.0f}"
            if tokens["mean_total_per_call"] is not None
            else ""
        ),
        "Estimated cost: "
        + (
            f"{cost['total']:.6f} {currency} total, {cost['mean_per_call']:.6f} per call "
            f"({cost['priced_calls']} priced calls; estimate, not a bill)"
            if cost["total"] is not None
            else "not configured (set LLM_PRICING to estimate)"
        ),
        "",
        "By case type",
        f"  {'type':<22}{'cases':>6}{'outcome ok':>12}{'unsupported':>13}{'generated':>11}",
    ]
    for case_type, row in m["by_type"].items():
        lines.append(
            f"  {case_type:<22}{row['cases']:>6}{row['outcome_ok']:>12}"
            f"{row['unsupported_claims']:>13}{row['generated']:>11}"
        )

    results = report["results"]
    groups = [
        ("Wrong outcome", [r for r in results if not r["outcome_ok"]]),
        ("Unsupported claims", [r for r in results if r["forbidden_hits"]]),
        ("Answers missing required facts", [r for r in results if r["include_ok"] is False]),
        ("Answers with wrong sources", [r for r in results if r["source_ok"] is False]),
        ("Answers failing grounding checks", [r for r in results if r["grounding_issues"]]),
    ]
    lines += ["", "Failure examples"]
    for title, rows in groups:
        lines.append(f"  {title} ({len(rows)})")
        for r in rows[:MAX_FAILURE_EXAMPLES]:
            snippet = " ".join(r["answer"].split())[:90]
            lines.append(f"    {r['case_id']} ({r['type']}) {r['query']!r}")
            lines.append(
                f"        expected {'/'.join(r['acceptable_outcomes'])}, got {r['outcome']} "
                f"via {r['generation_method']} {r['source_faq_ids']} | {snippet}"
            )
            if r["forbidden_hits"] or r["grounding_issues"]:
                lines.append(
                    f"        forbidden: {r['forbidden_hits'] or '-'}   grounding: "
                    f"{r['grounding_issues'] or '-'}"
                )
        if len(rows) > MAX_FAILURE_EXAMPLES:
            lines.append(f"    ... and {len(rows) - MAX_FAILURE_EXAMPLES} more (see --output)")
    return "\n".join(lines)


# --- Entry point ----------------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate grounded answers.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--faq-data", type=Path)
    parser.add_argument(
        "--llm",
        choices=["off", "live"],
        default="off",
        help="off: LLM disabled (free, deterministic); live: real OpenAI calls (paid)",
    )
    parser.add_argument("--yes", action="store_true", help="confirm paid requests for --llm live")
    parser.add_argument("--max-cases", type=int, help="evaluate only the first N cases")
    parser.add_argument("--output", type=Path, help="also write the full report as JSON")
    parser.add_argument(
        "--offline", action="store_true", help="use only the locally cached embedding model"
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    embedding_factory: Callable[[str], EmbeddingService] = EmbeddingService,
    settings: Settings | None = None,
    llm: LLMService | None = None,
) -> int:
    args = parse_args(argv)
    if args.max_cases is not None and args.max_cases < 1:
        print("--max-cases must be at least 1", file=sys.stderr)
        return 2
    if args.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"  # read when huggingface_hub is first imported
    if args.llm == "live" and not args.yes:
        print(
            "--llm live makes one PAID OpenAI request per answered question "
            f"(up to {args.max_cases or 'all'} cases). Re-run with --yes to confirm.",
            file=sys.stderr,
        )
        return 2
    if settings is None:
        try:
            settings = Settings(llm_enabled=args.llm == "live")
        except ValueError as exc:  # e.g. live mode without an OPENAI_API_KEY
            print(f"Invalid configuration: {exc}", file=sys.stderr)
            return 2
    faq_path = args.faq_data or resolve_data_path(settings.faq_data_path)
    report = evaluate(
        args.dataset,
        faq_path,
        settings,
        mode=args.llm,
        max_cases=args.max_cases,
        embedding_factory=embedding_factory,
        llm=llm,
    )
    print(format_report(report))
    if args.output:
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nFull results written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
