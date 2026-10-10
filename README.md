# AI Customer Support Chatbot

An AI-powered customer support chatbot API built with FastAPI, designed to be embedded into client websites.

**Status:** Stage 1. The project foundation is in place: API skeleton, configuration, logging and tests. The chat endpoint returns a placeholder reply until the AI model is connected.

## Tech stack

- Python 3.11+ (developed on 3.13)
- FastAPI + Uvicorn
- Pydantic v2 / pydantic-settings
- pytest, Ruff

## Project structure

```
app/
├── main.py                 # App factory (create_app) and ASGI entry point
├── api/routes/             # HTTP endpoints, one module per resource
│   ├── health.py
│   └── chat.py
├── core/
│   ├── config.py           # Typed settings from environment / .env
│   ├── logging_config.py   # Logging setup with per-request IDs
│   └── middleware.py       # Request ID, access log, safe 500 handler
├── services/               # Business logic, independent of HTTP
├── models/                 # Pydantic request/response models
└── database/               # Persistence layer (later stage)
frontend/                   # Streamlit, then React UI (later stage)
data/                       # Knowledge-base documents (later stage)
tests/                      # pytest suite
requirements.txt            # Pinned dependencies
pyproject.toml              # Project metadata and tool settings (pytest, Ruff)
```

## Getting started (Windows, PowerShell)

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

If PowerShell blocks `Activate.ps1`, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

### Run the API

```powershell
uvicorn app.main:app --reload
```

- API root: http://127.0.0.1:8000/
- Interactive docs: http://127.0.0.1:8000/docs (disabled when `ENVIRONMENT=production`)

### Run tests and linting

```powershell
pytest
pytest --cov=app --cov-report=term-missing
ruff check .
ruff format .
```

## API

| Method | Path             | Description                     |
|--------|------------------|---------------------------------|
| GET    | `/`              | Service info                    |
| GET    | `/api/v1/health` | Health check                    |
| POST   | `/api/v1/chat`   | Send a message, receive a reply |

Example:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/chat `
  -ContentType "application/json" -Body '{"message": "Where is my order?"}'
```

Every response includes an `X-Request-ID` header that matches the `req=` field in the server logs.

## Configuration

Settings are read from environment variables or a `.env` file. See [.env.example](.env.example).

| Variable       | Default       | Description                                        |
|----------------|---------------|----------------------------------------------------|
| `ENVIRONMENT`  | `development` | `development`, `staging` or `production`           |
| `LOG_LEVEL`    | `INFO`        | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`  |
| `CORS_ORIGINS` | `[]`          | JSON list of allowed browser origins               |

Never commit `.env`. It is excluded by `.gitignore`.
