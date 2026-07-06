# System Design — the autonomous power-law tail-rider + dashboard

> **STATUS 2026-07-04: BUILT & DEPLOYED (paper), running 24/7 on Railway.** This doc is the original
> design; it held up. What shipped beyond it: the **shadow lab** (18 challengers + custom `X*` — see
> `docs/ADAPTIVE.md`), a **STREAM** execution log, a **strategy-lab detail/add-strategy** UI, the
> **Phosphor CRT** design (`DESIGN.md`), and `[?]` hover tooltips for all explanations. Live URL +
> deploy/verify procedure are in `CLAUDE.md`'s STATUS section. Frontend stack chosen: **React + Vite +
> Tailwind + ECharts** (FastAPI + WebSocket backend). Iterate-on-the-live-system phase now.

The build. Goal (user's words): **"a fully autonomous, fully efficient, fully self-aware system and a
beautiful dashboard."** Implements config **#1** (see `RESEARCH.md §1`).
**Paper-first**, safety-gated before any real capital.

Design principle: the live engine must **mirror the backtest** (`stage37/38 sim` + `exit_sim.py`) so
that paper results ≈ backtest results. If they diverge, the engine is wrong — that equivalence is the
first correctness test.

---

## 1. Architecture (components)

Proposed package: `src/memebot/live/` + `dashboard/`. Each component is small and single-purpose.

| Component | File | Responsibility |
|---|---|---|
| **Signal listener** | `live/listener.py` | Telethon client on @your_channel (real-time `events.NewMessage`), reuse session `TELEGRAM_SESSION_STRING`. Parse via `parser.signal_parser.parse_message` → `Signal`; keep only first-call-per-mint (persist seen mints); emit a `Call` event. |
| **Price watcher** | `live/pricefeed.py` | Per active token, poll live price (`JupiterClient.price([mint])`, ~1/s keyless) + recent candles (`JupiterChartsClient`). Feeds the dip-trigger and the ride/stop monitor. Backoff + dedup shared across tokens. |
| **Strategy engine** | `live/strategy.py` | The #1 state machine (WATCHING→ENTERED→SECURED→RIDING→EXITED / STOPPED / EXPIRED). Pure function of price events; emits intents (BUY/SELL fraction). Port the exact thresholds from `stage37 sim`. |
| **Executor** | `live/executor.py` | `PaperExecutor` (default): simulate fills with the honest slip model (90s max-high +1.5% on entry; −1.5% TP / −5% stop costs) and record intended trades. `LiveExecutor` (later, gated): Jupiter Swap API on a **burner** wallet (`WALLET_PRIVATE_KEY`), slippage + priority-fee config, `solders`/`solana` extra. |
| **Risk governor** | `live/risk.py` | Enforces tail-bet sizing: a small fixed stake (or a fractional mode; size to your own risk tolerance — the research says stay small), max concurrent positions, total-deployed cap, daily-loss cap, global kill-switch. This is the integrity guardrail *in code*. |
| **State store** | `live/state.py` (SQLite) | Positions, closed trades, per-token lifecycle events, bankroll history, seen mints. SQLite = simple, durable, easy for the dashboard to read. |
| **Self-awareness / monitor** | `live/monitor.py` | Continuously compare live stats to a backtest expectation band **derived from the sim at startup, not hardcoded** (real values ≈ win 10%, per-trade mean ≈ 0.81 ex-ANSEM / 1.38 with, drop3 0.79; the old "19%" was stale). Track drawdown, days-since-last-≥10x, expected-vs-actual. Alert on drift beyond bands, on feed failures, and when a position deviates from the modeled path. "Self-aware" = it knows it's bleeding-as-designed vs genuinely broken, and says so honestly. |
| **Orchestrator** | `live/run.py` | Wires listener → strategy → executor → state; async event loop; graceful restart; structured logging. |
| **Dashboard** | `dashboard/` | See §4. |

---

## 2. Build order (paper-first, each step verifiable)

1. **Port #1 to a live state machine and unit-test it against the historical sim.** Feed a token's
   real candle series through `live/strategy.py` and assert the realized multiple equals
   `stage38 sim` for the same inputs. *Verify:* matches on ANSEM (≈197x) and a sample of ~20 tokens.
2. **Signal listener** → dedup (first-call-per-mint) → SQLite. *Verify:* replays the last N days of
   @your_channel and reproduces the same first-calls as the corpus pull.
3. **Price watcher + paper executor** → run the full loop in **PAPER** mode 24/7. *Verify:* a week of
   paper trades; per-trade stats land in the backtest's ballpark (mostly small losses, occasional
   ride).
4. **Dashboard** reading SQLite (read-only first). *Verify:* shows live positions/bankroll correctly.
5. **Self-awareness / monitoring + alerts.** *Verify:* deliberately inject a feed outage and a
   drift; confirm it flags both.
6. **(Gated, optional, later) Live execution** via Jupiter Swap on a **burner** wallet, tiny stake,
   risk governor + kill-switch armed. **Only after** paper ≈ backtest for a sustained period. *Verify:*
   dust-live single trade reconciles on-chain vs the paper model.

---

## 3. Config knobs (add to `config.toml`, `[strategy.tailrider]`)

```toml
dip_trigger      = 0.50    # enter at 0.50x the signal price (-50% dip)
dip_window_h     = 48      # give up if the dip doesn't arrive within 48h
entry_slip       = 0.015   # +1.5% over the 90s reaction-window max-high
reaction_win_s   = 90
stop_level_mult  = 0.70    # hard stop at 0.70x entry = -30% from entry, PRE-SECURE ONLY
stop_cost        = 0.05
tp1_mult         = 3.00    # secure rung
tp1_sell_frac    = 0.33    # sell 33% at 3x, then remove the stop
ride_sell_frac   = 0.25    # sell 25% of remainder at each subsequent rung
ride_step_x2     = 5       # rungs 6/12/24/48 use x2 steps; after 5 TPs switch to x3
tp_cost          = 0.015
reentry          = false
# sizing (tail bet):
stake_mode       = "fixed" # "fixed" | "fraction"
stake_usd        = 3.0     # fixed $ per token  (fraction mode: ~0.25-0.5%)
max_concurrent   = 25
total_deployed_cap_usd = 200.0
daily_loss_cap_usd     = 50.0
```

---

## 4. Dashboard (the "beautiful" part)

**Recommended stack:** FastAPI backend (serves SQLite state + a WebSocket for live push) + a modern
frontend (React/Next.js + Tailwind + a chart lib such as `lightweight-charts`/Recharts). If speed of
delivery matters more than polish for v1, **Streamlit** gets a good-looking dashboard in pure Python
fast — decide with the user (see §5).

**Panels:**
- **Equity curve** — bankroll over time vs the backtest expectation band (so a normal bleed reads as
  "on track", not "broken").
- **Live signal feed** — incoming calls (ticker, mint, time), parsed and deduped.
- **Positions table** — lifecycle state (WATCHING / ENTERED / SECURED / RIDING / EXITED), entry, current
  multiple, next rung target, realized + unrealized PnL.
- **Lifetime stats** — total trades, win%, per-trade mean, best multiple, **"days since last ≥10x"**,
  realized vs deployed capital, current drawdown.
- **Honest-status banner** — the power-law reality surfaced: expected bleed, dry powder remaining, and a
  plain reminder that most positions go to zero and the edge (if any) is one rare tail. (Guardrail
  from `CLAUDE.md` — this must stay visible.)
- **Per-token drill-down** — price chart with entry / stop / 3x-secure / ride-rung markers.
- **System health** — feed status, last message time, error/alert log (from `live/monitor.py`).

---

## 5. Open decisions for next session (ask the user)

1. **Dashboard stack** — Streamlit (fast, decent) vs React+FastAPI (beautiful, more work).
2. **Paper-only, or eventually live?** If live: burner wallet setup, Jupiter Swap vs another router,
   priority fees, and the exact stake.
3. **Bankroll & stake** — total bankroll, fixed $ (~$1–5) vs fractional (~0.25–0.5%). This is the
   lever that decides bust-vs-survive; default conservative.
4. **Where it runs** — local 24/7 vs a small VPS (the Telethon listener needs to stay connected).

---

## 6. What to reuse (don't rebuild)

- **Exit logic:** `analysis/exit_sim.py` + the `stage37/38 sim` reference — port, don't reinvent.
- **Entry fill / cost model:** `entry_fill()` and the tp/stop costs from `stage14_untruncated.py`.
- **Price data:** `data/jupiter.py` (`JupiterClient` for live price, `JupiterChartsClient` for candles),
  `data/cache.py`.
- **Signal parsing / ingest:** `parser/signal_parser.py`, `ingest/telegram_mcp.py`.
- **Channel pull (batch/backfill):** `scripts/pull_channel_history.py`.
- **Safety (optional pre-buy filter):** `safety/rugcheck.py`, `JupiterClient.shield()`.

## 7. Dependencies to add

`telethon>=1.36` (extra `prod-ingest`, already declared) for the live listener; `solders`/`solana`
(extra `solana`, already declared) only when building `LiveExecutor`; plus the chosen dashboard stack
(`fastapi`+`uvicorn`+`websockets` or `streamlit`) — add to `pyproject.toml` when we start.
