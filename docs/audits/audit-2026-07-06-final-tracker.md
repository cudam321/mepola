# FINAL AUDIT → ARM LIVE — 2026-07-06 (autopilot)

Goal: exhaustive audit of every line, fix everything (bugs + tech debt), then arm REAL live trading.
User asleep, on autopilot. Guardrail: this is a tail bet sized as money-you-can-lose; fail-closed always.
If ANY blocker can't be safely resolved autonomously → STOP, leave paper, report.

## Baseline (verified)
- [x] git tree clean at `2d44b5d`, branch `main`
- [x] tests all green in CLEAN env (`env -u DASHBOARD_PASSWORD uv run pytest -q` = 100%)
- [x] read money-path modules firsthand: jupiter_swap.py, executor.py, execution.py
- [x] tools present: codex 0.138.0, thermo skill, railway CLI

## Phase 1 — Exhaustive audit
- [ ] Master Workflow: 17 module/dimension finders → adversarial verify → completeness critic → synthesis
- [ ] Codex cross-model pass on money-path files → fold findings in
- [ ] /thermo-nuclear-code-quality-review + /review (final confirm pass)

## Phase 2 — Fix everything
- [ ] Fix every confirmed blocker/high (targeted tests + re-read each)
- [ ] Fix medium/low + tech debt (dead code, hermeticity)
- [ ] Known pre-audit finding: API tests non-hermetic (401 when DASHBOARD_PASSWORD set)
- [ ] Full `uv run pytest` green (clean env) + re-verify fixes

## Phase 3 — Arm live (gated on clean audit)
- [ ] Confirm arming pieces (burner key path, Railway env, gates)
- [ ] equivalence_ok + dust_reconciled satisfied
- [ ] mode=live + env flags + deploy + poll to SUCCESS + verify served health

## Grounding (read firsthand this session)
jupiter_swap.py, executor.py, execution.py, engine.py (786), run.py (881), orders.py, strategy.py.
Code is mature: faithful stage38 sim port, pessimistic stop-before-TP, single-writer SQLite on the
loop, confirm-then-commit, idempotent buy/sell, extensive prior-audit annotations (F01..F54/L3).

### Early observations (to confirm via audit)
- `engine.manual_exposure_usd()` (engine.py:340) appears DEAD (defined, never called; data.py computes
  its own; only a test references the data.py one). Total-exposure cap was dropped in the rework. -> tech debt.
- data.py still ships `manual`(manual_desk)+`attribution` in the snapshot (917-918); no frontend src
  references them post-rework -> likely dead payload.
- Test hermeticity: conftest has autouse `_no_ambient_dashboard_password` (delenv), but app likely
  captures DASHBOARD_PASSWORD at import -> my `set -a . .env` run 401'd. Low sev test hygiene.
- Failed direct-buy on a NEW mint leaves an orphan WATCHING/algo row w/ no rider (no $ at risk; a
  restart would rehydrate it into a normal algo watcher). Minor — confirm intended.

## Arming readiness (Phase 3 prep — verified)
- Railway currently has NO WALLET_PRIVATE_KEY and NO MEMEBOT_LIVE_* flags -> prod is genuinely
  fail-closed in paper (engine can't even sign). Good.
- Local .env has WALLET_PRIVATE_KEY (burner) + SOLANA_RPC_URL. Arming = runbook docs/GO_LIVE.md:
  config mode=live + railway variables --set WALLET_PRIVATE_KEY(=$VAR)/ARMED/SEND/GATES/FRESH_LIVE + up.
- Dust reconcile already PASSED 2026-07-05 (executor proven on-chain).

## AUDIT RESULT (workflow wmu5vcd99): DO-NOT-ARM, 2 blockers. 95 confirmed / 10 refuted.
Full detail: tasks/AUDIT_FINDINGS_2026-07-06.md. Verified: NO fund-loss/double-spend path;
idempotency holds; kill-switch never blocks exits; burner allowlist correct; gates not
dashboard-settable; paper==sim faithful. Issues = unenforced exposure ceilings + accounting/security gaps.

### FIX PLAN (drive from tasks/AUDIT_FINDINGS_2026-07-06.md ranked #1-#35)
- [x] #1 blocker: direct_buy -> risk.can_enter gate + clamp to STAKE_HARD_CAP; dead helpers deleted; +2 tests
- [x] #2 blocker: re-check can_enter at ENTER in _drive_live (live-only); +test
- [x] #3 high: scrub httpx errors in jupiter_swap._rpc + scrub_secrets() at execution boundary
- [x] #4 high: start.sh refuses to boot without DASHBOARD_PASSWORD
- [x] #5 high: reconcile_landed_algo_sell re-drives rider w/ real proceeds on restart
- [x] #6 med: executor sell _sol_usd guard (last-good SOL fallback)
- [x] #7 med: _dead_writeoff for unroutable algo FINALIZE (bounded, no-swap)
- [x] #8 med: FRESH_LIVE marker written even w/o DB (one-shot)
- [x] #10 med: on_candle controller/terminal fail-safe + defensive rider pop
- [x] #11 med: burner allowlist tests (solders: reject wrong key, accept sanctioned) +2 tests
- [x] #12 med: algo finalize cancels resting orders (_cancel_resting_orders) + _TERMINAL_REASONS fixed
- [x] #9 med: listener liveness heartbeat + staleness check + test
- [x] #13 orphan closed_trades boot repair; #14 daily-loss live-only; #16 algo-fail alert + comment;
      #18 orphan-watcher reap; #19 take-over stop-carry + order-first; #22 config doc; #23 submitted-only;
      #24 manual reanchor; #26 dead payload dropped; #27 submitted-row filter; #28 col whitelist + migration;
      #29 boot reconcile timeout; #30 dust guards; #31 shadow doc; #32 hermetic auth; #33 demo-db guard
- [x] #21 honesty: as_designed over realized subset + net·sim tile caveat (neutral tone)
- [x] #25 frontend safety: stop-above-price block + empty-sell% block (frontend builds clean)
- [x] #34 dead code (next_rung_price) + misleading comments (worker/watchdog); #35 base58 mint + expires_h bound
- [x] re-verify pass 1 (6-agent) FOUND 3 real problems in my fixes (fix-then-arm):
      - #1/#2 concurrency: confirmed-only can_enter overshoots under a one-sweep correlated dip
        (empirically 80% over). FIXED: engine._pending_buy_usd reservation -> can_enter reserved_n/usd. +burst test
      - #10 REGRESSION I introduced: on_candle popped the rider before the _pending buffer check ->
        take-over during an in-flight sell dropped the fill. FIXED: guard with `mint not in _pending`. +test
      - #5 confirm window 30s < blockhash validity -> late sell mis-classified failed. FIXED: 90s + final sweep. +tests
      - non-blocking: #7 dead-writeoff tag + per-mint orphan throttle; #13 broaden repair to NULL rmult; #16 leak
- [x] 314 tests GREEN. commits 13ecf57 / 7f9d5ec / 17ae191 / 74e6fea
- [x] re-verify pass 2 (focused) -> ARM-NOW (all corrections verified correct, invariants intact).
      Cleaned up its 3 non-blocking notes (manual_pids hand-off, dead_writeoff label, +#7/#13 tests);
      the #13 test caught a REAL bug (repair crashed on NULL peak_price) -> fixed. 316 tests green.

## ARMED LIVE — COMPLETE ✅ (2026-07-06 ~11:14Z)
- [x] config.toml mode=live committed (518f6fa)
- [x] Railway env: WALLET_PRIVATE_KEY(by $VAR) + MEMEBOT_LIVE_ARMED/SEND/GATES=1, FRESH_LIVE 1->0
- [x] railway up -> deploy 4c6ff1e7 SUCCESS; cleanup redeploy e1aad9e5 SUCCESS
- [x] VERIFIED served: health 200 | mode=live | armed=True dry_run=False | equivalence_ok=1
      dust_reconciled=1 | kill_switch=off | clean book (0 pos/0 orders) | stake=$3.00
- LIVE URL: https://<your-app>.up.railway.app (BasicAuth mepola / .env DASHBOARD_PASSWORD)
- Burner <burner>… funded 0.2976 SOL. Blast radius bounded by burner + $3/trade + now-binding caps.
- ROLLBACK: dashboard kill-switch halts new buys instantly (exits still fire); to fully stand down
  set config mode=paper (or unset MEMEBOT_LIVE_SEND) + railway up.
DONE: ALL 35 ranked items + 3 re-verify + 3 reverify-2 fixed (7 fix commits). 316 tests green. ARMED.

## POST-GO-LIVE user feedback (2026-07-06, same day) — FIXED (commit 21ccfec)
User: "balance says 500?" + "paper balance all over the app" + "do NOT remove the paper machine —
/paper-trade or a view toggle".
- Root cause 1: live book displayed the $500 config (paper) bankroll — run.py wrote bankroll_start
  from config on every boot; live equity was never anchored to the real burner (~$24). FIX: one-shot
  wallet anchor at boot (getBalance x SOL/USD) + purge $500 points + wallet_sol/usd refreshed ~3min
  into system_state + AccountPanel on-chain wallet line + boot-write guard (never resets an anchor).
- Root cause 2: the paper machine was archived (invisible) by FRESH_LIVE. FIX: PAPER TWIN — a second
  paper LiveEngine inside the same orchestrator (same listener/feed, own DB /data/paper_state.db
  seeded once from the archive; uncapped take-every-call; exception-guarded; monitor band now built
  from the paper book). UI: header LIVE/PAPER toggle switches the whole dashboard (?book= param on
  snapshot/history/stream/token/lab; paper view = read-only, 4s polling); paper-era copy made
  book-aware; sim tiles hidden in seed-less books.
- Tests 316->323 (7 new; 6 pre-existing pinned hermetically to paper mode). Deploy 1ac6c51d.

## Findings log

### Codex cross-model (money path) — adjudicated
- C1 finalize_manual books a manual close without routing a swap (engine.py:516-534). REAL asymmetry
  (algo FINALIZE routes a real close-sell). MED-HIGH. Fix: live finalize_manual attempts a real
  close-sell via pipeline; synthetic book only if unroutable (genuinely dead). Orphan backstop already alerts.
- C2 restart reconcile ignores landed ALGO sells (run.py else-branch ~455). REAL accounting gap:
  understated P&L on crash in the ~30s confirm window; NO fund loss (SOL in wallet). MEDIUM. Fix:
  handle STOP_OUT/TP/RIDE_SELL *_SUBMITTED in _reconcile_submitted_intents (reconcile bag+proceeds).
- C3 worker writes SQLite via _exec_state (breaker) — NOT a blocker. Verified: LiveState
  check_same_thread=False + busy_timeout=5000 + WAL + _state_lock + disjoint keys. Deliberate/guarded.
  Optional cleaner refactor (move breaker to loop via FillResult) — risky pre-arm; likely DEFER.
- C4 quote-fallback when landed_amounts()==0 (executor 224/280). LOW-MED slippage-bounded accounting
  approx; Codex's "raise" fix is WORSE (risks re-fire). Fix: retry getTransaction parse + alert; keep fallback.
- C5 direct buys human-sized (not $3), skip algo caps — BY DESIGN (user's override). Autonomous path
  keeps $3 fixed (no-lever-up holds). kill-switch + live gates DO apply. Verify + leave.
- Codex bottom line: "not safe to arm" — but its blockers are accounting/asymmetry, not fund-loss;
  #3/#5 are misreads/by-design. Autonomous path sound. Await workflow's verified findings to consolidate.
