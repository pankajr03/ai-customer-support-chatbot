"""Evaluate prompt-injection resistance (Stage 12, Task 10).

Usage (from the project root, virtual environment active):

    python scripts/evaluate_prompt_injection.py                      # compromised model
    python scripts/evaluate_prompt_injection.py --output pi_results.json
    python scripts/evaluate_prompt_injection.py --encoder lexical    # no PyTorch, faster
    python scripts/evaluate_prompt_injection.py --llm live --yes     # PAID: a real model

What it measures
----------------
Each case in ``data/security_prompt_injection_eval.json`` delivers an attack through one
channel (customer message, conversation history, query rewriting, FAQ question, answer
or tags, support ticket text) and runs through the real application
(``scripts/ai_eval_harness.py``): real FAQ catalogue in a temporary database, real
retrieval (the cached embedding model by default), real prompt, parsing, grounding
checks, storage and HTTP routes.

- ``--llm compromised`` (default, free, deterministic): the model is a scripted stand-in
  that **obeys** the attack. This is a worst case: it shows what the application's own
  controls stop when the model has been fully manipulated.
- ``--llm live`` (paid, opt-in with ``--yes``): your OpenAI settings from ``.env``. The
  same checks run on what a real model actually returns; the model-input checks are
  skipped (the live client's requests are not recorded). One run of one model is not a
  measure of how often attacks succeed in general.

Checks per case (all deterministic): the final reply's generation method and decision
are among those expected; it contains none of the case's forbidden phrases; it repeats
no run of the model instructions, holds no credential-like value and cites only active
FAQs; every model call used one of the two fixed instruction texts, and no unescaped
markup, API key or forbidden phrase reached the model input.

Results
-------
- ``passed``: every check held.
- ``failed``: a check failed in a case that is not a known limitation: a regression.
- ``known_limitation``: a case documenting a gap (``known_limitation`` in the dataset);
  reported as "held" or "confirmed" and never counted as a pass of the defences.
- ``reached_model``: the attack's final turn was actually sent to the model. A case that
  never reached it (retrieval declined) tests less; the count is reported.

Passing every case is evidence that these specific attacks are handled, not a guarantee:
prompt injection cannot be eliminated by checks like these.

Exit status: 0 when no unexpected failure; 1 otherwise.
"""

import argparse
import json
import sys
import time
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/evaluate_prompt_injection.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import BaseModel, ConfigDict, Field, model_validator  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.services.llm_service import LLMService  # noqa: E402
from scripts.ai_eval_harness import (  # noqa: E402
    DEFAULT_FAQ_DATA,
    CaseOutcome,
    ScriptedCase,
    embedding_service,
    eval_app,
    run_scripted_case,
)
from scripts.evaluate_semantic_search import environment_info, file_sha256  # noqa: E402

DEFAULT_DATASET = PROJECT_ROOT / "data" / "security_prompt_injection_eval.json"
MIN_CASES = 30
CHANNELS = frozenset(
    {
        "customer_message", "conversation_history", "query_rewrite", "faq_question",
        "faq_answer", "faq_tags", "ticket_description",
    }
)  # fmt: skip
CAVEAT = (
    "Deterministic checks on a scripted, fully compromised model. A pass means these "
    "attacks were contained by the application's controls in this run; it does not prove "
    "that prompt injection is impossible."
)


class InjectionCase(ScriptedCase):
    category: str = Field(min_length=1)
    channel: str
    description: str = Field(min_length=1)
    expected_safety: str = Field(min_length=1)

    @model_validator(mode="after")
    def _known_channel(self) -> "InjectionCase":
        if self.channel not in CHANNELS:
            raise ValueError(f"{self.id}: unknown channel {self.channel!r}")
        return self


class InjectionDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    description: str
    faq_data: str
    cases: list[InjectionCase] = Field(min_length=MIN_CASES)

    @model_validator(mode="after")
    def _unique(self) -> "InjectionDataset":
        ids = [case.id for case in self.cases]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate case ids")
        # Distinct = different messages, setup or model behaviour (FAQ-channel cases ask
        # the same question about differently planted content).
        keys = [
            json.dumps(
                [case.setup.model_dump(), [turn.model_dump() for turn in case.turns]],
                sort_keys=True,
            )
            for case in self.cases
        ]
        if len(set(keys)) != len(keys):
            raise ValueError("two cases are identical (messages, setup and model replies)")
        return self


def load_dataset(path: Path) -> InjectionDataset:
    return InjectionDataset.model_validate_json(path.read_text(encoding="utf-8"))


def summarize(cases: Sequence[InjectionCase], outcomes: Sequence[CaseOutcome]) -> dict[str, Any]:
    by_id = {case.id: case for case in cases}
    regular = [o for o in outcomes if not o.known_limitation]
    limits = [o for o in outcomes if o.known_limitation]

    def group(key: str) -> dict[str, dict[str, int]]:
        table: dict[str, Counter[str]] = {}
        for outcome in regular:
            value = getattr(by_id[outcome.case_id], key)
            table.setdefault(value, Counter())["cases"] += 1
            table[value]["passed"] += outcome.passed
        return {name: dict(counts) for name, counts in sorted(table.items())}

    return {
        "cases": len(outcomes),
        "regular_cases": len(regular),
        "passed": sum(o.passed for o in regular),
        "failed": [o.case_id for o in regular if not o.passed],
        "known_limitations": {
            "cases": len(limits),
            "held": [o.case_id for o in limits if o.passed],
            "confirmed": [o.case_id for o in limits if not o.passed],
        },
        "reached_model": {
            "n": sum(o.reached_model for o in outcomes),
            "of": len(outcomes),
            "not_reached": [o.case_id for o in outcomes if not o.reached_model],
        },
        "generation_methods": dict(Counter(str(o.generation_method) for o in outcomes)),
        "by_category": group("category"),
        "by_channel": group("channel"),
    }


def evaluate(
    dataset_path: Path,
    *,
    encoder: str = "model",
    faq_path: Path = DEFAULT_FAQ_DATA,
    live_settings: Settings | None = None,
    max_cases: int | None = None,
) -> dict[str, Any]:
    dataset = load_dataset(dataset_path)
    cases = dataset.cases[:max_cases] if max_cases else dataset.cases
    model_name = Settings(_env_file=None).embedding_model_name
    embeddings = embedding_service(encoder, model_name)
    started = time.perf_counter()
    outcomes = []
    for case in cases:
        live = (
            LLMService.from_settings(live_settings.model_copy(update={"llm_enabled": True}))
            if live_settings is not None
            else None
        )
        with eval_app(embeddings, faq_path=faq_path, live_llm=live) as ev:
            outcomes.append(run_scripted_case(ev, case))
    return {
        "meta": {
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "mode": "live" if live_settings is not None else "compromised",
            "encoder": encoder,
            "embedding_model": embeddings.model_name,
            "dataset": dataset_path.name,
            "dataset_version": dataset.version,
            "dataset_sha256": file_sha256(dataset_path),
            "faq_data": faq_path.name,
            "faq_data_sha256": file_sha256(faq_path),
            "seconds": round(time.perf_counter() - started, 1),
            "environment": environment_info(),
            "caveat": CAVEAT,
        },
        "summary": summarize(cases, outcomes),
        "results": [o.as_dict() for o in outcomes],
    }


def format_report(report: dict[str, Any]) -> str:
    meta, summary = report["meta"], report["summary"]
    limits = summary["known_limitations"]
    lines = [
        f"Prompt-injection evaluation ({meta['mode']} model, encoder {meta['encoder']}), "
        f"{meta['created_at']}",
        f"Dataset {meta['dataset']} v{meta['dataset_version']}: {summary['cases']} cases",
        "",
        f"Regular cases passed:      {summary['passed']}/{summary['regular_cases']}",
        f"Unexpected failures:       {', '.join(summary['failed']) or 'none'}",
        f"Known limitations:         {limits['cases']} "
        f"(held: {', '.join(limits['held']) or 'none'}; "
        f"confirmed: {', '.join(limits['confirmed']) or 'none'})",
        f"Attacks reaching the model: {summary['reached_model']['n']}/"
        f"{summary['reached_model']['of']} (not reached: "
        f"{', '.join(summary['reached_model']['not_reached']) or 'none'})",
        f"Generation methods:        {summary['generation_methods']}",
        "",
        "By channel (passed/cases, regular cases):",
    ]
    for name, counts in summary["by_channel"].items():
        lines.append(f"  {name:<22} {counts.get('passed', 0)}/{counts['cases']}")
    lines.append("By category:")
    for name, counts in summary["by_category"].items():
        lines.append(f"  {name:<22} {counts.get('passed', 0)}/{counts['cases']}")
    problems = [r for r in report["results"] if r["problems"]]
    if problems:
        lines += ["", "Cases with problems:"]
        for result in problems:
            tag = " (known limitation)" if result["known_limitation"] else ""
            lines.append(f"  {result['case_id']}{tag}: {'; '.join(result['problems'])}")
    lines += ["", meta["caveat"]]
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--faq-data", type=Path, default=DEFAULT_FAQ_DATA)
    parser.add_argument("--encoder", choices=("model", "lexical"), default="model")
    parser.add_argument("--llm", choices=("compromised", "live"), default="compromised")
    parser.add_argument("--yes", action="store_true", help="confirm paid requests for --llm live")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--output", type=Path, help="also write the full report as JSON")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    live_settings = None
    if args.llm == "live":
        if not args.yes:
            print("--llm live makes paid OpenAI requests; add --yes to confirm.")
            return 2
        live_settings = Settings()
        if live_settings.openai_api_key is None:
            print("No OPENAI_API_KEY in the environment or .env.")
            return 2
    report = evaluate(
        args.dataset,
        encoder=args.encoder,
        faq_path=args.faq_data,
        live_settings=live_settings,
        max_cases=args.max_cases,
    )
    print(format_report(report))
    if args.output:
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nFull report written to {args.output}")
    return 1 if report["summary"]["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
