# syntax=docker/dockerfile:1
#
# Images for the single-server Docker Compose deployment (deploy/compose.yaml).
# Build context: the project root (compose sets it). Two targets:
#
#   backend   FastAPI + the embedding stack (PyTorch CPU-only). Also runs the maintenance
#             scripts (cleanup, anonymization, FAQ import, staff tokens).
#   frontend  Both Streamlit pages (customer and staff). No AI packages: the pages talk to
#             the backend over HTTP only.
#
# Versions come from requirements.txt, the single pinned list: nothing is re-pinned here.
# Both images run as an unprivileged user (uid 10001) and are designed for a read-only
# root filesystem with a tmpfs at /tmp (HOME points there).
#
# Pin the base image by digest for reproducible builds, for example
#   docker build --build-arg PYTHON_IMAGE=python:3.13-slim@sha256:<digest> ...
ARG PYTHON_IMAGE=python:3.13-slim

FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /tmp --shell /usr/sbin/nologin app
WORKDIR /app
COPY requirements.txt ./


FROM base AS backend
# The PyTorch CPU wheel first (far smaller than the default CUDA build), as in
# .github/workflows/quality.yml; then the runtime requirements without the frontend and
# the development tools.
RUN TORCH="$(grep -E '^torch==' requirements.txt)" \
 && pip install "$TORCH" --index-url https://download.pytorch.org/whl/cpu \
 && grep -v -E '^(streamlit|pytest|pytest-cov|ruff|mypy)==' requirements.txt > /tmp/requirements-backend.txt \
 && pip install -r /tmp/requirements-backend.txt \
 && rm /tmp/requirements-backend.txt
COPY app/ app/
COPY scripts/ scripts/
# Only the FAQ seed: it fills an empty FAQ table once (FAQ_DATA_PATH). After that the
# database is the source of truth. Evaluation data is not needed at runtime.
COPY data/faqs.json data/faqs.json
# Mount points for the named volumes. Created here, owned by the app user, so a new
# named volume takes this ownership when Docker first populates it.
RUN mkdir -p /var/lib/chatbot /app/data/backups /models \
 && chown app:app /var/lib/chatbot /app/data/backups /models
USER app
EXPOSE 8000
# One worker on purpose: rate limits, the FAQ catalogue snapshot, the semantic index and
# the LLM circuit breaker are per process (security audit SEC-09, SEC-10).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--no-server-header", "--timeout-graceful-shutdown", "20"]


FROM base AS frontend
RUN grep -E '^(pydantic|pydantic-settings|python-dotenv|streamlit|httpx)==' requirements.txt > /tmp/requirements-frontend.txt \
 && pip install -r /tmp/requirements-frontend.txt \
 && rm /tmp/requirements-frontend.txt
COPY frontend/ frontend/
# Streamlit reads .streamlit/config.toml from the working directory (/app).
COPY .streamlit/config.toml .streamlit/config.toml
# Fails the build if the frontend needs a package that was not selected above.
RUN python -c "import frontend.config, frontend.api_client, frontend.admin_api_client, frontend.state, frontend.formatting"
USER app
EXPOSE 8501 8502
# compose.yaml sets the command for each page; this default runs the customer page.
CMD ["streamlit", "run", "frontend/app.py", "--server.address", "0.0.0.0", \
     "--server.port", "8501", "--server.headless", "true", \
     "--server.fileWatcherType", "none", "--server.runOnSave", "false"]
