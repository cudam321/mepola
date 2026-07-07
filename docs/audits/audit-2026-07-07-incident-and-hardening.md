> Public copy of the 2026-07-07 post-incident audit + same-day re-audit. Wallet-
> identifying specifics (mints, exact amounts) are redacted; every finding and fix
> is real and shipped. The one-shot data repairs referenced live only in the
> operator's private tree — they are content-keyed no-ops on any other database.

# AUDIT 2026-07-07 — post-incident re-audit of the live money paths

Four parallel auditors over executor/swap, engine, orchestrator, and manual/dashboard paths,
each hunting the incident class ("a single RPC read / restored field treated as truth").
Every finding below survived the auditor's own refutation attempt + a spot-check.
Status: ☐ open ☑ fixed (commit in parens).

## BLOCKERS

- ☑ **B1 executor: post-confirm exception books a landed sell as $0 on the re-fire** (fixed same day)
  After `res.confirmed`, `sell_event` still ran `_sol_usd()` (network, can RAISE) before returning
  the Fill. A raise after the money moved → pipeline marks the leg failed → rollback → re-fire →
  balance already at target → $0 idempotent skip with no stashed sig. Incident #1 reborn.
  Fix: nothing may raise after confirm — post-confirm section is fail-safe; and see B2.
- ☑ **B2 executor: late-landed proceeds recovered ONLY if the next leg sizes to zero** (fixed same day)
  `_unconfirmed_sell` recovery lived in the `amount_raw <= 0` branch. Timed-out TP1 lands late →
  rider rolled back → next leg is a NONZERO stop/finalize → sig popped at line 363 without ever
  reading the landed proceeds → realized P&L permanently short.
  Fix: any next sell resolves the stashed sig FIRST and folds recovered proceeds into its fill.
- ☑ **B3 engine: `_dead_writeoff` runs on the already-advanced rider** (fixed same day)
  On the 3rd FINALIZE failure the writeoff used the optimistically-finalized rider (rem=0) →
  residual always valued $0, no FINALIZE event appended, and `dead_writeoff` close_reason then
  SUPPRESSES the orphan alert for a real bag. Fix: rollback to the DB state before writing off.
- ☑ **B4 notify: a failed Telegram send permanently drops the alert** (fixed same day)
  `kind_last` was stamped on the ATTEMPT; after a send failure the retry pass saw the row as
  throttled but still advanced the high-water → CRIT consumed, never delivered.
  Fix: throttle stamps + high-water advance only after the send succeeds, per message.
- ☑ **B5 release: fast-forward bypassed when the release lands while the engine is down** (fixed same day)
  Dashboard release only wrote controller+rev; boot `_rehydrate` restores `lvl = next_rung_mult
  or tp1_mult` and the boot-cached rev absorbs the bump → the ladder-replay incident recurs.
  Fix: the release ENDPOINT persists the fast-forwarded n_tp/next_rung before flipping.
- ☑ **B6 boot reconcile trusts one zero-balance read** (fixed f36d70e) (run.py `_reconcile_submitted_intents` +
  `_held_tokens`): a lagged 0 can book a PHANTOM full stop (proceeds that never arrived, position
  closed, bag unmanaged), close a manual position with the bag still on-chain, or double-buy on
  ENTER retry (`buy`'s adopt check is also a single read, no n_accounts/no retry — executor
  side partially fixed by using token_balance_ex + retry in adopt too). NEXT SESSION.

## HIGH

- ☑ H1 (fixed f36d70e — commitment:"confirmed" + 5-attempt retry) getTransaction defaults to FINALIZED while `_confirm` returns at CONFIRMED → the
  landed-amounts/post-balance parse routinely returns (0,0,-1): proceeds book from the QUOTE
  (up to 8% high with exit slippage), `_post_bal` cache stays empty, drift check no-ops.
  Fix shape: pass commitment:"confirmed" to getTransaction + brief retry. (jupiter_swap.py:222)
- ☑ H2 (fixed f36d70e — _unconfirmed_buy stash + landed-tx adopt + token_balance_ex/cache) buy adopt check (executor.py:232) is a single raw read — ambiguous zero → DOUBLE BUY.
  Apply the sell-side hardening (token_balance_ex + _post_bal + retry) to the buy path.
- ☑ H3 (fixed 6005ff6 — state.claim_order CAS on every transition, both processes) order cancel/fill has no compare-and-swap: a cancelled resting order can still sell
  (engine reads a pre-cancel snapshot; `update_order` has no `AND status=` guard); a failed
  in-flight order resets a user's cancel back to 'open'. Fix: claim orders with
  `UPDATE ... WHERE id=? AND status='open'` + rowcount check. (orders.py/engine.py/state.py)
- ☑ H4 (fixed 6005ff6 — GET/POST book-scoped; challengers stay live-DB by design) /api/control has NO book scoping: a practice-tab user flips the LIVE kill_switch or
  live stake. Fix: `_db_for(book)` like every other endpoint + frontend withBook.
- ☑ H5 (fixed 6005ff6 — force_close books real proceeds + EXTERNAL_SELL_RECONCILED; dead-manual finalize swaps 3x then honest writeoff + WARN) external burner sells (operator workflow!) make a clamped 'close' fill leave the
  position OPEN with a phantom bag; a later dead finalize_manual books held×price with NO swap
  → fictional realized P&L. Fix: close branch should key off the REAL post-fill balance;
  finalize_manual on a LIVE book must attempt the swap (or book $0 with an orphan alert).
- ☑ H6 (fixed f36d70e — step-change detection + wide backstop + M12 race guard; M2 fee ledger folded in) equity-invariant zero point is frozen USD: SOL beta ±15% or any deposit/withdrawal
  → permanent false CRIT every 30 min → operator tunes out the one alarm that matters.
  Fix shape: track SOL quantity vs book-expected SOL quantity (price-independent), or re-anchor flow.
- ☑ H7 (fixed f36d70e — Monitor.check_recon RECON_STALE watchdog in the sampler; alert-push supervised, bot mode independent of the listener) both safety nets can die SOFTLY: `_reconcile_onchain` no-ops forever on a broken RPC
  (no alert, no staleness watchdog on wallet_at); the alert-push task is unsupervised (a boot-time
  sqlite-busy kills it until the next listener reconnect). Fix: staleness self-check + supervise.

## MED — ALL WORKED 2026-07-07 (see per-item notes)

- ☑ M1 (SELLS_DISABLED CRIT on the transition, cleared on the next sent sell) F11 gates/mode block SELLS too: unsetting dust_reconciled/equivalence_ok (or mode) with
  open bags silently disables every stop (engine `_can_send_live` + `_require_armed`) — no alert.
- ☑ M2 (SwapResult.fee_lamports from meta.fee → cum_onchain_fees_usd ledger, subtracted in the invariant; ATA rent excluded as recoverable) buy books stake only: priority fee + ATA rent ≈ $0.3-0.6/position unbooked → slow phantom
  drift into the equity alarm.
- ☑ M3 (last_sol_usd persisted in system_state; _sol_usd_safe falls back to it) `_last_sol_usd` cold start: first action after boot = a stop during a price-API blip →
  proceeds book at $0 (flagged in note only).
- ☑ M4 (partial legs without tokens_qty now RAISE; full-bag legs may dump) `tokens_qty is None` on a non-FINALIZE sell dumps the whole bag on TP1 (fail-open; make it raise).
- ☑ M5 (_apply_manual_result pops _buffered) manual-result path never clears `_buffered` → a stale covering candle can merge extremes
  hours apart → phantom instant stop/TP right after a later entry.
- ☑ M6 (boot reconcile folds the row forward to the newest committed sell event; _close_position_from_events) manual full-close crash window (MANUAL_SELL event committed, position row not) is invisible
  to boot reconcile (newest event ≠ *_SUBMITTED) → later finalize_manual double-books.
- ☑ M7 (unmark_failed_manual_seen on the never_entered reap; channel dedupe untouched) direct-buy marks mint seen BEFORE confirm → a failed direct buy permanently blacklists the
  mint from every entry path (F27 regression).
- ☑ M8 (landed in_amount vs sized amount separates stale-pre-read WARN from real sizing CRIT) restored-rider drift-check false-trip after restart (stale-high read, empty cache) → CRIT
  LEG_DRIFT + kill-switch on a healthy leg. (Accepted for now: fails SAFE, but noisy.)
- ☑ M9 (account() banked part now from events' real USD; cm only a fallback) open-position marks use modeled `proceeds_units` while events hold real USD → balance_usd
  overstates after any $0-skip/clamped leg until close (the exact divergence the v2 repair hand-patched).
- ☑ M10 (fresh trailing order seeds hwm from the current close, arms next candle) fresh trailing stop seeds hwm from ALL-TIME peak_price → can instantly market-dump the
  full bag; no server-side "would fire now" rejection for trailing.
- ☑ M11 (unsecured takeover auto-carries the −30% stop as a REAL resting order the user can cancel/edit) explicit /takeover strips the −30% stop with no carried protection; chart still DRAWS a
  stop no code executes; partial/expiring user stops leave manual bags silently unprotected.
- ☑ M12 (open-set re-check before the invariant; skipped when it changed mid-pass) equity pass races its own snapshot (stale open_mark + fresh realized) → false CRIT exactly
  when the big winner closes.
- ☑ M13 (server 409 would_stop_immediately unless force=true; UI confirm dialog) release of an UNSECURED manual position below the −30% line = instant full market dump
  (fast-forward is secured-only by design; needs a confirm step in the UI).
- ☑ M14 (oldest-first pagination until dry; high-water advances only after on_call succeeds) listener catch-up: >200 missed messages lose the oldest; high-water advances before ingest
  succeeds → a dropped call is silent EV loss.
- ☑ M15 (boot REFUSES a dry-run/unarmed executor on a live-mode DB with open bags; env override for recovery) dry-run boot on the live DB with real bags: simulated fills book against real positions,
  intent reconcile + on-chain safety net both OFF.
- ☑ M16 (book is a REQUIRED param on by-id cancel/modify — an omitted book 422s, never defaults to live) dashboard order IDs are per-DB autoincrements; an omitted ?book=paper on cancel/modify
  hits the same-numbered LIVE order (latent until a caller forgets).
- ☑ M17 (accepted-by-design: both repairs set their done-flag unconditionally and are content-keyed, so a restore either correctly re-applies or safely no-ops; equity invariant catches a stale-constant mismatch) incident-specific one-shot repairs (private tree only) re-fire on restore-from-backup with stale constants; the v2 repair sets its
  done-flag even on no-match.

## Older-audit residuals
- ☑ F03 WIRED 2026-07-07: Monitor.check_path compares every new closed live algo trade vs the
  shadow C1 sim twin (25% tol — gross path divergence, not fill noise), incremental high-water.
- ☑ F31 fixed: executor per-mint caches trimmed at 256 entries (oldest-first).
- ☐ F21 accepted: reanchor replaces the sig anchor within ~1m of ingest; the 48h dip window is
  anchored at signal time by design, so not resetting t0/dip_deadline shifts nothing material.
- ☐ F29 (reconciler 3-min fixed lookback) — still open, LOW.
- ☐ F56 accepted BY DESIGN: the kill-switch is reset by the OPERATOR only (never auto, never the
  agent) — that asymmetry is the point of a kill-switch.
- ☐ F13 accepted: fraction sizing reachable but hard-capped at $10 (STAKE_HARD_CAP_USD).

## 2026-07-07 (evening) — additional fixes shipped with this pass
- Alert delivery moved to a Telegram BOT (Saved Messages never notify — you can't be notified of
  your own outgoing messages). TELEGRAM_ALERT_BOT_TOKEN + optional TELEGRAM_ALERT_CHAT_ID
  (auto-discovered via getUpdates and cached). Alert bodies hard-truncated (a 7KB RPC program-log
  dump paged the operator raw); ALGO_ORDER_FAILED writer trims to 300 chars; bot errors are
  status-only (token never leaks); bot-mode pusher runs supervised from boot, listener-independent.

## RE-AUDIT (2026-07-07 evening) — 5 parallel adversarial auditors over every line post-fixes

All ☑ fixed same-day unless noted. The headline: the fixes above had shipped 4 new edges of
their own (each caught + fixed here), plus one boot-fatal regression the 349-test suite could
not see (tests never await run()).

### Blockers/High (all ☑ fixed)
- ☑ **R1 run.py missing top-level `import os`** — every deploy would have crash-looped
  (NameError in run()'s prelude). Fixed + a run()-prelude smoke test so the class can't recur.
- ☑ **R2 CRIT: the $0-sell door was still open for VISIBLE empty ATAs** — token accounts are
  never closed by a full sell, so a lagged node shows account-with-0 (bypassing the
  n_accounts==0 guard) and min(fresh, cache) then DISCARDED the healthy cache. Now: when the
  low side would zero a leg while any source says a real sell remains → re-read; two agreeing
  fresh reads overrule a disagreeing cache; else RAISE. Cache purged on FINALIZE (variant b).
- ☑ **R3 B2 recovery stash lifecycle** — the sig was popped before the current leg was safe;
  now a per-mint {sig: attempts} stash, committed ONLY at fill-return points; unresolved sigs
  survive any raise and are bounded at 5 attempts.
- ☑ **R4 sendTransaction transport failure orphaned a possibly-delivered tx** — the signature
  is derived locally from the signed tx and returned as an unconfirmed SwapResult on
  HTTP/transport errors, so the executor stashes it like any confirm timeout.
- ☑ **R5 zombie force-close** — a CLOSE whose fill was the $0 idempotent skip left state
  ACTIVE with rem=0 forever; force_close now closes on the summed real event proceeds.
- ☑ **R6 crash-gap fold resurrected the −30% stop** — the M6 fold now reconstructs
  secured/n_tp/next_rung (stop removed) for TP/RIDE_SELL gaps, not just remaining_frac.
- ☑ **R7 garbage OHLC bars fired real triggers** — _feed_candle now applies the frontend's
  _sane() contract server-side + a 20x/0.02x one-minute outlier quarantine.
- ☑ **R8 pricefeed on_dead unguarded** — one raising finalize crash-looped the whole feed;
  now guarded on both sides + DEAD_FINALIZE_FAILED alert; _absent_since reset pre-callback.

### Medium (all ☑ fixed)
- ☑ buy post-confirm made raise-proof (B1 parity); unresolvable prior buy now BLOCKS a new
  buy (fail closed); boot ENTER-ambiguity alerts like the sell side
- ☑ finalize_manual gates-down now DEFERS (no instant phantom writeoff); writeoff tagged
  dead_writeoff (orphan-backstop parity)
- ☑ M7 completed: a reaped never-entered row is REUSED on a new call (no IntegrityError)
- ☑ orphan scan bounded (buy-event + 14-day + LIMIT 200, WATCHING-with-submitted included)
- ☑ instant-fire stop guard server-side (create + modify, force=true escape) + paper fills
  clamped to the bar (no fill-at-off-market-trigger free money)
- ☑ book REQUIRED on every mutating by-mint route (takeover/release/watchlist/signal/order)
- ☑ release cancels via CAS from 'open' only; _drop_expired never expires in-flight orders
- ☑ catch-up wedge alert (CATCHUP_WEDGED after 5 fails at one watermark)
- ☑ FEED_OUTAGE transition-gated; alert high-water pinned in __init__ (first-boot CRITs heard)
- ☑ chat-id discovery accepts PRIVATE chats only; auth adds a 1s failed-attempt delay;
  modify clamps to STAKE_HARD_CAP; snapshot TOCTOU 404s instead of serving live-as-paper
- ☑ "sell amount too small" terminal; _last_price pruned on untrack; _held_tokens_ex under
  the executor lock; _book_fee uses _sol_usd_safe; caps refetch on book switch; manual event
  tooltips; LIVE/PAPER badge inside the token terminal

### Accepted / notes (not fixed, deliberate)
- equity step-window stretches when passes skip (bags in flight); wide backstop covers it.
- bankroll_history grows ~1M rows/yr/book — prune/downsample when it matters.
- Settings.load() env side-effect inside GET /api/control — refuted for prod (Railway env
  wins; .env absent in the container); local-dev quirk documented in conftest.
- incident-specific one-shot repairs (private tree only) (M17) stay as-is: content-keyed one-shots; a restore either correctly
  re-applies or safely no-ops.
