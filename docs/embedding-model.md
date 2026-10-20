# Embedding model: version, loading and caching (Stage 13)

How the sentence-transformer model for semantic search is fetched, verified, loaded, shared
and cached, and what it costs. Code: [app/services/embedding_service.py](../app/services/embedding_service.py)
(loading), [app/services/model_files.py](../app/services/model_files.py) (files and
checksums), [app/services/semantic_search_service.py](../app/services/semantic_search_service.py)
(the FAQ index), [scripts/fetch_embedding_model.py](../scripts/fetch_embedding_model.py)
(compose's `model-init`).

## Model version

| | |
|---|---|
| Repository | `sentence-transformers/all-MiniLM-L6-v2` (Hugging Face Hub) |
| Revision (pinned) | `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, the commit every evaluation used |
| License | Apache-2.0 |
| Architecture | BERT (`BertModel`), 6 layers, hidden size 384, 12 attention heads, vocabulary 30,522 |
| Parameters | 22,713,216 (90.9 MB as float32) |
| Output | 384-dimensional vectors, mean pooling, normalised to length 1 |
| Input limit | 256 tokens (`max_seq_length`); longer text is truncated by the model |
| Weights file | `model.safetensors`, 90,868,376 bytes, SHA-256 `53aa51172d142c89d9012cce15ae4d6cc0ca6895895114379cacb4fab128d9db` |
| Files used | 12 files, 91.6 MB (weights, tokenizer, configuration, model card) |
| Runtime | sentence-transformers 6.1.0, PyTorch 2.14.1 (CPU), huggingface_hub 1.33.0 |

The revision is pinned in three places that a test keeps equal
(`test_the_app_and_the_deployment_pin_the_same_revision`):

- the deployment: `EMBEDDING_MODEL_REVISION` in `deploy/.env.example` and compose's
  `model-init`;
- the application: `PINNED_EMBEDDING_MODEL_REVISIONS` in `app/core/config.py`. When
  `EMBEDDING_MODEL_NAME` is the default Hub ID (local development, evaluation scripts),
  that commit is loaded unless `EMBEDDING_MODEL_REVISION` names another. Another model
  gets no invented pin: it loads its latest revision unless one is set.

Changing the revision means a new evaluation run (`scripts/evaluate_*.py`), a new
`model-init` and new checksums; nothing else adapts automatically.

## Measured cost

From [docs/performance-profile.md](performance-profile.md) (2026-10-20, 4-CPU laptop) and
this task's checks:

| | Local (Windows) | Docker (production limits) |
|---|---:|---:|
| Backend memory before the model | 93.9 MiB | 78.3 MiB |
| After the model and FAQ index | 591.4 MiB | 541.4 MiB |
| **Added by the model** | **≈ 497 MiB** | **≈ 463 MiB** |
| Peak during 40 chat turns and 90 searches | 591.5 MiB | 553.0 MiB (27% of the 2 GB limit) |
| Load (model + index), first chat | 18.9 s | 22.3 s; 17.5–17.7 s in this task's runs |
| Load in a fresh process, 3 runs | 59.3, 18.4, 18.5 s | — |
| Embed one query, p50 | 22–28 ms | 17 ms |
| Embed all 30 FAQ questions, p50 of 5 | 140 ms | — |
| `model-init` download | — | 91.6 MB in 35 s (was 977 MB) |

The Streamlit pages hold no model: 51–69 MiB each.

## Review of the loading strategy

| Requirement | How it is met | Evidence |
|---|---|---|
| Never reload per request | One `EmbeddingService` per application (built in `create_app`), loaded on first use and kept for the process's life | `test_model_is_loaded_once_and_reused` |
| Not loaded by unrelated services | Only the backend loads it. The Streamlit pages call the API over HTTP, import nothing from `app`, and their image contains neither PyTorch nor sentence-transformers. Importing the backend loads nothing either | `test_development_tools_are_in_neither_image` (frontend lock has no torch, sentence-transformers or openai), `tests/test_main_import.py` |
| Safe reuse | The model object is only read (`encode`). The FAQ index is an immutable snapshot published by one reference assignment; readers never see a half-built index | `tests/test_semantic_search_service.py` |
| No concurrent initialization race | The model loads under a lock with a double check, so concurrent first requests load it once. The index builds under its own lock, so concurrent first searches, or a request during the background preload, build it once | `test_concurrent_first_requests_load_the_model_once`, `test_concurrent_first_use_builds_the_index_once`, `test_background_preload_serves_at_once_and_loads_the_model_once` |
| No unnecessary embedding regeneration | Question vectors are cached in memory by question text. A rebuild after an FAQ edit embeds only questions whose text changed; answer, tag, category or activation changes embed nothing. A request pinned to an older catalogue revision reuses cached vectors | `tests/test_semantic_search_service.py`, `tests/test_faq_update_consistency.py` |
| Initialization failures | A failed load (missing folder, corrupt files, no network) is logged with its cause. Chat continues with keyword-only retrieval or the safe "temporarily unavailable" reply; `/semantic-search` answers `503`, `retryable: true`. `/ready` reports `semantic_search: unavailable` while chat stays ready. The next attempt waits `EMBEDDING_LOAD_RETRY_SECONDS` (30 s), so queued requests do not each repeat it | `tests/test_safe_error_reporting.py`, `tests/test_readiness.py`; verified in Docker (below) |
| Cache corruption | See "Corruption" below | `tests/test_model_files.py`; verified in Docker |
| Model version documented | Section above; the backend also logs the verified repository and revision at every load | |
| Memory documented | Section above, measured | [docs/performance-profile.md](performance-profile.md) |
| No vector database | The index is a NumPy matrix in the backend's memory (30 FAQs × 384 values, float32 plus a float64 unit-length copy: about 138 KB); search is one matrix-vector product | |

## Fetching: only what the backend reads

`model-init` used to download the whole repository at the pinned revision: 30 files,
977 MB, of which the backend reads 12 (91.6 MB). The rest are the same weights for other
runtimes (ONNX ×8, OpenVINO ×2, TensorFlow, Rust, legacy PyTorch `.bin`) and training
files. It now runs `python -m scripts.fetch_embedding_model`, which:

1. asks the Hub for the file list at the pinned commit **with checksums** (SHA-256 for
   the weights, Git object IDs for the small files), and refuses a revision that is not
   a full commit;
2. downloads only the needed files that are missing or wrong, and checks each one
   after download;
3. removes everything else from the folder;
4. writes `model-manifest.json`: repository, revision, size and SHA-256 of each file.

A re-run is cheap and safe: intact files are kept (0 downloads on a second run). An
existing volume from the old `model-init` is migrated by re-running it: in the check,
932 MB became 88 MB on disk, with nothing re-downloaded.

## Corruption

Before loading a local folder, the backend checks every file against
`model-manifest.json`: presence, size, then SHA-256. This takes well under a second for
91.6 MB, against a ~18 s load. A mismatch is a load failure that names the files and
the fix, never a PyTorch traceback or a different model loaded silently:

```
ERROR Failed to load embedding model '/models/all-MiniLM-L6-v2': ModelFilesError: Embedding
model files in /models/all-MiniLM-L6-v2 are missing or corrupt: model.safetensors (checksum
mismatch). Fetch the model again (docker compose --profile setup run --rm model-init)
```

**Repair:** `docker compose --profile setup run --rm model-init`. Only the damaged files
are downloaded again. To check a folder without the network:

```
docker compose --profile setup run --rm model-init python -m scripts.fetch_embedding_model \
    --dest /models/all-MiniLM-L6-v2 --verify-only
```

Verified in Docker on 2026-10-20 with the production image:

| Step | Result |
|---|---|
| Fresh `model-init` | 12 files, 91.6 MB, 35 s; manifest written |
| Backend start, first search | Files verified (logged with the revision), model loaded in 17.7 s, `200` |
| One byte of `model.safetensors` flipped (same size) | Search `503 SERVICE_UNAVAILABLE`, `retryable: true`; `/ready` `semantic_search: unavailable`; the log line above |
| `model-init` with `HF_HUB_OFFLINE` set | One line: `Could not fetch … (OfflineModeIsEnabled): HF_HUB_OFFLINE is set, so the Hub cannot be reached`, exit 1 |
| `model-init` again | `1 downloaded, 11 already intact`; backend loads it, search `200` |

What this does **not** cover:

- **A folder without a manifest** (copied by hand, or filled by the old `model-init`
  and not re-run) loads **unverified**, with a warning in the log.
- **Local development** loads the Hub ID from the Hugging Face cache. On Windows that
  cache stores plain copies without checksums, so it is not verified at load. A broken
  cache fails to load (logged, keyword fallback). Repair: delete
  `~/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2` and let the
  next load download the pinned revision again. The development copy used for every
  evaluation was checked once against the Hub: `model.safetensors` matches the SHA-256
  above.
- **Damage after a successful load** is not detected: the model is in memory and the
  files are not read again until the next start (the volume is read-only for the
  backend).

## Cache persistence

| Cache | Where | Lifetime | Decision |
|---|---|---|---|
| Model files | `models` volume (Docker), Hugging Face cache (development) | Survives restarts and rebuilds | Kept: downloading on every start would need internet and take 35 s or more |
| Loaded model | Backend process memory | Until the process stops | Kept: one per process |
| FAQ question vectors | Backend process memory, by question text | Until the process stops | **Not persisted to disk.** Re-embedding all 30 FAQs takes 140 ms, against ~18 s to load the model, which is needed anyway to embed queries. A disk cache would save about 0.14 s per start and add a file that can go stale or be corrupted (and would need its own model-revision keying and verification). Revisit if the catalogue grows to many thousands of FAQs |

## Processes and workers

The model and the index live in **one process's memory**. The deployment runs exactly
one backend worker (`uvicorn --workers 1`, security audit SEC-09/SEC-10), and every
statement in this document is about that setup.

With several workers or containers, each would load its own copy (about 0.5 GB and
18–60 s each) and build its own index. Whether they would stay consistent after FAQ
edits (each process refreshes its catalogue and index when *it* notices a new
catalogue revision) **has not been tested**, and no claim is made for it. Measure it
before running more than one worker; a shared vector store would be the usual answer
at that point, and is deliberately not introduced here.

## Settings

| Setting | Default | Effect |
|---|---|---|
| `EMBEDDING_MODEL_NAME` | `sentence-transformers/all-MiniLM-L6-v2` (Docker: `/models/all-MiniLM-L6-v2`) | Hub ID or local folder |
| `EMBEDDING_MODEL_REVISION` | empty: the pinned commit for the default model | Hub commit to load; ignored for a folder |
| `SEMANTIC_INDEX_PRELOAD` | `false` (production env example: `background`) | Load on first use, before serving, or in the background right after startup |
| `EMBEDDING_LOAD_RETRY_SECONDS` | `30` | Wait between load attempts after a failure |
| `HF_HUB_OFFLINE` | `1` in the backend image | Never contact the Hub at runtime |
