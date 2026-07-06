# Running the autonomous tail-rider + dashboard

The system implements config #1 (see `RESEARCH.md`). **Paper-first.** Live execution
is built but ships **inert** and triple-gated.

## 0. Install

```bash
uv sync --extra dashboard --extra dev        # engine + dashboard API + tests
uv sync --extra prod-ingest                  # + telethon (live Telegram listener)
uv sync --extra solana                        # + solders (live on-chain execution, Phase D)
```

## 1. Seed the DB from the config #1 backtest (real power-law data for the dashboard)

```bash
set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/seed_live_db.py --reset
```
Replays config #1 over the whole corpus via the live `TailRider` (proven == backtest by
`tests/test_strategy_equivalence.py`) and writes `runs/live_state.db`. Honest result over the FULL
history: **$3-fixed → $0 (busts in the pre-ANSEM bleed); 0.6%-fractional → ~$337**; best 197.6x
(ANSEM = 56% of all gains). Only the ANSEM sub-window was net positive — the size-fragility is real.

## 2. Dashboard (React + FastAPI + ECharts)

```bash
# one-time frontend build:
cd dashboard/frontend && npm install && npm run build && cd -
# serve API + built frontend on http://localhost:8000
uv run --extra dashboard uvicorn dashboard.server.app:app --host 127.0.0.1 --port 8000
```
For frontend dev with hot reload: `npm --prefix dashboard/frontend run dev` (proxies /api + /ws to :8000).
The hero is a log-scale distribution of every position's multiple + a Pareto concentration line, with
the break-even / tail / graveyard reference lines and an always-visible honest-status banner.

## 3. Live PAPER loop (autonomous, 24/7)

```bash
set -a && . ./.env && set +a && PYTHONPATH=src python -m memebot.live.run
```
Listens to @your_channel, opens a WATCHING position per first-call, fills on a −50% dip, runs the
config #1 ladder, and writes everything to `runs/live_state.db` (the dashboard updates live over the
WebSocket). `[strategy.tailrider].mode = "paper"` in `config.toml`. Requires the telethon session
(`TELEGRAM_SESSION_STRING_*`, from `~/telegram-mcp/.env` or the project `.env`).

## 4. Live execution (Phase D) — GATED, real money, BURNER ONLY

Built but **UNVERIFIED against a live wallet.** It stays inert unless ALL of these hold:

1. `config.toml [strategy.tailrider].mode = "live"`
2. env `MEMEBOT_LIVE_ARMED=1`      (arms the executor; still dry-run/quote-only without #3)
3. env `MEMEBOT_LIVE_SEND=1`       (actually signs + sends on-chain)
4. `WALLET_PRIVATE_KEY` (**BURNER ONLY**, never a main wallet) + `SOLANA_RPC_URL` set
5. kill-switch off (`system_state.kill_switch`)

First real use MUST be a single **dust** trade reconciled on-chain against the paper model before any
size. The private key is never logged. Sizing stays fixed-tiny; there is no lever-up path in code.

## Tests

```bash
uv run pytest        # 63 tests; the crux is tests/test_strategy_equivalence.py (ANSEM ≈197.6x == sim)
```

## Kill-switch / control

`system_state` in `runs/live_state.db` is the control plane: `mode` (paper|live), `kill_switch`
(on|off). Set `kill_switch=on` to halt all new entries immediately.

## Deploy (24/7 VPS)

Full runbook: `docs/DEPLOY.md`. The system runs 24/7 on a ~4 EUR/mo Ubuntu VPS (Hetzner CX22/CAX11
or a $6 DigitalOcean droplet) as two systemd services; deploys are one command from the Mac:

```bash
./deploy/deploy.sh root@<IP> --setup --init-db   # first time (provision + seed DB)
./deploy/deploy.sh root@<IP>                     # every redeploy after that
```

Dashboard is private-only (Tailscale `tailscale serve --bg 8000`, or `ssh -L 8000:localhost:8000`).

## Deploy (Railway)

Cloud alternative: one Docker container (engine + dashboard) with the SQLite DB on a Railway volume
at `/data`, deployed by directory upload (`railway up` — no git/GitHub needed). Full runbook incl.
variables table and security notes: `docs/DEPLOY_RAILWAY.md`. Quickstart from the repo root
(set variables in the Railway dashboard between steps 2 and 3):

```bash
railway login && railway init
railway volume add --mount-path /data
railway up
railway domain
```
