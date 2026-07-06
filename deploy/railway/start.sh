#!/usr/bin/env bash
# Railway container entrypoint: seed the volume DB on first boot, then run
# BOTH processes (engine + dashboard) and die loudly if either one dies so
# Railway's restart policy brings the whole container back.
set -euo pipefail

MEMEBOT_DB="${MEMEBOT_DB:-/data/live_state.db}"
export MEMEBOT_DB
mkdir -p "$(dirname "$MEMEBOT_DB")"

# First boot ONLY: copy the baked-in seed DB onto the volume. On every later
# boot the file already exists on /data and is left untouched (redeploy-safe).
if [ ! -f "$MEMEBOT_DB" ] && [ -f /app/seed_live_state.db ]; then
    cp /app/seed_live_state.db "$MEMEBOT_DB"
    echo "[start] first boot: seeded $MEMEBOT_DB from /app/seed_live_state.db"
fi

# Fresh LIVE cutover (deliberate, one-time). The volume DB has been PAPER-trading, so it holds
# paper-era watchers + open bags. Flipping straight to live would make the live engine inherit them —
# a paper watcher could fire a REAL buy on a dip, and stale paper bags would be mismanaged. Set
# MEMEBOT_FRESH_LIVE=1 on the FIRST live deploy to archive the paper DB (history preserved) and start
# the live position book clean. Idempotent via a marker so reboots don't wipe live state.
# The marker MUST be written whenever the cutover is requested, even if no DB exists yet (audit #8):
# otherwise a fresh/reset volume boots with the env still set, creates a LIVE book, and the NEXT
# reboot (marker still absent, DB now present) would archive+wipe that live book, orphaning real bags.
if [ "${MEMEBOT_FRESH_LIVE:-0}" = "1" ] && [ ! -f "${MEMEBOT_DB}.live-cutover-done" ]; then
    if [ -f "$MEMEBOT_DB" ]; then
        cp "$MEMEBOT_DB" "${MEMEBOT_DB}.paper-archive" || true
        rm -f "$MEMEBOT_DB" "${MEMEBOT_DB}-wal" "${MEMEBOT_DB}-shm"
        echo "[start] MEMEBOT_FRESH_LIVE=1 -> archived paper DB to ${MEMEBOT_DB}.paper-archive; clean live book"
    else
        echo "[start] MEMEBOT_FRESH_LIVE=1 -> no existing DB; starting live with a clean book"
    fi
    touch "${MEMEBOT_DB}.live-cutover-done"    # one-shot regardless of DB presence
fi

# PAPER TWIN (measurement book): the paper machine keeps running alongside live (user request
# 2026-07-06). Seed it ONCE from the fresh-live cutover archive so the pre-live paper history +
# seed replay carry over seamlessly; after that the engine's twin keeps writing it.
export MEMEBOT_PAPER_DB="${MEMEBOT_PAPER_DB:-/data/paper_state.db}"
if [ ! -f "$MEMEBOT_PAPER_DB" ] && [ -f "${MEMEBOT_DB}.paper-archive" ]; then
    cp "${MEMEBOT_DB}.paper-archive" "$MEMEBOT_PAPER_DB"
    echo "[start] paper twin: seeded $MEMEBOT_PAPER_DB from the cutover archive"
fi

PORT="${PORT:-8000}"

# Fail-CLOSED auth on the public deploy (audit #4): the dashboard is the money-moving control plane
# (kill-switch, caps, manual orders, take-over) served on a public URL. BasicAuth passes through when
# the password is empty, so an unset/typo'd DASHBOARD_PASSWORD would open every mutating route to the
# internet. Refuse to boot the container in that case. (Local dev runs uvicorn directly, unaffected.)
if [ -z "${DASHBOARD_PASSWORD:-}" ]; then
    echo "[start] REFUSING TO START: DASHBOARD_PASSWORD is empty/unset — the public dashboard would be unauthenticated" >&2
    exit 1
fi

# Live-arming gates (deliberate operator step, kept OFF the dashboard by design — F11). Set
# MEMEBOT_LIVE_GATES=1 ONLY after completing the pre-live checklist: paper≈backtest equivalence
# (the test suite's equivalence gate) AND a real on-chain dust reconcile. It applies the two DB
# gates the LiveExecutor requires for REAL sends. Idempotent (upsert); a no-op when unset.
if [ "${MEMEBOT_LIVE_GATES:-0}" = "1" ]; then
    /app/.venv/bin/python -c "
from memebot.live.state import LiveState
s = LiveState('${MEMEBOT_DB}')
s.set_system('equivalence_ok', '1'); s.set_system('dust_reconciled', '1'); s.close()
print('[start] MEMEBOT_LIVE_GATES=1 -> applied equivalence_ok=1, dust_reconciled=1')
"
fi

/app/.venv/bin/python -m memebot.live.run --db "$MEMEBOT_DB" &
ENGINE_PID=$!
echo "[start] engine started (pid $ENGINE_PID, db $MEMEBOT_DB)"

/app/.venv/bin/python -m uvicorn dashboard.server.app:app --host 0.0.0.0 --port "$PORT" &
DASH_PID=$!
echo "[start] dashboard started (pid $DASH_PID, port $PORT)"

shutdown() {
    echo "[start] caught TERM/INT — stopping engine and dashboard"
    kill "$ENGINE_PID" "$DASH_PID" 2>/dev/null || true
    wait || true
    exit 0
}
trap shutdown TERM INT

# Block until ANY child exits. A healthy container never gets past this line;
# if either process dies we kill the other and exit non-zero so Railway
# restarts the container (restartPolicyType ON_FAILURE in railway.json).
wait -n
status=$?
echo "[start] a process exited (status $status) — shutting down container for restart"
kill "$ENGINE_PID" "$DASH_PID" 2>/dev/null || true
wait || true
exit 1
