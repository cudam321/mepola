# DEPLOY.md — running memebot 24/7 on a VPS

The engine and dashboard run as two systemd services on a cheap Ubuntu VPS instead of the Mac.
Deploys are one command from the Mac (`deploy/deploy.sh`). The frontend is built locally with npm
and rsynced — **no node on the VPS**. The engine's SQLite state (`runs/live_state.db`) lives only
on the VPS and is never overwritten by routine deploys.

## 1. Provider & specs

Any of these is plenty (the system is a lightweight Python loop + a tiny FastAPI app):

| Provider | Plan | Price | Notes |
|---|---|---|---|
| **Hetzner** (recommended) | **CX22** (2 vCPU x86, 4 GB) or **CAX11** (2 vCPU ARM, 4 GB) | ~4 EUR/mo | Best value; ARM works fine (uv installs an ARM Python) |
| DigitalOcean | Basic droplet (1 vCPU, 1 GB) | $6/mo | Fine too |

OS: **Ubuntu 22.04 or 24.04**. Create the server **with your SSH key** (key-only auth, no password).

## 2. Quickstart (3 commands)

```bash
# 1. Create the VPS (Ubuntu 22.04/24.04, your SSH key), note its IP.

# 2. From the Mac, in the repo root — first deploy: provision + seed the DB:
./deploy/deploy.sh root@<IP> --setup --init-db

# 3. On the VPS — private HTTPS dashboard via Tailscale (recommended):
ssh root@<IP>
bash /opt/memebot/deploy/setup_vps.sh --with-tailscale   # installs tailscale (idempotent)
tailscale up                                             # authenticate the box into your tailnet
tailscale serve --bg 8000                                # -> https://<host>.<tailnet>.ts.net
```

That HTTPS URL is reachable **only** from devices on your tailnet (your Mac/phone). The dashboard
itself binds to `127.0.0.1:8000` and the firewall allows nothing but SSH — it is never public.

**SSH-tunnel alternative** (no Tailscale): from the Mac,

```bash
ssh -L 8000:localhost:8000 root@<IP>
# then open http://localhost:8000 on the Mac
```

## 3. Operations

| Task | Command |
|---|---|
| Engine logs (live) | `journalctl -u memebot-engine -f` |
| Dashboard logs | `journalctl -u memebot-dashboard -f` |
| Service status | `systemctl status memebot-engine memebot-dashboard` |
| **Kill switch** (halt all new entries NOW) | `sqlite3 /opt/memebot/runs/live_state.db "UPDATE system_state SET value='on' WHERE key='kill_switch';"` |
| Un-kill | same with `value='off'` |
| Redeploy (code change) | on the Mac: `./deploy/deploy.sh root@<IP>` |
| Config change | edit `config.toml` on the Mac, then redeploy (deploy restarts both services) |
| Health check | `curl -s localhost:8000/api/health` (on the VPS or through the tunnel) |

`deploy.sh` flags: `--setup` (provision), `--init-db` (seed DB once; refuses if the remote DB
exists unless `--force`), `--no-build` (skip the npm build), `--force` (allow DB overwrite —
destroys engine state, almost never what you want).

## 4. Backups

Nightly SQLite backup on the VPS, keep 7. Create `/etc/cron.d/memebot-backup`:

```
17 3 * * * root sqlite3 /opt/memebot/runs/live_state.db ".backup /opt/memebot/backups/live_state.$(date +\%F).db" && ls -1t /opt/memebot/backups/live_state.*.db | tail -n +8 | xargs -r rm --
```

(`/opt/memebot/backups/` is created by setup_vps.sh. In cron files `%` must be escaped as `\%`.)

Optional off-box copy — pull the latest backup to the Mac now and then:

```bash
rsync -z root@<IP>:/opt/memebot/runs/live_state.db "/path/to/mepola/runs/live_state.vps-backup.db"
```

## 5. Security — read this

- `.env` contains `TELEGRAM_SESSION_STRING_*` = **full access to the Telegram account**. Treat the
  VPS as a secrets box: dedicated machine for memebot only, **key-only SSH**, nothing else hosted
  on it. deploy.sh pushes `.env` with mode 600 and never prints its contents.
- **Never expose port 8000 publicly.** The dashboard binds to 127.0.0.1 and ufw denies all
  incoming except SSH. Access only via Tailscale (`tailscale serve`) or the SSH tunnel. Do not add
  a ufw rule for 8000, do not rebind uvicorn to 0.0.0.0.
- Routine deploys exclude `runs/` — the live DB is written only by the engine on the VPS.
- Later, when Phase-D live execution adds `WALLET_PRIVATE_KEY`: **BURNER wallet only**, funded with
  money you can lose entirely. Never a main wallet.
- Nice-to-have (not done in v1): run the services as a dedicated non-root user.

## 6. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Engine fails at startup with a telethon auth/session error | `TELEGRAM_SESSION_STRING_*` (or API_ID/API_HASH) missing from `/opt/memebot/.env` — redeploy from the Mac (step c pushes `.env`), then `systemctl restart memebot-engine` |
| Dashboard loads blank / 404 on assets | `dashboard/frontend/dist` wasn't synced — redeploy **without** `--no-build` |
| Engine restarting in a loop (`systemctl status` shows repeated restarts) | `journalctl -u memebot-engine -n 100` and read the traceback; RestartSec=5 means the journal has one traceback per attempt |
| `uv: command not found` during deploy step f1 | `--setup` was never run on this box: `./deploy/deploy.sh root@<IP> --setup` |
| `/api/health` no response right after deploy | give uvicorn a few seconds, then check `journalctl -u memebot-dashboard -n 50` |
| Deploy refuses at `--init-db` | correct behavior: the remote DB already exists (engine state). Only `--force` overrides, and it destroys that state |
