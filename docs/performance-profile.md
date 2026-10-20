# Application performance profile (Stage 13)

Measured on 2026-10-20 on the development machine: Windows 10 (19045), Intel Core
(family 6, model 78, 4 logical CPUs), 8 GB RAM, no GPU, Python 3.13.3. Docker 28.1.1 runs
in WSL 2 (kernel 5.15, 4 vCPUs, 3.8 GB, cgroup v1). Every figure below was measured
then. Nothing is estimated, and nothing here is a performance improvement: this task
only measures.

Raw results: [data/performance_profile_local.json](../data/performance_profile_local.json),
[data/performance_profile_docker.json](../data/performance_profile_docker.json).
Earlier in-process measurements (Stage 9): [docs/performance-and-evaluation.md](performance-and-evaluation.md).

## How to reproduce

```powershell
pip install -r requirements.txt                      # includes psutil (measurement only)
python scripts/profile_performance.py --target local --output data/performance_profile_local.json

# Production images, built from deploy/Dockerfile and tagged :profile
docker build -f deploy/Dockerfile --target backend  -t support-chatbot-backend:profile .
docker build -f deploy/Dockerfile --target customer -t support-chatbot-customer:profile .
docker build -f deploy/Dockerfile --target admin    -t support-chatbot-admin:profile .
python scripts/profile_performance.py --target docker --docker "wsl.exe -e docker" `
    --model-dir <the all-MiniLM-L6-v2 folder as the Docker host sees it> `
    --output data/performance_profile_docker.json

python scripts/measure_performance.py --offline --llm mock --skip-frontend   # LLM path
```

From Git Bash, set `MSYS_NO_PATHCONV=1` first, or the `/mnt/...` model path is rewritten.

`profile_performance.py` starts the **real servers** (uvicorn, Streamlit) and measures
them from outside over HTTP on 127.0.0.1:

- **local**: processes from the virtual environment. Memory is resident memory of the
  whole process tree (the venv's `python.exe` is a launcher with a child interpreter);
  CPU is user + system time of that tree.
- **docker**: the production images with the production limits from
  `deploy/compose.yaml` (backend 2 GB / 2 CPUs, pages 512 MB / 1 CPU, read-only root
  filesystem). Memory and CPU come from the container's cgroup, computed as
  `docker stats` computes them (usage minus reclaimable file cache).

Fixed for every run: `LLM_ENABLED=false` (**no paid call is possible**), the developer's
`.env` and shell settings ignored, defaults otherwise (`SEMANTIC_INDEX_PRELOAD=false`,
so the model loads on the first chat), rate limits off, a fresh temporary database.
Workload: 3 cold starts per server, 3 model loads in fresh processes (local), then 40 warm chat
turns over fixed conversations, 20 history reads, and 30 calls each to keyword, semantic and
hybrid search. The backend's own timings come from its in-memory metrics
(`GET /admin/monitoring`, [docs/monitoring.md](monitoring.md)).

## Results

### Startup

Seconds from launching the process (local) or `docker run` (docker) until the endpoint
answers 200. Three runs each: the first run of a series is the slowest (cold disk cache).

| Server | Local, 3 runs | Docker, 3 runs |
|--------|---------------|----------------|
| Backend live (`/api/v1/health`) | 10.27, 3.59, 4.53 s | 10.79, 4.52, 4.22 s |
| Backend ready (`/api/v1/ready`) | 10.80, 4.16, 5.06 s | 11.28, 5.05, 4.71 s |
| Customer page (`/_stcore/health`) | 4.68, 3.10, 3.10 s | 3.60, 2.96, 2.94 s |
| Admin page (`/_stcore/health`) | 3.13, 3.12, 3.16 s | 3.27, 2.61, 2.68 s |

Ready follows live by about 0.5 s. Docker times include the `docker run` command itself
(here through `wsl.exe`). The customer page's first script run against the live backend
(Streamlit `AppTest`, no browser) took **1.55 s**; sending a message then took 63 ms.

### Embedding model load

| How | Measured |
|-----|----------|
| Fresh Python process: import, load `all-MiniLM-L6-v2`, embed one text (local, 3 runs) | 59.31 s, 18.42 s, 18.53 s (imports 1.1–1.3 s on top) |
| Inside the first chat request (`faq_index_build_duration`: load + index the FAQs) | local 18.9 s; docker 22.3 s |
| Same, in the Stage 9 in-process tool run today (`--llm mock`) | 67.1 s for the whole first turn |

The load varies from about 18 s to over 60 s. The slow runs were each the first load of
a series, which points to the operating system's disk cache; that cause was not isolated.

### Chat requests

| | Local | Docker |
|---|---:|---:|
| **First chat request** (loads the model on demand) | **19.0 s** | **22.4 s** |
| CPU used by the first request | 16.7 s | 16.6 s |
| **Warm chat request**, client side, p50 / p95 / max | **33.3 / 41.6 / 45.5 ms** | 91.1 / 136.9 / 162.9 ms ⚠ |
| Warm chat turn inside the backend (`chat_turn_duration`) p50 / p95 | 22.0 / 31.3 ms | 35.9 / 86.6 ms |
| … retrieval and generation stage (`pipeline`) p50 | 15.0 ms | 20.8 ms |
| … storing the exchange (`store`) p50 | 4.8 ms | 9.8 ms |
| … loading history (`history`) p50 | 2.3 ms | 3.9 ms |
| … query rewrite p50 | 0.0 ms | 0.0 ms |

⚠ The Docker client-side figures for POST requests are inflated by this machine's
Windows-to-WSL port relay, not by the application. See "Docker POST latency" below.

### Retrieval

Over HTTP, client side, 30 calls each; and the same requests timed inside the backend.

| | Local client p50 / p95 | Local backend p50 | Docker backend p50 | Docker, direct to the container p50 / p95 |
|---|---:|---:|---:|---:|
| Keyword (`GET /faqs/search`) | 3.9 / 5.5 ms | 2.3 ms | 2.9 ms | — |
| Semantic (`POST /semantic-search`) | 19.4 / 24.4 ms | 17.6 ms | 64.9 ms ⚠ | 17.1 / 20.4 ms |
| Hybrid (`POST /hybrid-search`) | 18.9 / 25.8 ms | 17.1 ms | 67.7 ms ⚠ | — |
| Retrieval inside a chat turn (`retrieval_duration`) | — | 14.6 ms | 21.8 ms | — |

Raw query embedding, the bulk of semantic retrieval, 60 encodes: Windows 27.7 ms p50;
container 17.1 ms p50 with `--cpus 2.0`, 18.7 ms without a limit. PyTorch uses 2 threads
in both.

### Database

The backend's `db_transaction_duration` (whole transactions including commit, not just
statements), and a history read over HTTP:

| | Local p50 / p95 / max | Docker p50 / p95 / max |
|---|---:|---:|
| Read transaction (289) | 1.0 / 2.1 / 4.6 ms | 1.5 / 4.0 / 9.7 ms |
| Write transaction (105) | 2.2 / 5.3 / 59.0 ms | 7.1 / 15.1 / 79.0 ms |
| History read, client side (20) | 8.7 / 16.6 ms | 9.9 / 20.9 ms |

The counts cover every request of the run (conversation creation, chat, history reads),
not chat turns only. Write transactions are about 3× slower in the container, where the
database is on a Docker volume in the WSL 2 virtual disk.

### LLM generation

**Live OpenAI latency: not measured.** It needs paid API calls, which were not
approved, so `LLM_ENABLED=false` was forced and the backend reported 0 LLM calls in both
runs. Measured instead, with the local stand-in that replies instantly
(`measure_performance.py --llm mock`, 41 turns, 35 of them through the LLM path):

| | p50 | p95 |
|---|---:|---:|
| Whole chat turn | 40.5 ms | 47.6 ms |
| Grounded answer (retrieval, prompt, generation, validation) | 20.1 ms | 27.3 ms |
| The stand-in LLM call itself | 0.1 ms | 0.2 ms |
| Prompt size | 4,222 characters (median), 4,344 max | |

So this application adds a few milliseconds around a generation; the real call's
latency is OpenAI's and will dominate every answered turn when the LLM is on.

### Memory

| | Local (process tree RSS) | Docker (cgroup, as `docker stats`) |
|---|---:|---:|
| Backend after startup | 94 MiB | 76–91 MiB |
| Backend idle, before the model | 93.9 MiB | 78.3 MiB |
| Backend after the model load | 591.4 MiB | 541.4 MiB |
| Backend after 40 warm turns and 90 searches | 582.6 MiB | 541.6 MiB |
| Backend peak (sampled every 0.5 s) | 591.5 MiB | 553.0 MiB |
| Customer page after startup | 68 MiB | 51–59 MiB |
| Admin page after startup | 68–69 MiB | 51–53 MiB |

The embedding model adds about 460–500 MiB and memory stays flat afterwards. The backend
peak is 27% of its 2 GB container limit; each page is about 11% of its 512 MB limit
(an idle page; every browser session adds to it).

### CPU

| | Local | Docker (`--cpus 2.0`) |
|---|---:|---:|
| Backend idle (10 s) | 0.0% of one core | 0.7% of one core |
| First chat request (model load) | 16.7 CPU-s | 16.6 CPU-s |
| **Per warm chat turn** | **0.082 CPU-s** | **0.083 CPU-s** |
| During back-to-back warm turns | 190% of one core | 82% of one core ⚠ |

CPU per turn is the same in both. The utilisation differs because the Docker run spends
more wall time per turn in the port relay. With `--cpus 2.0`, the container was throttled
in **0 of 25** CPU periods during 30 back-to-back semantic searches. The limit is not
reached at this load.

### Container images

`docker image inspect` sizes (uncompressed), built from `deploy/Dockerfile` at the
commit before this report:

| Image | Size |
|-------|-----:|
| `support-chatbot-backend` | 1,507,746,069 bytes (1.40 GiB) |
| `support-chatbot-customer` | 572,503,653 bytes (546 MiB) |
| `support-chatbot-admin` | 572,503,653 bytes (546 MiB), the same layers as customer |

The embedding model is not in the image (it is mounted from the `model-init` volume).
Build times for this run on this machine (with whatever layers Docker had cached):
backend 277 s, customer 82 s, admin 4 s.

## Docker POST latency: a measurement artefact on this machine

In the Docker run, POST requests looked 3–4× slower than locally (semantic search
65 ms in the backend's own timer, against 17.6 ms locally). GET requests did not
(keyword search 2.9 ms). Two hypotheses were tested:

1. **CPU throttling by `--cpus 2.0`: rejected.** 0 throttled periods out of 25, and
   raw encoding in the container is as fast as on Windows (17 ms).
2. **The Windows-to-WSL port relay: confirmed.** The same 30 semantic searches against
   the same container: **17.1 ms p50** from inside WSL straight to the container, **65.1
   ms p50** from Windows through `127.0.0.1:18000`. The backend's request timer includes
   reading the request body, so the relay's delay on the body shows up there as well.

In production, Nginx reaches the backend on the same Linux Docker network, so this relay
does not exist. Use the backend's internal stage timings and the direct figure for
Docker, not the client-side POST times.

During the investigation, a backend container exited (code 255, not OOM-killed) twice
while a request was in flight through the relay. It did not happen in 11 of 11 repeats
started and called from inside WSL (5 without and 6 with the published port and both
CPU settings), nor in either full profiling run. It was not reproduced, so it is
recorded here, not explained.

## Observations

These are measurements, not changes. Nothing was tuned for this report.

1. **The first chat request after startup takes 19–22 s (up to about 60 s on a cold disk
   cache)** because the model loads on demand (`SEMANTIC_INDEX_PRELOAD=false`, the
   default). The backend is "ready" after 4–5 s, well before the model is loaded.
   `SEMANTIC_INDEX_PRELOAD=background` exists for this (Stage 9); its effect on first-
   request latency was not re-measured here.
2. **A warm chat turn costs about 22 ms inside the backend locally (36 ms in the
   container) and 0.08 CPU-seconds.** Semantic retrieval (query embedding) is the largest
   share; storage is next (writes 2–7 ms each).
3. **Memory is dominated by the embedding model** (~0.5 GB), stable after loading, and
   well within the container limits.
4. **The backend image is 1.40 GiB**, the pages 546 MiB each (customer and admin are
   the same layers with a different command).
5. **Not measured:** live OpenAI latency (no approval for paid calls), browser rendering,
   behaviour under concurrent users (Stage 12's `load_test_local.py` covers load), and
   a Linux host without WSL 2.
