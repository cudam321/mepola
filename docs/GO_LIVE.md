# Go-Live runbook — arming config #1 + the manual desk for REAL money

> The live path is **fail-closed**: every gate below must align or the engine stays inert (watches,
> never sends). A missing/mistyped gate = **no trades**, never a wrong trade. Blast radius is bounded
> by the burner balance you choose to fund and the per-order caps. This is a deliberate power-law tail bet.

## Preconditions (verified 2026-07-05)
- Burner `<YOUR_BURNER_PUBKEY>` funded **0.300002 SOL**; the `.env`
  `WALLET_PRIVATE_KEY` loads to exactly this pubkey (allowlist-enforced in `jupiter_swap.py`).
- `SOLANA_RPC_URL`, `JUPITER_API_KEY` present locally and on Railway.
- Dry-run dust reconcile against BONK succeeded (buy+sell quotes + on-chain reads).
- Full suite green (paper == backtest equivalence intact). Manual money path adversarially reviewed.

## The four things the engine checks before a REAL send (`executor._require_armed` + `engine._can_send_live`)
1. `armed` = `mode=="live"` **and** env `MEMEBOT_LIVE_ARMED=1`
2. `dry_run` off = env `MEMEBOT_LIVE_SEND=1`
3. DB `system_state`: `equivalence_ok=1` **and** `dust_reconciled=1` (set on the volume DB by
   `start.sh` when env `MEMEBOT_LIVE_GATES=1` — deliberate, kept off the dashboard)
4. `kill_switch=off` (blocks BUYS only; risk-reducing SELLS always allowed)
Plus the container must have `solders` (Dockerfile `--extra solana`) and `WALLET_PRIVATE_KEY` in env.

## Sequence

### 1. Real on-chain dust reconcile — ✅ DONE 2026-07-05 (the gate the user set)
```bash
set -a && . ./.env && set +a
MEMEBOT_LIVE_ARMED=1 PYTHONPATH=src \
  uv run --extra solana python scripts/dust_reconcile.py \
  --mint DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263 --usd 1.0 --send   # BONK, $1 round-trip
```
**Result: ✅ RECONCILED** — bought 206,890 BONK (on-chain delta matched), sold flat for $0.9997,
round-trip cost 0.000022 SOL. The executor + confirm path is proven on-chain. (`--sell-only` recovers
a stranded bag; the first attempt false-failed on an eager RPC read — fixed. If it ever prints ❌, STOP.)

### 2. Arm the deploy (deliberate — NOT auto-done this session; ~2 min)
```bash
set -a && . ./.env && set +a       # loads $WALLET_PRIVATE_KEY (never printed)

# a) config: flip to live (commit — the engine reads mode from config.toml on boot)
#    config.toml [strategy.tailrider] mode = "live"

# b) Railway env. MEMEBOT_FRESH_LIVE=1 archives the paper-trading volume DB so the LIVE engine
#    starts with a CLEAN position book — WITHOUT it, paper-era watchers could fire a REAL buy on a
#    dip. WALLET_PRIVATE_KEY is passed by $VAR reference (never printed):
railway variables --set "WALLET_PRIVATE_KEY=$WALLET_PRIVATE_KEY" \
                  --set "MEMEBOT_LIVE_ARMED=1" \
                  --set "MEMEBOT_LIVE_SEND=1" \
                  --set "MEMEBOT_LIVE_GATES=1" \
                  --set "MEMEBOT_FRESH_LIVE=1"

# c) deploy (Dockerfile installs --extra solana so the container can sign)
railway up --detach
railway deployment list --json    # poll newest -> SUCCESS (do NOT trust `railway logs --build`)

# d) AFTER the first successful live boot, remove the one-time cutover flag so a later redeploy
#    doesn't re-trigger the archive logic (it's marker-guarded, but keep the env clean):
railway variables --set "MEMEBOT_FRESH_LIVE=0"
```
Why the reset (step b): the current Railway volume DB has been paper-trading and holds paper
positions/watchers. `MEMEBOT_FRESH_LIVE=1` archives it to `<db>.paper-archive` (history preserved)
and starts live with an empty book. Decide deliberately: fresh book (recommended) vs. keeping it.

### 3. Verify live
- `curl https://<host>/api/health` → 200.
- Dashboard shows **LIVE** mode badge; ControlsModal mode = live.
- Watch the STREAM / positions: the algo WATCHES new @your_channel calls and buys the −50% dip at
  $3/trade; manual desk is dormant until you place an order.
- On the first real entry, confirm the fill books from the **confirmed on-chain amount** (real P&L).

## Rollback (revert to paper instantly)
- Fastest: engage the **kill switch** in the dashboard (halts new buys immediately; open positions
  still exit). Then to fully stand down: set config `mode="paper"` (or unset `MEMEBOT_LIVE_SEND`) and
  `railway up`. The engine drops to inert; no code rollback needed.
