# Audit Remediation Report — 2026-07-05

**What this is.** A full account of the audit findings and the fixes shipped in response. It follows
the exhaustive audit of 2026-07-04 (`tasks/AUDIT_2026-07-04.md`), which found **56 confirmed issues
(19 real-money blockers)** across the live engine, the dashboard backend, and the React frontend.

**Outcome in one line.** All 56 findings addressed across three phases; **242 tests pass** (up from 215),
the frontend builds clean, and the whole system was verified end-to-end. Everything is **committed
locally on `main`, NOT deployed** — the live bot is unchanged until you deploy. The gated live-execution
path was hardened but **remains inert** (never armed, never funded). One architectural item (F26 + the
advance-after-confirm ordering) is intentionally left as the documented **#1 go-live blocker**.

---

## How the audit ran

A multi-agent workflow: **12 dimensioned reviewers** read the actual source (money-safety, state/
durability, strategy-equivalence, shadow, pipeline, monitor/research, dashboard-backend, two frontend
slices, a frontend↔backend contract cross-check, a real-money-readiness lens, and a silent-failure
critic) → **triage/dedup** → **adversarial verify** (a skeptic re-read the source for every finding) →
**synthesis**. 74 raw → 56 unique → 56 confirmed, **0 false-positives** (45 confirmed, 11 partial). The
two criticals and the key highs were independently re-read against source before any work began.

**The core verdict:** the *paper* system was sound; the danger was concentrated in the **gated/inert
live-execution path** (code that only runs once armed), plus a cluster of robustness bugs that degrade
the *running paper bot* today.

---

## Phase B — harden the running paper bot  (commit `ceb93a4`)

Robustness bugs affecting the 24/7 paper engine right now. Nine findings:

| ID | Problem | Fix |
|----|---------|-----|
| **F05** | One tick exception crash-looped the whole process (gather used `return_exceptions=False`) | `engine.on_candle` guarded in `_on_tick`; all four loops run under a `_supervise` wrapper that restarts a crashed task with backoff + a CRIT alert |
| **F30** | Telegram listener had no reconnect — crash-on-blip, or silent deafness with the heartbeat still ticking | Supervised reconnect loop with capped backoff; **replays calls missed during downtime** from a persisted high-water message id |
| **F24** | Dead-token detection was poll-count based (~44s) → a transient feed gap could FINALIZE a live RIDING position and forfeit the one tail | Time-based (15 min) **+ datapi cross-confirmed** (`note_alive` on a fresh true candle) |
| **F28** | A single stale/cross-pool tick drove the machine → a spurious high tick fired TP1 and removed the −30% stop forever | Ratio-guard quarantine: an extreme single-poll move waits for a 2nd confirming poll (the −50% dip / −30% stop pass through) |
| **F20** | Entry priced off the transient ingest-time spot, racing the sim's true first-1m-open anchor | Pin the anchor **before the first tick** (sync reanchor in `_on_call`) |
| **F15** | The weekly research worker shared the engine's SQLite connection cross-thread (a research commit flushed the engine's pending write) | Research gets its **own** `LiveState` connection |
| **F33 / F25** | Feed heartbeat was written unconditionally → `FEED_OUTAGE` could never fire; poll failures were silently swallowed | Heartbeat reflects **real** feed liveness (last successful poll); a failure counter logs sustained outages |
| **F16** | Two Railway processes on one DB with no `busy_timeout`, and a full-table shadow-dedup scan re-ran on every dashboard POST | `busy_timeout=5000` on every connection; the heavy dedup runs **once** (until the index exists) |
| **F32** | Drift monitor compared the trade population to itself → could never fire; a degenerate zero band false-alarmed every 30s | Judge **live-provenance** trades against a **frozen seed band**; degenerate-band guard; transition-based alert dedup |

**Tests (+13):** time-based death, outlier quarantine/confirm, `note_alive` reset, feed-liveness/fail
counter, listener high-water + catch-up ordering + dedup, frozen-band drift + alert dedup,
supervisor-survives-crash.

---

## Phase C — dashboard correctness & reporting  (commit `66bfe9c`)

What the dashboard shows and how the control plane behaves. Backend (`data.py`, `app.py`, `engine.py`,
`shadow.py`) and frontend:

**Correctness / integrity**
- **F42 / F44** — the stat tiles counted an open bag marked >1× as a *win*. Outcome metrics (win%, mean,
  best, bleed, concentration, hill_alpha) are now **closed-trades only**; `best` agrees with
  days-since-≥10x; `n_open` split out.
- **F27** (engine) — a mint declined by a LIVE cap (max_concurrent / kill) was `mark_seen`'d and thus
  **blacklisted forever**. Now marked seen only when actually positioned → it can enter on a later re-call.
- **F22** (shadow) — a horizon leg's wall-clock `closed_at` differed on a reboot re-finalize →
  double-count. `_retire` now persists the done-snapshot **before** flush+delete, so the crash-replay
  re-flushes the same `closed_at` and `INSERT OR IGNORE` dedups.
- **F52** — hero open-position `peak_multiple` was the raw price; now `peak/entry` (a true multiple).
- **F43** — `deployed`/`dry_powder` ignored the 3×-recovered stake; now uses remaining cost basis.
- **F40 / F53** — the hero read `stats.hill_alpha` (all-scope) and `stats.best_multiple` (a key that
  doesn't exist on the scoped object); now reads the scoped `hill_alpha` and `best`.

**API / control plane**
- **F38** — champion promotion isn't wired to the engine (it always trades C1). The endpoint now
  **rejects** the write (422) instead of storing a key the dashboard would display as a false champion.
- **F37** — a custom challenger persisted the **raw client dict** (stray keys survived validation);
  now a clean, validated projection.
- **F35** — the candles endpoint 500'd when datapi hiccuped; now degrades to 200 (empty candles +
  the level overlay).
- **F36** — the stream route made up to 4 blocking DexScreener fetches per request (~20s on a cold
  cache); FDV enrichment moved to a **bounded background warmer**.
- **F51** — dropped the unused `bankroll` key from the ~2s snapshot (a full-table scan per push).

**Frontend UX / honesty**
- **F46** — the lab greened any config mean ≥1, so a lucky n=1 tail read as a winning strategy; now
  green requires **n≥10**, `CONTROL` badges mark C7/C10, drop-top1 shows "—" at n<2.
- **F50** — the caveat hard-coded "$3/trade" while the stake is editable to $10; now **interpolates the
  live stake** (truer, never softer).
- **F45** — positions P&L cell uses `$` + U+2212, neutral for flat/null (no green "0.00").
- **F47 / F48 / F49** — total-column tooltip no longer overclaims reconciliation; clone disabled for
  never-secure (ftp≥1e8) configs the builder can't represent; the add-strategy form mirrors all backend
  bounds inline.
- **F39** — WS staleness watchdog + malformed-frame guard + backoff (silent drops now reconnect).
- **F41** — a monotonic snapshot guard so a slow HTTP fetch can't clobber a fresher WS snapshot.

**Tests (+2, +updates):** rejected-call-not-blacklisted (F27), horizon-leg replay stability (F22),
plus updated the champion (now-422) and scoped-stats (now closed-only) assertions.

---

## Phase A — live-execution safety rewrite  (commit `223f6a6`, still INERT)

The money-moving path, made correct for when it is eventually armed. It **ships disabled** (`dry_run`
default, `armed=False`, triple-gated) and was never armed or funded. Design + go-live checklist in
`docs/LIVE_EXECUTION.md`.

- **F01 — phantom fills (the critical).** A swap that doesn't confirm now **raises `SwapNotConfirmed`**;
  the state machine never advances on a phantom fill. Fills are built from the **actual on-chain balance
  deltas** (`getTransaction` pre/post token balances), not the quote's `outAmount`.
- **F54 — non-idempotent entry (the critical).** Buy first checks the burner's on-chain balance and
  **adopts** it if already held → a crash between a landed swap and the DB commit can't double-buy.
- **F02 — sells sized from the model.** Now sized from the **real held balance** (`min(modeled, held)`);
  `FINALIZE` dumps the whole bag; a sell can never request more than is held.
- **F04 — kill-switch stranded exits.** The kill-switch now blocks **new buys only**; stop/TP/finalize
  sells always execute.
- **F06 — decimals defaulted to 6.** Now read from the on-chain mint and **fail closed**.
- **F07 — static priority fee.** Dynamic `priorityLevelWithMaxLamports` so stops land in congestion.
- **F08 — dead slippage knob.** Per-leg slippage: tight entry, **wide configurable exit**.
- **F09 — no burner allowlist.** `load_burner_keypair` asserts the pubkey equals the sanctioned burner
  (`49fLD…2BVT`) and raises otherwise — never printing the key. The compromised key can never be signed with.
- **F11 — gates were comments.** A consecutive-failure **breaker** trips the kill-switch; real sends
  additionally require two persisted operator gates (`equivalence_ok`, `dust_reconciled`), not settable
  from the dashboard.

**Tests (+13, offline via a fake swap client, no solders/RPC):** confirm-then-commit (buy/sell),
on-chain out-amount, idempotent adopt, sell clamp-to-balance, FINALIZE dump-all, kill-buys-only,
decimals fail-closed, per-leg slippage, breaker-trips-kill, gates-required, breaker reset, burner env guard.

### What remains before real money (the #1 go-live blocker)
**F26 + advance-after-confirm.** Two coupled, live-only issues left as a deliberate, documented item —
too risky to redesign an inert path unattended:
1. The swap `send+confirm` (~30s) runs on the tick thread; it must be dispatched to a dedicated
   single-consumer worker (its own SQLite connection) so a confirming trade can't freeze every other
   position's −30% stop.
2. `engine.on_candle` advances the in-memory machine before the executor confirms; the transition must
   commit only **after** a confirmed fill.

Plus the audit's **missed-areas** (untouched, tracked in `docs/LIVE_EXECUTION.md`): no devnet/dust
end-to-end test of the swap path; MEV/sandwich into thin pools; partial-fill / route-split; blockhash
expiry/replay; burner SOL/rent/ATA gas funding; a periodic on-chain↔SQLite reconciler; `/data` backup +
WAL checkpointing. **F03** (modeled-vs-actual live P&L) is now naturally covered — Phase A derives fills
from real on-chain amounts; wiring those into `closed_trades.pnl_usd` / bankroll is the remaining piece
of the same dispatch refactor.

---

## Verification (end-to-end, all green)

1. **`uv run pytest` → 242 passed** (was 215; +27 net new tests across the three phases).
2. **Frontend production build** — clean (605 modules, no errors); all JSX edits compile.
3. **Engine→dashboard paper pipeline E2E** — a synthetic call driven through the real engine:
   ingest → −50% dip ENTER (50.5) → 3× SECURE (sell 33%, stop removed) → 6× RIDE SELL → finalize →
   a closed trade (5.15×, +$12.45) → the read-only dashboard snapshot reflects it (balance $512.45,
   live n=1, best 5.15×, scoped `n_open`/`hill_alpha` present, `bankroll` key absent).
4. **Endpoint sweep** (served against a demo DB): health / snapshot / history / stream / control / lab /
   token all 200; POST validation — valid knob 200, locked strategy param 400, **champion 422 (F38)**,
   bad kill 422, `add_challenger` 200, out-of-bounds `add_challenger` 422; **F37** the stored custom
   challenger carries only the clean whitelisted keys.
5. **Served-page render** (Playwright, 1600×1400) — **zero console/page errors**; hero, account,
   distribution tiles, positions, stream, and strategy lab all present.
6. **Controls modal** — the **F50** caveat reads *"a deliberate tail-bet at $5/trade …"* (interpolated
   from the stake I set to $5), proving the interpolation.
7. **Strategy lab** — **2 CONTROL badges** on C7/C10 (F46); the E2E-added custom strategy appears; the
   champion badge renders.
8. **Live path inertness** — the executor refuses to buy while unarmed / not in live mode; arming a real
   send additionally requires the two operator gates. Verified by the gating tests; nothing was armed or
   funded.

---

## Status & recommendation

- **Committed locally on `main`**, in four commits (`b7145f1` audit doc, `ceb93a4` Phase B,
  `66bfe9c` Phase C, `223f6a6` Phase A). **Not deployed** — the live paper bot on Railway is unchanged.
- **To ship the paper-bot + dashboard improvements:** deploy as usual (`railway up --detach`, then poll
  the deployment id to SUCCESS, then verify the served page). Phases B and C are safe, paper-only wins.
- **Real money:** see Phase A2 below — the F26 blocker is now built, reviewed, and deployed (inert).

---

## Phase A2 — off-loop execution pipeline  (commit `0cf4c90`, deployed inert 2026-07-05)

The remaining go-live blocker (F26 + advance-after-confirm), built to a top-tier bar:

- **Architecture** (`src/memebot/live/execution.py`, design in `docs/LIVE_EXECUTION_PIPELINE.md`): the
  engine **decides on the event loop** and submits an `ExecJob`; a **dedicated worker thread** runs the
  ~30s swap+confirm off the loop; the result crosses back via `loop.call_soon_threadsafe` and the engine
  **commits it on the loop — after confirmation** (advance-after-confirm). All DB writes stay
  single-threaded (no locks). Live P&L is now **real** (F03): `closed_trades.pnl_usd` = Σ actual sell
  proceeds − real cost.
- **Paper is byte-identical** — `pipeline=None` in paper mode, the equivalence gate + engine E2E pass
  unchanged (verified: same 5.149× close).
- **Adversarially reviewed** (6 reviewers → verify → synthesize; verdict *safe-to-commit-inert, must-fix
  before arming*). All **3 must-fix + 3 should-fix** fixed and pinned with tests:
  - one exec leg per job (`single_exec`) so a failed leg can't strand a landed one (partial-batch orphan);
  - **idempotent** fractional sells keyed to a target remaining balance (no double-sell on re-fire);
  - boot **restart reconciliation** that adopts a landed buy (no orphaned bag after a crash mid-submit);
  - **real SOL proceeds** parsed from the tx's lamport delta (the WSOL side used to read 0);
  - orphan-balance CRIT backstop; accumulating pending buffer; dedicated executor client; etc.
- **Tooling:** `scripts/dust_reconcile.py` — the operator's pre-live round-trip verifier (dry-run by
  default; `--send` gated behind `MEMEBOT_LIVE_ARMED=1` + a typed phrase; reconciles buy/sell vs on-chain).
- **Verified:** 256 tests; paper E2E byte-identical; the **full pipeline dry-run against the real chain**
  (decide → quote off-loop → apply → ENTERED, nothing signed/sent). Deployed inert (prod stays `mode=paper`).

### Go-live steps (all deliberate, in order — nothing here has been done)
The burner `49fLD…2BVT` is funded (~0.1 SOL). To actually trade real money:
1. `set -a && . ./.env && set +a` then dry-run: `PYTHONPATH=src python scripts/dust_reconcile.py --mint <liquid_mint> --usd 1.0`
2. Real round-trip: `MEMEBOT_LIVE_ARMED=1 PYTHONPATH=src python scripts/dust_reconcile.py --mint <mint> --usd 1.0 --send` → confirm it reconciles.
3. Set the gates on the real DB: `equivalence_ok=1` + `dust_reconciled=1` (the script prints the exact command).
4. On the Railway deploy: config `mode=live`, env `MEMEBOT_LIVE_ARMED=1` and `MEMEBOT_LIVE_SEND=1`.
5. Watch the first real trades closely; the kill-switch, the swap-failure breaker, and the orphan/drift
   reconciler are all armed. Fund more SOL before running many concurrent positions (~$3 each + gas).

Remaining refinement (not a blocker): true **cross-mint execution concurrency** (one worker today; a
config bump with connection-per-worker). Size it as money you can lose entirely — it stays a tail bet.
