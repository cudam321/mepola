# Dashboard + Live-Engine Audit — 2026-07-04

Multi-agent audit (12 reviewers → triage → adversarial verify → synthesis). 74 raw → 56 unique → **56 confirmed** (45 confirmed, 11 partial, 0 false-positive). 2 critical / 11 high / 14 medium / 29 low; **19 real-money blockers**.

> Criticals + key highs independently re-verified against source by the lead session (executor.py, jupiter_swap.py) — accurate.

## Executive summary

Not ready for real money; keep paper. All live-path findings are gating.

## Real-money finalization checklist (ordered gates)

1. Rewrite live exec to confirm-then-commit on real on-chain balances; idempotent entry (F01,F54)
2. Size sells from real balance; fail-closed decimals; derive PnL/bankroll/breaker from real fills (F02,F06,F03)
3. Guarantee exits: kill-switch blocks only buys; generous exit slippage; dynamic priority fee; off-loop confirm; tick outlier guard (F04,F08,F07,F26,F28)
4. Harden runtime: guard tick+supervise tasks, reconnect listener+catch-up, PRAGMA busy_timeout, time-based cross-confirmed death, real feed-outage alarm (F05,F30,F24,F33,F15)
5. Enforce arming gates + burner-pubkey allowlist + swap-failure breaker; fund only the sanctioned burner; first trade is a dust reconcile (F11,F09)
6. Restore dashboard honesty (closed-only win-rate, stake-aware caveats) and add external CRIT/WARN alerting (F42,F12)

## Missed / not-covered areas

- No live-wallet/devnet end-to-end test exists; the whole swap path (quote->sign->send->confirm->reconcile) is unverified
- MEV/sandwich into thin pools, partial-fill/route-split, blockhash-expiry/replay under maxRetries, and burner SOL/rent/ATA gas funding are unhandled
- No periodic on-chain-vs-SQLite balance reconciliation; /data volume backup/DR and WAL checkpointing unexamined
- Dashboard control-endpoint auth/rate-limiting/CSRF, Jupiter/datapi API contract drift, and soak load at 25 champions + 18 shadow challengers untested
- The stage38/39 sim oracle and the $10 cap vs no-ANSEM ruin band were taken as given, not re-measured

## All confirmed findings


### CRITICAL

#### `F01-phantom-fills-unconfirmed-swaps` **[REAL-MONEY BLOCKER]**
- **file**: src/memebot/live/executor.py, src/memebot/live/jupiter_swap.py
- **loc**: LiveExecutor.buy (126-141) & sell_event (143-156); JupiterSwap.execute_swap/_confirm (jupiter_swap.py 68-97)
- **problem**: buy() and sell_event() build the Fill from the QUOTE's outAmount and return it unconditionally, never inspecting res.confirmed. _confirm() returns False after ~30s if the tx never lands, and execute_swap hardcodes in_amount=0/out_amount=0 (actual on-chain filled amounts are never read back).
- **impact**: A dropped/reverted buy is booked as an owned position; a failed stop/TP sell is booked as closed while tokens still ride to zero. SQLite state, positions, and bankroll silently diverge from the real wallet — the single most dangerous class of real-money bug.
- **fix**: Two-part, confirm-then-commit: (1) In JupiterSwap.execute_swap, after `_confirm` returns True, fetch the landed tx (`getTransaction` jsonParsed) and compute the real balance deltas from pre/postTokenBalances for the burner's token account + lamport delta, populating SwapResult.in_amount/out_amount with ACTUAL filled amounts. (2) In LiveExecutor.buy/sell_event, gate on `res.confirmed`: if not confirmed, raise (or return a PENDING/UNRECONCILED Fill) and do NOT let the position advance; build the Fill from the parsed on-chain deltas, not `q["outAmount"]`/`sol_out`. Because engine._apply_event advances the TailRider BEFORE calling the executor, the ordering must also change so the state transition is committed only after a confirmed fill (e.g. execute+confirm first, then apply the TailRider event and persist), otherwise a raised exception leaves `tr` mutated in-memory but the DB row stale. Reconcile any unconfirmed/timed-out tx against on-chain balances before resuming.

#### `F54-nonidempotent-entry-doublebuy` **[REAL-MONEY BLOCKER]**
- **file**: src/memebot/live/engine.py
- **loc**: LiveEngine.on_candle (86-101) + _apply_event ENTER (138-145); rehydrate (37-49); run.py restart-tracking (184-193)
- **problem**: on_candle mutates the in-memory TailRider to ENTERED, then _apply_event calls executor.buy() (real swap), then _persist_position writes the DB. If the process dies after the swap lands but before the UPDATE commits, the row stays WATCHING; on reboot _rehydrate restores it WATCHING with entry=None and the next dip candle re-fires ENTER — a second real buy. There is no on-chain balance reconciliation and no per-signal idempotency key.
- **impact**: Real duplicate purchases of an illiquid microcap (the worst place to average up), with deployed capital exceeding the intended $3 stake / deployed cap. Railway restarts are routine, so this is not a corner case.
- **fix**: Make ENTER idempotent against on-chain truth before any live send. Minimal, architecture-fitting: (a) before `executor.buy()`, persist a durable intent+commit — e.g. set `positions.state='ENTERING'` (or write an ENTER_SUBMITTED position_event) so a crash-mid-send is detectable; (b) in `LiveExecutor.buy`, first query the burner's SPL token balance for `mint` and if it is already above dust, adopt that balance as the fill instead of sending a second swap; (c) in `_rehydrate`/startup, for any position in ENTERING (or WATCHING whose intent event exists), reconcile the on-chain balance before feeding candles — if nonzero, transition to ENTERED from the observed fill rather than re-buying. This gives per-position idempotency keyed on the chain, which is the real source of truth.


### HIGH

#### `F02-sell-sized-from-model` **[REAL-MONEY BLOCKER]**
- **file**: src/memebot/live/executor.py
- **loc**: LiveExecutor.sell_event (143-156); entry stores tokens_qty at buy (135-141)/engine.py:144
- **problem**: Each live sell recomputes token quantity from the MODELED entry price and recorded stake (event.frac * (stake_usd/entry_price)) instead of the actual tokens acquired at buy (real slippage/impact/decimals). The engine persisted fill.tokens at entry but the sell ignores it.
- **impact**: If the real fill was worse, ladder sells (each a fraction of the modeled remainder) can cumulatively exceed 100% of holdings; the swap fails for insufficient balance (and per F01 that failure is booked as a completed sell). If better, dust is stranded. Exit sizes never reconcile to the on-chain balance.
- **fix**: Size sells from the real held balance, not the model. Track a running real remaining-token quantity: seed it from the confirmed-buy out_tokens (already stored as tokens_qty), decrement by each confirmed sell's actual out. In sell_event, compute the sell quantity as event.frac * real_remaining_tokens (frac being of original notional -> multiply the stored real total; or of remainder as the strategy already scales), then clamp amount_raw to min(computed, on-chain token-account balance) queried via getTokenAccountBalance/getTokenAccountsByOwner before swap.quote. On the FINALIZE leg, sell the entire remaining on-chain balance rather than a modeled fraction so no dust is stranded. Pass tokens_qty (and the running sold total) into sell_event so exit sizes reconcile to the wallet.

#### `F03-modeled-pnl-not-actual` **[REAL-MONEY BLOCKER]**
- **file**: src/memebot/live/engine.py, dashboard/data.py
- **loc**: LiveEngine._finalize (169-190), _persist_position (154-167), _live_closed_pnl (195-203); Monitor.path_deviation (monitor.py 117-126, never called)
- **problem**: On close, realized P&L and record_realized_pnl (which drives the daily-loss kill-switch) are derived from tr.realized_multiple (the sim model). The executor's real USD out (usd = sol_out * _sol_usd()) is stored only in position_events.proceeds_usd and never reconciled into closed_trades.pnl_usd, bankroll, or equity. Monitor.path_deviation, the intended model-vs-actual check, has zero callers. The STREAM panel shows actual proceeds while AccountPanel/PositionsTable/TradeHistory show modeled, so once armed they visibly disagree with each other and the wallet.
- **impact**: Live equity/P&L is the backtest model regardless of real slippage/MEV/partial-fills/unsellable tokens. The daily-loss circuit breaker trips (or fails to trip) on fictional numbers; reported gains can be fiction; dashboard money panels diverge from actual fills.
- **fix**: derive live pnl from executor fills; wire path deviation

#### `F04-killswitch-blocks-exits` **[REAL-MONEY BLOCKER]**
- **file**: src/memebot/live/executor.py
- **loc**: LiveExecutor._require_armed (95-101) called from both buy (128) and sell_event (145)
- **problem**: _require_armed raises PermissionError when kill_switch=='on', and sell_event calls it too. Tripping the kill-switch (manually or via the daily-loss cap in risk.record_realized_pnl) therefore prevents stop-loss and take-profit SELLS on already-open live positions.
- **impact**: The control meant to reduce risk strands open positions with no automated exit — during a drawdown (exactly when the cap trips) you cannot cut losers; they ride uncontrolled to zero.
- **fix**: Split the gate so risk-reducing SELLS are never blocked by the kill-switch. Keep the not-armed and mode!=live checks in both paths (those govern whether live execution is active at all), but remove the kill-switch check from _require_armed and apply it ONLY in buy() (new risk). Concretely: `def _require_armed(self, *, block_on_kill=True)`; call from buy with block_on_kill=True and from sell_event with block_on_kill=False; only raise on kill-switch when block_on_kill. Additionally: (a) wrap engine.on_candle in _on_tick (and the sell in engine._apply_event) in try/except so one failed event cannot crash the whole PriceFeed/orchestrator via asyncio.gather; (b) consider making trip_kill proactively flatten open live positions (or at least keep managing their exits) rather than freeze them. Add a test: with kill_switch='on' and a live-armed executor, a STOP_OUT event must still place the sell.

#### `F05-unguarded-tick-crash` **[REAL-MONEY BLOCKER]**
- **file**: src/memebot/live/run.py, src/memebot/live/pricefeed.py
- **loc**: Orchestrator._on_tick (196-215); PriceFeed._poll_once emits on_tick outside try/except (pricefeed.py 118-131); Orchestrator.run gather (440-449)
- **problem**: engine.on_candle is called unguarded (only shadow.on_candle is crash-safe), _poll_once calls on_tick outside its except block, and run() gathers the four coroutines with default return_exceptions=False. Any live-executor exception (quote/RPC/insufficient-funds/could-not-price-SOL/PermissionError) or a transient sqlite OperationalError propagates out of feed.run -> gather -> process exit.
- **impact**: A single RPC/quote/DB hiccup on any position crash-loops the entire bot (feed, listener, sampler, reconciler all die), losing coverage on every open position; recovers only via Railway restart.
- **fix**: Wrap engine.on_candle in _on_tick (and the on_tick emit in _poll_once) in a try/except that logs, alerts, and continues, matching shadow. Supervise the four tasks (restart-on-failure, or gather with return_exceptions=True plus a watchdog) so one failure never ends the process. Make the executor gates (disarmed, kill-switch, could-not-price-SOL, quote failure) return a skip sentinel the engine defers instead of raising into the tick path. Set PRAGMA busy_timeout on the writer connection.

#### `F06-token-decimals-default-6` **[REAL-MONEY BLOCKER]** _(partial)_
- **file**: src/memebot/live/executor.py
- **loc**: LiveExecutor._decimals (114-119); used in buy (135) and sell_event (148)
- **problem**: _decimals reads decimals from Jupiter price_full and on any exception/missing field silently returns 6. This scales both the sell's raw amount and the buy's reported quantity.
- **impact**: For any token whose real decimals != 6 (or whenever the field is omitted/errors), the sell requests 10^(6-d)x the intended raw units (e.g. a 9-decimal token sells ~0.1% of intended, leaving the bag unsold) and the buy mis-reports qty/effective price by 10^(d-6)x — silently, no alert.
- **fix**: For the sell, read the on-chain token account via RPC getTokenAccountBalance (returns raw amount and decimals) and sell the real held raw amount for the intended fraction. Anywhere decimals is needed, fetch from the immutable mint and FAIL CLOSED if unknown. Never default to 6 for a real swap. Cache per-mint decimals.

#### `F07-priority-fee-static` **[REAL-MONEY BLOCKER]**
- **file**: src/memebot/live/jupiter_swap.py, src/memebot/live/executor.py
- **loc**: LiveExecutor._ensure_clients (103-112) constructs JupiterSwap without priority_fee; JupiterSwap.__init__/build_swap default 200_000 (jupiter_swap.py 40-65)
- **problem**: _ensure_clients constructs JupiterSwap without passing priority_fee_lamports, so every swap uses the static 200_000-lamport default sent as a fixed prioritizationFeeLamports. There is no dynamic fee estimation and no bump-and-resend; RPC maxRetries:3 does not raise the fee.
- **impact**: During congestion a fixed ~0.0002 SOL fee often fails to land within the 30s confirm window — buys mis-book (per F01) and, critically, stop-loss/TP sells never confirm, so the -30% risk control does not execute and the position bleeds past its floor.
- **fix**: Wire the priority fee through config and make it dynamic: pass Jupiter's `"prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {"priorityLevel":"high","maxLamports": <cap>}}` (or `"auto"`) instead of a static int, sourced from config (align/replace the existing priority_fee_sol so sim and live agree). On any sell (esp. STOP_OUT/TP) that fails to confirm, escalate the fee and re-quote/re-send with bounded retries before alerting — and stop ignoring SwapResult.confirmed in sell_event/engine so an unlanded protective sell is not booked as executed.

#### `F08-slippage-hardcoded-both-legs` **[REAL-MONEY BLOCKER]**
- **file**: src/memebot/live/executor.py, src/memebot/live/jupiter_swap.py
- **loc**: LiveExecutor._ensure_clients (103-112, slippage read at 107); JupiterSwap uses one slippage_bps for all quotes
- **problem**: slippage = int(getattr(self.cfg, 'entry_slip', 0.015)*10000), but self.cfg is a TailRiderConfig with NO entry_slip field, so it always falls back to 0.015 = 150 bps, applied to BOTH buy and sell quotes. The [cost_model] entry_slippage_bps/exit_slippage_bps=300 are never consulted.
- **impact**: Selling a collapsing microcap with a 150 bps cap frequently fails to route, so the -30% stop and TP sells cannot execute — unbounded downside on the leg the whole risk model depends on; the operator has no working knob to widen exit slippage.
- **fix**: Delete the dead `getattr(self.cfg, "entry_slip", 0.015)` in executor.py:107 and source slippage from Settings.cost. Give quote() a per-call slippageBps override (or construct/parameterize JupiterSwap with distinct buy/sell caps) so buys use entry_slippage_bps and sells use a generous exit_slippage_bps (exits into collapsing microcaps need materially more tolerance than 300 bps — make it configurable and default it wide, e.g. 500-1000+ bps). Wire the exit-slippage value into the operator knob so it is actually controllable, and surface a warning/failure path when a stop sell reverts on slippage so the position isn't silently left unhedged.

#### `F11-arming-gates-not-enforced` **[REAL-MONEY BLOCKER]**
- **file**: src/memebot/live/executor.py
- **loc**: LiveExecutor._require_armed (95-101) & docstring (68-82); run.py arming (144-151)
- **problem**: _require_armed enforces only self.armed + mode=='live' + kill-switch off. The advertised paper==backtest equivalence gate and the dust reconciliation are 'operational' comments with no machine check, and there is no circuit breaker that trips the kill-switch after N consecutive swap failures.
- **impact**: Nothing in code prevents setting MEMEBOT_LIVE_ARMED=1 + MEMEBOT_LIVE_SEND=1 and sending real swaps before equivalence/dust reconcile pass, and failing swaps keep firing with no automatic halt.
- **fix**: Persist checked flags in system state for the equivalence check and dust reconcile, and require both in the armed check before real sends. Add a consecutive swap failure counter that trips the kill switch after N failures, and wrap the executor calls in the engine so a swap exception increments it.

#### `F24-transient-dropout-finalizes` **[REAL-MONEY BLOCKER]**
- **file**: src/memebot/live/pricefeed.py
- **loc**: PriceFeed._poll_once (109-131) + default dead_after=40 (75); run.py _on_dead (217-222)
- **problem**: Dead detection is poll-count based and cannot distinguish a genuinely dead token from one Jupiter /price/v3 transiently omits. Any mint priced at least once then absent for 40 consecutive polls (~44s, or ~10s if a JUPITER_API_KEY drops the interval to 0.25s) fires on_dead -> finalize_token, permanently closing the position with no re-entry. The datapi reconciler does not reset this counter.
- **impact**: For a strategy whose entire EV is one rare winner ridden to a huge multiple, a ~44s feed gap during a SECURED/RIDING position forfeits all remaining upside — the exact failure that destroys the tail bet's expectancy.
- **fix**: Make dead-detection time-based and cross-confirmed rather than spot-poll-count: require minutes of continuous absence, confirm death against the datapi true-candle stream before finalizing an entered position, and reset feed._missing on any fresh reconciler candle. Keep the short threshold only for never-entered WATCHING mints already covered by the 48h expiry.

#### `F26-swap-blocks-event-loop` **[REAL-MONEY BLOCKER]**
- **file**: src/memebot/live/jupiter_swap.py, src/memebot/live/executor.py, src/memebot/live/run.py
- **loc**: JupiterSwap._confirm (89-97, time.sleep(1.0) x30) reached via LiveExecutor.buy/sell_event called synchronously from engine._apply_event (138-152) inside Orchestrator._on_tick (196-203) on the loop
- **problem**: Price polling is offloaded via asyncio.to_thread, but the tick CALLBACK is not: in live mode _on_tick -> on_candle -> _apply_event calls executor.buy/sell_event, which does synchronous httpx quote/swap/RPC plus _confirm() sleeping time.sleep(1.0) up to 30 times, all on the event-loop thread.
- **impact**: While one live trade confirms (~30s), the bot cannot fire the -30% stop on any other open position, ingest new calls, or mark-to-market — in a fast dump this directly causes missed stops and losses beyond the modeled -30%.
- **fix**: Never block the event loop with the swap+confirm. Minimal: at the _apply_event live boundary, run the blocking executor call in a worker thread (the state-machine advance is sync and loop-driven, so route the actual swap via asyncio.to_thread or a dedicated single-consumer execution queue/thread that performs quote+build+send+confirm and reconciles the Fill back). Additionally, replace time.sleep(1.0) in JupiterSwap._confirm so confirmation polling never sleeps on the loop thread (either keep it strictly inside the worker thread, or make it async with asyncio.sleep). A dedicated serialized execution worker also prevents overlapping sends racing the single-writer SQLite path.

#### `F30-listener-no-reconnect` **[REAL-MONEY BLOCKER]**
- **file**: src/memebot/live/listener.py
- **loc**: run_listener (59-79); gathered in run.py Orchestrator.run (440-449)
- **problem**: run_listener awaits client.run_until_disconnected() with no retry/supervision. If it raises (exhausted reconnect), gather (return_exceptions=False) tears down asyncio.run and kills the whole process. If it returns cleanly, the listener task simply completes while the other three loops keep running — no new signals are ever ingested, and the dashboard still looks alive.
- **impact**: Either the bot crash-restarts on a transient Telegram disconnect, or it goes silently deaf to new calls for hours/days with the feed heartbeat still ticking — for a signal bot, missing entries is directly EV-negative and hard to notice.
- **fix**: Wrap run_listener in a supervised reconnect loop with capped exponential backoff that catches ALL exceptions and never lets them reach the top-level gather. On each (re)connect, fetch messages since the last processed message id (persist a high-water id in system_state) to catch up on calls missed during downtime — critical since restart alone does not recover them. Write a dedicated last_listener_ok_ts on connect and on each message, and add a Monitor check that alerts if it goes stale. Decouple the feed heartbeat from the unconditional _sampler write so a genuine feed/listener outage actually trips FEED_OUTAGE (only write last_feed_ok_ts on a real tick in PriceFeed._poll_once, not every _sampler pass). At the orchestrator level, treat any child coroutine of the top-level gather returning as a fatal condition (re-raise / trigger restart) rather than silently continuing.


### MEDIUM

#### `F09-no-burner-allowlist` **[REAL-MONEY BLOCKER]**
- **file**: src/memebot/live/jupiter_swap.py
- **loc**: load_burner_keypair (100-110)
- **problem**: load_burner_keypair returns a Keypair from whatever WALLET_PRIVATE_KEY contains with no assertion that its pubkey equals the sanctioned burner (<YOUR_BURNER_PUBKEY>). A grep for the burner pubkey in src/ returns nothing.
- **impact**: An env misconfiguration or reuse of the CLAUDE.md-documented compromised key would be signed with and funded — a direct path to using the wrong/compromised wallet. 'BURNER ONLY' is a comment, not enforced.
- **fix**: Add a module-level constant EXPECTED_BURNER_PUBKEY set to the sanctioned burner address in jupiter_swap.py. In load_burner_keypair, after building the Keypair, assert str of kp.pubkey equals that constant and raise RuntimeError otherwise, never printing the key. Also enforce this as a precondition to arming in _ensure_clients or _require_armed so LiveExecutor refuses to arm with an unsanctioned wallet.

#### `F15-research-shared-sqlite-connection` **[REAL-MONEY BLOCKER]** _(partial)_
- **file**: src/memebot/live/run.py, src/memebot/live/research.py
- **loc**: Orchestrator._run_research (~428-438) -> run_remeasurement (research.py 235-467)
- **problem**: run.py passes its own LiveState (a single sqlite3.Connection, check_same_thread=False, no lock) into asyncio.to_thread(run_remeasurement, self.state, ...). The worker thread does state.conn.execute()+commit() repeatedly while the event-loop thread keeps writing on the SAME connection every tick. commit() is connection-global, so a research-thread commit flushes the engine thread's pending writes (losing atomicity) and concurrent execute can raise OperationalError/ProgrammingError into the unguarded tick path.
- **impact**: A research pass prices up to ~400 tokens over minutes with continuous overlapping write windows against the real-money DB; a timing collision can raise into the engine loop (crash) or commit a partially-built transaction.
- **fix**: Stop sharing the live connection cross-thread. Open a fresh LiveState(db_path) inside run_remeasurement and close it in finally; since WAL still permits only one writer, set PRAGMA busy_timeout on it so the research write yields instead of erroring. Or add a threading.Lock to LiveState held around every conn.execute/commit. Also wrap engine.on_candle in _on_tick in try/except so a DB hiccup cannot crash the feed loop.

#### `F20-entry-anchor-race` **[REAL-MONEY BLOCKER]**
- **file**: src/memebot/live/run.py, src/memebot/live/engine.py
- **loc**: Orchestrator._on_tick vs _maybe_reanchor (316-350); engine.reanchor guard (121-135)
- **problem**: The sim's sig = first 1m candle OPEN at/after the call; the live rider is seeded with the ingest-time SPOT price. _maybe_reanchor fixes this to the true first-1m open but runs only on the ~60s reconciler and refuses once the dip has triggered, while the feed evaluates the -50% dip against the spot sig every ~1.1s from ingest.
- **impact**: On fast-dipping launches (the volatile tokens this strategy targets) a >50% dip in the first ~60s enters at a level off by up to the measured ~24% spot-vs-open gap, diverging the entry price, realized multiple, and even the enter/no-enter decision from the sim.
- **fix**: Close the window before any tick can fire the dip: when a mint is first tracked, synchronously fetch the first 1m candle at/after signal_at via `self.charts.fetch_candles(mint, "1_MINUTE", sig_at, sig_at+5min)` and seed `tr.sig` with `first.open` before `feed.track`; if datapi hasn't indexed it yet, gate dip evaluation — skip WATCHING dip checks in `on_candle`/`_on_tick` for mints not in `self._anchored` and run the anchor pass on a tight retry (not the 60s reconciler cadence) — so an entry is never priced off the transient spot anchor. Add a test that ingests a call, feeds a >50% dip tick before any reconcile pass, and asserts the entry matches the first-1m-open anchor (pin to stage38 sim).

#### `F28-no-tick-outlier-guard` **[REAL-MONEY BLOCKER]**
- **file**: src/memebot/live/pricefeed.py
- **loc**: PriceFeed._poll_once tick emit (118-131) -> _on_tick 1-tick candle (run.py 196-202) -> TailRider._process_bar (strategy.py 194-218)
- **problem**: The only sanity check is `px is None or px <= 0`. Each tick drives the state machine immediately. Jupiter /price/v3 occasionally returns a stale/cross-pool price; a single spurious high tick reaching tp1 (~1.5x sig) fires TP1, which sets secured=True and removes the -30% stop permanently (config #1 never re-arms). A single spurious low tick can trigger the pre-secure stop-out.
- **impact**: A one-off bad print can disarm the sole loss-control (position then rides down unprotected) or force a phantom stop-out — realizing real losses on a data glitch, with no cross-check against the true-candle layer.
- **fix**: Ratio-guard poll_once against the tracked last_price; quarantine far-off ticks until a 2nd poll confirms.

#### `F32-drift-monitor-self-referential` **[REAL-MONEY BLOCKER]**
- **file**: src/memebot/live/monitor.py
- **loc**: Monitor.from_closed_trades (78-82) + run_once/assess (85-141); wired at run.py:153
- **problem**: build_expectation() and run_once() both read the SAME state.closed_trades() (seeded backtest replay rows AND live rows, unfiltered). A sample mean is by construction near the center of a bootstrap CI of a superset of itself, so 'off_expectation' can essentially never fire; the handful of live trades is diluted on both sides. Secondary: on an unseeded DB with <5 trades, build_expectation returns a degenerate all-zeros band, so once >=5 trades close every run_once fires a false WARN DRIFT (mean>0 outside [0,0]) every 30s with no dedup.
- **impact**: The component meant to warn that the live system is genuinely broken (not just bleeding as designed) is a no-op that compares data to itself; real degradation (feed corrupting fills, sim divergence, broken exit ladder) would not raise DRIFT. On an unseeded deploy it instead spams false alerts.
- **fix**: Build the expectation once from a fixed reference that is not the live population (seed-only rows partitioned by the seeded_at boundary from seed_live_db.py:197, or the sim oracle) and freeze it. In run_once/assess filter closed_trades to live-provenance rows only so real live degradation moves the assessed stats against the frozen band. Guard the degenerate zero-width band so it never fires on a positive mean. Dedup DRIFT alerts by recording only on a status transition. Optionally wire the existing path_deviation into the per-close path.

#### `F33-poll-swallow-no-alert` **[REAL-MONEY BLOCKER]**
- **file**: src/memebot/live/pricefeed.py
- **loc**: PriceFeed._poll_once (109-131)
- **problem**: The batched fetch is wrapped in `except Exception: return`. On any failure the poll returns without incrementing _missing, emitting a tick, or recording an alert; there is no consecutive-failure counter.
- **impact**: During a Jupiter outage no stops/TP rungs fire (tick-driven), tokens are never even declared dead (dead_after only advances on a successful poll returning None), and nothing is logged/alerted; combined with F25 the outage is fully masked and a -30% stop can sit unfilled indefinitely.
- **fix**: Two coupled fixes. (1) In `_poll_once`, stop silently returning: track `self._consecutive_fail`, increment on the except branch (reset to 0 on success), and log WARN past a small threshold and CRIT past a larger one; expose the last-successful-poll timestamp for the dashboard. (2) Fix the neutered watchdog: set `last_feed_ok_ts` from ACTUAL tick/candle arrival (e.g. in `_on_tick`/`_feed_candle`), not from the unconditional sampler `heartbeat`. Remove or repurpose `Monitor.heartbeat` so it no longer resets the timestamp on every pass; then `check_feed`'s `feed_max_gap_s` gate will actually fire and record a FEED_OUTAGE alert. This makes a sustained outage visible and blocks the -30% stop from sitting blind with no operator signal.

#### `F12-alerts-no-external-delivery`
- **file**: src/memebot/live/state.py
- **loc**: LiveState.record_alert (390-396); consumers throughout monitor/engine/executor
- **problem**: Every alert (including CRIT kill-switch trips and would-be path-deviation warnings) is only written to the alerts table. There is no external notification channel.
- **impact**: For unattended 24/7 real-money operation, a tripped kill-switch, feed outage, or divergence never reaches the operator unless they happen to be watching the dashboard.
- **fix**: Add a best-effort, non-blocking egress for severity in {CRIT, WARN}. Cheapest option reuses the already-authenticated Telethon session from listener.py to DM the operator (e.g. Saved Messages / a private chat id from config); alternatively read an optional ALERT_WEBHOOK_URL from config and POST the alert JSON. Wrap the send in try/except so a notification failure never propagates into the single-writer SQLite path (write the DB row first, notify after). Rate-limit/dedupe (shadow.py already models a once-only _alerted flag) so a flapping feed does not spam. Separately consider having CRIT KILL_SWITCH/PATH_DEVIATION also self-trip the kill switch.

#### `F16-dashboard-second-writer` _(partial)_
- **file**: deploy/railway/start.sh, dashboard/server/app.py, src/memebot/live/state.py
- **loc**: start.sh (two processes); app.py post_control (319-325) & _custom_challenger_control (243-285); LiveState.__init__ (209-235)
- **problem**: start.sh runs the engine and uvicorn dashboard as two processes on the same /data/live_state.db, yet state.py's contract says it is the ONLY writer. Control endpoints open LiveState(DB_PATH) writable, so LiveState.__init__ re-runs executescript(DDL) + ALTER + a whole-table `DELETE FROM shadow_trades WHERE id NOT IN (SELECT MIN(id)...)` dedup + CREATE UNIQUE INDEX on EVERY POST, contending with the engine. No PRAGMA busy_timeout is set (only journal_mode/foreign_keys).
- **impact**: The money-safety kill_switch is written only via this contended, retry-less path; if a write exceeds the default 5s busy window under engine load it returns HTTP 500 and the operator's halt silently does not apply. The per-POST DDL replay + full-table dedup also amplifies lock contention and can interleave with the engine's shadow writes.
- **fix**: Make the control write a lightweight single upsert on a bare sqlite3 connection with an explicit busy_timeout, instead of reconstructing LiveState which replays DDL and dedups shadow_trades every POST. Also set busy_timeout on all LiveState connections and add a bounded retry plus UI error surfacing for the operator kill-switch write.

#### `F22-shadow-horizon-doublecount`
- **file**: src/memebot/live/shadow.py
- **loc**: ShadowEngine.finalize/_retire/_flush_legs (748-784); _record_leg_value (504-523); run.py _on_dead (217-222)
- **problem**: Dedup relies on UNIQUE(config_id,mint,entered_at,closed_at) producing the SAME closed_at on replay. Stop/sellout legs use a deterministic candle timestamp, but horizon legs are closed by finalize() with a wall-clock utcnow() from _on_dead. _retire does record_shadow_trade(commit) then delete_shadow_rider(commit) without persisting the done-snapshot, so a crash between commits leaves the leg AND a stale active rider; on reboot it re-finalizes with a NEW timestamp T2, so INSERT OR IGNORE does not dedup.
- **impact**: A crash/SIGKILL (routine on Railway redeploys) in the flush->delete window double-records a horizon-riding token (the winners, incl. the ANSEM-class tail) with two different multiples, materially biasing a challenger's measured EV/bankroll and the lab P&L that drives promotions.
- **fix**: Make the horizon leg's closed_at replay-stable OR persist the done-snapshot before the flush+delete. Cleanest: in finalize, after `r.finalize(ts)`, call `upsert_shadow_rider` with the done snapshot (horizon leg buffered in r.legs carrying closed_at=T1, flushed left BELOW len) BEFORE `_flush_legs`+`delete`. Then a crash in the window leaves a snapshot whose buffered leg already carries T1; on reboot the `len(r.legs) > r.flushed` safety net (shadow.py:597) flushes that SAME leg with the stored T1, so INSERT OR IGNORE dedups. Equivalent alternative: thread the last-processed candle timestamp into shadow.finalize so closed_at is deterministic across restarts instead of a fresh utcnow().

#### `F23-shadow-refresh-unguarded-crash`
- **file**: src/memebot/live/run.py
- **loc**: Orchestrator._maybe_refresh_customs (255-276), called from _sampler (237-253) in gather (444-449)
- **problem**: Shadow's crash-safe guarantee is only in ShadowEngine methods; the custom-challenger control plane in run.py calls self.state.delete_shadow_config(cid) (a DB write that can raise OperationalError) with no try/except. _sampler has no try/except around its body and gather uses default return_exceptions=False.
- **impact**: A DB error while pruning a deleted custom challenger propagates out of _sampler -> gather -> asyncio.run and exits the engine; start.sh then restarts the container, and the condition can recur into a restart loop — a shadow-lab maintenance failure takes down the live champion trading engine.
- **fix**: Wrap the _maybe_refresh_customs body and the _sampler loop body in try/except that logs and continues, mirroring _reconciler and _backfill. Also set PRAGMA busy_timeout on the State connection so concurrent engine/dashboard writes retry instead of raising OperationalError.

#### `F25-feed-outage-detection-dead`
- **file**: src/memebot/live/monitor.py
- **loc**: Monitor.heartbeat (113-114) & check_feed (102-111); run.py _sampler (248-250)
- **problem**: check_feed alarms only if now - last_feed_ok_ts > feed_max_gap_s, but last_feed_ok_ts is written ONLY by heartbeat(), which the sampler calls unconditionally every ~30s immediately before run_once() calls check_feed. _on_tick never touches it and PriceFeed never writes it, so the gap is always ~0 and FEED_OUTAGE can never fire.
- **impact**: During a Jupiter outage positions freeze, the tick-driven -30% pre-secure stop cannot fire, yet no FEED_OUTAGE alert is raised and the dashboard shows 'feed ok' — the one self-awareness signal for the documented feed-outage failure mode is unreachable exactly when needed.
- **fix**: Stamp feed freshness from the actual feed, not the loop. In PriceFeed._poll_once, after a successful price poll that yields at least one live tick, record the success time (via a callback, or by setting state last_feed_ok_ts); equivalently stamp it from Orchestrator._on_tick. Remove the unconditional heartbeat call from run.py _sampler (keep run_once, which still calls check_feed). Then check_feed measures true seconds-since-last-tick and FEED_OUTAGE fires after feed_max_gap_s of no ticks. Update tests/test_monitor.py to exercise the sampler wiring (advance time with no tick, assert the alert fires) instead of hand-injecting a stale timestamp. Optionally fold a persistently empty datapi reconciler into the staleness signal so a dual-source outage is caught.

#### `F27-rejected-call-blacklisted`
- **file**: src/memebot/live/engine.py
- **loc**: LiveEngine.ingest_call (56-72)
- **problem**: ingest_call marks the mint seen (outcome='rejected') BEFORE `if not decision.ok: return False`, and the top guard rejects any is_seen() mint. So a token declined by a LIVE cap (max_concurrent, deployed_cap, daily_loss, kill) is blacklisted forever; a later fresh BUY first-call is recorded as a dup and never traded. The backtest oracle has no capacity model and would trade it.
- **impact**: Live-only but material: the 'no re-entry' rule should forbid re-buying a token you TRADED, not one you merely declined for lack of a slot. Given the EV lives in one rare winner, silently forfeiting a re-called token has outsized expectancy cost.
- **fix**: Only write seen_mints when actually positioned. Move the mark_seen(outcome="positioned") call into the decision.ok branch (below the `if not decision.ok: return False` guard, alongside create_position). The rejected attempt is already captured by record_signal (engine.py 63-67, accepted=False, reject_reason=decision.reason) for the audit trail, so nothing observability-related is lost. This leaves transient-rejected mints eligible for a future re-call. Accept and document the residual live-vs-oracle divergence: a re-call enters at a new anchor rather than the original first-call anchor. Optionally, if you want to also skip re-trading tokens called during a maintenance kill, gate on reject reason — but leaving them eligible is strictly safer than the current permanent blacklist.

#### `F29-reconciler-fixed-lookback`
- **file**: src/memebot/live/run.py
- **loc**: Orchestrator._reconcile_mint (352-363) + _feed_candle high-water rule (279-290)
- **problem**: The true-candle reconciler only fetches now-3min..now and feeds candles strictly newer than the per-mint high-water mark. There is no coverage for a mid-run gap >3 min while the process stays up (e.g. datapi timing out: 20s x5 retries per mint, sequential), and the advancing high-water mark makes the unfetched window permanently unrecoverable.
- **impact**: During partial datapi degradation a stop/TP wick in the gap (which 1.1s spot polls cannot see) is lost, so the live realized multiple diverges from the sim — the machine keeps riding a position the backtest would have stopped out.
- **fix**: Anchor the fetch start to the mint's high-water mark instead of a fixed 3 min: start = max(self._candle_hw.get(mint, now - timedelta(minutes=3)), now - MAX_LOOKBACK), capping MAX_LOOKBACK (e.g. 30-60 min) so a long-stranded mint does not replay full history, keeping the existing clamp. Add a staleness guard: when a tracked mint's HW is older than a few reconcile periods (e.g. > 3-5 min), log WARN / raise a monitor alert so datapi degradation is visible rather than silent. Optionally run the per-mint reconciles concurrently (asyncio.gather with a small semaphore) so one slow/timing-out mint cannot starve the reconcile cadence of the others.

#### `F42-tiles-count-open-positions`
- **file**: dashboard/frontend/src/components/StatTiles.jsx, dashboard/data.py
- **loc**: StatTiles win-rate tile (~80-83); hero_points() (115-125) & _scope_stats() (204-236)
- **problem**: The distribution tiles are computed over `points`, which hero_points builds from closed trades PLUS every open ENTERED/SECURED/RIDING position at its live current_multiple. _scope_stats counts wins = sum(1 for m in mults if m>1) over that combined set, so an open bag transiently marked >1x is counted as a WIN now, and the sub-label calls the denominator '${s.n} closed trades' where s.n = closed + open.
- **impact**: Violates the 'never oversell' guardrail: while any position is open and green, headline win rate and per-trade mean read higher than realized truth and the count overstates resolved trades — for a strategy whose honest win rate is <25%, exactly the flattering-but-false number the dashboard must avoid.
- **fix**: In _scope_stats (and the legacy top-level block in stats()), compute win_rate, mean, mean_ex_tail, bleed_rate, total_loss_rate, and the tile denominator n over CLOSED (kind=="realized") points only — e.g. `closed_pts = [p for p in points if p.get("kind")=="realized"]` and derive mults/n/wins from that — while keeping the full mixed set for the hero power-law chart and CCDF (which intentionally display live bags). Alternatively keep the mixed set but split n into n_closed vs n_open and relabel the StatTiles sub from "N closed trades" to something accurate (e.g. "N resolved + M open marks"). Cleanest: closed-only for the outcome tiles, mixed for the visual power-law.


### LOW

#### `F10-stake-cap-contradicts-doc` _(partial)_
- **file**: src/memebot/live/risk.py, dashboard/server/app.py
- **loc**: STAKE_HARD_CAP_USD (risk.py:28); ctl_stake_usd bound (app.py:180)
- **problem**: STAKE_HARD_CAP_USD=10.0 and the dashboard clamps ctl_stake_usd to (0.5,10.0), justified by a comment that $10-fixed 'survived (->$1815)'. This contradicts CLAUDE.md verbatim: 'At $10-fixed/trade it goes to $0. You cannot lever it.'
- **impact**: An operator trusting the dashboard can raise the stake to $10 (3.3x default) — up to the exact per-trade size one authoritative doc says drives the bankroll to zero for an explicitly size-fragile strategy.
- **fix**: Reconcile the docs, not the code: the risk.py comment already matches the stage39 ANSEM-in column, so no re-run is needed. Edit CLAUDE.md line 56 to state that the $10-fixed goes-to-zero figure is the no-ANSEM expectation (realized-history value is 1815) so the docs stop reading as a contradiction. For conservative margin, optionally lower STAKE_HARD_CAP_USD to the no-ANSEM survival band (about 5, where no-ANSEM keeps 178 instead of 0) and update the app.py line 180 bounds plus the two comments, so the dashboard ceiling does not imply more safety than the honest band grants. Keep the verbatim tail-bet caveat next to the stake slider.

#### `F13-fraction-stake-leverage` _(partial)_
- **file**: src/memebot/live/risk.py
- **loc**: RiskGovernor.size_for (91-95); called with _realized_equity() at engine.py:141
- **problem**: When stake_mode=='fraction' (config-selectable), stake = fraction of realized equity passed from _realized_equity(). This is the equity-scaling the strategy says must never happen; it grows the stake as the account grows.
- **impact**: If fraction mode is selected, the strategy levers into any winner — the precise behavior the size-fragile tail-bet must avoid. Bounded to $10 and off by default, so limited, but the lever is reachable via config.
- **fix**: No fix required to trade real money: fraction mode is off by default (fixed 3 dollars), bounded to the 10 dollar hard cap, and is a research-endorsed sizing option. If extra caution is wanted, hard-disable fraction mode in live so the sizing regime cannot silently switch via config.

#### `F14-rpc-url-secret-leak`
- **file**: src/memebot/live/jupiter_swap.py
- **loc**: JupiterSwap._rpc (80-87)
- **problem**: _rpc calls r.raise_for_status(); httpx's error message embeds the full request URL, and RPC provider URLs commonly carry the API key as a query param. These exceptions surface via log.exception and as uncaught tracebacks.
- **impact**: A non-2xx RPC response (401/429/5xx) can write the SOLANA_RPC_URL (with embedded key) into logs — contrary to the 'never log secrets' guardrail.
- **fix**: In _rpc, catch httpx.HTTPStatusError and re-raise a scrubbed RuntimeError containing only method and status code (no URL); do the same in quote and build_swap. Prefer passing the RPC provider key via an Authorization or x-api-key header rather than a query string, and add a log filter that redacts the RPC host and query. Optionally wrap the engine trade call so a swap error cannot crash the orchestrator, removing the uncaught-traceback leak too.

#### `F17-no-schema-migration`
- **file**: src/memebot/live/state.py
- **loc**: LiveState.__init__ migration block (213-235); _init_system (238-239)
- **problem**: Schema evolution relies on CREATE TABLE IF NOT EXISTS (cannot add columns to existing tables) plus exactly one hardcoded ALTER (positions ADD COLUMN low_price). SCHEMA_VERSION=1 is written once and never read back to drive any migration.
- **impact**: Currently low, but a durability trap: the next column added to an existing table will pass tests on fresh DBs yet silently not apply to the production volume DB, causing read errors or wrong defaults on the real-money instance.
- **fix**: Read schema_version on boot or diff PRAGMA table_info and apply idempotent ALTERs per new column; add a pre-migration DB regression test.

#### `F18-shadow-idempotency-index-besteffort`
- **file**: src/memebot/live/state.py
- **loc**: LiveState.__init__ (221-234); record_shadow_trade (426-435)
- **problem**: The crash-idempotence of shadow-leg replay depends on ux_shadow_trades_leg, but its creation (with the preceding whole-table dedup DELETE) is wrapped in `except sqlite3.OperationalError: pass`. If the DELETE raises a lock error (more likely now the dashboard also runs this block on every POST), the index is skipped and INSERT OR IGNORE has no constraint.
- **impact**: Transient/self-healing, but between boots a config's shadow trade count and lab P&L can be inflated by replayed legs, corrupting promotion evidence.
- **fix**: Add PRAGMA busy_timeout=5000 (or higher) to the DDL so concurrent dashboard/engine writes wait instead of instantly raising OperationalError; this is the highest-leverage fix and also hardens the broader single-writer-vs-dashboard write contention. Additionally split index creation into its OWN try/except independent of the DELETE (run the DELETE, swallow its error, then always attempt CREATE UNIQUE INDEX IF NOT EXISTS separately), and optionally assert the index exists (PRAGMA index_list) before trusting INSERT OR IGNORE.

#### `F19-paper-not-fp-exact-live` _(partial)_
- **file**: src/memebot/live/run.py
- **loc**: Orchestrator._on_tick (196-215) & _feed_candle (279-290); claim in executor.py (10-13/51-52)
- **problem**: In production the champion is not fed the cached OHLC series the equivalence test uses: _on_tick wraps each spot poll as a 1-tick candle (o=h=l=c) and calls engine.on_candle immediately, while true intrabar OHLC arrives later/out-of-order from the ~60s reconciler. A position can SECURE on a real up-tick and thereby skip a pre-secure -30% stop that the backtest (low-before-high in the same bar) would have taken.
- **impact**: Realized paper multiples systematically drift from the oracle (asymmetrically optimistic), so the live 'paper≈backtest' arming gate cannot assume exact equivalence; docs present fp-exactness as unconditional.
- **fix**: (1) Correct the wording: state that fp-exact equivalence holds for offline in-order candle replay, not for the live spot-tick+reconciler path, where intraminute ordering can differ (uniformly optimistic vs the pessimistic stop-first sim) — fix the 'same as the backtest' comment in run.py:197-200 and 'paper == backtest' in executor.py:11,52. (2) Wire the already-defined Monitor.path_deviation into the closed-trade path: on each champion close, re-run the oracle sim on the reconciled true 1m candle series for that mint and record a CRIT alert on divergence (the method is unit-tested but never invoked in run_once/_sampler).

#### `F21-dip-window-anchor`
- **file**: src/memebot/live/engine.py
- **loc**: LiveEngine.ingest_call (73-78); WATCHING expiry
- **problem**: The sim measures the 48h window from T[0] = first candle at/after posted_at. Live sets t0 = now.timestamp() at ingest and reanchor resets sig but never t0, so parse/listener/network latency opens the window at a slightly different anchor than the backtest.
- **impact**: Small (seconds-to-a-minute) shift of the 48h window; a borderline dip near the edge could be included/excluded differently. Minor, but it compounds the anchor divergence.
- **fix**: In run.py::_maybe_reanchor, when calling engine.reanchor with the first 1m candle, also reset t0 to that candle's timestamp so sig and t0 come from the SAME candle, matching sim's cds[0]. Concretely: extend LiveEngine.reanchor to accept the first candle's epoch (e.g. reanchor(mint, first.open, t0=first.ts.timestamp())), set tr.t0 = t0, and persist via state.update_position(mint, t0_epoch=...) plus recompute dip_deadline = first.ts + dip_window_h. This makes the anchor internally consistent. The deeper residual (live signal_at/anchor use ingest `now` rather than the message posted_at) is a separate, larger fidelity gap and out of scope for this low-severity fix.

#### `F31-unbounded-memory-dicts`
- **file**: src/memebot/live/pricefeed.py, src/memebot/live/run.py
- **loc**: PriceFeed.untrack (90-92); Orchestrator _anchored/_candle_hw (179-180)
- **problem**: untrack() removes a mint from _active and _missing but never from _last_price, so every mint ever priced stays in memory. The orchestrator's _anchored set and _candle_hw dict accumulate one entry per mint ever tracked and are never pruned on close.
- **impact**: For a bot ingesting many calls over months these leak a few thousand small entries — negligible memory but genuinely unbounded.
- **fix**: Add `self._last_price.pop(mint, None)` to PriceFeed.untrack. In the Orchestrator, whenever a mint is finalized/untracked (_on_dead, and the untrack branches in _on_tick/_reconcile_mint/_sampler), also do `self._candle_hw.pop(mint, None)` and `self._anchored.discard(mint)`. All are one-line dict/set pops with no behavioral effect (a mint under the no-re-entry rule never returns), so no correctness risk.

#### `F34-snapshot-no-read-transaction`
- **file**: dashboard/data.py
- **loc**: snapshot() (760-782); account() (145-199); repeated closed_trades()/positions reads; engine _finalize two-commit close (engine.py 181-189)
- **problem**: A /api/snapshot payload is built from many separate SELECTs in autocommit, so different sections can reflect different DB states. The engine closes a position in two commits (flip positions.state, THEN insert closed_trades). A snapshot whose closed_trades read lands before the second commit while its open-positions read lands after the first counts the closing position in neither, dropping its P&L from balance_usd until the next snapshot.
- **impact**: The headline balance and P&L visibly flicker to a wrong value at the exact moment a trade closes (the moment the operator is watching); a large-multiple winner closing produces a large transient swing, and chart sections disagree with each other.
- **fix**: Preferred: make the close atomic — wrap update_position + record_close in a single transaction (one commit) in engine._finalize, so a position is never simultaneously flipped-out-of-open and absent-from-closed. This also closes a latent crash-atomicity gap (a kill between the two current commits leaves a STOPPED position with realized_pnl_usd set but no closed_trades row, which _live_closed_pnl would miss). Optionally also wrap the whole dashboard snapshot() read in one explicit deferred read transaction (conn.execute("BEGIN"); build payload; conn.commit()) so WAL gives every SELECT one consistent snapshot and cross-section reads agree.

#### `F35-candles-endpoint-500`
- **file**: dashboard/server/app.py
- **loc**: get_candles() (432-496, fetch at 470); jupiter.py _get re-raises after retries
- **problem**: get_candles calls _charts().fetch_candles(...) with no try/except; when datapi is down/rate-limited/4xx, _get re-raises httpx.HTTPError which propagates as an unhandled 500 — inconsistent with the sibling get_live which returns 200 with null fields.
- **impact**: On the first open of a token-terminal view during any Jupiter blip the chart is empty and the server logs a 500; cosmetic + noisy, not a money bug.
- **fix**: Wrap the fetch_candles call (line 470) in try/except httpx.HTTPError (or broad Exception) mirroring get_live: on error set candles = [] and still return 200 with the levels block computed from the stored position (signal/entry/stop/rungs), so the terminal shows the level overlay even when datapi is momentarily unavailable. Optionally short-cache the empty result briefly to avoid hammering datapi during an outage.

#### `F36-stream-blocks-threadpool` _(partial)_
- **file**: dashboard/server/app.py
- **loc**: get_stream() (96-119); _supply_fetch() (376-405)
- **problem**: get_stream is a sync route that performs up to _SUPPLY_BUDGET_PER_REQ (4) blocking DexScreener fetches per request, each with a 5s timeout; on a cold cache it can block its worker ~20s. The endpoint is polled, so distinct cold mints tie up multiple workers.
- **impact**: Latency spikes and threadpool starvation under cold-cache/concurrent load, which can also slow the WebSocket snapshot pushes sharing the threadpool. Bounded and self-healing.
- **fix**: move FDV supply enrichment off the request path into a background warmer thread

#### `F37-custom-challenger-unvalidated`
- **file**: dashboard/server/app.py
- **loc**: _custom_challenger_control() add branch (258-283)
- **problem**: On add_challenger the code validates via challenger_from_dict (reads only id/label/dip/sl/ftp/fsell/reentry/entry_mode) but then stores the raw client dict (existing.append(value)) into system_state, so extra/oversized keys survive validation and are re-parsed on every load/snapshot/rev-poll. Count is capped at 8 but per-item size is not.
- **impact**: An authenticated add can bloat the blob with junk repeatedly parsed by dashboard and engine; low blast radius (auth, 8-item cap) but stores unvalidated data as trusted state.
- **fix**: Persist a clean projection derived from the validated Challenger instead of the raw client dict. Replace existing.append(value) with a whitelisted dict built from cc, appending only id, label, dip, sl, ftp, fsell, reentry, entry_mode read off the validated Challenger. This drops all unknown keys and guarantees only bounded, validated fields hit system_state.

#### `F38-champion-config-noop`
- **file**: dashboard/server/app.py
- **loc**: post_control() champion_config_id branch (301-308); _CHAMPION_IDS (194)
- **problem**: champion_config_id is validated against C1..C10 and written to system_state as a 'human-approved champion promotion', but grep shows it is only referenced by state.py (default seed) and research.py (docstring); engine/strategy/executor/run.py never read it, so the live engine stays config #1.
- **impact**: An operator who 'promotes' a different config gets ok:true but the engine is unchanged — misleading during real-money finalization. Latent today (no frontend invokes it).
- **fix**: Reject champion_config_id with 422 as inert until engine support lands, or wire it into engine config selection; at minimum stop data.py showing a champion the engine is not trading.

#### `F39-ws-no-staleness-watchdog`
- **file**: dashboard/frontend/src/api.js
- **loc**: connectWS (9-36); App.jsx useEffect (82-90); server heartbeat app.py ws() (156-165)
- **problem**: Reconnect and the liveness indicator are driven only by the WebSocket onclose event; there is no timer watching for absence of the every-2s heartbeat/snapshot. On a silent drop (laptop sleep, idle-timeout without a close frame, mobile partition) onclose never fires, so no reconnect is scheduled and the status stays 'live'.
- **impact**: The operator's window into engine health shows stale positions/P&L while the header pulses 'feed: live' — a dead feed/engine can be mistaken for healthy. Self-heals only when the OS eventually surfaces a close.
- **fix**: In connectWS, record `lastFrameTs = Date.now()` at the top of ws.onmessage (every frame, snapshot or heartbeat). Start a setInterval (e.g. every 2s) that, if `Date.now() - lastFrameTs > 6000-8000ms`, calls onStatus?.("down") and ws.close() (which routes into onclose -> scheduled reconnect); clear that interval in onclose and in the returned disconnect cleanup. Wrap the JSON.parse on line 19 in try/catch so a malformed frame is dropped, not thrown. Optionally replace the flat 2000ms retry with exponential backoff + jitter. Low-effort complement: in App.jsx, derive/override the feed dot to "down" when `Date.now() - updated` exceeds a threshold so the indicator can't stay green over stale data.

#### `F40-ideal-alpha-scope-mismatch`
- **file**: dashboard/frontend/src/components/PowerLawHero.jsx
- **loc**: effect body (alpha at 376, ideal at 414); idealMarkPoint label (102)
- **problem**: alpha = Number(snapshot.stats?.hill_alpha) || 1.4 reads the top-level (all-scope) stat; the per-scope stats.live/stats.seed objects (from data.py _scope_stats) have no hill_alpha. So when scope is live/seed the ideal curve is anchored at the scoped top multiple but drawn/labeled 'IDEAL POWER LAW alpha=X' with the all-data exponent.
- **impact**: The reference curve and its alpha annotation mislabel the scoped view. Decorative only, no money effect.
- **fix**: Add a scoped hill_alpha in _scope_stats and read it in the frontend, or hide the alpha label when scope is not all.

#### `F41-initial-fetch-duplicates-ws`
- **file**: dashboard/frontend/src/App.jsx
- **loc**: useEffect (82-90); server sends initial snapshot in app.py ws() (151-153)
- **problem**: On mount App both HTTP-fetches a snapshot AND opens the WS (which itself pushes a full snapshot on accept). Both call the same onSnap with no ordering guard, so if a newer WS snapshot arrives before the slower HTTP fetch resolves, the late fetch clobbers it with an older payload.
- **impact**: One redundant round-trip per load plus a brief (~one 2s tick) regression to stale numbers on initial paint. Self-heals on the next WS push.
- **fix**: Prefer a monotonic guard over deleting the fetch, since dropping fetchSnapshot removes the only initial paint when the WS upgrade is proxy-blocked. Add a generation or watermark field to snapshot meta and in onSnap ignore any snapshot not newer than the last applied one. Alternatively set a flag once the first WS snapshot lands and discard late HTTP fetch results.

#### `F43-deployed-drypowder-ignore-tp1`
- **file**: dashboard/data.py
- **loc**: account() (164-166, 189-190)
- **problem**: deployed = sum(stake_usd over open positions) and dry_powder = start + live_realized - deployed. But config #1 sells 33% at 3x to recover stake while the position stays open; that recovered cash is captured only in `unrealized`, never added back to dry powder nor removed from deployed. A fully-secured position that already returned its $3 still shows $3 deployed and subtracts $3 from dry powder.
- **impact**: The deployed/cap utilization reads higher and dry powder lower than reality after positions secure; headline balance is still correct, so it's an allocation-clarity issue that can drive wrong sizing decisions.
- **fix**: In account(), add each open position's realized TP proceeds back into dry_powder and compute deployed as remaining cost basis (stake * remaining_frac) instead of the full original stake, so deployed + dry_powder + closed cash reconcile to balance_usd. Display-only; leave fixed sizing and balance_usd untouched.

#### `F44-best-mult-caption-mismatch`
- **file**: dashboard/data.py
- **loc**: _scope_stats() days_since_last_10x (209-216, from closed_rows) vs best (230, from points)
- **problem**: days_since_last_10x is derived only from closed_rows, but best (srt[0]) is derived from points including open positions' live current_multiple. A position marked >=10x sets best=12x while days_since_last_10x=None, so the tile renders 'best 12x · no >=10x yet'.
- **impact**: Cosmetic self-contradiction in the tail tile that undermines trust; not a money error.
- **fix**: Gate the no-tail-yet caption on whether the max points multiple is under TAIL_X rather than on closed-only data, or label best as open/riding when it comes from an open position, so value and sub-caption agree.

#### `F45-positions-pnl-format`
- **file**: dashboard/frontend/src/components/PositionsTable.jsx
- **loc**: positions row P&L cell (205-215)
- **problem**: The cell prints (p.realized_pnl_usd || 0).toFixed(2) with no '$' and ASCII '-', unlike other money cells (which use '$' and U+2212), and colors on >=0 ? text-win : text-loss, so a flat position (or null coalescing to 0) renders as green '0.00'.
- **impact**: Formatting inconsistent with the rest of the money UI; a flat/unknown P&L shows green, subtly overstating open-position health. Low impact (value is correct).
- **fix**: Reuse the shared signed-money formatter and treat exactly 0 and null as neutral text-muted instead of green.

#### `F46-lab-no-integrity-caveats` _(partial)_
- **file**: dashboard/frontend/src/components/StrategyLab.jsx
- **loc**: table + InfoHint (158, 197-289; mean coloring 252-258)
- **problem**: The forward-race leaderboard renders mean/win%/P&L with no sample-size context and colors any config mean>=1 green. A config with n_trades=1 shows drop_top1_mean==mean and win_rate=100%, so a single lucky tail (the non-repeatable ANSEM tail-bet) is shown green as a winning strategy. There is no caveat that C7/C10 are controls chosen to document failure, nor that no config clearing the gate is the healthy outcome.
- **impact**: Undercuts the project's core honesty guardrail. A user preparing to trade real money can read a tiny-sample run as a validated edge and promote a deliberately-losing control (allowlist C1-C10 includes C10).
- **fix**: In StrategyLab.jsx mean cell, gate the green class on a minimum sample size (e.g. only `text-win` when `c.mean >= 1 && c.n_trades >= 10`, else render neutral/muted) so a lucky n=1 tail is not shown as a winning strategy; optionally render drop-top1 as "—" when n_trades < 2 since it equals the mean there. Optionally surface the backend's control tag (shadow.py already flags C7/C10) as a small "control" badge next to those labels. The tail-bet caveat itself does not need re-adding to the lab — it already lives in the controls modal by explicit project design.

#### `F47-lab-total-hardcoded-3usd` _(partial)_
- **file**: dashboard/data.py, dashboard/frontend/src/components/StrategyLab.jsx
- **loc**: lab() (~620-661, computes at 3.0); StrategyLab total column tooltip (205-206); ConfigDetail p&l (LabModals ~215)
- **problem**: lab() builds realized = 3.0*sum(m-1) and open_pnl from 3.0*(mult-1.0), but the StrategyLab total header tooltip asserts 'reconciles with the account balance for the champion'. The real account/engine size at the user-editable ctl_stake_usd (clamped to $10), which the Controls modal lets the user raise. The moment stake != $3 the champion's lab total no longer equals the account P&L.
- **impact**: When the operator changes stake for go-live (the stated goal), the champion 'total' visibly diverges from the headline balance — re-triggering the exact 'lab in loss vs balance in profit' confusion this feature was built to eliminate.
- **fix**: Reword the StrategyLab.jsx line 206 total tooltip: drop the unconditional reconciles-with-account-balance phrase and add an at-3usd phantom-stake qualifier (equals the account balance only while stake is 3). Leave the ConfigDetail column as-is since it already reads p-and-l at 3usd. Alternative: in data.py lab() scale the P-and-L by the effective ctl_stake_usd instead of the literal 3.0 and rename the fields off the at_3usd suffix.

#### `F48-clone-diamond-hand-rewrites`
- **file**: dashboard/frontend/src/components/LabModals.jsx
- **loc**: StrategyBuilder prefill (312); clone-guard only covers exit_policy configs (227-243)
- **problem**: The clone guard only disables exit_policy configs. C10 (ftp=1e9, no exit_policy) is clonable, but the prefill collapses any ftp>=1e8 to 3.0, so 'clone diamond hand' produces a strategy that secures 33% at 3x and removes the stop — the opposite of a never-secure config — with no warning.
- **impact**: Same silent-strategy-substitution the exit_policy clone guard was added to prevent; the user believes they are racing a copy of C10 but is racing a completely different exit shape.
- **fix**: Extend the ConfigDetail clone guard (LabModals.jsx line 227) to also disable/warn for never-secure plain configs, e.g. disable when p.exit_policy is set OR p.ftp is at or above 1e8, with a tooltip that these cannot be expressed in the builder knobs. The builder secure-at-x input has min 1.1 and cannot represent ftp of 1e9, so blocking is the honest fix. Alternative: show an in-builder notice when the prefill secure multiple was reset.

#### `F49-add-form-missing-validation`
- **file**: dashboard/frontend/src/components/LabModals.jsx
- **loc**: StrategyBuilder.submit() (323-345); inputs (426-437)
- **problem**: submit() only pre-validates stop % and dip %. It does not check ftp (backend 1.01..1e9), fsell (0<fsell<=1), or reentry (1.1..20); HTML min/max attributes do not block typed out-of-range values, so invalid entries pass the form and fail only on the POST with a raw server error.
- **impact**: UX drift, not data corruption (the backend validator is a correct backstop): the user gets a delayed round-trip rejection instead of inline guidance.
- **fix**: In submit(), before postControl, mirror the backend bounds in form units: reject ftp below 1.01 (and add a sane upper cap), reject fsell_pct outside the 1 to 100 range, and reject non-empty reentry outside 1.1 to 20; set the same inline err message so the user gets immediate client-side guidance instead of a POST round-trip. Optionally add max attributes to the reentry and ftp inputs for stepper hygiene, but the JS check is what actually matters.

#### `F50-caveat-hardcodes-3usd`
- **file**: dashboard/frontend/src/components/ControlsModal.jsx
- **loc**: caveat text (245-248); stake field (52-124, fragile warning at 77 Number(val)>5)
- **problem**: The guardrail caveat is phrased 'a deliberate tail-bet at $3/trade', but the same modal exposes ctl_stake_usd editable to $10. If the user sets $10, the always-visible caveat still reads '$3/trade', understating fragility precisely where risk is highest.
- **impact**: At elevated stakes the standing caveat undersells the size-fragility the project treats as a hard guardrail; the shown number no longer matches the traded number.
- **fix**: Interpolate the live stake into the caveat, e.g. `a deliberate tail-bet at ${ctl.editable.ctl_stake_usd.value}/trade`, or drop the dollar figure entirely ("a deliberate tail-bet · most positions go to zero · ...") so the string cannot go stale. The dynamic fragile warning at line 77 already covers the "risk is highest" case.

#### `F51-snapshot-bankroll-unused`
- **file**: dashboard/data.py
- **loc**: bankroll_series() (320-336) included in snapshot() (769)
- **problem**: snapshot() emits both bankroll (bankroll_series) and equity (equity_series); the frontend EquityCurve reads snapshot.equity only, and a full-tree grep finds zero references to snapshot.bankroll. bankroll_series does a full scan + per-row dict build on every snapshot (~2s cadence) sent in every REST/WS payload.
- **impact**: Wasted CPU and bytes on the hot path, growing with bankroll_history size; no correctness impact.
- **fix**: Drop the bankroll key from snapshot and delete the bankroll_series helper.

#### `F52-hero-peak-multiple-is-price`
- **file**: dashboard/data.py
- **loc**: hero_points() active-positions loop (~123)
- **problem**: For closed trades hero_points stores peak_multiple = c["peak_multiple"] (a true multiple); for open positions it stores peak_multiple = p["peak_price"] (the raw price). Harmless today only because _pareto strips peak_multiple before it reaches the frontend.
- **impact**: Latent: any future consumer reading a hero point's peak_multiple gets a raw price (often ~3e-7) for live positions and a multiple for closed ones — a silent order-of-magnitude/semantic mismatch.
- **fix**: In hero_points open branch, set peak_multiple to round(peak_price / entry_price, 3) when both present else None, matching the closed branch and open_positions; or drop the key since no consumer reads it.

#### `F53-hero-best-multiple-field-drift`
- **file**: dashboard/frontend/src/components/PowerLawHero.jsx
- **loc**: effect signature build (~372-390)
- **problem**: stats = (snapshot.stats||{})[scope]||... selects the _scope_stats object whose key is `best`, not `best_multiple`; the change-signature reads stats.best_multiple ?? null, which is always undefined->null on the scoped object.
- **impact**: The intended 'redraw when best changes' trigger via this field never fires; harmless only because topMult (also in the signature) captures the same info. Real field-name drift.
- **fix**: In PowerLawHero.jsx line 386 read stats.best (matches the scoped object's actual key) instead of stats.best_multiple, or drop the redundant term since the hero point array and topMult already cover any change to the best multiple.

#### `F55-sell-zero-proceeds` _(partial)_
- **file**: src/memebot/live/executor.py
- **loc**: LiveExecutor.sell_event (150-156) vs buy (130-132)
- **problem**: buy() guards against a missing SOL price (raises 'could not price SOL'), but sell_event() multiplies sol_out by _sol_usd() with no guard; a transient price-feed hiccup returning 0 yields usd=0 for a real sale.
- **impact**: A real exit gets booked as $0 proceeds, corrupting realized P&L/bankroll and potentially the daily-loss accounting for that trade.
- **fix**: Mirror the buy guard in sell_event: capture sol_usd = self._sol_usd() and raise when it is 0 or less before usd = sol_out * sol_usd. Keeps proceeds_usd honest and restores buy/sell symmetry.

#### `F56-killswitch-sticky-no-reset` _(partial)_
- **file**: src/memebot/live/risk.py
- **loc**: RiskGovernor.record_realized_pnl (149-161), reset_kill (107-108, no caller), _daily_loss (144-147)
- **problem**: record_realized_pnl trips the kill-switch when accumulated daily loss >= daily_loss_cap_usd (default $50), in both paper and live. reset_kill is never called by the engine, and while _daily_loss resets the daily counter at the UTC rollover, the kill_switch value stays 'on'. Once tripped it halts all entries until a human manually flips it off.
- **impact**: The strategy is explicitly a bleed-heavy tail-bet; a single heavy call-day crossing $50 realized losses auto-halts trading and STAYS halted across the next day and beyond, silently starving the dataset / stopping trading until someone notices.
- **fix**: Optional and design-dependent. If auto-recovery is desired, at the daily rollover clear only a kill caused by the daily-loss cap while leaving manual and critical trips latched, by tagging the trip cause in system state and auto-clearing that cause in the rollover branch. Wire reset_kill into that path or remove it as dead code. Surface the existing CRIT alert as a dashboard banner and add a runbook note. Reconsider the fifty-dollar cap against same-day cluster closes at three-dollar stakes.
