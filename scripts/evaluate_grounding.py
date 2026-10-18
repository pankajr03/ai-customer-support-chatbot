"""Grounded answer evaluation of the complete AI answer pipeline (Stage 12, Task 14).

Usage (from the project root, virtual environment active):

    python scripts/evaluate_grounding.py                          # scripted model, free
    python scripts/evaluate_grounding.py --llm off                # approved answers only
    python scripts/evaluate_grounding.py --output data/grounding_eval_results.json
    python scripts/evaluate_grounding.py --llm live --yes         # PAID: your OpenAI model

Every question of ``data/grounded_answer_eval.json`` goes through the real chat API
(``scripts/ai_eval_harness.py``): conversation storage, rewriting, hybrid retrieval and
the answer decision over the real FAQ catalogue, context building, the prompt, reply
parsing, citation validation, the grounding checks and the fallbacks.

Model modes
-----------
- ``scripted`` (default, free, deterministic): the model returns the approved primary
  answer. Measures the pipeline (decisions, citations, fallbacks) and that a faithful
  reply passes the checks. It says nothing about a real model's wording.
- ``off``: ``LLM_ENABLED=false``; customers get approved answers verbatim.
- ``live`` (paid; requires ``--yes``; never run by tests): your OpenAI settings from
  ``.env``. Generated wording is then judged by the same deterministic checks, and every
  generated answer is queued for manual review.

How correctness is judged (deterministic only)
----------------------------------------------
No model judges another model's answer here: a model's unverified opinion is not proof
of correctness. Each reply is checked by:

1. **Citation validation**: cited FAQ IDs must be active, among those supplied to the
   model for this question, and (for answers) include an acceptable FAQ.
2. **Claim comparison**: the reply is split into sentences; each sentence's content words
   are compared with the cited FAQs' text. A sentence whose words are mostly
   (``SUPPORT_THRESHOLD``) found there is *supported*; otherwise it is *uncertain* and
   queued for manual review. This is a lexical comparison, not an understanding of
   meaning.
3. **Grounding checks** (``app/services/grounding_validator.py``, checks 1-13) on the
   reply against the cited FAQs: changed numbers, invented terms, promises, actions,
   disclosures and so on. Any issue is an *unsupported claim*.
4. **Forbidden phrases** from the dataset (``must_not_include``): any present is an
   unsupported claim.

Metrics and denominators
------------------------
- Grounded answer rate: answers given (decision ``answered``) with valid citations, no
  unsupported claim and every sentence supported / answers given.
- Unsupported claim rate: replies with an unsupported claim / all replies.
- Correct fallback rate: cases whose only acceptable outcome is a fallback that got one /
  those cases.
- Clarification correctness: cases whose only acceptable outcome is a clarifying question
  that got one / those cases.
- Source attribution correctness: answers to cases where an answer is acceptable whose
  citations are valid and include an acceptable FAQ / those answers.
- Prompt-injection resistance: the Task 10 suite's regular cases contained / regular
  cases (compromised model, ``scripts/evaluate_prompt_injection.py``).
- Response latency: end-to-end chat request time in-process (no network), per question.
- Token usage: from the API's usage metadata, in ``live`` mode only (the scripted model's
  counts are placeholders and are not reported as usage).

Reports contain the dataset's synthetic questions, FAQ IDs and replies built from the
FAQ catalogue: no customer data. All numbers come from the run.
"""

import argparse
import json
import re
import statistics
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/evaluate_grounding.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings  # noqa: E402
from app.database.faq_repository import FAQRepository  # noqa: E402
from app.prompts.customer_support import PROMPT_VERSION, SYSTEM_INSTRUCTIONS  # noqa: E402
from app.prompts.query_rewrite import REWRITE_INSTRUCTIONS  # noqa: E402
from app.services.grounding_validator import check_grounding  # noqa: E402
from app.services.llm_service import LLMService  # noqa: E402
from scripts import evaluate_prompt_injection  # noqa: E402
from scripts.ai_eval_harness import (  # noqa: E402
    DEFAULT_FAQ_DATA,
    embedding_service,
    eval_app,
)
from scripts.evaluate_semantic_search import environment_info, file_sha256  # noqa: E402

DEFAULT_DATASET = PROJECT_ROOT / "data" / "grounded_answer_eval.json"
SUPPORT_THRESHOLD = 0.6  # share of a sentence's content words found in the cited FAQs
_SUPPLIED = re.compile(r'<faq id="(faq_\d+)"')
_STOPWORDS = frozenset(
    "a an and are as at be been but by can could do does for from has have how i if in "
    "is it its me my no not of on or our please so that the their them then there these "
    "this to up us was we what when where which while who will with would you your".split()
)
OUTCOME = {"answered": "answer", "ambiguous": "clarification"}


def _content_words(text: str) -> set[str]:
    words = set()
    for word in re.findall(r"[a-z0-9$%]+", text.casefold()):
        if word in _STOPWORDS or len(word) < 3 and not word.isdigit():
            continue
        for suffix in ("ing", "ed", "es", "s"):
            if len(word) > 4 and word.endswith(suffix):
                word = word[: -len(suffix)]
                break
        words.add(word)
    return words


def claim_support(answer: str, evidence: Sequence[str]) -> list[dict[str, Any]]:
    """Each sentence of ``answer`` with the share of its content words found in the
    evidence. Lexical only: it cannot tell a reversed meaning from a faithful one."""
    known = _content_words(" ".join(evidence))
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", answer) if s.strip()]
    rows = []
    for sentence in sentences:
        words = _content_words(sentence)
        coverage = len(words & known) / len(words) if words else 1.0
        rows.append(
            {
                "sentence": sentence,
                "coverage": round(coverage, 2),
                "supported": coverage >= SUPPORT_THRESHOLD,
            }
        )
    return rows


def _fraction(n: int, of: int) -> dict[str, Any]:
    return {"n": n, "of": of, "rate": n / of if of else None}


def _latency(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"samples": 0, "median_ms": None, "p90_ms": None, "max_ms": None}
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        "median_ms": round(statistics.median(ordered), 1),
        "p90_ms": round(ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))], 1),
        "max_ms": round(ordered[-1], 1),
    }


def evaluate_case(ev: Any, case: dict[str, Any], faqs: dict[str, Any], live: bool) -> dict:
    session_id, token = ev.start_conversation()
    # Warm: each case has a fresh app, whose first search would build the FAQ index (and
    # the first case would load the model). Cold start is measured by load_test_local.py.
    ev.app.state.semantic_search_service.build_index()
    calls_before = len(ev.model.calls)
    started = time.perf_counter()
    reply = ev.chat(session_id, token, case["query"])
    latency_ms = (time.perf_counter() - started) * 1000
    calls = ev.model.calls_since(calls_before)

    decision = reply.get("decision")
    method = reply.get("generation_method")
    answer = str(reply.get("answer", ""))
    sources = [s["faq_id"] for s in reply.get("sources") or []]
    outcome = OUTCOME.get(str(decision), "fallback")
    acceptable_outcomes = case["acceptable_outcomes"]
    acceptable = set(case["acceptable_source_ids"])

    supplied = {i for c in calls if c.kind == "answer" for i in _SUPPLIED.findall(c.input)}
    problems: list[str] = []
    active = ev.active_faq_ids()
    if any(s not in active for s in sources):
        problems.append("cites an inactive or unknown FAQ")
    if calls and outcome == "answer" and any(s not in supplied for s in sources):
        problems.append("cites an FAQ that was not supplied")
    citations_valid = not problems
    if outcome == "answer" and not (set(sources) & acceptable):
        problems.append(f"cites none of the acceptable FAQs {sorted(acceptable)}")

    cited_text = [t for s in sources if s in faqs for t in (faqs[s].question, faqs[s].answer)]
    unsupported: list[str] = []
    sentences: list[dict[str, Any]] = []
    if outcome == "answer" and cited_text:
        report = check_grounding(
            answer,
            cited_text,
            faqs[sources[0]].answer,
            protected=(SYSTEM_INSTRUCTIONS, REWRITE_INSTRUCTIONS),
        )
        unsupported += [f"grounding:{kind}" for kind in report.kinds]
        sentences = claim_support(answer, cited_text)
    unsupported += [
        f"forbidden:{p}" for p in case["must_not_include"] if p.casefold() in answer.casefold()
    ]
    uncertain = [s for s in sentences if not s["supported"]]
    usage = reply.get("usage") if live else None
    return {
        "case_id": case["id"],
        "type": case["type"],
        "query": case["query"],
        "acceptable_outcomes": acceptable_outcomes,
        "acceptable_source_ids": sorted(acceptable),
        "outcome": outcome,
        "outcome_correct": outcome in acceptable_outcomes,
        "decision": decision,
        "generation_method": method,
        "sources": sources,
        "citations_valid": citations_valid,
        "attribution_correct": outcome == "answer" and not problems,
        "unsupported_claims": unsupported,
        "uncertain_sentences": uncertain,
        "grounded": outcome == "answer" and not problems and not unsupported and not uncertain,
        "manual_review": bool(uncertain) or (live and method == "llm_grounded"),
        "answer": answer,
        "latency_ms": round(latency_ms, 1),
        "model_called": any(c.kind == "answer" for c in calls),
        "usage": usage,
        "problems": problems,
    }


def summarize(results: Sequence[dict[str, Any]], injection: dict[str, Any] | None) -> dict:
    answers = [r for r in results if r["outcome"] == "answer"]
    answerable = [r for r in answers if "answer" in r["acceptable_outcomes"]]
    fallback_only = [r for r in results if r["acceptable_outcomes"] == ["fallback"]]
    clarify_only = [r for r in results if r["acceptable_outcomes"] == ["clarification"]]
    tokens = [r["usage"] for r in results if r["usage"]]
    return {
        "cases": len(results),
        "outcomes": {o: sum(r["outcome"] == o for r in results) for o in OUTCOME.values()}
        | {"fallback": sum(r["outcome"] == "fallback" for r in results)},
        "outcome_correctness": _fraction(sum(r["outcome_correct"] for r in results), len(results)),
        "grounded_answer_rate": _fraction(sum(r["grounded"] for r in answers), len(answers)),
        "unsupported_claim_rate": _fraction(
            sum(bool(r["unsupported_claims"]) for r in results), len(results)
        ),
        "correct_fallback_rate": _fraction(
            sum(r["outcome"] == "fallback" for r in fallback_only), len(fallback_only)
        ),
        "clarification_correctness": _fraction(
            sum(r["outcome"] == "clarification" for r in clarify_only), len(clarify_only)
        ),
        "source_attribution_correctness": _fraction(
            sum(r["attribution_correct"] for r in answerable), len(answerable)
        ),
        "prompt_injection_resistance": (
            _fraction(injection["passed"], injection["regular_cases"]) if injection else None
        ),
        "latency": _latency([r["latency_ms"] for r in results]),
        "latency_with_model_call": _latency(
            [r["latency_ms"] for r in results if r["model_called"]]
        ),
        "token_usage": (
            {
                "replies_with_usage": len(tokens),
                "input_tokens": sum(t.get("input_tokens") or 0 for t in tokens),
                "output_tokens": sum(t.get("output_tokens") or 0 for t in tokens),
            }
            if tokens
            else "not available (no real model calls in this mode)"
        ),
        "manual_review": [r["case_id"] for r in results if r["manual_review"]],
        "failures": [
            r["case_id"]
            for r in results
            if not r["outcome_correct"] or r["unsupported_claims"] or r["problems"]
        ],
    }


def evaluate(
    dataset_path: Path = DEFAULT_DATASET,
    *,
    llm: str = "scripted",
    encoder: str = "model",
    faq_path: Path = DEFAULT_FAQ_DATA,
    live_settings: Settings | None = None,
    with_injection: bool = True,
    max_cases: int | None = None,
) -> dict[str, Any]:
    cases = json.loads(dataset_path.read_text(encoding="utf-8"))["cases"][:max_cases]
    faqs = {faq.id: faq for faq in FAQRepository.from_json_file(faq_path).list_all()}
    unknown = [
        f"{c['id']}: {i}" for c in cases for i in c["acceptable_source_ids"] if i not in faqs
    ]
    if unknown:
        raise ValueError(f"labels name unknown FAQs: {unknown}")
    settings = Settings(_env_file=None)
    embeddings = embedding_service(encoder, settings.embedding_model_name)
    started = time.perf_counter()
    results = []
    for case in cases:
        live = None
        if llm == "live" and live_settings is not None:
            live = LLMService.from_settings(live_settings.model_copy(update={"llm_enabled": True}))
        overrides = {"llm_enabled": llm != "off", "query_rewrite_use_llm": False}
        with eval_app(embeddings, faq_path=faq_path, live_llm=live, **overrides) as ev:
            results.append(evaluate_case(ev, case, faqs, live=llm == "live"))
    injection = None
    if with_injection:
        injection = evaluate_prompt_injection.evaluate(
            evaluate_prompt_injection.DEFAULT_DATASET, encoder=encoder, faq_path=faq_path
        )["summary"]
    model = (
        live_settings.openai_model
        if (llm == "live" and live_settings)
        else f"{llm} (no real model)"
    )
    return {
        "meta": {
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "llm_mode": llm,
            "llm_model": model,
            "prompt_version": PROMPT_VERSION,
            "judgment": "deterministic only (no model-based judging)",
            "support_threshold": SUPPORT_THRESHOLD,
            "embedding_model": embeddings.model_name,
            "encoder": encoder,
            "dataset": dataset_path.name,
            "dataset_sha256": file_sha256(dataset_path),
            "faq_data": faq_path.name,
            "faq_data_sha256": file_sha256(faq_path),
            "settings": {
                name: getattr(settings, name)
                for name in (
                    "answer_min_similarity",
                    "answer_ambiguity_margin",
                    "hybrid_rrf_k",
                    "llm_context_max_faqs",
                    "llm_context_max_chars",
                    "openai_max_output_tokens",
                )
            },  # fmt: skip
            "seconds": round(time.perf_counter() - started, 1),
            "environment": environment_info(),
        },
        "summary": summarize(results, injection),
        "results": results,
    }


def _pct(fraction: dict[str, Any] | None) -> str:
    if not fraction or fraction["rate"] is None:
        return "n/a"
    return f"{fraction['n']}/{fraction['of']} ({fraction['rate']:.1%})"


def format_report(report: dict[str, Any]) -> str:
    meta, s = report["meta"], report["summary"]
    lines = [
        f"Grounded answer evaluation ({meta['llm_mode']} model, {meta['judgment']}), "
        f"{meta['created_at']}",
        f"Dataset {meta['dataset']}: {s['cases']} questions; outcomes {s['outcomes']}",
        "",
        f"Outcome correctness:            {_pct(s['outcome_correctness'])}",
        f"Grounded answer rate:           {_pct(s['grounded_answer_rate'])}",
        f"Unsupported claim rate:         {_pct(s['unsupported_claim_rate'])}",
        f"Correct fallback rate:          {_pct(s['correct_fallback_rate'])}",
        f"Clarification correctness:      {_pct(s['clarification_correctness'])}",
        f"Source attribution correctness: {_pct(s['source_attribution_correctness'])}",
        f"Prompt-injection resistance:    {_pct(s['prompt_injection_resistance'])}",
        f"Latency (all):                  {s['latency']}",
        f"Latency (with a model call):    {s['latency_with_model_call']}",
        f"Token usage:                    {s['token_usage']}",
        f"Manual review queue:            {', '.join(s['manual_review']) or 'none'}",
        f"Cases with a problem:           {', '.join(s['failures']) or 'none'}",
    ]
    for result in report["results"]:
        if result["case_id"] in s["failures"]:
            reasons = result["problems"] + result["unsupported_claims"]
            if not result["outcome_correct"]:
                reasons.append(
                    f"outcome {result['outcome']} not in {result['acceptable_outcomes']}"
                )
            lines.append(f"  {result['case_id']} ({result['type']}): {'; '.join(reasons)}")
    lines += [
        "",
        "Judged by deterministic checks only. Lexical sentence support is not an "
        "understanding of meaning: read the manual review queue.",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--faq-data", type=Path, default=DEFAULT_FAQ_DATA)
    parser.add_argument("--llm", choices=("scripted", "off", "live"), default="scripted")
    parser.add_argument("--yes", action="store_true", help="confirm paid requests for --llm live")
    parser.add_argument("--encoder", choices=("model", "lexical"), default="model")
    parser.add_argument("--skip-injection", action="store_true")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--output", type=Path, help="also write the full report as JSON")
    args = parser.parse_args(argv)
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
        llm=args.llm,
        encoder=args.encoder,
        faq_path=args.faq_data,
        live_settings=live_settings,
        with_injection=not args.skip_injection,
        max_cases=args.max_cases,
    )
    print(format_report(report))
    if args.output:
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nFull report written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
