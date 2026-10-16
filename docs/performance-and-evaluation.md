# Performance and journey evaluation (Stage 9)

Measurements and evaluation results from 2026-10-16, on the development machine: Windows 10, Python 3.13.3, an Intel Core CPU (family 6, model 78), no GPU. Every number below was measured there. Nothing is estimated, and each table says how it was measured.

## How to reproduce

```powershell
python scripts/measure_performance.py --offline                # LLM off (default)
python scripts/measure_performance.py --offline --llm mock     # LLM path, local stand-in
python scripts/evaluate_integration.py --offline               # the 31 customer journeys
```

Both scripts run the real FastAPI app **in process** (Starlette's `TestClient`, no uvicorn, no network), with the real FAQ file (`data/faqs.json`), the real embedding model (`all-MiniLM-L6-v2`, already downloaded) and conversation storage in a temporary SQLite file. **No request is sent to OpenAI**: `--llm mock` uses a local stand-in that replies instantly, so it measures this application's prompt building and validation, not OpenAI's latency. Rate limits are switched off in the measurement, because one client creating many conversations quickly is the point there.

**Not measured:** live OpenAI latency (it would need paid calls), uvicorn and network overhead (in-process client), and browser rendering time.

## Results

### Chat turns, warm (model loaded)

`measure_performance.py`, 24 conversations, 48 chat turns (40 answered, 4 ambiguous, 4 no match):

| Metric | LLM off p50 | p95 | max | LLM mocked p50 | p95 |
|--------|-----:|-----:|-----:|-----:|-----:|
| Chat request, whole (client side) | 50.4 ms | 64.1 ms | 328.1 ms | 42.0 ms | 48.3 ms |
| Retrieval and answer decision | 24.0 ms | 28.6 ms | 34.4 ms | 18.6 ms | 22.5 ms |
| … semantic search | 23.1 ms | 27.7 ms | 30.6 ms | 18.0 ms | 21.6 ms |
| … of which query embedding | 22.6 ms | 27.5 ms | 30.3 ms | 17.7 ms | 21.4 ms |
| … keyword search | 0.5 ms | 1.4 ms | 3.3 ms | 0.3 ms | 0.8 ms |
| Grounded answer (retrieval, decision, generation, validation) | 24.2 ms | 28.7 ms | 34.7 ms | 20.8 ms | 25.0 ms |
| LLM call (stand-in) | — | — | — | 0.1 ms | 0.1 ms |
| SQL statement execution, reads | 0.7 ms | 1.1 ms | 1.1 ms | 0.6 ms | 1.1 ms |
| SQL statement execution, writes | 0.2 ms | 0.4 ms | 0.7 ms | 0.1 ms | 0.2 ms |
| Create a conversation | 9.1 ms | 11.8 ms | 12.8 ms | 6.8 ms | 8.2 ms |
| Read a conversation's history | 11.9 ms | 14.5 ms | 18.1 ms | 9.5 ms | 13.5 ms |

Prompt size with the LLM on: median 4,189 characters, max 4,344 (instructions plus the selected FAQ evidence; `LLM_CONTEXT_MAX_FAQS=3`, `LLM_CONTEXT_MAX_CHARS=6000`).

### Where the rest of a turn goes

SQL statement times above count only statement execution. A second run (60 turns, LLM off) timed whole stages and transactions:

| Stage of a warm turn | p50 |
|----------------------|----:|
| Whole chat request | 66.4 ms |
| Semantic search (query embedding) | 32.5 ms |
| Store the exchange (write transaction) | 8.4 ms |
| Take the processing lease (write transaction) | 4.2 ms |
| Load recent history (read transactions) | 4.1 ms |
| Retry-key replay check (read transactions) | 3.7 ms |
| Query rewrite | 0.05 ms |
| Keyword search | 0.7 ms |

Per turn: about 6 read and 2.5 write transactions. Read transactions take 1.5 ms (p50), write transactions 4.3 ms (p50, max 310 ms, a rare SQLite checkpoint or disk flush). So storage costs about 20 ms per warm turn: mostly transaction and commit overhead, not queries. Each service step re-checks the session and token in its own short transaction.

### Cold start and first request

| What | Measured |
|------|----------|
| Build the app (load and validate the FAQ file) | 5.6 ms |
| Startup (schema, cleanup) | 83–127 ms |
| **First chat turn**, model loaded on demand (`SEMANTIC_INDEX_PRELOAD=false`) | **62.1 s** (index build 61.6 s), and 40.4 s in a second run |
| Model load at startup, earlier runs today (`true`) | 79.5 s and 88.4 s |
| `SEMANTIC_INDEX_PRELOAD=background` (new): from start to serving | 0.18 s; model ready 22.3 s later (warm disk cache) |
| First LLM call with the LLM enabled | +2.0–2.4 s once (the OpenAI SDK is imported on first use, by design) |

The model load varies a lot (22–88 s) with the disk cache: the first load after a reboot is the slowest.

### Frontend

The Streamlit page (`AppTest`) against the same backend:

| Action | HTTP calls | Script runs |
|--------|-----------|-------------|
| Open the page | 1 (`POST /conversations`) | 1 |
| Send a message | 1 (`POST …/chat`) | 2 (the submit, then one rerun to show the reply) |
| Any other rerun | 0 | 1 |

The service badge's readiness check is cached for 30 s for all visitors.

## Slowest operations

1. **Loading the embedding model and building the index: 22–88 s, once per process.** By default this happens on the first customer question, which then takes longer than the frontend's 60 s read timeout.
2. **Embedding the query: about 18–33 ms per turn**, roughly half of a warm turn with the LLM off. This is model inference on the CPU.
3. **Conversation storage: about 20 ms per turn** across about 8 transactions, with rare spikes to 0.3 s.
4. **A real LLM call** (not measured; seconds, by OpenAI's latency) will dominate every turn when the LLM is on. Our own share of generation (prompt and validation) is about 2 ms.

Keyword search, query rewriting, the answer decision, prompt building and validation are each 1 ms or less.

## Task 15: review of common performance problems

| Concern | Evidence | Action |
|---------|----------|--------|
| Repeated embedding model initialisation | One load per process: only the first turn shows `index_build` | None needed |
| Repeated FAQ indexing | Built once (61.6 s once, never again in 48 turns) | None needed |
| Cold start longer than the frontend waits | First turn 62.1 s vs a 60 s read timeout; blocking preload delays startup by the same time, on every `--reload` | **Added `SEMANTIC_INDEX_PRELOAD=background`**: serving after 0.18 s, the model loads in a background thread, and an early chat waits for that same load (tested) |
| Unnecessary database queries | Statements are cheap (~1 ms per turn); about 8 short transactions cost ~20 ms | Not changed. Merging the per-step authorisation into fewer transactions would touch security-critical Stage 6 code to save about 5–10 ms. Recommended for when a database server replaces SQLite |
| Excessive conversation history loading | The frontend reads history only on **Refresh messages**, after an uncertain send, or after a replay; the backend loads the last 10 messages within character and token budgets | None needed |
| Duplicate frontend API calls | One HTTP call per action, none on reruns | None needed |
| Repeated session creation | One per browser session (tested across reruns) | None needed |
| Unnecessary Streamlit reruns | Two runs per message: the second shows the reply and makes no HTTP call | None needed |
| Excessive LLM context size | About 4.2 K characters per prompt, bounded by settings | None needed |
| Unbounded HTTP timeouts | Frontend: 5 s connect, 60 s read, 3 s status checks. OpenAI: 20 s per attempt, at most 2 retries, circuit breaker. SQLite: 5 s busy timeout | None needed |

**Instrumentation added (lightweight, durations only, never message text):**

- The existing `event=conversation_turn` log line now ends with `history_ms`, `rewrite_ms`, `pipeline_ms`, `store_ms` and `turn_ms`.
- A debug-level `event=hybrid_search` line gives `keyword_ms` and `semantic_ms`.
- Already present: the access log's total time per request, `event=answer_decision duration_ms`, and the LLM call's `latency_ms`.

## Recommendations

1. **Load the model before customers arrive.** Use `SEMANTIC_INDEX_PRELOAD=background` for a server that should answer at once, or `true` where startup time does not matter (`scripts/start-dev.ps1` uses `true` and waits for readiness).
2. **With the LLM enabled**, import the OpenAI SDK in the same preload. That moves about 2 s off the first generated answer. Not done here, to keep the SDK out of every LLM-off process.
3. **Query embeddings:** if profiling under real traffic shows repeated identical questions (popular-question buttons, clarification choices), a small cache of query vectors would save about 20 ms per repeat. Not justified by the current evidence.
4. **Storage:** fewer, larger transactions per turn when moving to a database server (Stage 9 preview). On SQLite, `PRAGMA synchronous=NORMAL` in WAL mode would make commits cheaper, at the cost of possibly losing the last transactions on a power failure. That is a durability decision, not taken here.

## Customer journey evaluation

`data/integration_eval.json` holds 31 scenarios in 10 categories, using only real FAQ IDs. `scripts/evaluate_integration.py` runs each one through the frontend's own state and client against a fresh backend with the real model. The LLM is off, or replaced by a local stand-in for the injection and timeout scenarios.

**Result: 28 of 31 pass.** All prompt-injection, unrelated, ambiguous, topic-switch, returns, basic and service-failure scenarios pass. The 3 failures are recorded in the dataset as `known_issue` and reported as **KNOWN**, so they don't hide new failures:

| Scenario | What happened | Cause |
|----------|---------------|-------|
| INT-05 | "And where will the refund be sent?" was answered with refund *timing* (faq_023), not faq_024 | LLM off: the rule-based rewrite treats a leading "And" as a follow-up and prepends the previous question, although the message names its own topic |
| INT-10 | "What should I do if I still can't find it?" (about a parcel) was answered with faq_004 (missing or damaged items), similarity 0.41 | The rule did not see "it" as a reference (two own topic words), and the standalone match passed `ANSWER_MIN_SIMILARITY` (0.40) by 0.01 |
| INT-23 | "Do you have a loyalty rewards programme?" was answered with faq_017 (discount codes), similarity 0.45 | A false positive at the experimental 0.40 threshold |

None of the three is an integration bug, and none involves an unsafe or invented answer: each shows a real, approved FAQ answer that does not fit the question. Fixing them means retuning the AI pipeline (the follow-up rule and the acceptance threshold), which should be done with the existing labelled evaluations (`scripts/evaluate_hybrid_search.py`, `scripts/evaluate_conversations.py`) so other questions don't regress. Not done in this stage. Turning on the LLM rewrite (`QUERY_REWRITE_USE_LLM`, with `LLM_ENABLED=true`) is expected to help INT-05 and INT-10. That is not measured here, because it would need paid calls.

Turn latency in that run: median 99 ms, max 4.1 s (the maximum was not analysed further).
