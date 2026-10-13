# AI Customer Support Chatbot

A backend API for an AI-powered customer support chatbot that businesses can embed into their websites. It is built with FastAPI and designed to grow in stages, from a well-tested foundation to a production-ready assistant that answers customer questions from each client's own knowledge base.

> **Current status: Stage 2 (FAQ knowledge base) is complete.**
> The API serves a validated FAQ knowledge base with listing, category filtering, pagination and **keyword** search.
> **No AI features are operational yet.** Search is deterministic keyword matching, not semantic or AI search. The API does not accept chat messages, call a language model or generate answers. Those are planned for later stages (see [Roadmap](#development-roadmap)).

---

## Contents

- [Overview](#overview)
- [Implementation status](#implementation-status)
- [Planned features](#planned-features)
- [Technology stack](#technology-stack)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Setup on Windows](#setup-on-windows)
- [Configuration](#configuration)
- [Running the API](#running-the-api)
- [API reference](#api-reference)
- [Running tests](#running-tests)
- [Logging](#logging)
- [Development roadmap](#development-roadmap)
- [Troubleshooting](#troubleshooting)

---

## Overview

### Purpose

Small and medium-sized businesses often answer the same customer questions repeatedly: order status, returns, pricing, opening hours. This project aims to provide a support assistant that:

- answers common questions instantly, around the clock;
- bases its answers on the business's own documents (FAQs, policies, product information) rather than generic knowledge;
- can be added to an existing website with minimal effort;
- hands over to a human when it cannot help.

### Intended users

| User | What they get |
|------|---------------|
| **Business owners and support teams** | A chatbot on their website that reduces repetitive support work |
| **Website visitors and customers** | Fast answers to common questions without waiting for an agent |
| **Developers and freelancers** | A clean, documented, tested codebase to customise and deploy for clients |

---

## Implementation status

### Implemented

**Stage 2: FAQ knowledge base**

| Area | Details |
|------|---------|
| FAQ data | 30 fictional e-commerce FAQs in `data/faqs.json` (replaceable via `FAQ_DATA_PATH`), validated on startup |
| Validation | Pydantic models reject blank text, bad IDs, unknown categories, invalid keywords, duplicate IDs; the app refuses to start on bad data |
| Repository | Loads the JSON once with specific errors for missing files, malformed JSON, invalid records and duplicates |
| Keyword search | Deterministic, explainable scoring (exact question first, then keywords and words); a baseline for future semantic search |
| FAQ endpoints | List (with category filter and pagination), categories, search, get by ID; inactive FAQs are never exposed |
| Error handling | 404 / 422 / 503 / 500 with no internal paths or stack traces in responses; control characters escaped in logs |

**Stage 1: Foundation**

| Area | Details |
|------|---------|
| FastAPI application | App factory (`create_app`), versioned routes under `/api/v1` |
| Endpoints | `GET /` (API information), `GET /api/v1/health` (health check) |
| API documentation | Swagger UI (`/docs`), ReDoc (`/redoc`), OpenAPI schema (`/openapi.json`); disabled in production |
| Configuration | Typed, validated settings via pydantic-settings and `.env`; invalid values stop startup |
| Security defaults | Secrets kept out of code and logs, `DEBUG` blocked in production, CORS closed by default, generic 500 responses |
| Logging | Standard `logging`, configurable level, one consistent format, per-request IDs, redaction of tokens and emails |
| Tests | 411 pytest tests, 100% line coverage, fully offline (network access is blocked during tests) |
| Tooling | Pinned dependencies, Ruff linting and formatting, Git-ready project files |

### Not yet implemented

- Chat endpoint and conversation handling
- Any language model (LLM) integration
- Semantic (meaning-based) search: current search matches words only, with no synonyms or stemming
- Document ingestion and retrieval-augmented generation (RAG)
- Database and conversation history
- User interface (Streamlit or React)
- Authentication, rate limiting, Docker and deployment

The `frontend/` folder is a placeholder for these later stages. `app/database/` currently holds the JSON-backed FAQ repository; no SQL database is used.

---

## Planned features

These are goals, not current functionality.

- **AI chat:** natural-language answers to customer questions via a `/chat` endpoint
- **Knowledge-base answers (RAG):** answers grounded in each client's documents, with fewer made-up responses
- **Conversation memory:** context kept across messages in a session
- **Human handover:** escalation when the assistant cannot answer confidently
- **Admin interface:** upload and manage knowledge-base documents
- **Embeddable widget:** a React chat widget that clients add to their site with a script tag
- **Multi-client support:** separate configuration and knowledge per business
- **Production readiness:** authentication, rate limiting, monitoring, Docker deployment

---

## Technology stack

### In use now

| Purpose | Technology |
|---------|------------|
| Language | Python 3.11+ (developed and tested on 3.13) |
| Web framework | [FastAPI](https://fastapi.tiangolo.com/) |
| ASGI server | [Uvicorn](https://www.uvicorn.org/) |
| Data validation | [Pydantic v2](https://docs.pydantic.dev/) |
| Configuration | [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) + python-dotenv |
| Testing | pytest, pytest-cov, FastAPI `TestClient` (httpx) |
| Code quality | [Ruff](https://docs.astral.sh/ruff/) (lint + format) |

### Installed for Stage 3 (not yet used by application code)

| Purpose | Technology |
|---------|------------|
| Sentence embeddings | [sentence-transformers](https://www.sbert.net/) with the `all-MiniLM-L6-v2` model (384-dimensional vectors), running locally on the CPU |
| Numerical arrays | NumPy |
| Model runtime | PyTorch (CPU build, installed as a dependency of sentence-transformers) |

### Planned (not installed)

| Purpose | Candidates |
|---------|------------|
| Language model | OpenAI API or another LLM provider |
| Vector database | e.g. Chroma, once the knowledge base outgrows in-memory search |
| Frontend | Streamlit (prototype), then React |

---

## Architecture

### Request flow

```mermaid
flowchart LR
    Client["Client<br/>(browser, curl, frontend)"] --> Uvicorn
    Uvicorn --> MW["RequestContextMiddleware<br/>request ID · access log · safe 500"]
    MW --> CORS["CORSMiddleware<br/>(only if origins configured)"]
    CORS --> Routes["API routes<br/>app/api/routes"]
    Routes --> Service["FAQService<br/>app/services"]
    Service --> Search["KeywordSearcher<br/>(replaceable)"]
    Service --> Repo["FAQRepository<br/>app/database"]
    Repo -. "loaded once at startup" .-> Data[("data/faqs.json")]
    Settings["Settings<br/>app/core/config.py"] -.-> Routes
```

### Layers

| Layer | Folder | Responsibility | Status |
|-------|--------|----------------|--------|
| Entry point | `app/main.py` | Builds and configures the app | Implemented |
| API | `app/api/routes/` | HTTP only: parse requests, return responses | Implemented |
| Models | `app/models/` | Request and response schemas | Implemented |
| Core | `app/core/` | Settings, logging, middleware | Implemented |
| Services | `app/services/` | FAQ rules (active-only, pagination) and keyword search, independent of HTTP | Implemented |
| Database | `app/database/` | Loads and validates the FAQ JSON file (no SQL database yet) | Implemented |

Routes stay thin and delegate to services. The FAQ service is built once in `create_app()` and handed to routes with FastAPI's `Depends`; the search strategy sits behind a `FAQSearcher` interface so a semantic searcher can replace it later without touching the HTTP layer.

### Project structure

```
ai-customer-support-chatbot/
├── app/
│   ├── __init__.py            # Package version (__version__)
│   ├── main.py                # create_app() factory and ASGI app
│   ├── api/
│   │   ├── dependencies.py    # get_faq_service (FastAPI dependency)
│   │   ├── errors.py          # 422 logging, 503 for FAQ data failures
│   │   └── routes/
│   │       ├── health.py      # GET /api/v1/health
│   │       └── faqs.py        # GET /api/v1/faqs, /categories, /search, /{faq_id}
│   ├── core/
│   │   ├── config.py          # Typed settings from environment / .env
│   │   ├── logging_config.py  # Logging setup and redaction
│   │   └── middleware.py      # Request ID, access log, safe 500 handler
│   ├── models/
│   │   ├── faq.py             # FAQ model, validation rules, API response models
│   │   ├── health.py          # HealthResponse
│   │   └── root.py            # RootResponse
│   ├── services/
│   │   ├── faq_service.py     # Active-only rules, categories, pagination
│   │   └── faq_search.py      # KeywordSearcher and the FAQSearcher interface
│   └── database/
│       └── faq_repository.py  # Load/validate data/faqs.json, typed loading errors
├── frontend/                  # (later stages) Streamlit, then React
├── data/
│   └── faqs.json              # Sample FAQ knowledge base (fictional store; replaceable)
├── tests/                     # pytest suite
├── .env.example               # Documented example configuration
├── requirements.txt           # Pinned, tested dependencies
├── pyproject.toml             # Project metadata, pytest and Ruff settings
└── README.md
```

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Windows 10 or 11 | Instructions use PowerShell. macOS/Linux work with equivalent commands |
| Python 3.11 or newer | 3.13 recommended. Check with `py --version` |
| Git | Check with `git --version` |
| VS Code (optional) | With the Python extension |

No API keys or internet services are needed to run the current stage.

---

## Setup on Windows

Run all commands in **PowerShell** from the project root.

### 1. Get the code

```powershell
git clone https://github.com/<your-username>/ai-customer-support-chatbot.git
cd ai-customer-support-chatbot
```

### 2. Create a virtual environment

```powershell
py -3.13 -m venv .venv
```

This creates an isolated Python environment in `.venv/`, so project packages don't affect other projects. Use `py -3.11` or `py -3.12` if that is the version you have.

### 3. Activate the virtual environment

```powershell
.\.venv\Scripts\Activate.ps1
```

Your prompt should now start with `(.venv)`. Activate it in every new terminal before working on the project.

| Shell | Activation command |
|-------|--------------------|
| PowerShell | `.\.venv\Scripts\Activate.ps1` |
| Command Prompt (cmd) | `.venv\Scripts\activate.bat` |
| Git Bash | `source .venv/Scripts/activate` |

To deactivate, run `deactivate`.

**VS Code:** press `Ctrl+Shift+P`, choose **Python: Select Interpreter**, and pick `.venv\Scripts\python.exe`. New terminals will then activate it automatically.

### 4. Install dependencies

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

`requirements.txt` pins exact versions that have been tested together, so every machine installs the same setup.

> **Large download:** the semantic-search dependencies include PyTorch, so the first install downloads several hundred MB and the virtual environment grows to about **1 GB**. On Windows the PyPI build of PyTorch is CPU-only; no GPU is needed.

### 5. Configure the environment

```powershell
Copy-Item .env.example .env
```

The defaults work as-is for local development. See [Configuration](#configuration) to change them.

---

## Configuration

Settings are defined in [app/core/config.py](app/core/config.py) using pydantic-settings. Every variable is optional, and the defaults are safe for local development.

### How values are loaded

Values are resolved in this order (highest priority first):

1. **Environment variables** set in the shell or on the server, e.g. `$env:APP_ENV = "staging"`
2. **`.env` file** in the project root. It is always found, whichever directory you run commands from.
3. **Defaults** defined in `Settings`

Variable names are case-insensitive. All values are validated at startup, so an invalid value (e.g. `APP_ENV=prod`) stops the app with a clear error instead of misbehaving later.

### Variables

| Variable | Default | Rules |
|----------|---------|-------|
| `APP_NAME` | `AI Customer Support Chatbot API` | 1–100 characters |
| `APP_ENV` | `development` | `development`, `staging` or `production`. Production disables the docs endpoints |
| `DEBUG` | `false` | `true` / `false`. Must be `false` in production |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` |
| `API_V1_PREFIX` | `/api/v1` | Starts with `/`, no trailing slash |
| `CORS_ORIGINS` | `[]` | JSON list of full origins, e.g. `["http://localhost:8501"]`. No `*` |
| `FAQ_DATA_PATH` | `data/faqs.json` | FAQ JSON file; relative paths resolve from the project root. The app will not start if it is missing or invalid |
| `EMBEDDING_MODEL_NAME` | `sentence-transformers/all-MiniLM-L6-v2` | Hugging Face model ID or local folder for semantic search (1–200 characters). See below |
| `OPENAI_API_KEY` | *(unset)* | **Not used yet.** Optional, reserved for a later stage |

The application version is defined in code (`app/__init__.py`), not in the environment.

### Embedding model

- The model is **not** loaded when the app is imported or started; it is loaded the first time semantic search needs it. Importing `sentence-transformers` and PyTorch alone can take 10–30+ seconds, so ordinary startup and tests stay fast. A test enforces this.
- **First use downloads the model** from Hugging Face (about 90 MB for the default) into `%USERPROFILE%\.cache\huggingface`. Set `HF_HOME` to use another folder.
- After the first download the model works offline. Set `HF_HUB_OFFLINE=1` to forbid any download attempt, e.g. on a server.
- To use a different model, set `EMBEDDING_MODEL_NAME` to another sentence-transformers model ID or to a local folder containing a saved model.

### Secrets

- **Never commit `.env`.** It is excluded by `.gitignore`. Only `.env.example`, which has no real values, is committed.
- **Never hardcode API keys** in source code. Set them in `.env` locally or as environment variables on the server.
- Secret settings use Pydantic's `SecretStr`, which displays as `**********` in output.
- At startup the app logs a summary that states *whether* a key is set, never its value.

---

## Running the API

With the virtual environment activated:

```powershell
uvicorn app.main:app --reload
```

`--reload` restarts the server when code changes. Use it in development only. The API is then available at:

| URL | Description |
|-----|-------------|
| http://127.0.0.1:8000/ | API information |
| http://127.0.0.1:8000/api/v1/health | Health check |
| http://127.0.0.1:8000/api/v1/faqs | FAQ list (see [API reference](#api-reference)) |
| http://127.0.0.1:8000/docs | Swagger UI: interactive docs where you can try requests |
| http://127.0.0.1:8000/redoc | ReDoc: readable reference documentation |
| http://127.0.0.1:8000/openapi.json | Machine-readable OpenAPI schema |

Stop the server with `Ctrl+C`. To use another port: `uvicorn app.main:app --reload --port 8001`.

---

## API reference

All responses are JSON. Every response includes an `X-Request-ID` header whose value also appears in the server logs (`req=...`), which helps trace a specific request.

### `GET /`

Returns basic information about the API.

```powershell
Invoke-RestMethod http://127.0.0.1:8000/
```

```json
{
  "message": "AI Customer Support Chatbot API",
  "version": "0.1.0"
}
```

### `GET /api/v1/health`

Reports that the service is running. Intended for uptime monitors, load balancers and deployment checks. It does no expensive work, so it is safe to call frequently.

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health
```

```json
{
  "status": "healthy",
  "service": "ai-customer-support-chatbot"
}
```

### FAQ endpoints

All FAQ endpoints are read-only and only ever return **active** FAQs. Public FAQ objects contain `id`, `question`, `answer` and `category`.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/faqs` | Active FAQs, paginated. Query: `category` (optional, case-insensitive), `offset` (default 0), `limit` (1–100, default 20) |
| `GET` | `/api/v1/faqs/categories` | Categories that contain at least one active FAQ, alphabetical |
| `GET` | `/api/v1/faqs/search` | Keyword search. Query: `q` (required, 1–200 characters), `limit` (1–50, default 5) |
| `GET` | `/api/v1/faqs/{faq_id}` | One active FAQ (ID is case-insensitive); `404` if missing or inactive |

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/faqs?category=shipping&limit=2"
```

```json
{
  "count": 2,
  "total": 6,
  "offset": 0,
  "limit": 2,
  "faqs": [
    { "id": "faq_006", "question": "How long does delivery take?", "answer": "Standard delivery usually takes ...", "category": "shipping" },
    { "id": "faq_007", "question": "How much does shipping cost?", "answer": "Shipping costs depend on ...", "category": "shipping" }
  ]
}
```

To page through results, repeat the request with `offset` increased by `limit` until `offset + count >= total`.

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/faqs/search?q=track+order&limit=1"
```

```json
{
  "query": "track order",
  "count": 1,
  "results": [
    {
      "id": "faq_001",
      "question": "How can I track my order?",
      "answer": "You can track your order from the My Orders section ...",
      "category": "orders",
      "score": 25,
      "reasons": ["keyword 'track' in query (+15)", "question words: order, track (+10)"]
    }
  ]
}
```

Search is **keyword matching**, not AI: results are ranked by fixed, explainable rules (an exact question match always ranks first), and `reasons` shows which rules matched. A query with no matches returns `200` with empty `results`.

### Documentation endpoints

| Method | Path | Returns |
|--------|------|---------|
| `GET` | `/docs` | Swagger UI (HTML) |
| `GET` | `/redoc` | ReDoc (HTML) |
| `GET` | `/openapi.json` | OpenAPI 3 schema (JSON) |

These return `404` when `APP_ENV=production`, so the full API map is not exposed publicly.

### Errors

| Status | When | Body |
|--------|------|------|
| `404` | Unknown path | `{"detail": "Not Found"}` |
| `404` | Unknown or inactive FAQ ID | `{"detail": "FAQ not found"}` |
| `422` | Invalid parameter (empty `q`, unknown `category`, bad `limit`/`offset`) | FastAPI's standard list of field errors |
| `503` | FAQ data unavailable while serving a request | `{"detail": "The FAQ knowledge base is temporarily unavailable.", "request_id": "<id>"}` |
| `500` | Unexpected server error | `{"detail": "Internal server error", "request_id": "<id>"}` |

Internal error details are written to the server log only, never returned to the client.

---

## Running tests

With the virtual environment activated, from the project root:

```powershell
pytest                                       # run all tests
pytest -v                                    # list each test
pytest tests/test_health.py                  # run one file
pytest --cov=app --cov-report=term-missing   # with a coverage report
```

The suite currently has **411 tests with 100% line coverage**, and runs in about 20 seconds.

| File | Covers |
|------|--------|
| `test_root.py` | `GET /` status and response |
| `test_health.py` | Health endpoint status, structure and routing |
| `test_docs.py` | OpenAPI schema, Swagger UI, ReDoc; disabled in production |
| `test_config.py` | Defaults, `.env` and environment loading, validation, secret masking |
| `test_logging.py` | Levels, format, handler count, redaction |
| `test_app.py` | Request IDs, safe 500 errors, CORS |
| `test_isolation.py` | Proves tests cannot reach the internet |
| `test_faq_models.py` | FAQ validation and normalisation rules |
| `test_faq_repository.py` | Loading, all loading errors, lookups and category filters |
| `test_faq_search.py` | Keyword scoring rules, ordering and determinism |
| `test_faq_service.py` | Active-only rules, categories, pagination, search behaviour, Stage 3 baseline |
| `test_faq_api.py` | FAQ endpoints, pagination, validation errors, route ordering |
| `test_dependencies.py` | FAQ data loaded once per app; test injection |
| `test_error_handling.py` | 404/422/503/500 responses and safe diagnostic logging |
| `test_faq_data.py` | The bundled sample `data/faqs.json` (marked `sample_data`) |

Tests are fully isolated:

- They never call external APIs. Any attempt to reach a non-local host fails the test.
- They ignore your local `.env` and shell variables, so results are the same on every machine.
- `TestClient` calls the app in-process. No server needs to be running.
- FAQ tests use a small fixed dataset written to a temporary file, not `data/faqs.json`.

### Real embedding model test (opt-in)

Embedding tests use a fast fake model by default. One test loads the real model to check dimensions and similarity; it takes about a minute and needs the model already downloaded (it runs offline and never downloads):

```powershell
$env:RUN_EMBEDDING_MODEL_TESTS = "1"; pytest tests/test_embedding_service.py; Remove-Item Env:RUN_EMBEDDING_MODEL_TESTS
```

### Using client-specific FAQ data

After replacing `data/faqs.json` with a client's FAQs, skip the checks that are specific to the bundled sample:

```powershell
pytest -m "not sample_data"
```

### Code quality

```powershell
ruff check .      # lint
ruff format .     # auto-format
```

---

## Logging

Logging uses Python's standard `logging` module, configured in [app/core/logging_config.py](app/core/logging_config.py).

```
2026-10-10 22:28:19,652 | INFO     | app.request | req=95389afc4a29... | GET /api/v1/health -> 200 (50.7 ms)
```

- **Format:** timestamp, level, logger name, request ID and message. Uvicorn's own messages use the same format.
- **Level:** controlled by `LOG_LEVEL`.
- **No duplicates:** a single handler on the root logger. Reconfiguring never repeats output.
- **Sensitive data:** requests are logged by path only, without query strings, headers or bodies. As a safety net, bearer tokens, API keys, `password=`/`token=` values and email addresses are redacted from every line, including tracebacks.
- **In new modules:** use `logger = logging.getLogger(__name__)`.

---

## Development roadmap

The roadmap is indicative; scope and order may change as the project evolves.

| Stage | Focus | Status |
|-------|-------|--------|
| **1. Foundation** | FastAPI structure, configuration, logging, tests, Git setup | **Complete** |
| **2. FAQ knowledge base** | FAQ models, JSON repository, keyword search baseline, FAQ REST API | **Complete** |
| **3. Semantic search (RAG)** | Embeddings, vector search, comparison against the keyword baseline | Planned |
| **3b. AI chat** | `/chat` endpoint, LLM integration grounded in retrieved FAQs | Planned |
| **4. Conversations and storage** | Database, session history, conversation memory | Planned |
| **5. Streamlit frontend** | Chat UI and simple admin page for testing and demos | Planned |
| **6. React widget** | Embeddable chat widget for client websites | Planned |
| **7. Production readiness** | Authentication, rate limiting, Docker, deployment, monitoring | Planned |

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `Activate.ps1 cannot be loaded because running scripts is disabled` | Run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `py` is not recognised | Install Python from [python.org](https://www.python.org/downloads/) with the **py launcher** option, or use `python` instead |
| `ModuleNotFoundError: No module named 'fastapi'` | The virtual environment is not active. Activate it and reinstall with `pip install -r requirements.txt` |
| `ModuleNotFoundError: No module named 'app'` | Run commands from the project root, not from inside `app/` or `tests/` |
| App stops with a `ValidationError` on startup | A value in `.env` or the environment is invalid. The message names the variable; compare with `.env.example` |
| Port 8000 is already in use | Start on another port: `uvicorn app.main:app --reload --port 8001` |
| `/docs` returns 404 | `APP_ENV` is set to `production`. Use `development` locally |
| `huggingface_hub ... cache-system uses symlinks ... your machine does not support them` warning | Harmless on Windows: the model cache works but uses a little more disk space. Enable Windows Developer Mode to allow symlinks, or set `HF_HUB_DISABLE_SYMLINKS_WARNING=1` to hide the warning |
| First semantic search is slow | The model is being downloaded and loaded (one-off). Later calls reuse it |
| App stops with `Cannot start: FAQ knowledge base failed to load` | `FAQ_DATA_PATH` points to a missing or invalid file. The log line names the error type, file and failing records |
