# syntax=docker/dockerfile:1
#
# Production images for the single-server deployment (deploy/compose.yaml). One file,
# three targets that share a base stage:
#
#   base ──┬── backend    FastAPI, the embedding stack (PyTorch CPU-only), maintenance
#          │              scripts. Port 8000.
#          └── frontend ──┬── customer   Streamlit customer page (frontend/app.py). 8501.
#                         └── admin      Streamlit staff dashboard (frontend/admin_app.py). 8502.
#
# customer and admin are the same layers plus a different command, port and health
# check, so the second costs no extra disk or build time.
#
# Build from the project root (compose does this):
#   docker build -f deploy/Dockerfile --target backend  -t support-chatbot-backend .
#   docker build -f deploy/Dockerfile --target customer -t support-chatbot-customer .
#   docker build -f deploy/Dockerfile --target admin    -t support-chatbot-admin .
#
# Reproducibility: the base image is pinned by digest, and Python packages are installed
# from deploy/requirements/*.txt: every package (transitive ones included) at an exact
# version with SHA-256 hashes, installed with --require-hashes --no-deps. Those files are
# generated from requirements.txt by `python -m scripts.lock_deploy_requirements`.
# Platform: linux/amd64 (the locks are resolved for x86-64; PyTorch is the CPU build).
# What is NOT pinned: Debian packages inside the base image (fixed by its digest, but a
# digest bump brings new ones) and pip itself (the base image's; fixed by the digest).
# Details and measurements: docs/deployment-architecture.md, "Images".

# Python 3.13.16 on Debian 13 (trixie), slim variant, built 2026-10-06. Update the tag and
# digest together (see the docs for the command that reads the digest).
ARG PYTHON_IMAGE=python:3.13.16-slim-trixie@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

# --- base: user, environment, working directory ------------------------------------------
FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    HOME=/tmp
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /tmp --shell /usr/sbin/nologin app
WORKDIR /app
# uvicorn and Streamlit both stop gracefully on SIGTERM (Docker's default stop signal).
STOPSIGNAL SIGTERM

# --- backend -------------------------------------------------------------------------------
FROM base AS backend
COPY deploy/requirements/backend.txt /tmp/requirements.txt
# torch==...+cpu exists only on PyTorch's index; every other package is on PyPI. The
# hashes decide which file is accepted, so a second index cannot substitute a package.
RUN pip install --no-deps --require-hashes --only-binary=:all: \
        --index-url https://pypi.org/simple \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt
COPY app/ app/
COPY scripts/ scripts/
# Only the FAQ seed: it fills an empty FAQ table once. After that the database is the
# source of truth. Evaluation data is not needed at runtime.
COPY data/faqs.json data/faqs.json
# Mount points for named volumes, owned by the app user: a new named volume copies this
# ownership when Docker first populates it.
RUN mkdir -p /var/lib/chatbot /app/data/backups /models \
 && chown app:app /var/lib/chatbot /app/data/backups /models
# Defaults that make the image safe on its own (compose sets the same values):
# - the database on the volume, not inside the image;
# - the embedding model from a local folder, never downloaded at startup
#   (HF_HUB_OFFLINE). The folder is filled once by compose's model-init service. Without
#   it the backend still starts and answers keyword-only.
ENV DATABASE_URL=sqlite:////var/lib/chatbot/conversations.db \
    FAQ_DATA_PATH=/app/data/faqs.json \
    EMBEDDING_MODEL_NAME=/models/all-MiniLM-L6-v2 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HOME=/tmp/huggingface
USER app
EXPOSE 8000
# Readiness, not just liveness: 200 only when the knowledge base and storage are usable.
# Does not wait for the embedding model (keyword answers work without it).
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/ready', timeout=4).status == 200 else 1)"]
# Exec form: uvicorn is PID 1 (or tini's child) and receives SIGTERM itself. One worker
# on purpose (security audit SEC-09, SEC-10). On SIGTERM it stops accepting, waits up to
# 20 s for running requests, then runs the app's shutdown (closes the OpenAI client and
# the database).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--no-server-header", "--timeout-graceful-shutdown", "20"]

# --- frontend: shared by customer and admin ---------------------------------------------
FROM base AS frontend
COPY deploy/requirements/frontend.txt /tmp/requirements.txt
RUN pip install --no-deps --require-hashes --only-binary=:all: \
        --index-url https://pypi.org/simple \
        -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt
COPY frontend/ frontend/
# Streamlit reads .streamlit/config.toml from the working directory (/app).
COPY .streamlit/config.toml .streamlit/config.toml
# Fails the build if the pages need a package the lock does not contain.
RUN python -c "import frontend.config, frontend.api_client, frontend.admin_api_client, frontend.state, frontend.formatting"
# The backend is on the private Docker network (compose overrides this if needed).
ENV BACKEND_API_URL=http://backend:8000 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_FILE_WATCHER_TYPE=none \
    STREAMLIT_SERVER_RUN_ON_SAVE=false \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
USER app

# --- customer --------------------------------------------------------------------------
FROM frontend AS customer
EXPOSE 8501
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4).status == 200 else 1)"]
CMD ["streamlit", "run", "frontend/app.py", "--server.address", "0.0.0.0", "--server.port", "8501"]

# --- admin -----------------------------------------------------------------------------
FROM frontend AS admin
EXPOSE 8502
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8502/_stcore/health', timeout=4).status == 200 else 1)"]
CMD ["streamlit", "run", "frontend/admin_app.py", "--server.address", "0.0.0.0", "--server.port", "8502"]
