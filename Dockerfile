# syntax=docker/dockerfile:1
# memebot — single container: live engine + FastAPI dashboard.
# Built by Railway from a `railway up` directory upload (repo is not a git repo).
# SQLite DB lives on a Railway volume mounted at /data (see deploy/railway/start.sh).

########################################################################
# Stage 1: build the Vite frontend (reproducible, no host node needed)
########################################################################
FROM node:20-slim AS frontend
WORKDIR /fe

# package files first for layer caching; package-lock.json exists -> npm ci
COPY dashboard/frontend/package.json dashboard/frontend/package-lock.json ./
RUN npm ci

# frontend source (node_modules and dist are .dockerignore'd out of the context)
COPY dashboard/frontend/ ./
RUN npm run build

########################################################################
# Stage 2: runtime (python + uv-managed venv)
########################################################################
FROM python:3.13-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_LINK_MODE=copy

# 1) dependencies only, for layer caching (project itself not installed yet)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra dashboard --extra prod-ingest --extra solana --no-install-project

# 2) application code
#    - src/memebot gets installed into the venv by the full uv sync below
#    - dashboard/ is repo-local (NOT installed) -> importable via PYTHONPATH=/app
COPY src/ ./src/
COPY config.toml ./config.toml
COPY dashboard/__init__.py ./dashboard/__init__.py
COPY dashboard/data.py ./dashboard/data.py
COPY dashboard/server/ ./dashboard/server/
COPY deploy/railway/start.sh ./deploy/railway/start.sh

# 3) full sync installs the memebot package into /app/.venv
RUN uv sync --frozen --no-dev --extra dashboard --extra prod-ingest --extra solana

# 4) built frontend. (The seed-DB bake was removed 2026-07-03: `railway up` respects
#    .gitignore once the repo became a git repo, so runs/live_state.db vanished from the
#    build context and broke the build. The Railway volume already holds the real DB and
#    start.sh tolerates a missing seed — a fresh environment honestly starts empty.)
COPY --from=frontend /fe/dist ./dashboard/frontend/dist

ENV PYTHONPATH=/app/src:/app \
    PYTHONUNBUFFERED=1

# Railway injects PORT; MEMEBOT_DB defaults to /data/live_state.db in start.sh
CMD ["bash", "/app/deploy/railway/start.sh"]
