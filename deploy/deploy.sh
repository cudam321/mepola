#!/usr/bin/env bash
# deploy.sh -- one-command deploy of memebot from the Mac to a VPS.
#
# Usage: ./deploy/deploy.sh user@host [--setup] [--init-db] [--no-build] [--force]
#
#   --setup     first-time provisioning: runs deploy/setup_vps.sh on the VPS
#   --init-db   copy the local seeded runs/live_state.db ONCE (refuses if the
#               remote DB already exists, unless --force is also given)
#   --no-build  skip the local npm frontend build (reuse existing dist/)
#   --force     with --init-db: overwrite the remote DB (DESTROYS engine state!)
#
# The remote runs/ dir is NEVER touched by the routine rsync (the engine's
# SQLite state lives there). Secrets are synced but never echoed.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"   # repo root (path contains a space -- quote everything)
REMOTE_DIR="/opt/memebot"

usage() {
  echo "Usage: $0 user@host [--setup] [--init-db] [--no-build] [--force]"
}

TARGET=""
SETUP=0; INIT_DB=0; NO_BUILD=0; FORCE=0
for arg in "$@"; do
  case "$arg" in
    --setup)    SETUP=1 ;;
    --init-db)  INIT_DB=1 ;;
    --no-build) NO_BUILD=1 ;;
    --force)    FORCE=1 ;;
    -h|--help)  usage; exit 0 ;;
    -*)         echo "ERROR: unknown flag: $arg" >&2; usage; exit 1 ;;
    *)          if [[ -z "$TARGET" ]]; then TARGET="$arg"; else
                  echo "ERROR: unexpected argument: $arg" >&2; usage; exit 1
                fi ;;
  esac
done
if [[ -z "$TARGET" ]]; then usage; exit 1; fi

if [[ ! -f "$REPO_DIR/.env" ]]; then
  echo "ERROR: $REPO_DIR/.env not found -- the engine cannot run without it." >&2
  exit 1
fi

# ---------- (pre) first-time bootstrap: remote needs rsync + target dir ----------
if [[ "$SETUP" -eq 1 ]]; then
  echo "==> [pre] ensuring rsync + $REMOTE_DIR exist on the VPS (fresh-image bootstrap)"
  # shellcheck disable=SC2029
  ssh "$TARGET" "command -v rsync >/dev/null 2>&1 || (export DEBIAN_FRONTEND=noninteractive; apt-get update -y && apt-get install -y rsync); mkdir -p $REMOTE_DIR"
fi

# ---------- (a) build the dashboard frontend locally (no node on the VPS) ----------
if [[ "$NO_BUILD" -eq 1 ]]; then
  echo "==> [a] skipping frontend build (--no-build); reusing dashboard/frontend/dist"
else
  echo "==> [a] building dashboard frontend (npm run build)"
  npm --prefix "$REPO_DIR/dashboard/frontend" run build
fi

# ---------- (b) rsync the repo (runs/ and .env are NEVER part of this sync) ----------
echo "==> [b] rsync repo -> $TARGET:$REMOTE_DIR/"
RSYNC_EXCLUDES=(
  --exclude '.venv'
  --exclude 'data_cache'
  --exclude 'runs'
  --exclude 'vendor'
  --exclude 'dashboard/frontend/node_modules'
  --exclude '__pycache__'
  --exclude '.pytest_cache'
  --exclude '*.pyc'
  --exclude '.git'
  --exclude '.env'
)
rsync -az --delete "${RSYNC_EXCLUDES[@]}" "$REPO_DIR/" "$TARGET:$REMOTE_DIR/"

# ---------- (c) push .env separately, mode 600, contents never printed ----------
echo "==> [c] syncing .env (mode 600; contents not shown)"
rsync -z --chmod=F600 "$REPO_DIR/.env" "$TARGET:$REMOTE_DIR/.env"

# ---------- (d) one-time DB seed ----------
if [[ "$INIT_DB" -eq 1 ]]; then
  echo "==> [d] --init-db: seeding runs/live_state.db"
  if [[ ! -f "$REPO_DIR/runs/live_state.db" ]]; then
    echo "ERROR: local seed $REPO_DIR/runs/live_state.db not found" >&2
    exit 1
  fi
  # shellcheck disable=SC2029
  if ssh "$TARGET" "test -f $REMOTE_DIR/runs/live_state.db"; then
    if [[ "$FORCE" -eq 1 ]]; then
      echo "    WARNING: remote DB exists -- overwriting because --force was given."
      rsync -z "$REPO_DIR/runs/live_state.db" "$TARGET:$REMOTE_DIR/runs/live_state.db"
    else
      echo "ERROR: remote $REMOTE_DIR/runs/live_state.db already exists." >&2
      echo "       That file is the ENGINE'S LIVE STATE. Refusing to overwrite." >&2
      echo "       Re-run with --init-db --force only if you really mean to reset it." >&2
      exit 1
    fi
  else
    # shellcheck disable=SC2029
    ssh "$TARGET" "mkdir -p $REMOTE_DIR/runs"
    rsync -z "$REPO_DIR/runs/live_state.db" "$TARGET:$REMOTE_DIR/runs/live_state.db"
    echo "    seeded remote runs/live_state.db"
  fi
fi

# ---------- (e) first-time VPS provisioning ----------
if [[ "$SETUP" -eq 1 ]]; then
  echo "==> [e] running setup_vps.sh on the VPS"
  # shellcheck disable=SC2029
  ssh "$TARGET" "bash $REMOTE_DIR/deploy/setup_vps.sh"
fi

# ---------- (f) deps, units, restart, status ----------
echo "==> [f1] uv sync (dashboard + prod-ingest extras)"
# shellcheck disable=SC2029
ssh "$TARGET" "cd $REMOTE_DIR && PATH=/usr/local/bin:\$PATH uv sync --extra dashboard --extra prod-ingest"

echo "==> [f2] installing systemd units + restarting services"
# shellcheck disable=SC2029
ssh "$TARGET" "cp $REMOTE_DIR/deploy/*.service /etc/systemd/system/ && systemctl daemon-reload && systemctl restart memebot-engine memebot-dashboard"

echo "==> [f3] waiting 3s, then status"
sleep 3
# shellcheck disable=SC2029
ssh "$TARGET" "echo \"  engine:    \$(systemctl is-active memebot-engine || true)\"; \
               echo \"  dashboard: \$(systemctl is-active memebot-dashboard || true)\"; \
               echo \"  health:    \$(curl -s -m 5 localhost:8000/api/health || echo '(no response)')\""

echo "==> deploy complete. Logs: ssh $TARGET journalctl -u memebot-engine -f"
