"""Check the local development setup before (or after) starting the two services.

Usage (from the project root, virtual environment active):

    python scripts/check_setup.py

What it checks, in plain words:

1. The backend's settings (``.env`` and the environment) are valid.
2. The FAQ knowledge base loads and has active FAQs.
3. The conversation database opens and has its tables. Missing tables are created, as
   the backend does at startup; this is idempotent and never changes existing rows.
4. Whether the embedding model is already on this machine (if not, the backend
   downloads it on first use, which needs internet and takes a while).
5. The frontend's settings are valid and agree with the backend's (API prefix, message
   limit).
6. Whether a backend is already answering at ``BACKEND_API_URL``, and if so, its
   readiness (``GET /api/v1/ready``).

It loads no embedding model, downloads nothing and sends no OpenAI request. Settings
values are never printed, only their names. Exit code 0 when the backend can start,
1 when something must be fixed first.
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allow `python scripts/check_setup.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import ValidationError  # noqa: E402

from app.core.config import ENV_FILE, Settings, describe_settings_error  # noqa: E402
from app.database.connection import Database, DatabaseError  # noqa: E402
from app.database.faq_repository import FAQDataError, FAQRepository  # noqa: E402
from frontend.api_client import BackendStatus, ChatApiClient  # noqa: E402
from frontend.config import FrontendSettings  # noqa: E402
from frontend.config import describe_settings_error as describe_frontend_error  # noqa: E402

STATUS_TEXT = {
    BackendStatus.ONLINE: "running and ready",
    BackendStatus.WARMING_UP: "running and ready; the search model loads on the first question",
    BackendStatus.LIMITED: "running; semantic search failed to load (check its log)",
    BackendStatus.UNAVAILABLE: "not running, or not ready (start it, or check its log)",
}


def hf_hub_cache_dir() -> Path:
    """Where huggingface_hub stores models (computed without importing it)."""
    if cache := os.environ.get("HF_HUB_CACHE"):
        return Path(cache)
    hf_home = os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface"
    return Path(hf_home) / "hub"


def model_is_available(model_name: str) -> bool:
    """Whether the model is a local folder or already in the Hugging Face cache."""
    if Path(model_name).is_dir():
        return True
    snapshots = hf_hub_cache_dir() / f"models--{model_name.replace('/', '--')}" / "snapshots"
    return snapshots.is_dir() and any(snapshots.iterdir())


class Report:
    def __init__(self) -> None:
        self.failed = False

    def ok(self, text: str) -> None:
        print(f"[ OK ] {text}")

    def note(self, text: str) -> None:
        print(f"[NOTE] {text}")

    def fail(self, text: str) -> None:
        self.failed = True
        print(f"[FAIL] {text}")


def check_backend(report: Report) -> Settings | None:
    try:
        settings = Settings()
    except ValidationError as exc:
        report.fail(describe_settings_error(exc))
        return None
    report.ok(
        f"Backend settings are valid (APP_ENV={settings.app_env}, "
        f"LLM_ENABLED={str(settings.llm_enabled).lower()}, "
        f"OPENAI_API_KEY {'set' if settings.openai_api_key else 'not set'})"
    )
    try:
        faqs = FAQRepository.from_json_file(settings.faq_data_path)
    except FAQDataError as exc:
        report.fail(f"FAQ knowledge base: {exc}. Check FAQ_DATA_PATH.")
    else:
        active = len(faqs.list_active())
        if active:
            report.ok(f"FAQ knowledge base loads ({active} active FAQs)")
        else:
            report.fail("FAQ knowledge base has no active FAQs: every question would go unanswered")
    try:
        database = Database(settings.database_url)
        try:
            database.init_schema()
            database.check()
        finally:
            database.dispose()
    except (DatabaseError, OSError, ValueError) as exc:
        report.fail(f"Conversation database: {exc}. Check DATABASE_URL.")
    else:
        report.ok("Conversation database is ready (missing tables created; data kept)")
    if model_is_available(settings.embedding_model_name):
        report.ok("Embedding model is on this machine (no download needed)")
    else:
        report.note(
            "Embedding model is not downloaded yet: the backend downloads it on first use "
            "(internet needed, a few minutes). See README, 'Download the embedding model now'."
        )
    if not settings.semantic_index_preload:
        report.note(
            "SEMANTIC_INDEX_PRELOAD=false: the first chat question loads the search model and "
            "may take longer than the frontend waits. Use true (load before serving) or "
            "background (load while serving); scripts/start-dev.ps1 uses true."
        )
    return settings


def check_frontend(report: Report, backend: Settings | None) -> FrontendSettings | None:
    try:
        settings = FrontendSettings()
    except ValidationError as exc:
        report.fail(describe_frontend_error(exc))
        return None
    report.ok(f"Frontend settings are valid (BACKEND_API_URL={settings.base_url})")
    if settings.uses_plain_http_remotely:
        report.note("BACKEND_API_URL uses http:// to a remote host: tokens would be unencrypted")
    if backend is not None:
        if settings.backend_api_prefix != backend.api_v1_prefix:
            report.fail(
                f"BACKEND_API_PREFIX ({settings.backend_api_prefix}) differs from the backend's "
                f"API_V1_PREFIX ({backend.api_v1_prefix}): every request would get 404"
            )
        if settings.max_message_characters != backend.max_customer_message_characters:
            report.note(
                "MAX_MESSAGE_CHARACTERS differs from MAX_CUSTOMER_MESSAGE_CHARACTERS; the "
                "backend's limit applies (the page adapts after the first refusal)"
            )
    return settings


def check_running_backend(report: Report, settings: FrontendSettings) -> None:
    with ChatApiClient(
        settings.base_url,
        api_prefix=settings.backend_api_prefix,
        connect_timeout=2.0,
        read_timeout=5.0,
    ) as client:
        status = client.backend_status()
    report.note(f"Backend at {settings.base_url}: {STATUS_TEXT[status]}")


def main() -> int:
    print(f"Project root: {PROJECT_ROOT}")
    print(f".env file:    {ENV_FILE} ({'found' if ENV_FILE.is_file() else 'not found; defaults'})")
    report = Report()
    backend = check_backend(report)
    frontend = check_frontend(report, backend)
    if frontend is not None:
        check_running_backend(report, frontend)
    if report.failed:
        print("\nFix the [FAIL] items above, then run this check again.")
        return 1
    print("\nReady to start. See README, 'Starting the application'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
