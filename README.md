# AI Customer Support Chatbot

An AI-powered customer support chatbot API built with FastAPI, designed to be embedded into client websites.

**Status:** Stage 1. The project foundation is in place: API skeleton, configuration, logging and tests. Chatbot features arrive in later stages.

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
│   └── health.py
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
- Swagger UI: http://127.0.0.1:8000/docs
- ReDoc: http://127.0.0.1:8000/redoc
- OpenAPI schema: http://127.0.0.1:8000/openapi.json

The documentation endpoints are disabled when `APP_ENV=production`.

### Run tests and linting

```powershell
pytest
pytest --cov=app --cov-report=term-missing
ruff check .
ruff format .
```

## API

| Method | Path             | Description        |
|--------|------------------|--------------------|
| GET    | `/`              | API name and version |
| GET    | `/api/v1/health` | Health check       |

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health
# {"status": "healthy", "service": "ai-customer-support-chatbot"}
```

Every response includes an `X-Request-ID` header that matches the `req=` field in the server logs.

## Configuration

Configuration is managed by [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) in [app/core/config.py](app/core/config.py). Every variable is optional, and the defaults are safe for local development.

### How values are loaded

Values are resolved in this order (highest priority first):

1. **Environment variables** set in the shell or on the server, e.g. `$env:APP_ENV = "staging"`
2. **`.env` file** in the project root. It is always read from the project root, regardless of the current directory.
3. **Defaults** defined in `Settings`

Variable names are case-insensitive. All values are validated at startup, so an invalid value (e.g. `APP_ENV=prod`) stops the app with a clear error instead of misbehaving later.

```powershell
Copy-Item .env.example .env   # then edit .env
```

### Variables

| Variable          | Default                           | Rules                                                              |
|-------------------|-----------------------------------|--------------------------------------------------------------------|
| `APP_NAME`        | `AI Customer Support Chatbot API` | 1–100 characters                                                   |
| `APP_ENV`         | `development`                     | `development`, `staging` or `production`; production disables docs |
| `DEBUG`           | `false`                           | `true`/`false`; must be `false` in production                      |
| `LOG_LEVEL`       | `INFO`                            | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`                   |
| `API_V1_PREFIX`   | `/api/v1`                         | Starts with `/`, no trailing slash, letters/digits/`-`/`_` only    |
| `CORS_ORIGINS`    | `[]`                              | JSON list of full origins, e.g. `["http://localhost:8501"]`; no `*` |
| `OPENAI_API_KEY`  | *(unset)*                         | Not used in Stage 1; optional                                      |

The app version is defined in code (`app/__init__.py`), not in the environment.

### Secrets

- Never commit `.env`; it is excluded by `.gitignore`. Only `.env.example`, which has no real values, is committed.
- Never hardcode API keys in source code. Set them in `.env` locally or as environment variables on the server.
- Secret settings use `SecretStr`, which shows `**********` in `repr()`, `str()` and JSON output.
- At startup the app logs only an allow-listed summary (`Settings.safe_summary()`). It reports whether a key is set, never its value.
