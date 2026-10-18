# Testing and quality report (Stage 12)

Test commands, results, coverage, dependency audit, performance measurements, regression
results and outstanding defects, as measured on **2026-10-18** on the development
machine (Windows 10, 4 CPUs, Python 3.13.3). Checks that were **not executed** are marked
as such. Security findings are in [docs/security-audit.md](security-audit.md); AI
evaluation results in [docs/ai-evaluation-report.md](ai-evaluation-report.md).

> Coverage and passing tests are not proof of correctness or security. They show which
> code ran under test and that the tested behaviour holds, nothing more.

## Contents

1. [Commands](#1-commands)
2. [Test results](#2-test-results)
3. [Coverage](#3-coverage)
4. [Static checks](#4-static-checks)
5. [Dependency audit](#5-dependency-audit)
6. [Performance](#performance)
7. [Regression results](#7-regression-results)
8. [Not executed](#8-not-executed)
9. [Outstanding defects and important untested areas](#9-outstanding-defects-and-important-untested-areas)

---

## 1. Commands

All commands run from the project root with the virtual environment active, in Windows
PowerShell 5.1 or PowerShell 7.

| Purpose | Command |
|---|---|
| Everything required, offline (lint, formatting, type check as information, all tests with coverage, AI safety suite) | `.\scripts\quality_checks.ps1` |
| One group | `.\scripts\quality_checks.ps1 -Suite lint` (or `types`, `unit`, `integration`, `security`, `ai`, `coverage`) |
| With the dependency audit (network) | `.\scripts\quality_checks.ps1 -Audit` |
| All tests | `python -m pytest -q` |
| Integration tests only | `python -m pytest -q tests/integration` |
| Coverage | `python -m pytest -q --cov=app --cov=frontend --cov-branch --cov-report=term-missing` |
| Lint and formatting | `python -m ruff check app frontend tests scripts`; `python -m ruff format --check app frontend tests scripts` |
| Type checking (informational) | `python -m mypy` (configured in `pyproject.toml`) |
| AI evaluations (cached model, free) | `python scripts/evaluate_retrieval.py`, `evaluate_grounding.py`, `evaluate_prompt_injection.py`, `evaluate_ai.py` |
| Load test (local only) | `python scripts/load_test_local.py` |
| Opt-in, **paid** live-model checks | `$env:RUN_OPENAI_LIVE_TESTS = "1"; python -m pytest -m openai_live`; `python scripts/evaluate_grounding.py --llm live --yes` |
| Opt-in real-embedding-model tests | `$env:RUN_EMBEDDING_MODEL_TESTS = "1"; python -m pytest -m embedding_model` |

Default runs are deterministic. They make no paid model call, download no model
(`HF_HUB_OFFLINE=1`), never open `data/conversations.db` (tests use temporary or
in-memory databases), and need no secrets. `scripts/quality_checks.ps1` exits with `0`
when every required step passed and `1` otherwise, and prints a summary table.
`.github/workflows/quality.yml` runs the same offline checks plus `pip-audit` on GitHub.
It has read-only permissions, no secrets, and no deployment or publishing.

## 2. Test results

**Full suite** (`python -m pytest -q --cov=app --cov=frontend --cov=scripts --cov-branch`):

| | Count |
|---|---|
| Collected | 3,639 |
| **Passed** | **3,635** |
| **Failed** | **0** |
| **Skipped** | **4**: the opt-in tests (2 paid OpenAI live tests, 2 real-embedding-model tests) |
| Duration | 14 min 48 s |

After that run, `tests/test_quality_scripts.py` (7 tests, for the grounding-evaluation
and load-test scripts) was added and run on its own: **7 passed**. The suite now holds
3,646 tests.

**`scripts/quality_checks.ps1`, run group by group in PowerShell:** lint PASS;
formatting PASS; security PASS (231 tests); AI safety evaluation PASS (39/39 injection
cases); type checking INFO (exit 1, not a gate). All exited with code 0. The `-Suite
all` and `-Audit` runs of the script were not executed as a whole; their steps were run
individually as reported here.

Stage 12 added these test files: `test_security_audit.py` (129), `test_frontend_security.py`
(5), `test_grounding_manipulation.py` (39), `test_ai_evaluation.py` (11),
`test_conversation_quality.py` (11), `test_faq_update_consistency.py` (3),
`test_ticket_lead_security.py` (8), `test_audit_contact_views.py` (5),
`test_failure_paths.py` (19), `test_main_import.py` (3), `test_quality_scripts.py` (7),
plus new cases in `test_admin_frontend.py`.

## 3. Coverage

Measured with `pytest-cov` (branch coverage) on the full run above.

| Package | Lines | Branches |
|---|---|---|
| `app/` (backend) | **7,399 / 7,459 (99.2%)** | **1,316 / 1,366 (96.3%)** |
| `frontend/` (Streamlit pages) | 2,408 / 2,536 (95.0%) | 523 / 632 (82.8%) |
| `scripts/` (evaluation and maintenance tools) | 2,712 / 4,104 (66.1%) | 537 / 960 (55.9%) |
| All measured code | 87.3% | |

**How coverage was used.** The first coverage run (app 98.9% lines, 95.6% branches)
pointed at untested **failure and integrity paths in high-risk code**. These were tested
in `tests/test_failure_paths.py`:

- the audit log failing to write or read;
- ticket storage locked, refusing a write or holding invalid data;
- customer ticket reads with storage down;
- every invariant of a support ticket record;
- start-up failures.

All passed without code changes. Lower-value gaps were left deliberately: report
printing in scripts, and Streamlit widget branches only reachable by clicking.

## 4. Static checks

| Check | Result |
|---|---|
| `ruff check` (rules E, F, W, I, B, UP, S, ASYNC) | **Pass**, 0 findings |
| `ruff format --check` | **Pass**, 237 files formatted |
| `mypy` on `app/` (new, informational) | **87 findings in 21 files**, reviewed: all typing precision, no defect found. Pydantic's intended `str`→enum coercion; JSON-schema example dict types; Optional values guarded earlier; a `bool`-returning callback in a list typed `-> None`. Reported, not a gate, so the gate does not depend on cosmetic fixes |
| `bandit` (security linter, run in a separate environment) | 11 low-confidence findings, all triaged as false positives (security audit SEC-17) |

## 5. Dependency audit

| | |
|---|---|
| Tool | `pip-audit` 2.10.1, in a separate virtual environment (the project environment is not modified by it) |
| What was audited | **Every package installed in the project environment**: 95 packages from `pip freeze`, transitive dependencies included. The 18 direct requirements were also audited on their own earlier the same day |
| Advisory source | The PyPI JSON API vulnerability data (OSV), queried with package names and versions only |
| Result | **No known vulnerabilities found** (0 of 95; none skipped) |
| Upgrades | None needed, none made, so no regression risk to the sentence-transformers or OpenAI SDK pins |
| Completeness | The scan completed. It reflects the advisory data of 2026-10-18 only; a clean result does not mean the dependencies are safe. Transitive versions are not hash-locked (`pip-compile --generate-hashes` is recommended, security audit SEC-16) |

## Performance

`python scripts/load_test_local.py` (results in `data/load_test_results.json`):

- **Setup:** the real application under uvicorn on `127.0.0.1`, a temporary SQLite database seeded from the real FAQs, the real cached embedding model, and a **mocked language model** (no network, no cost).
- **Load:** 200 requests per scenario at concurrency 8, with rate limits switched off in the test server so capacity is measured rather than the limiter.
- **Machine:** run with nothing else running.

**Cold start versus warm**

| Step | Time |
|---|---|
| Build the application (FAQ seeding, services) | 268 ms |
| First request (health) | 47 ms |
| **First semantic request** (PyTorch import, model load from the local cache, index build) | **159,349 ms (about 2 min 39 s)** |
| Second semantic request (warm) | 74 ms |

The cold start is dominated by loading PyTorch and the model on this machine. The evaluation runs measured it at 73 s when the operating system had the files cached. `SEMANTIC_INDEX_PRELOAD=background` moves this out of the first customer's request.

**Warm load** (client-observed over loopback HTTP)

| Scenario | Requests | Throughput | p50 | p90 | p99 | Max | Errors |
|---|---|---|---|---|---|---|---|
| Health check | 200 | 116.0/s | 59.5 ms | 93.3 ms | 210.4 ms | 265.6 ms | 0 |
| FAQ keyword search | 200 | 87.1/s | 76.7 ms | 140.9 ms | 218.4 ms | 231.1 ms | 0 |
| Hybrid search (embedding + RRF) | 200 | 24.3/s | 322.8 ms | 390.0 ms | 447.9 ms | 450.3 ms | 0 |
| Create conversation (a database write) | 200 | 81.8/s | 66.9 ms | 147.5 ms | 715.8 ms | 1,199.8 ms | 0 |
| Chat, 8 conversations in parallel (25 messages each, mocked model) | 200 | 9.1/s | 573.4 ms | 957.1 ms | 7,122.3 ms | 7,164.8 ms | 0 |

**Database calls** (direct, 50 each): conversation lookup p50 1.6 ms (max 4.8 ms);
ticket list page p50 1.8 ms (max 9.6 ms); FAQ catalogue reload p50 3.4 ms (max 6.3 ms).

**Memory** (process working set): 92 MB before the model, **561 MB after warm-up**, 590 MB
after the load (peak 591 MB). Measured with the Windows process API; no extra dependency.

**Error rate:** 0 of 1,000 requests.

**Reading these numbers**

- Chat is slowest under concurrency because every turn writes several times, and **SQLite allows one writer**. Writers queue for the lock (busy timeout 5 s), which produces the p99 of about 7 s. Beyond 5 s a request would get `503` with `Retry-After`; none did at this load.
- A real language model would add its own latency, typically seconds per reply.
- These are local, single-process figures, not production capacity. Several workers or servers need a database server and shared state (security audit SEC-09, SEC-10).

The grounding evaluation measured end-to-end in-process chat latency on a warm index at
a median 67.8 ms (p90 84.8 ms) over 44 questions, without HTTP or concurrency.

## 7. Regression results

Every fix of Stage 12 has a regression test that was run **against the code without the
fix (it failed) and with it (it passed)**:

| Fix | Tests that failed without it |
|---|---|
| SEC-01 Markdown on the staff ticket page | `test_customer_text_on_the_ticket_page_is_shown_as_typed` |
| SEC-02 token in the URL on ticket reads | `test_credentials_in_the_url_are_refused_even_with_a_valid_header` |
| SEC-03 staff-token log redaction | `test_staff_tokens_are_redacted_from_log_text_even_without_bearer` |
| SEC-13, SEC-18, SEC-19, SEC-20 (headers, manipulated replies, FAQ lint, session expiry) | 29 tests in `test_grounding_manipulation.py`, `test_security_audit.py`, `test_admin_frontend.py` |
| SEC-23 audit triggers lost in a rebuild | `test_an_older_audit_table_is_upgraded_keeping_rows_and_append_only_triggers` (0 triggers without the fix) |

The other fixes (SEC-05, SEC-07, SEC-21 partly, SEC-22, SEC-24) have tests that exercise
behaviour which did not exist before. After all fixes the **full suite passed** (section
2), including every test from earlier stages. **No existing test needed weakening.** Two
were adjusted for intended changes: the FAQ history test now expects escaped Markdown,
and the anonymization test expects placeholders rather than a missing contact record.

## 8. Not executed

| Check | Why |
|---|---|
| Paid live-model evaluation and `openai_live` tests | Require your approval and OpenAI credits; opt-in commands exist |
| Real-embedding-model pytest tests (`embedding_model` marker) | Opt-in; the evaluation scripts used the real cached model instead |
| GitHub Actions workflow | The repository has no remote; the workflow has never run on GitHub |
| `scripts/quality_checks.ps1 -Suite all` and `-Audit` as single runs | Their steps were run individually (sections 2, 4, 5) |
| Load test with several uvicorn workers or a real model | Out of scope for local, mocked testing; SQLite multi-writer behaviour under real workers is untested |
| Penetration test or dynamic scan of a deployed instance | Nothing is deployed |
| Fixing the 87 mypy findings | Typing precision only; left as information |

## 9. Outstanding defects and important untested areas

**Defects (measured, not fixed in this stage):**

1. **Wrong-FAQ answers** to some questions, for example "Are you hiring?" answered with the delivery FAQ. The cause is loose semantic matches and follow-up merging. AI report section 8.
2. **Clarification behaviour:** 3 of 4 deliberately ambiguous questions got a single answer instead of a clarifying question. AI report section 4.
3. **Open security items:** staff identity prototype, per-process state, no model spending cap, invented combinations of supported words passing the grounding checks, unscheduled retention, support staff seeing every ticket's contact details. Security audit, "Production-readiness limitations".

**Important untested or lightly tested areas:**

- Behaviour under several real uvicorn worker processes. Leases and idempotency are designed for it and tested with two engines, not real workers.
- A real model's wording, refusals, latency and token usage.
- Streamlit pages in a real browser beyond `AppTest` (layout, focus, accessibility).
- Script command-line paths (`scripts/` 66% line coverage): argument handling and report printing.
- Restore from backup and disaster recovery.
