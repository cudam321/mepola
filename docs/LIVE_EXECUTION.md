# LIVE_EXECUTION.md — the real-money path (gated, inert by default)

> **STATUS: built, hardened, and STILL INERT.** `mode` defaults to `paper`; live sending needs
> `mode=live` **and** `MEMEBOT_LIVE_ARMED=1` **and** `MEMEBOT_LIVE_SEND=1` **and** the two persisted
> operator gates below **and** the kill-switch off **and** the burner pubkey allowlist match. Nothing
> here has ever moved real funds. The first real use MUST be a single dust trade (see the checklist).

This documents the live executor after the 2026-07-05 audit remediation (phase A). The audit found the
original live path booked **unconfirmed swaps as fills from quote amounts**, sized sells from the model,
defaulted token decimals to 6, used a static priority fee and a dead slippage knob, had no burner
allowlist, no idempotency, and no failure breaker. All of that is fixed below. One item —
**event-loop-blocking execution (F26)** — is a dispatch/architecture change and remains the #1 go-live
blocker (see the checklist); the code is written to be driven off the loop but the orchestrator does not
yet do so.

## The model: confirm-then-commit on on-chain truth

The chain is the source of truth, not the quote. Every real swap now:

1. **sends** (`sendTransaction`), then **confirms** (`getSignatureStatuses`, ~30s budget, `err`-aware).
2. If **not confirmed** → the executor **raises** (`SwapNotConfirmed`); the strategy state machine does
   NOT advance and the position row is NOT written. A dropped/reverted buy is never booked as owned; a
   failed stop/TP sell is never booked as closed. (F01)
3. If confirmed → the `Fill` is built from the **actual on-chain balance deltas** parsed from the landed
   tx (`getTransaction` → pre/post token balances for the burner), not the quote's `outAmount`. (F01)

### Sizing is from the real wallet, never the model
- **Buy** is **idempotent** (F54): before sending, if the burner already holds this mint above dust, the
  existing balance is **adopted** as the fill instead of sending a second buy — so a crash between a
  landed swap and the DB commit can't double-buy on the next dip candle.
- **Sell** sizes from the **real held balance** (F02): `min(modeled fraction, on-chain balance)`; the
  `FINALIZE` leg dumps the **entire** remaining balance so no dust is stranded. Selling can never
  request more than is held (which would revert and, pre-fix, be booked as a completed sell).
- **Decimals** are read from the **on-chain mint** and **fail closed** (F06) — never the old default of 6
  (which silently sold ~0.1% of a 9-decimal bag).

### The kill-switch protects, it doesn't strand
The kill-switch now blocks **new buys only** (F04). Risk-reducing **sells** (the −30% stop, TPs, finalize)
are never blocked by it — tripping the cap during a drawdown must not freeze open positions with no exit.
(Live sends still require `armed` + `mode=live` regardless.)

### Fees, slippage, safety
- **Priority fee** is dynamic (F07): Jupiter `prioritizationFeeLamports.priorityLevelWithMaxLamports`
  (`priorityLevel=high`, capped), not a static 200k lamports that fails to land in congestion.
- **Slippage** is configurable per leg (F08): a tight entry cap, a **wide exit cap** (exits into
  collapsing microcaps need room), sourced from config — not the dead `cfg.entry_slip` that always fell
  back to 150 bps on both legs.
- **Burner allowlist** (F09): `load_burner_keypair` asserts the loaded pubkey equals the sanctioned
  burner `<YOUR_BURNER_PUBKEY>` and raises otherwise (never printing the key).
  A previously user-pasted key is COMPROMISED — never use or fund it.
- **Failure breaker** (F11): N consecutive unconfirmed/failed swaps trip the kill-switch automatically.
- **Operator gates** (F11): real sends additionally require two persisted `system_state` flags —
  `equivalence_ok=1` (paper≈backtest held) and `dust_reconciled=1` (a dust trade reconciled on-chain).
  Neither is settable from the dashboard; both require a deliberate CLI/operator step.

## Pre-live checklist (the gate to real money) — DO IN ORDER

1. **[F26 + advance-after-confirm — BUILT 2026-07-05]** The off-loop execution pipeline now exists
   (`live/execution.py`; design in `docs/LIVE_EXECUTION_PIPELINE.md`): the engine decides on the loop,
   a dedicated worker thread runs the ~30s swap+confirm, and the DB advances only after a confirmed fill
   (on the loop). A single worker keeps one dedicated executor connection (zero races); true cross-mint
   execution concurrency is the documented next refinement (a config bump, not a redesign). Live P&L is
   now REAL (F03). Still verify it on devnet/dust before arming — the pipeline is tested offline but the
   full quote→sign→send→confirm→reconcile round-trip is only proven in dry-run so far.
2. Confirm **paper ≈ backtest** has held for a sustained window; set `equivalence_ok=1`.
3. Fund **only** the sanctioned burner (`49fLD…2BVT`) with a tiny SOL amount (gas + one dust position).
4. Do a **single dust trade** end-to-end (quote → sign → send → confirm → parse deltas) and reconcile the
   recorded fill against the wallet on-chain. Set `dust_reconciled=1` only if it reconciles.
5. Verify the **failure breaker** and **kill-switch = buys-only** behave (a forced not-confirmed trips
   the kill; a kill still lets a stop sell through).
6. Only then flip `MEMEBOT_LIVE_SEND=1` for production, at the $3 fixed stake.

## Not yet covered (tracked, out of scope for this pass)
MEV/sandwich into thin pools; partial-fill / route-split accounting; blockhash-expiry / replay under
`maxRetries`; burner SOL/rent/ATA gas top-ups; a periodic on-chain↔SQLite balance reconciler; `/data`
volume backup + WAL checkpointing. These are go-live engineering items, not paper-mode risks.
