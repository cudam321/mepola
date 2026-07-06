# CLAUDE.md — mepola / memebot (project)

> Read this first every session. Then read `RESEARCH.md` and `docs/SYSTEM_DESIGN.md`.

## What this is

`memebot` — a measurement-first Solana memecoin signal-trading machine that pulls calls from a
Telegram call group/channel of your choice (`MEMEBOT_CHANNEL`). Package `memebot` (Python 3.13 via
**uv**), deliberately tiny deps (`httpx`, `numpy`); the dashboard adds `fastapi`/`uvicorn` (extra)
and a React/ECharts frontend (`dashboard/frontend`) — the **"MEPOLA"** dashboard (Phosphor CRT design).

## What's here

The system runs 24/7 (Railway-ready; PAPER mode by default) — the autonomous config-#1 tail-rider +
the dashboard behind BasicAuth (`DASHBOARD_PASSWORD` in `.env`). Deploy = `railway up --detach` then
**poll the deployment ID to SUCCESS** (`railway deployment list --json`); do NOT trust
`railway logs --build` (it shows the prior deploy). Tests: `uv run pytest`.

What exists (all under `src/memebot/live/` + `dashboard/`, isolated from the research lib):
- **Engine**: `strategy.py` (config-#1 machine, fp-exact to `stage38 sim`), `engine.py`, `run.py`
  (async orchestrator), `executor.py` (Paper default; Live gated+inert), `state.py` (SQLite, single
  writer), `risk.py` ($3 fixed, `STAKE_HARD_CAP_USD=10`), `pricefeed.py`, `listener.py` (telethon),
  `monitor.py` (self-awareness).
- **Shadow lab** (`shadow.py`): 18 challengers C1–C18 + user-added **custom X\*** strategies race
  forward on the SAME live ticks; per-leg flush to `shadow_trades`; `research.py` re-measures the full
  grid on request. Promotion is human-only (allowlist C1–C10). ALL 18 are pinned to their research
  oracle in `tests/test_shadow.py` (sim-space vs `stage37 sim`; exit-family vs `exit_sim`).
- **Dashboard** (`dashboard/`): FastAPI (`server/app.py`) + read-only `data.py` + React/ECharts
  (`frontend/`). Power-law hero, STREAM execution log, strategy lab (detail + add-strategy), token
  terminal, controls modal. Design law = `DESIGN.md`; explanations live in `[?]` hover tooltips
  (`InfoHint.jsx`), not always-on subtitles. The honest caveat lives verbatim in the controls modal.

**Live-arming is gated/inert by default** (mode=live + `MEMEBOT_LIVE_ARMED=1` + `MEMEBOT_LIVE_SEND=1`
+ kill-switch off + paper≈backtest gate + dust reconcile). The signing wallet is allowlisted via
`MEMEBOT_BURNER_PUBKEY` (unset = nothing can sign). **Use a BURNER wallet only** — never a key that
has ever been pasted into a chat, shared, or used as a main wallet.

Iteration loop: change dashboard/engine, verify with a demo DB + local screenshot
(`scripts/make_demo_db.py` → `/tmp/demo_state.db`), commit, deploy, watch to SUCCESS, verify the
served page. Do NOT re-litigate the research or the fixed tiny-stake sizing decision.

## The honest reality — keep this visible, never oversell it

This strategy is a **power-law tail bet**, chosen with eyes open. The measured truth (fresh,
un-truncated data, tail events fully included, uncapped):

- Per-trade EV is structurally **≤ ~1**. There is no observable-at-entry signal that separates
  winners from losers on this channel (every field ≈ 0.50 rank-AUC).
- Over the out-of-sample window that *happened to contain* a 197x (ANSEM), trading straight through
  at tiny fixed-fraction sizing ended **slightly positive** ($500 → ~$583 at 0.25%/trade) — but that
  entire gain is **one token**. Remove ANSEM and it's a loss at every bet size.
- It is **size-fragile**: it survives at small stakes (~$1–10 fixed / ~0.25–1% per trade) and
  measurably dies above that — at $25-fixed/trade the backtest bankroll goes to **$0**. You cannot lever it.
- Expect **long stretches of small losses ("the bleed")**, punctuated by a rare large winner that
  *may* (not *will*) make a period net-positive. Most tokens go to zero.

**Guardrail (do not violate):** size it as money you can lose entirely; never present it as reliable
income; keep these caveats surfaced in the dashboard.

## The strategy in one line (full spec in `RESEARCH.md`)

Config **#1** = wait for a **−50% dip** from the signal price (≤48h) → buy → **hard stop at −30% from
entry** *until secured* → at **3× sell 33%** (recover stake) & **remove the stop** → then **ride**,
selling 25% of the remainder at 6×/12×/24×/48× (then ×3 steps) → **no re-entry** → tiny fixed stake.
Reference implementation: `scripts/stage37_grid.py::sim` / `scripts/stage38_ansem_dependence.py::sim`
with `dip=0.5, sl=0.7, ftp=3.0, fsell=0.33, reentry=None`.
📎 Note on the `sl` parameter: it's the stop *level* as a fraction of entry, so `sl=0.7` = stop at
0.7× entry = a **−30% stop** (confirmed: `sl=0.7` reproduces #1's OOS mean 1.387 / drop3 0.787 /
ANSEM 197.6x). The −30% stop is what makes #1 win the grid — it cuts the losers' bleed fast, while the
−50% dip entry means the rare winner (ANSEM) has already bottomed and survives the cut (ANSEM stays
197.6x even with the −30% stop). Verified by `scripts/verify_sl_semantics.py`.

## Repo map

```
src/memebot/            library
  models.py             Signal, SignalSide, Candle, PriceSeries, Pool, ... (the dataclass contract)
  config.py             loads config.toml + .env
  parser/signal_parser.py   free-text channel message -> Signal (mint/ticker/side)
  ingest/telegram_mcp.py    message dicts -> Signal; load_corpus_json / first_call_per_mint / save_corpus_json
  data/                 jupiter.py (PRIMARY OHLCV), cache.py (CachedPriceClient), geckoterminal.py,
                        dexscreener.py, dune.py
  analysis/             exit_sim.py (ExitPolicy + simulate_exit), excursion.py, features.py
  sim/fill_simulator.py     latency-honest fill + cost model
  backtest/horizon_backtest.py
  safety/rugcheck.py
scripts/                61 research CLIs (stage0..stage39 + attack_*/refute_*); pull_channel_history.py
runs/                   corpora + stage outputs (gitignored). your_channel_fresh.json = current corpus
data_cache/             ~749MB OHLCV cache; jupiter_untrunc/ backs the current tests
docs/                   SYSTEM_DESIGN.md, RUNNING.md, GO_LIVE.md, audits/  (RESEARCH.md at root)
vendor/telegram-mcp/    vendored chigwell/telegram-mcp (also cloned at ~/telegram-mcp)
config.toml  pyproject.toml  uv.lock  .env(SECRETS)  .env.example
```

## How to run

```bash
# research/backtest scripts (from repo root):
set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/<stage>.py

# pull fresh channel history (read-only telethon; reads ~/telegram-mcp/.env session):
uv run --project ~/telegram-mcp python scripts/pull_channel_history.py @your_channel --limit 6000 --out runs/your_channel_fresh.json

# tests:
uv run pytest
```
Always run from the repo root so `./.env`, `config.toml`, `data_cache/`, `runs/` resolve.

## Data pipeline (backtest)

corpus JSON `{channel,title,messages:[{id,date,text}]}` → `load_corpus_json` → `first_call_per_mint`
(the **trading unit** = first actionable BUY per mint) → per token `series_to_today()` builds a
mixed-resolution OHLCV series (minute for the first 12h, coarser out to min(t0+45d, now)) via
`CachedPriceClient(JupiterChartsClient(min_interval=0.4), data_cache/jupiter_untrunc)` → `entry_fill()`
(pessimistic 90s-max-high +1.5% slip) → `simulate_exit(series, fill, t, policy)` → realized multiple.

## The measurement bar (the discipline that killed every false GO)

A policy is a **GO** only if an *executable* policy clears, at a realistic liquidity cap (≤50x):
`ci_lo > 1` AND `drop3 > 1` AND `f2_logG > 0` AND `$500 single-pass bankroll grows`. Never gate on
point EV or max-over-policies. Six "spectacular" results were each caught as artifacts
(lookahead / undefined-mean lottery / bottom-catching / resampling / single-regime / fill-fragility).
`RESEARCH.md` lists all six — check new results against them.

## Secrets (.env — never print values)

`DUNE_API_KEY`, `DUNE_API_KEY_2`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_EXPOSED_TOOLS`,
`TELEGRAM_SESSION_STRING` (**HIGHLY SENSITIVE** — full Telegram account access). Later phases
(`.env.example`): `WALLET_PRIVATE_KEY` (live execution — **BURNER ONLY**), `SOLANA_RPC_URL`,
`BIRDEYE/HELIUS/JUPITER_API_KEY`. Registering a credential-bearing MCP via `claude mcp add` must be
done BY THE USER (the classifier blocks the agent).

## Start-of-session checklist

1. Read this file + `RESEARCH.md` (the strategy + why) + `docs/SYSTEM_DESIGN.md` (the build).
2. Implement — don't re-litigate the research.
