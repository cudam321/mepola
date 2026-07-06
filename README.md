<p align="center">
  <img src="docs/img/logo.png" alt="MEPOLA — MEME · POWER · LAW" width="720">
</p>

**MEPOLA** (*MEme POwer LAw*) is an autonomous Solana trading machine built around one idea:
memecoin returns follow a **power law**, and everything about trading them — entries, exits,
sizing, expectations, even the dashboard you watch — should be designed around that
distribution instead of around the averages that it breaks.

It pulls calls from your Telegram call group/channel, waits for its price, runs a single
measured dip-entry tail-riding configuration 24/7, races 18 challenger strategies against it
on the same live ticks, and renders all of it on a phosphor-CRT terminal dashboard.

![MEPOLA dashboard](docs/img/dashboard.png)

---

## The thesis — two dynamics, one design

Everything in this repo is downstream of two measured facts that pull in opposite directions:

**1. The tail is real.** Memecoin outcomes are power-law distributed. Our corpus of 1,263
first-calls contains a verified ~700× runner; the top ~1% of tokens carries essentially all
aggregate gain. A distribution like this cannot be summarized by a mean or traded by a win
rate — one token *is* the P&L of a whole quarter.

**2. Your seat is late.** By the time a call reaches you, you are (median) ~2.65× above the
callers' own entry. Measured honestly — full dead-token denominator, latency-real fills,
bootstrap CIs — per-trade EV for a follower is structurally ≤ 1, and no signal observable at
entry separates the future tail from the graveyard (every feature we tested ≈ 0.50 rank-AUC).

The design question that follows is *the* question of this project: **if you can't buy an
edge, what is the best-shaped vehicle for holding tail exposure while surviving the bleed?**
The answer — found by a 144-configuration grid search and verified out-of-sample — is the
strategy this machine runs. The full research (11 tested angles, every number, and the six
false positives we caught on the way) is in **[RESEARCH.md](RESEARCH.md)**.

## The strategy — config #1, the dip-entry tail-rider

1. On a channel call, **never chase**. Watch up to 48h for a **−50% dip** from the signal
   price; no dip, no trade.
2. Buy the dip. From entry, hold a **hard stop at −30%** — cut the bleed fast. Because entry
   was already a −50% dip, the rare tail token has typically bottomed and survives the stop.
3. At **3×**, sell 33% — the stake is recovered — and **remove the stop**. House money.
4. **Ride the ladder**: sell 25% of the remainder at 6× / 12× / 24× / 48×, then ×3 steps
   (144×, 432×, …). The final moonbag never fully exits — that's the tail exposure.
5. **No re-entry**, ever.

```
WATCHING ── price ≤ 0.5×signal within 48h ──► ENTERED ── 3× hit ──► SECURED ──► RIDING ──► EXITED
   │                                             │  (sold 33%, stop off)  (25% of rem at
   └── 48h elapses, no dip ──► EXPIRED (skip)    │                        6/12/24/48×, ×3…)
                                                 └── price ≤ 0.7×entry (pre-secure) ──► STOPPED
```

Out-of-sample, traded straight through with no foresight, this shape ends +43–55% on the
window while the naive follow-the-post seat loses ~20% per trade — but the gain is
tail-concentrated and the strategy is **measurably size-fragile** (see
[Sizing](#sizing--the-most-important-knob) below). The live engine is pinned
floating-point-exact to the research simulation by `tests/test_strategy_equivalence.py`:
what was measured is what trades.

## What it does

### The autonomous engine
Telethon listener → signal parser → TailRider state machine → resilient multi-source price
feed → executor, orchestrated async, state in SQLite (single writer, crash-safe, idempotent
order keys). A self-awareness monitor watches feed liveness, listener health, fill-vs-model
drift, and paper≈backtest equivalence, and surfaces alerts to the dashboard. Paper mode is
the default; live execution (Jupiter swaps, confirm-then-commit — a fill is only booked
after the transaction lands on-chain) ships **inert** behind five independent gates.

### The shadow lab — the research never stops
Eighteen challenger strategies (deeper stops, no stop, chase entries, trails, moonbags,
regime gates, …) plus any custom configurations you add run forward on the **same live
ticks** as the champion, so every week of deployment produces a fresh out-of-sample grid.
Each challenger is pinned to its research oracle in `tests/test_shadow.py`. Promotion is
human-only — the machine measures; it does not self-modify.

![Strategy lab — the forward shadow race](docs/img/strategy-lab.png)

### The MEPOLA dashboard
FastAPI + React/ECharts behind BasicAuth, designed as a phosphor CRT instrument
(`DESIGN.md` is the design law). The hero chart is the power law itself — every position
ranked by return multiple on a log scale, with break-even, the bleed, and the tail as
reference lines, and a Pareto concentration curve showing how much of the P&L is one token.
An honest-status banner reports whether the machine is behaving *as designed* (bleed
included) — not just whether it's up.

- **LIVE / PAPER book toggle** — a full paper practice desk runs beside the live book, same
  engine, same UI, strictly isolated state (per-tab, so a practice click can never route to
  the live book).
- **Positions & watchlist** — every WATCHING token with its dip-progress bar, distance to
  trigger, and expiry clock; every open position with its lifecycle state.
- **Execution stream** — every order and event, newest first, FDV-enriched.

![Positions, watchlist and the execution stream](docs/img/positions.png)

- **Token terminal** — click any token for a per-token trading terminal: price chart with
  entry/stop/rung overlays, the full lifecycle event log, and (when you take over) manual
  override controls — take-profit, stop-loss, trailing, or algo-release.

![Token terminal](docs/img/token-terminal.png)

- **Runtime controls** — sizing and risk caps are adjustable at runtime (stake per trade,
  max concurrent, total-deployed cap, daily-loss cap, per-buy cap) inside research-measured
  hard bounds, plus a global **kill switch**. The strategy itself is locked — knobs change
  how much you risk, never what the machine believes.

![Runtime controls](docs/img/controls.png)

### The research harness
The measurement machinery that produced every number in [RESEARCH.md](RESEARCH.md) ships in
the repo: corpus ingestion from your channel's history, disk-cached OHLCV, a deliberately
pessimistic fill simulator (latency window, slippage, gas, dead-token total losses),
exit-policy simulation, excursion analysis, and 60+ stage/attack/refute scripts you can run
against your own channel's corpus.

## Sizing — the most important knob

Stake sizing is **configurable** — fixed-dollar or fraction-of-equity mode in `config.toml`,
adjustable at runtime from the dashboard — because risk tolerance and bankroll are personal.
What is *not* negotiable is the shape of the constraint, because it was measured, not
opined: on a $500-class backtest bankroll the strategy survives small stakes and **dies
above roughly $10–25 per trade** — sizing up amplifies the bleed faster than the tail can
repay it. The risk governor therefore enforces a hard per-trade cap, concurrency and
deployed-capital caps, a daily-loss cap, and a kill switch, and there is deliberately **no
code path that scales stake with equity**. Scale the numbers to your own bankroll; keep the
proportions.

## Audits

The codebase has been through **multiple independent audit passes using different review
workflows**: exhaustive multi-agent adversarial audits (100+ agents sweeping every line for
correctness, money-path safety, idempotency, and honesty of displayed numbers), automated
code-review tooling, an independent review by a different model family (OpenAI Codex), and
a focused delta-audit after each significant feature. Together they produced 50+ confirmed
findings — including 19 real-money blockers, all fixed and regression-tested before the live
path was ever armed. The full reports, findings and remediation trackers are published in
[`docs/audits/`](docs/audits/):

| report | scope |
| --- | --- |
| [audit-2026-07-04-live-path](docs/audits/audit-2026-07-04-live-path.md) | first exhaustive engine + dashboard audit (56 findings, 19 real-money blockers) |
| [audit-2026-07-05-remediation](docs/audits/audit-2026-07-05-remediation.md) | remediation tracker — every blocker closed with its fix |
| [audit-2026-07-06-final-findings](docs/audits/audit-2026-07-06-final-findings.md) | final pre-arming audit — full-codebase, multi-workflow findings |
| [audit-2026-07-06-final-tracker](docs/audits/audit-2026-07-06-final-tracker.md) | final verification passes and arming decision trail |

The test suite (330 tests) pins the live engine to the research sims, exercises the
fail-closed gates, and regression-tests every audit finding.

## Quick start (demo, no Telegram needed)

```bash
uv sync --extra dev --extra dashboard
uv run pytest                                  # full test suite

# build the frontend once:
npm --prefix dashboard/frontend install
npm --prefix dashboard/frontend run build

# realistic demo DB + dashboard:
PYTHONPATH=src python3 scripts/make_demo_db.py --out /tmp/demo_state.db
MEMEBOT_DB=/tmp/demo_state.db uv run --extra dashboard \
    uvicorn dashboard.server.app:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000
```

## Run it on your channel

1. Copy `.env.example` → `.env` and fill in your Telegram API credentials (comments in the
   file walk you through it). The account must have joined the channel you want to follow.
2. Set `MEMEBOT_CHANNEL=@your_channel`.
3. Start the paper loop (autonomous, 24/7): `PYTHONPATH=src python -m memebot.live.run`
4. Optionally, pull the channel's history and run the research harness on it — measure
   *your* channel before you trust it: see [RESEARCH.md](RESEARCH.md) and
   [`docs/RUNNING.md`](docs/RUNNING.md).

Deployment (Railway or a VPS) is covered in [`docs/DEPLOY_RAILWAY.md`](docs/DEPLOY_RAILWAY.md)
and [`docs/DEPLOY.md`](docs/DEPLOY.md).

**Going live with real money** requires five independent gates to align — config mode, two
explicit env arms, a burner-wallet allowlist (`MEMEBOT_BURNER_PUBKEY`; unset = nothing can
ever sign), and the kill switch — plus a first dust trade reconciled on-chain. Read
[`docs/GO_LIVE.md`](docs/GO_LIVE.md) first. Use a burner wallet, always.

## Repo map

```
src/memebot/          research library: models, parser, ingest, data clients, exit sims,
                      fill simulator, backtest harness, safety checks
src/memebot/live/     the 24/7 machine: strategy, engine, executor, risk governor, state,
                      price feed, listener, monitor, shadow lab, jupiter swaps
dashboard/            FastAPI server + React/ECharts frontend (the MEPOLA CRT terminal)
scripts/              60+ research CLIs (stage0..stage39, attack_*, refute_*) + utilities
docs/                 system design, running, deploy, go-live, audits/
RESEARCH.md           the full research story — start here
DESIGN.md             the dashboard's design law
```

## Disclaimer

This is not financial advice, and this software does not produce reliable income — the
research in this repo exists precisely to demonstrate what following call channels is and
isn't worth. Memecoin trading is extremely high risk: expect long stretches of small losses;
most tokens go to zero; profitable periods, when they happen, are typically carried by a
single tail event. If you run this with real money: use a burner wallet, keep stakes within
the measured envelope, and size it as money you can lose entirely.
