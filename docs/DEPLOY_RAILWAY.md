# Deploying memebot to Railway (engine + dashboard, one container)

This is the cloud alternative to the VPS runbook in `docs/DEPLOY.md`. One Docker container runs
both the live engine (`memebot.live.run`) and the FastAPI dashboard (uvicorn); the SQLite DB lives
on a Railway **volume** at `/data` so it survives redeploys. The repo is **not a git repo** — you
deploy by uploading the directory with `railway up`. No GitHub involved.

Pieces (all in the repo already):

| File | Role |
|---|---|
| `Dockerfile` | multi-stage build: Vite frontend → python:3.13-slim + uv venv |
| `.dockerignore` | keeps secrets (`.env`) and the 749MB `data_cache/` OUT of the image |
| `railway.json` | tells Railway to use the Dockerfile, healthcheck `/api/health`, restart on failure |
| `deploy/railway/start.sh` | seeds `/data/live_state.db` on first boot, runs both processes, exits if either dies |

---

## 0. One-time: install the CLI and log in

```bash
brew install railway        # or: npm i -g @railway/cli
railway login               # opens a browser to authenticate
```

## 1. Create the project (from the repo root)

The repo path contains a space — always `cd` with quotes:

```bash
cd "/path/to/mepola"
railway init                # create a new project; accept the prompts
```

## 2. Create the volume (the DB lives here)

```bash
railway volume add --mount-path /data
```

(Or in the Railway dashboard: your service → **Settings → Volumes → Add Volume**, mount path `/data`.)

On the very first boot, `start.sh` copies the seeded backtest DB baked into the image
(`/app/seed_live_state.db`, from `runs/live_state.db`) to `/data/live_state.db`. Every boot after
that, the existing DB is left untouched.

## 3. Set the environment variables

Copy the secret values from your local `.env` — **never commit or paste them anywhere else**.
Tip: in the Railway dashboard, service → **Variables → Raw Editor** lets you bulk-paste all of
these as `KEY=value` lines at once.

| Variable | Required now? | Notes |
|---|---|---|
| `TELEGRAM_API_ID` | **Yes** | from local `.env` |
| `TELEGRAM_API_HASH` | **Yes** | from local `.env` |
| `TELEGRAM_SESSION_STRING` | **Yes** | from local `.env`. **Full Telegram account access** — see the security box below. Any `TELEGRAM_SESSION_STRING*` name works. |
| `JUPITER_API_KEY` | **Yes** | price feed |
| `HELIUS_API_KEY` | Yes (if in local `.env`) | RPC/data |
| `SOLANA_RPC_URL` | Yes (if in local `.env`) | RPC endpoint |
| `MEMEBOT_DB` | **Yes** | set exactly `/data/live_state.db` |
| `DASHBOARD_PASSWORD` | **Yes** | choose a **strong** one — the dashboard URL is public |
| `TZ` | Yes | `UTC` |
| `WALLET_PRIVATE_KEY` | **NO — Phase D only** | do NOT set yet; burner wallet only, ever |
| `MEMEBOT_LIVE_ARMED` | **NO — Phase D only** | do NOT set yet |
| `MEMEBOT_LIVE_SEND` | **NO — Phase D only** | do NOT set yet |

Via CLI instead: `railway variables --set "MEMEBOT_DB=/data/live_state.db" --set "TZ=UTC" ...`

## 4. Deploy and get the URL

```bash
railway up          # uploads the directory, builds the Dockerfile on Railway, deploys
railway domain      # generates the public https URL
```

Open the URL in a browser — it prompts for a password (basic auth; **any username**, the password
is `DASHBOARD_PASSWORD`). The healthcheck endpoint `/api/health` is auth-exempt, which is how
Railway knows the deploy is healthy.

> **Upload size note:** `railway up` honors ignore files, but if the upload looks large/slow
> (it should be a few MB, not hundreds), create a `.railwayignore` mirroring the exclusions:
> `cp .dockerignore .railwayignore` and run `railway up` again. The patterns in `.dockerignore`
> are gitignore-compatible, including the `runs/*` + `!runs/live_state.db` seed exception.

---

## Operations

- **Logs:** `railway logs`. Both processes interleave in one stream; engine lines are prefixed by
  their logger name, uvicorn access lines are the dashboard.
- **Redeploy after code changes:** just `railway up` again. The DB is safe — it's on the volume,
  and the seed copy in `start.sh` only happens if `/data/live_state.db` does not exist.
- **Restarts:** if either the engine or the dashboard process dies, `start.sh` exits non-zero and
  Railway restarts the whole container (up to 10 retries, per `railway.json`).
- **Kill switch:** use the dashboard's controls modal — it works remotely and flips
  `system_state.kill_switch` in the DB, halting all new entries immediately.
- **Backups (keep it simple):** the DB is small (single-digit MB). Two honest options:
  1. `railway run bash` (or the dashboard's service shell), then
     `sqlite3 /data/live_state.db ".backup /data/backup.db"` — a consistent snapshot next to the
     live file. Note `sqlite3` the CLI tool is not in the slim image; if missing, use
     `/app/.venv/bin/python -c "import sqlite3; sqlite3.connect('/data/live_state.db').backup(sqlite3.connect('/data/backup.db'))"`.
  2. If you need the file locally, add a temporary authenticated download endpoint to the
     dashboard, fetch it once, and remove the endpoint. There is no built-in "download volume
     file" in Railway — don't pretend otherwise.

## Security — read this before deploying

> **The Telegram session string is a full-account credential.** Putting
> `TELEGRAM_SESSION_STRING` into Railway's variable store means trusting Railway with the
> ability to read and send as your Telegram account. That is the tradeoff vs a self-managed VPS
> (where only your box holds it). Accept it consciously or stay on the VPS path (`docs/DEPLOY.md`).
>
> - `DASHBOARD_PASSWORD` must be strong: the URL is public internet the moment `railway domain` runs.
> - **Never** set `WALLET_PRIVATE_KEY` until Phase D arming — and then only the **burner** wallet,
>   never a main wallet. The engine ships paper-mode and stays inert without the Phase D gates.
> - `.env` is the first line of `.dockerignore` — secrets never enter the image. Keep it that way.

## Cost

Hobby plan: $5/mo which **includes** $5 of usage. This footprint (one small always-on container +
a tiny volume) fits within roughly that — expect ~$5/mo total, a bit more if the engine burns CPU.

## Reality check (unchanged by the deployment)

Deploying this does not change what it is: a deliberately-sized **power-law tail bet** with a structural
per-trade EV ≤ ~1, long bleed stretches, and an edge that in backtest was one token (ANSEM). The
dashboard's honest-status banner stays visible. Size it as money you can lose entirely.
