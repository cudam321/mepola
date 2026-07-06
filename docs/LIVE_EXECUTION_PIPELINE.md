# Live Execution Pipeline — design (the F26 / advance-after-confirm refactor)

> Design + review artifact for the 2026-07-05 pipeline build. Companion to `docs/LIVE_EXECUTION.md`
> (the executor + go-live checklist). Ships **inert** — nothing here runs until the operator arms.

## The problem being solved

After the phase-A executor rewrite, live execution was *correct on-chain* (confirm-then-commit, real
balances, fail-closed decimals, breaker) but had two coupled flaws, both **live-only** (paper never hits
them, `PaperExecutor` is instant and never raises):

1. **F26 — it blocked the event loop.** `engine.on_candle` ran on the tick thread and called
   `executor.buy/sell`, which does a synchronous `send + confirm` (~30s of `time.sleep`). While one live
   trade confirmed, the loop could not decide/fire the −30% stop on any *other* open position, ingest new
   calls, or mark-to-market. In a market-wide dump that directly causes missed stops.
2. **advance-after-confirm.** `engine.on_candle` advanced the in-memory machine *before* the executor
   confirmed. With confirm-then-commit now raising on a bad fill, the in-memory rider could sit ahead of
   the (un-persisted) DB row until a restart healed it.

## The design: decide on the loop, execute off it, apply on the loop

Three roles with a **strict ownership rule that removes every shared-state race**:

```
  loop thread            worker pool (N)                 loop thread
  ───────────            ──────────────                  ──────────────
  engine.on_candle  ──►  quote→build→send→confirm  ──►   apply consumer
  (DECIDE)               →parse real amounts             (COMMIT after confirm)
  marks rider busy       (NETWORK ONLY — no DB,          advances DB + rider,
  submits ExecJob        no shared-state mutation)       or rolls back on failure
```

- **Decide (loop, `engine.on_candle`).** Advances the in-memory `TailRider` (unchanged modeled logic)
  and emits Events. In **paper** mode it applies them inline exactly as today (the equivalence path is
  untouched). In **live** mode, if the candle produced execution-bearing events
  (`ENTER|TP|RIDE_SELL|STOP_OUT|FINALIZE`), it:
  - marks the rider **busy** (its `mint` enters `pending`); further candles for a busy rider are
    **buffered** (latest-only) and never processed — so the machine can't act on an unconfirmed entry;
  - writes a durable `*_SUBMITTED` intent event (for restart reconciliation);
  - submits one **`ExecJob`** (mint, pid, ordered exec events, stake) to the pool;
  - does **not** persist the exec advance (the DB stays at the last *confirmed* state — that IS the
    rollback point).
- **Execute (a bounded pool of worker threads, default 8, draining one queue).** Workers run swaps for
  DIFFERENT mints **concurrently** — a dozen simultaneous stop-outs don't serialize behind one confirm —
  while a single mint is never in two workers at once (the engine's `_pending` guard, both ops on the
  loop, admits at most one job per mint in flight). The shared state workers touch is thread-safe: the
  executor's sqlite connection + failure breaker under its own fine-grained lock (held only for the quick
  state ops, NOT the ~30s confirm); the Jupiter clients' rate gates and decimals cache under theirs. So
  the expensive confirm holds no lock and truly overlaps. **Retry is via the engine, not a blind re-send:**
  a not-confirmed swap returns a failure, the engine rolls back, and the *next candle* re-decides — which
  re-reads on-chain state (idempotent-adopt for a buy, real-balance sizing for a sell), so a late-landing
  tx can never double-execute. `_confirm` distinguishes a reverted tx (`err` → stop) from a timeout,
  tolerates a transient RPC blip (keeps polling), and each re-decision re-builds with a fresh blockhash.
  Results cross back to the loop via `loop.call_soon_threadsafe(on_result, result)`. **Why one worker, not a pool:** the point of this pass
  is to unblock the *event loop* (so it keeps deciding/firing stops and ingesting) — that is fully solved
  with one off-loop worker. True *cross-mint execution concurrency* (so a dozen simultaneous stops don't
  serialize) needs a connection-per-worker for the arming/breaker state the executor reads, which is a
  clean but separate change; a single worker keeps one dedicated executor connection, zero races, and the
  phase-A executor + its 16 tests intact. The upgrade path (connection-per-worker or move arming/breaker
  onto the loop) is noted here and is a config bump, not a redesign. Decisions are immediate regardless;
  only the *swaps* serialize — an accepted, documented tradeoff for the first correct inert version.
- **Apply (loop, single async consumer draining the queue).** Because it runs on the loop thread — the
  same thread as `on_candle`, the sampler and the reconciler — **all DB writes and all `riders` mutations
  stay single-threaded; no lock is needed and the single-writer invariant holds.** For each result:
  - **confirmed** → persist the real fills (real tokens/USD), advance the DB position, record real
    proceeds per leg, close + book **real** P&L if terminal, clear busy, then re-feed any buffered candle.
  - **failed** → roll the in-memory rider back to the persisted (pre-job) state (`TailRider.restore` from
    the DB row), clear busy, alert. The next candle retries the same decision.

### Accounting is REAL; decisions stay modeled (F03)
The `TailRider` keeps deciding on **modeled** price levels (deterministic, paper-verifiable — the
equivalence gate still means something). But the money that is **reported** is real: each live sell's
`proceeds_usd` is the actual USD received (from the confirmed fill), and a live position's
`closed_trades.pnl_usd` / `realized_multiple` / the bankroll are computed from **Σ real proceeds − real
cost**, not from `tr.realized_multiple`. Known, bounded nuance: the stop/TP *levels* are relative to the
modeled entry, which differs from the real fill by the entry slippage (~1–1.5%); a −30% stop is therefore
~1% off its real-entry-relative ideal. Documented and accepted; re-anchoring levels to the real entry is
a later refinement, not a safety issue.

## Restart reconciliation (idempotency across a crash)
A swap submitted just before a crash must never double-send. On boot, for any rider whose latest intent
is an unresolved `*_SUBMITTED` (no matching confirmed fill after it): reconcile against the chain.
- `ENTER_SUBMITTED` → if the burner now **holds** the mint above dust, the buy landed → adopt it
  (F54); else the buy never landed → clear the marker and let the dip window retry.
- `SELL_SUBMITTED` → compare the held balance to the expected pre-sell balance; if it dropped, the sell
  landed → apply; else retry.
This reuses the phase-A idempotent `token_balance` reads. The DB row is the authoritative pre-intent
state, so a rebuilt rider always matches confirmed reality.

## Periodic on-chain ↔ SQLite reconciler
A slow task (~every few minutes) walks every open **live** position and compares the burner's real token
balance for that mint against the DB's expected remaining bag. Drift beyond a tolerance → a `RECON_DRIFT`
alert (it does not silently "fix" balances — a human investigates). This is the standing safety net that
catches anything the per-trade path missed (a partial fill mis-parsed, an unaccounted transfer, etc.).

## What stays out of scope (documented, not silently skipped)
- **Partial fills** are handled implicitly: sizing and accounting read the **real** on-chain balance /
  received amount, never the quote — a partial fill just means a smaller real amount, correctly recorded.
- **Route-split** is internal to Jupiter; the landed-delta parse sums all the owner's token accounts.
- **MEV / sandwich** into thin pools cannot be prevented by a small outside participant; mitigation is the
  tight-entry / wide-exit slippage split and small size. Accepted risk, surfaced in the go-live doc.

## Hardening from the adversarial review (2026-07-05)

An adversarial multi-agent review of this pipeline (verdict: *safe to commit inert; must-fix before
arming*) drove these, all now implemented + tested:

- **One exec leg per job (`single_exec`).** The machine runs in `single_exec` mode in live, emitting at
  most one execution event per call; the engine re-feeds the same candle to collect the next rung. So a
  bar that clears several rungs executes **one confirmed swap at a time** — a failed leg can never strand
  an already-landed one (the review's #1 defect). Paper keeps the multi-rung default (equivalence intact).
- **Idempotent fractional sells.** A sell is keyed to a **target remaining balance**
  (`held − round(tokens_qty·remaining_frac)`), not a fresh fraction-of-original — so a rollback/restart
  re-fire of a landed-but-unconfirmed sell is a **no-op** (the executor short-circuits), never a double-sell.
- **Restart reconciliation.** On boot (real-send), `_reconcile_submitted_intents` resolves any
  `*_SUBMITTED` intent a crash left unapplied: `ENTER_SUBMITTED` + a held balance → **adopt** the landed
  buy (else the machine may never re-enter after the price pumps off the dip, orphaning the bag); sells are
  idempotent so a re-fire is safe.
- **Real P&L (F03) from landed amounts.** A sell's proceeds are the **actual net SOL received**, parsed
  from the confirmed tx's owner lamport delta (the WSOL side used to read 0); `closed_trades.pnl_usd` =
  Σ real proceeds − real cost.
- **Orphan backstop.** The periodic reconciler also flags any mint holding a real balance under a
  WATCHING/EXPIRED/closed row (`ORPHAN_BALANCE`, CRIT).
- Accumulating (min-low/max-high) pending buffer so an intra-window stop-low/rung-high isn't dropped;
  dedicated executor `JupiterClient` (thread-safe rate limit); reconciler scheduled only in real-send;
  the worker always delivers a result (guarded) so a rider can't hang busy.

## Invariants the tests must pin
1. **Paper is byte-identical** — the equivalence gate and every existing engine test pass unchanged.
2. A busy rider never processes a candle until its job resolves.
3. A confirmed job advances the DB with **real** amounts and books **real** P&L.
4. A failed job rolls the rider back to the DB state; no phantom position; the next candle retries.
5. Concurrent jobs across mints don't interleave DB writes (all applied on the loop, in queue order).
6. A crash mid-submit reconciles on boot with **no double-send** (idempotent adopt).
7. The event loop is never blocked by a swap confirm (submit returns immediately).
