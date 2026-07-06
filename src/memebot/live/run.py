"""Orchestrator — the autonomous 24/7 loop wiring listener -> engine -> price feed -> monitor.

    listener (@your_channel)  -->  engine.ingest_call  -->  WATCHING position + price-feed.track
    price feed (Jupiter poll)  -->  engine.on_candle     -->  dip fill / stop / TP ladder / close
    sampler (periodic)         -->  bankroll snapshot + monitor pass + heartbeat + expire stale watchers
    reconciler (datapi 1m)     -->  TRUE candles into engine+shadow (intrabar wicks the 1s spot
                                    polls cannot see) + anchor fidelity + restart backfill

Paper by default (config.toml [strategy.tailrider].mode). Live execution is gated (Phase D). Run:

    set -a && . ./.env && set +a && PYTHONPATH=src python -m memebot.live.run
"""

from __future__ import annotations

import argparse
import asyncio
import os
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from memebot.config import Settings
from memebot.data.jupiter import JupiterChartsClient, JupiterClient
from memebot.live.engine import LiveEngine
from memebot.live.executor import PaperExecutor, make_executor
from memebot.live.monitor import Monitor
from memebot.live.orders import OrderBook
from memebot.live.pricefeed import PriceFeed
from memebot.live.risk import RiskConfig, RiskGovernor
from memebot.live.shadow import (CHALLENGERS, CUSTOM_REV_KEY, ShadowEngine,
                                 load_custom_challengers)
from memebot.live.state import LiveState, from_iso, utcnow
from memebot.live.strategy import PositionState, TailRider, TailRiderConfig
from memebot.models import Candle

log = logging.getLogger("memebot.live")

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB = ROOT / "runs" / "live_state.db"
CHANNEL = os.environ.get("MEMEBOT_CHANNEL", "@your_channel")


def load_configs(settings: Optional[Settings] = None) -> tuple[TailRiderConfig, RiskConfig, str]:
    """Build strategy + risk configs from config.toml [strategy.tailrider] (with #1 defaults)."""
    settings = settings or Settings.load()
    t = (settings.raw.get("strategy", {}) or {}).get("tailrider", {}) or {}
    cfg = TailRiderConfig(
        dip_trigger=t.get("dip_trigger", 0.50),
        dip_window_h=t.get("dip_window_h", 48.0),
        stop_level_mult=t.get("stop_level_mult", 0.70),
        tp1_mult=t.get("tp1_mult", 3.0),
        tp1_sell_frac=t.get("tp1_sell_frac", 0.33),
        ride_sell_frac=t.get("ride_sell_frac", 0.25),
        ride_step_x2=int(t.get("ride_step_x2", 5)),
        tp_cost=t.get("tp_cost", 0.015),
        stop_cost=t.get("stop_cost", 0.05),
    )
    risk = RiskConfig(
        bankroll_usd=t.get("bankroll_usd", 500.0),
        stake_mode=t.get("stake_mode", "fixed"),
        stake_usd=t.get("stake_usd", 3.0),
        stake_fraction=t.get("stake_fraction", 0.006),
        max_concurrent=int(t.get("max_concurrent", 25)),
        total_deployed_cap_usd=t.get("total_deployed_cap_usd", 200.0),
        daily_loss_cap_usd=t.get("daily_loss_cap_usd", 50.0),
    )
    mode = t.get("mode", "paper")
    return cfg, risk, mode


def repair_presignal_trades(state: LiveState) -> int:
    """One-time repair (2026-07-03): an unclamped backfill replayed PRE-SIGNAL candle
    history and manufactured trades dated before their calls existed (one 'entry' was
    seven weeks before the signal). Detect any position whose ENTER event predates its
    signal_at and reset it to a clean WATCHING state (its 48h dip window still applies);
    purge the fictional closed trade, its machine events, the poisoned shadow rows, and
    the polluted live bankroll heartbeats. Idempotent via system_state['trade_fix_v3']."""
    import sqlite3 as _sq
    if state.get_system("trade_fix_v3") is not None:
        return 0
    rows = state.query(
        "SELECT DISTINCT p.id AS pid, p.mint AS mint FROM positions p "
        "JOIN position_events e ON e.position_id = p.id "
        "WHERE e.event_type='ENTER' AND e.ts < p.signal_at")
    for r in rows:
        state.conn.execute("DELETE FROM closed_trades WHERE position_id=?", (r["pid"],))
        state.conn.execute(
            "DELETE FROM position_events WHERE position_id=? AND event_type != 'SIGNAL'",
            (r["pid"],))
        state.conn.execute(
            "UPDATE positions SET state='WATCHING', entry_at=NULL, entry_price=NULL,"
            " stake_usd=NULL, tokens_qty=NULL, stop_price=NULL, secured=0, n_tp=0,"
            " next_rung_mult=NULL, next_rung_price=NULL, remaining_frac=1.0,"
            " proceeds_units=0.0, peak_price=NULL, low_price=NULL, current_multiple=NULL,"
            " realized_multiple=NULL, realized_pnl_usd=0, unrealized_pnl_usd=0,"
            " closed_at=NULL, close_reason=NULL WHERE id=?", (r["pid"],))
        try:
            state.conn.execute("DELETE FROM shadow_riders WHERE mint=?", (r["mint"],))
            state.conn.execute("DELETE FROM shadow_trades WHERE mint=?", (r["mint"],))
        except _sq.OperationalError:
            pass
    if rows:
        state.conn.execute("DELETE FROM bankroll_history WHERE expected_equity_usd IS NULL")
    state.conn.commit()
    state.set_system("trade_fix_v3", "done")
    return len(rows)


def repair_corrupt_anchors(state: LiveState) -> int:
    """One-time (2026-07-06): a re-anchor that trusted GARBAGE datapi OHLC mis-set some watchers'
    signal_price to a value far from the channel call (SAKURA landed at ~half its call). Reset any
    WATCHING position whose signal_price diverges >40% from its first SIGNAL event price (the real
    channel call) back to that call. The reanchor sanity guard then keeps it. Idempotent."""
    if state.get_system("anchor_fix_v1") is not None:
        return 0
    n = 0
    try:
        rows = state.query(
            "SELECT p.id AS pid, p.mint AS mint, p.signal_price AS sp, "
            "(SELECT e.price FROM position_events e WHERE e.position_id=p.id "
            " AND e.event_type='SIGNAL' ORDER BY e.id LIMIT 1) AS call_price "
            "FROM positions p WHERE p.state='WATCHING'")
        for r in rows:
            sp, cp = r["sp"], r["call_price"]
            if sp and cp and sp > 0 and cp > 0 and abs(sp / cp - 1.0) > 0.40:
                state.conn.execute("UPDATE positions SET signal_price=?, low_price=NULL WHERE id=?",
                                   (cp, r["pid"]))
                n += 1
        state.conn.commit()
    except Exception:
        log.exception("anchor repair failed")
    state.set_system("anchor_fix_v1", "done")
    return n


def repair_orphan_closed_trades(state: LiveState) -> int:
    """Audit #13: a crash STRICTLY between update_position(state=EXITED...) and record_close() (two
    separate autocommits) leaves a closed position with NO closed_trades row — dropping its P&L from
    realized equity + the power-law hero (losing the ONE winner erases the whole realized gain).
    Reconstruct the missing row from the position on boot. Runs EVERY boot (the gap can recur) and is
    idempotent (only positions with realized data but no closed_trades row are touched)."""
    n = 0
    try:
        # audit re-verify #13: also cover the earlier window (crash between state=EXITED and
        # realized_multiple being written) — those rows have NULL realized_multiple, so reconstruct it
        # from the summed sell-event proceeds rather than excluding (and permanently losing) them.
        rows = state.query(
            "SELECT p.* FROM positions p LEFT JOIN closed_trades c ON c.position_id = p.id "
            "WHERE p.state IN ('EXITED','STOPPED') AND c.id IS NULL")
        for p in rows:
            entry_at = from_iso(p["entry_at"])
            exit_at = from_iso(p["closed_at"]) or utcnow()
            entry = p["entry_price"] or 0.0
            stake = p["stake_usd"] or 0.0
            held = ((exit_at - entry_at).total_seconds() / 3600.0) if entry_at else None
            rmult, pnl = p["realized_multiple"], p["realized_pnl_usd"]
            if rmult is None:
                prows = state.query(
                    "SELECT COALESCE(SUM(proceeds_usd),0) AS pp FROM position_events "
                    "WHERE position_id=? AND event_type IN "
                    "('TP','RIDE_SELL','STOP_OUT','FINALIZE','MANUAL_SELL')", (p["id"],))
                proceeds = float(prows[0]["pp"] or 0.0)
                rmult = (proceeds / stake) if stake else 0.0
                pnl = proceeds - stake
            state.record_close(
                position_id=p["id"], mint=p["mint"], ticker=p["ticker"], entry_at=entry_at,
                entry_price=entry, stake_usd=stake, exit_at=exit_at,
                close_reason=p["close_reason"] or "reconstructed_on_boot",
                realized_multiple=rmult, pnl_usd=pnl or 0.0,
                peak_multiple=((p["peak_price"] / entry) if (entry and p["peak_price"]) else None),
                held_hours=held, n_tp=p["n_tp"] or 0, was_stopped=(p["state"] == "STOPPED"),
                was_secured=bool(p["secured"]))
            n += 1
    except Exception:
        log.exception("orphan closed_trades repair failed")
    return n


class Orchestrator:
    def __init__(self, db_path=DEFAULT_DB, *, settings: Optional[Settings] = None):
        self.state = LiveState(db_path)
        self._alert_times: dict[str, float] = {}     # per-kind alert throttle — FIRST (init-time alerts)
        n_orphan = repair_orphan_closed_trades(self.state)
        if n_orphan:
            log.warning("closed_trades repair: reconstructed %d orphan closed trade(s)", n_orphan)
        n_repaired = repair_presignal_trades(self.state)
        if n_repaired:
            log.warning("trade_fix_v3: reset %d fictional pre-signal trades to WATCHING", n_repaired)
        n_anchor = repair_corrupt_anchors(self.state)
        if n_anchor:
            log.warning("anchor_fix_v1: reset %d watchers with a garbage re-anchored signal_price", n_anchor)
        # One-time repair (2026-07-03): engine bankroll rows written before the live-only
        # equity fix included the SEED replay's P&L (~+$394) — a fake jump at the seed/live
        # seam on the equity chart. Engine rows are identifiable by expected_equity_usd IS
        # NULL (only the seed writes that column); they are 30s heartbeats, safe to drop.
        if self.state.get_system("bankroll_fix_v2") is None:
            cur = self.state.conn.execute(
                "DELETE FROM bankroll_history WHERE expected_equity_usd IS NULL")
            self.state.conn.commit()
            self.state.set_system("bankroll_fix_v2", "done")
            if cur.rowcount:
                log.info("bankroll_fix_v2: purged %d polluted engine bankroll rows", cur.rowcount)
        # RESEARCH hygiene (user report 2026-07-06 "measurement running 92 min"): a re-measure runs
        # as an in-process thread, so a status='running' row found at BOOT is by definition a dead
        # run interrupted by a restart — mark it failed so the lab stops showing a forever-running
        # clock (the 11:06Z go-live run died 2 min in but displayed as running for hours).
        try:
            cur = self.state.conn.execute(
                "UPDATE research_runs SET status='failed' WHERE status='running'")
            self.state.conn.commit()
            if cur.rowcount:
                log.warning("research: marked %d interrupted run(s) failed "
                            "(an in-process run cannot survive a restart)", cur.rowcount)
        except Exception:
            pass
        # And never AUTO-fire the weekly re-measure just because a book is fresh: the go-live DB had
        # last_research_at=None, so the weekly trigger fired AT BOOT and hogged the chart-API budget.
        # Seed the clock — the first weekly run lands 7 days from now; on-demand runs are unaffected.
        if self.state.get_system("last_research_at") is None:
            self.state.set_system("last_research_at", utcnow().isoformat())
        settings = settings or Settings.load()
        self.cfg, self.risk_cfg, mode = load_configs(settings)
        t = (settings.raw.get("strategy", {}) or {}).get("tailrider", {}) or {}
        poll_interval_s = float(t.get("poll_interval_s", 1.1))
        self.state.set_system("mode", mode)
        # The config bankroll ($500) is the PAPER measurement bankroll. Once the live book has been
        # anchored to the REAL wallet value (see _refresh_wallet), never overwrite it back to the
        # config fiction on reboot (user report 2026-07-06: "the balance says 500").
        if self.state.get_system("live_bankroll_anchored") is None:
            self.state.set_system("bankroll_start_usd", str(self.risk_cfg.bankroll_usd))
        self.risk = RiskGovernor(self.state, self.risk_cfg)
        # With a JUPITER_API_KEY (from .env via Settings) the client flips to api.jup.ag
        # (x-api-key) and can poll faster than the keyless ~1 req/s floor. Config's
        # poll_interval_s is honored as-is when keyed; keyless clamps up to the safe floor.
        jup_key = settings.jupiter_api_key or None
        self.jc = JupiterClient(api_key=jup_key, min_interval=0.25 if jup_key else 1.05)
        if not jup_key and poll_interval_s < 1.1:
            log.info("no JUPITER_API_KEY -> clamping poll_interval_s %.2f -> 1.10 (keyless floor)",
                     poll_interval_s)
            poll_interval_s = 1.1
        # Live execution is inert unless BOTH mode=live AND env MEMEBOT_LIVE_ARMED=1; real on-chain
        # sends additionally require MEMEBOT_LIVE_SEND=1 (else dry-run quotes only). Triple-gated.
        import os
        armed = mode == "live" and os.environ.get("MEMEBOT_LIVE_ARMED") == "1"
        dry_run = os.environ.get("MEMEBOT_LIVE_SEND") != "1"
        self.pipeline = None
        self._exec_state = None
        if mode == "live":
            # The live executor gets its OWN sqlite connection, used ONLY on the pipeline's worker
            # threads (never the loop) — the N workers serialize their arming/breaker reads+writes on
            # the executor's _state_lock, so they never race the engine's connection. Execution runs
            # OFF the loop; results apply back ON the loop (advance-after-confirm). See
            # docs/LIVE_EXECUTION_PIPELINE.md.
            from memebot.live.execution import LiveExecutionPipeline
            self._exec_state = LiveState(db_path)
            # a DEDICATED JupiterClient for the executor — it runs on the worker thread, and the
            # keyless rate-limiter is not safe to share read-modify-write with the loop's client.
            exec_jc = JupiterClient(api_key=jup_key, min_interval=0.25 if jup_key else 1.05)
            self.executor = make_executor(mode, state=self._exec_state, jupiter_client=exec_jc,
                                          cfg=self.cfg, armed=armed, dry_run=dry_run)
            self.pipeline = LiveExecutionPipeline(self.executor, on_result=self._on_fill_result,
                                                  max_workers=int(t.get("exec_workers", 8)))
            log.warning("LIVE mode: armed=%s dry_run=%s (real funds require armed + MEMEBOT_LIVE_SEND=1)",
                        armed, dry_run)
        else:
            self.executor = make_executor(mode, state=self.state, jupiter_client=self.jc, cfg=self.cfg,
                                          armed=armed, dry_run=dry_run)
        self.engine = LiveEngine(self.state, self.risk, executor=self.executor, cfg=self.cfg,
                                 pipeline=self.pipeline)
        # PAPER TWIN (user request 2026-07-06 — "do not remove the paper machine"): in live mode the
        # paper MEASUREMENT book keeps running alongside real money, inside this same process — fed by
        # the SAME listener + price feed (no second telethon session, no extra poll budget), writing
        # its own DB (seeded from the fresh-live cutover archive by start.sh, so the pre-live paper
        # history continues seamlessly). Uncapped take-every-call, $500 paper bankroll — the
        # paper≈backtest measurement arm the strategy's self-awareness is judged against. It is
        # strictly advisory: every twin call is exception-guarded and can never touch the live book.
        self.paper_state: Optional[LiveState] = None
        self.paper_eng: Optional[LiveEngine] = None
        if mode == "live":
            paper_db = os.environ.get("MEMEBOT_PAPER_DB", str(ROOT / "runs" / "paper_state.db"))
            # HARD GUARD (audit reverify-3, reproduced): the twin must NEVER open the LIVE DB — it
            # would flip system_state.mode to 'paper', which makes _can_send_live refuse every REAL
            # send (stops included) while a second engine double-drives the same rows. One env typo.
            if Path(paper_db).resolve() == Path(db_path).resolve():
                log.error("MEMEBOT_PAPER_DB points at the LIVE DB (%s) — paper twin DISABLED", paper_db)
                self._alert("CRIT", "PAPER_DB_MISCONFIG",
                            "MEMEBOT_PAPER_DB equals the live DB path — paper twin disabled; fix the env")
            else:
                try:
                    self.paper_state = LiveState(paper_db)
                    self.paper_state.set_system("mode", "paper")
                    self.paper_state.set_system("bankroll_start_usd", str(self.risk_cfg.bankroll_usd))
                    # normalize practice-desk gates the archive may have frozen (an archived
                    # kill_switch=on would silently freeze the measurement book + 409 practice buys)
                    self.paper_state.set_system("kill_switch", "off")
                    # same research hygiene as the live book (an archived 'running' ghost row would
                    # hide the archive's last real verdict in the paper lab)
                    try:
                        self.paper_state.conn.execute(
                            "UPDATE research_runs SET status='failed' WHERE status='running'")
                        self.paper_state.conn.commit()
                    except Exception:
                        pass
                    self.paper_eng = LiveEngine(self.paper_state,
                                                RiskGovernor(self.paper_state, self.risk_cfg),
                                                executor=PaperExecutor(), cfg=self.cfg, pipeline=None)
                    # NB: archived controller='manual' rows are KEPT manual — the practice desk drives
                    # them (LiveEngine._rehydrate registers them in manual_pids); a boot must not undo
                    # a user's practice take-over (audit reverify-3).
                    n_rep = repair_orphan_closed_trades(self.paper_state)   # same crash-gap repair
                    if n_rep:
                        log.warning("paper twin: reconstructed %d orphan closed trade(s)", n_rep)
                    log.info("paper twin: measurement book at %s (%d riders, %d manual)",
                             paper_db, len(self.paper_eng.riders), len(self.paper_eng.manual_pids))
                except Exception:
                    log.exception("paper twin failed to start — live trading unaffected")
                    self._alert("WARN", "PAPER_TWIN_DOWN",
                                "paper twin failed to start — practice/measurement book offline; live unaffected")
                    self.paper_state = None
                    self.paper_eng = None
        # Self-awareness band: judge LIVE trades against the backtest/seed distribution — which after
        # the fresh-live cutover lives in the PAPER book (the live DB starts with no seed rows).
        # A paper-DB fault here must degrade the band, never kill the LIVE boot (audit reverify-3).
        try:
            band_state = self.paper_state if self.paper_state is not None else self.state
            self.monitor = Monitor(self.state, Monitor.from_closed_trades(band_state).exp)
        except Exception:
            log.exception("expectation band from the paper book failed — falling back to the live book")
            self.monitor = Monitor.from_closed_trades(self.state)
        # Adaptive layer: the challenger forward race (advisory bookkeeping — it never
        # trades, never promotes itself, and its exceptions never reach the champion path).
        # Rev read FIRST: an add/delete landing during the (slow) rehydrate below then
        # just triggers one redundant refresh instead of being silently missed.
        self._custom_rev = self.state.get_system(CUSTOM_REV_KEY) or "0"
        self._controller_rev = self.state.get_system("controller_rev") or "0"   # take-over / release
        self.shadow = ShadowEngine(self.state,
                                   configs=tuple(CHALLENGERS) + load_custom_challengers(self.state))
        # reseed the shadow race for any WATCHING champion position that lost its riders
        # (e.g. after a repair migration) — idempotent, keeps the forward race complete
        for pos in self.state.active_positions():
            if pos["state"] == "WATCHING" and not self.shadow.has_active(pos["mint"]):
                self.shadow.ingest(pos["mint"], pos["signal_price"] or None,
                                   pos["t0_epoch"], ticker=pos["ticker"])
        # Research artifacts live NEXT TO THE DB so on Railway they land on the /data volume.
        db_dir = Path(db_path).parent
        self.research_corpus = db_dir / "research_corpus.json"
        self.research_cache = db_dir / "research_cache"
        self._research_task: Optional[asyncio.Task] = None
        self.feed = PriceFeed(self.jc, on_tick=self._on_tick, on_dead=self._on_dead,
                              interval_s=poll_interval_s)
        # MANUAL layer: the OrderBook evaluates human `orders` rows on the same tick path the
        # TailRider runs on, firing them through engine.manual_buy/sell (the SAME safe pipeline).
        self.orderbook = OrderBook(self.state, self.engine, track_fn=self.feed.track)
        # PAPER practice desk (user request 2026-07-06 — full functionality, not read-only): the
        # paper book gets its OWN OrderBook + controller rev, driven by the same ticks, firing
        # through the paper twin (PaperExecutor inline — simulated fills, zero real-money surface).
        self.paper_orderbook: Optional[OrderBook] = None
        self._paper_controller_rev = "0"
        if self.paper_eng is not None:
            self.paper_orderbook = OrderBook(self.paper_state, self.paper_eng,
                                             track_fn=self.feed.track)
            self._paper_controller_rev = self.paper_state.get_system("controller_rev") or "0"
        self._manual_desk_mints: set[str] = set()   # mints kept tracked for orders/watchlist
        # TRUE-CANDLE truth layer (datapi 1m candles, keyless — a DIFFERENT rate budget
        # from the price API): reconciles spot-poll blind spots (intrabar wicks), backfills
        # deploy downtime, and pins each watcher's anchor to the backtest's definition
        # (the FIRST 1m candle OPEN at/after the signal — spot diverged up to 24%).
        self.charts = JupiterChartsClient(min_interval=0.4)
        self._candle_hw: dict[str, datetime] = {}    # mint -> ts of the last TRUE candle fed
        self._anchored: set[str] = set()             # mints whose anchor-fidelity pass is done
        self._backfill_targets: dict[str, datetime] = {}
        # re-track any positions (champion or shadow) that were live at shutdown, and
        # schedule a restart backfill from each position's last write (minus 2min slack)
        for pos in self.state.active_positions():
            self.feed.track(pos["mint"])
            upd = from_iso(pos["updated_at"]) if pos["updated_at"] else None
            self._backfill_targets[pos["mint"]] = (upd or utcnow()) - timedelta(minutes=2)
        for mint in self.shadow.riders:
            self.feed.track(mint)
            if mint not in self._backfill_targets:
                pos = self.state.get_position(mint)
                upd = from_iso(pos["updated_at"]) if pos and pos.get("updated_at") else None
                self._backfill_targets[mint] = (upd or utcnow()) - timedelta(minutes=2)
        # MANUAL: track mints that have resting orders or sit on the watchlist so ticks drive them
        for m in self.state.mints_with_open_orders():
            self.feed.track(m)
            self._manual_desk_mints.add(m)
        for w in self.state.watchlist():
            self.feed.track(w["mint"])
            self._manual_desk_mints.add(w["mint"])
        # PAPER TWIN: its book's mints tick + backfill too (earliest gap-start wins per mint).
        # Guarded: a paper-DB fault degrades the twin, never the LIVE boot (audit reverify-3).
        if self.paper_eng is not None:
            try:
                for pos in self.paper_state.active_positions():
                    self.feed.track(pos["mint"])
                    upd = from_iso(pos["updated_at"]) if pos["updated_at"] else None
                    since = (upd or utcnow()) - timedelta(minutes=2)
                    prev = self._backfill_targets.get(pos["mint"])
                    self._backfill_targets[pos["mint"]] = min(prev, since) if prev else since
                # paper practice orders / watchlist keep their mints ticking too
                for m in self.paper_state.mints_with_open_orders():
                    self.feed.track(m)
                    self._manual_desk_mints.add(m)
                for w in self.paper_state.watchlist():
                    self.feed.track(w["mint"])
                    self._manual_desk_mints.add(w["mint"])
            except Exception:
                log.exception("paper twin boot tracking failed — twin degraded; live unaffected")

    # -- feed callbacks ---------------------------------------------------- #
    def _on_tick(self, mint: str, price: float, ts: datetime) -> None:
        # Tick-driven: wrap each spot tick in a 1-tick candle (o=h=l=c) and advance the machine
        # immediately. TailRider is candle-driven and a 1-tick candle triggers dips (low<=level),
        # stops, and rungs (high>=level) exactly, with fills modeled AT the level — same as the
        # backtest. The candle also persists current price, so no separate mark() path is needed.
        candle = Candle(ts=ts, open=price, high=price, low=price, close=price, volume=0.0)
        try:
            self.engine.on_candle(mint, candle)     # F05: a tick error must never kill the feed
        except Exception:
            log.exception("engine.on_candle failed for %s", mint)
            self._alert("CRIT", "ENGINE_TICK_ERROR", f"engine tick failed for {mint}")
        self.shadow.on_candle(mint, candle)     # crash-safe inside; never breaks the main path
        self.orderbook.on_candle(mint, candle)  # MANUAL: mark + fire triggered orders (guarded inside)
        if self.paper_eng is not None:          # PAPER TWIN: same tick, its own book — never breaks live
            try:
                self.paper_eng.on_candle(mint, candle)
                self.paper_orderbook.on_candle(mint, candle)   # practice orders fire on the twin
            except Exception:
                log.exception("paper twin on_candle failed for %s", mint)
        # untrack only when NOTHING needs this mint: no champion rider, no live challenger, no
        # manual position, no paper-twin rider, and no resting order / watchlist entry.
        if not self._mint_needed(mint):
            self.feed.untrack(mint)
        elif mint not in self.engine.riders and mint not in self.engine.manual_pids:
            # only a shadow challenger / resting order keeps it: keep positions.current_price
            # fresh so the lab + order marks stay truthful (a no-op if no position row exists)
            try:
                self.state.update_position(mint, current_price=price)
            except Exception:
                pass

    def _mint_needed(self, mint: str) -> bool:
        return (mint in self.engine.riders or mint in self.engine.manual_pids
                or self.shadow.has_active(mint) or mint in self._manual_desk_mints
                or (self.paper_eng is not None
                    and (mint in self.paper_eng.riders
                         or mint in self.paper_eng.manual_pids)))   # practice take-overs stay tracked

    def _on_dead(self, mint: str, last_price: float) -> None:
        log.info("dead token %s -> finalize at %.8g", mint, last_price)
        now = utcnow()
        if mint in self.engine.manual_pids:          # M4: dead manual positions close too (else they
            self.engine.finalize_manual(mint, last_price, now)   # linger, holding cap + inflating equity
        else:
            self.engine.finalize_token(mint, last_price, now)
        if self.paper_eng is not None:               # PAPER TWIN: its book closes the dead token too
            try:
                if mint in self.paper_eng.manual_pids:   # a practice take-over closes like M4
                    self.paper_eng.finalize_manual(mint, last_price, now)
                else:
                    self.paper_eng.finalize_token(mint, last_price, now)
            except Exception:
                log.exception("paper twin finalize failed for %s", mint)
        self.shadow.finalize(mint, last_price, now)
        # keep tracking if resting orders / watchlist still reference the mint
        if not self._mint_needed(mint):
            self.feed.untrack(mint)

    # -- live execution pipeline callback (runs ON THE LOOP) --------------- #
    def _on_fill_result(self, result) -> None:
        """Delivered by the execution worker via call_soon_threadsafe — so this runs on the loop
        thread, where every DB write lives (single-writer preserved)."""
        try:
            self.engine.apply_fill_result(result)
        except Exception:
            log.exception("apply_fill_result failed for %s", result.mint)
            self._alert("CRIT", "APPLY_ERROR", f"failed to apply fill for {result.mint}")

    async def _reconcile_onchain(self, period_s: float = 180.0) -> None:
        """LIVE (real-send) only: periodically compare each open position's REAL on-chain token
        balance to the DB's expected remaining bag; alert on drift. ALSO flag any mint holding a
        real balance under a WATCHING/EXPIRED/closed row (an orphaned bag). Read-only; the network
        read runs off-loop. The standing safety net for anything the per-trade path missed."""
        while True:
            await asyncio.sleep(period_s)
            try:
                await self._refresh_wallet()     # keep the dashboard's wallet line fresh (~3min)
                # (a) open positions: real balance vs expected remaining bag
                rows = self.state.query(
                    "SELECT mint,tokens_qty,remaining_frac FROM positions "
                    "WHERE state IN ('ENTERED','SECURED','RIDING')")
                for r in rows:
                    mint = r["mint"]
                    if mint in self.engine._pending or not r["tokens_qty"]:
                        continue
                    real = await asyncio.to_thread(self._held_tokens, mint)
                    if real is None:
                        continue
                    expected = (r["tokens_qty"] or 0.0) * (r["remaining_frac"]
                                                           if r["remaining_frac"] is not None else 1.0)
                    tol = max(expected * 0.05, 1e-6)
                    if abs(real - expected) > tol:
                        self._alert("WARN", "RECON_DRIFT",
                                    f"{mint[:6]}… on-chain {real:.6g} vs expected {expected:.6g} tokens")
                # (b) orphan backstop: a balance held under a NON-open row = untracked money. Skip
                # dead-writeoff rows (audit re-verify #7): their residual dust is KNOWN + unsellable, so
                # CRIT-ing them every 180s would spam and mask a genuine orphan on another mint.
                orphans = self.state.query(
                    "SELECT mint FROM positions WHERE state IN ('WATCHING','EXPIRED','EXITED','STOPPED') "
                    "AND (close_reason IS NULL OR close_reason != 'dead_writeoff')")
                for r in orphans:
                    mint = r["mint"]
                    if mint in self.engine._pending:
                        continue
                    real = await asyncio.to_thread(self._held_tokens, mint)
                    if real and real > 1e-6:
                        # per-mint alert kind so one mint's orphan never throttles another's (audit #7)
                        self._alert("CRIT", f"ORPHAN_BALANCE_{mint[:8]}",
                                    f"{mint[:6]}… holds {real:.6g} tokens under a non-open position "
                                    "— untracked on-chain balance; investigate")
            except Exception:
                log.exception("onchain reconcile pass failed")

    async def _reconcile_submitted_intents(self) -> None:
        """Boot scan (real-send only): resolve any position whose newest event is a *_SUBMITTED
        intent left unresolved by a crash between an on-chain confirm and the loop-side apply.
        ENTER_SUBMITTED + a real held balance -> ADOPT the landed buy (else the machine may never
        re-enter after the price pumped off the dip, orphaning the bag). Runs before the feed drives
        candles. Sells are idempotent by construction (executor sizes to a target remaining), so a
        re-fired SELL_SUBMITTED is a no-op — we only log those for P&L awareness."""
        if self.pipeline is None or getattr(self.executor, "dry_run", True):
            return
        try:
            rows = self.state.query(
                "SELECT id,mint,signal_price FROM positions "
                "WHERE state IN ('WATCHING','ENTERED','SECURED','RIDING')")
            for r in rows:
                pid, mint = r["id"], r["mint"]
                ev = self.state.query("SELECT event_type,price,proceeds_usd FROM position_events "
                                      "WHERE position_id=? ORDER BY id DESC LIMIT 1", (pid,))
                if not ev or not str(ev[0]["event_type"]).endswith("_SUBMITTED"):
                    continue
                kind = str(ev[0]["event_type"])[: -len("_SUBMITTED")]
                # audit #29: bound each on-chain read so a slow (not-down) RPC can't stall the whole
                # boot (reconcile runs BEFORE the feed starts). A timeout defers this mint's reconcile,
                # identical to the 'real is None' path — the next intent path / retry re-resolves it.
                try:
                    real = await asyncio.wait_for(
                        asyncio.to_thread(self._held_tokens, mint), timeout=5.0)
                except asyncio.TimeoutError:
                    real = None
                if real is None:
                    continue
                if kind == "ENTER" and real > 1e-9:
                    entry = ev[0]["price"] or r["signal_price"]
                    stake = self.risk.size_for(self.engine._realized_equity())
                    self.state.update_position(
                        mint, state="ENTERED", entry_at=utcnow().isoformat(), entry_price=entry,
                        stake_usd=stake, tokens_qty=real, stop_price=self.cfg.stop_level_mult * entry,
                        remaining_frac=1.0, secured=0, n_tp=0, next_rung_mult=self.cfg.tp1_mult)
                    self.state.append_event(position_id=pid, mint=mint, ts=utcnow(),
                                            event_type="ENTER", price=entry, remaining_frac=1.0,
                                            note="adopted landed buy after restart (idempotent)")
                    self.engine._rollback(mint)      # rebuild the in-memory rider from the ENTERED row
                    log.warning("restart reconcile: adopted landed buy for %s (%.6g tokens)", mint, real)
                    self._alert("WARN", "RESTART_ADOPT", f"adopted landed buy for {mint[:6]}… on restart")
                elif kind == "MANUAL_BUY" and real > 1e-9:
                    # a direct buy landed on-chain but a crash lost the loop-side commit — adopt it as
                    # an ALGO-managed position (config #1 rides it), matching direct_buy's booking.
                    stake = ev[0]["proceeds_usd"]
                    entry = (stake / real) if (stake and real) else (ev[0]["price"] or r["signal_price"])
                    stake = stake or (real * (entry or 0.0))
                    if entry and entry > 0:
                        from memebot.live.executor import Fill
                        fill = Fill(mint, "ENTRY", entry, real, stake, ts=utcnow(),
                                    note="adopted landed direct buy after restart")
                        self.engine._direct_buy_book(mint, pid, fill, utcnow())
                        # audit #23: resolve ONLY the submitted order that landed — not every open buy
                        # order for the mint (a still-resting limit buy must not be marked filled).
                        for o in self.state.query(
                                "SELECT id FROM orders WHERE mint=? AND side='buy' AND status='submitted'",
                                (mint,)):
                            self.state.update_order(o["id"], status="filled",
                                                    filled_at=utcnow().isoformat(), position_id=pid)
                        self._alert("WARN", "RESTART_ADOPT",
                                    f"adopted landed direct buy for {mint[:6]}… on restart")
                elif kind == "MANUAL_SELL":
                    # Codex review: a manual sell may have LANDED before the crash. Reconcile the DB
                    # bag to the REAL balance so a phantom (already-sold) position can't linger open
                    # and understate P&L. The lost fill's exact proceeds are unrecoverable — book from
                    # the KNOWN legs and alert the operator to verify on-chain.
                    pos = self.state.get_position(mint)
                    tq = (pos or {}).get("tokens_qty") or 0.0
                    rem = pos.get("remaining_frac") if (pos and pos.get("remaining_frac") is not None) else 1.0
                    if pos and tq and real < tq * rem * 0.98:          # bag shrank -> the sell landed
                        if real <= max(tq * 1e-6, 1e-9):              # fully sold -> close it out
                            entry = pos.get("entry_price") or 0.0
                            stake = pos.get("stake_usd") or 0.0
                            prows = self.state.query(
                                "SELECT COALESCE(SUM(proceeds_usd),0) AS p FROM position_events "
                                "WHERE position_id=? AND event_type IN "
                                "('TP','RIDE_SELL','STOP_OUT','FINALIZE','MANUAL_SELL')", (pid,))
                            proceeds = float(prows[0]["p"] or 0.0)
                            rmult = (proceeds / stake) if stake else 0.0
                            now = utcnow()
                            self.state.update_position(
                                mint, state="EXITED", remaining_frac=0.0, realized_multiple=rmult,
                                current_multiple=rmult, realized_pnl_usd=proceeds - stake,
                                closed_at=now.isoformat(), close_reason="manual_close_reconciled")
                            self.state.record_close(
                                position_id=pid, mint=mint, ticker=pos.get("ticker"),
                                entry_at=from_iso(pos.get("entry_at")), entry_price=entry,
                                stake_usd=stake, exit_at=now, close_reason="manual_close_reconciled",
                                realized_multiple=rmult, pnl_usd=proceeds - stake, n_tp=0,
                                was_stopped=False, was_secured=bool(pos.get("secured")))
                            self.engine.manual_pids.pop(mint, None)
                            # the SUBMITTED order that landed -> filled; other resting orders ->
                            # cancelled (position closed). NOT all -> filled (Codex review).
                            for o in self.state.open_orders(mint):
                                self.state.update_order(
                                    o["id"],
                                    status="filled" if o["status"] == "submitted" else "cancelled",
                                    note="reconciled on restart (position closed)")
                            self._alert("WARN", "MANUAL_RECON",
                                        f"manual sell {mint[:6]}… landed during a crash — closed by "
                                        "reconcile; verify exact proceeds on-chain")
                        else:                                        # partial -> reconcile the bag
                            self.state.update_position(mint, remaining_frac=max(0.0, real / tq))
                            # resolve the SUBMITTED order that landed so it can't re-fire a 2nd sell
                            # (Codex review); leave any 'open' resting orders against the smaller bag.
                            for o in self.state.query(
                                    "SELECT id FROM orders WHERE mint=? AND status='submitted'", (mint,)):
                                self.state.update_order(o["id"], status="filled",
                                                        note="partial fill reconciled on restart")
                            self._alert("WARN", "MANUAL_RECON",
                                        f"manual partial sell {mint[:6]}… landed during a crash — bag "
                                        "reconciled to on-chain balance")
                    else:
                        log.info("restart reconcile: %s MANUAL_SELL idempotent on retry", mint)
                elif kind in ("ENTER", "MANUAL_BUY"):
                    log.info("restart reconcile: %s %s submitted but no balance landed — retry", mint, kind)
                elif kind in ("TP", "RIDE_SELL", "STOP_OUT", "FINALIZE"):
                    # audit #5: an algo sell leg landed on-chain but the loop-apply was lost (crash /
                    # confirm-timeout). Re-drive the rider through the SAME leg with the REAL proceeds
                    # (on-chain bag delta) so its P&L is not zeroed by the idempotent retry.
                    ev_price = ev[0]["price"] or 0.0
                    if self.engine.reconcile_landed_algo_sell(mint, kind, ev_price, real):
                        self._alert("WARN", "ALGO_SELL_RECON",
                                    f"algo {kind} {mint[:6]}… landed during a crash — booked from the "
                                    "on-chain bag delta; verify exact proceeds on-chain")
                    else:
                        log.info("restart reconcile: %s %s did not land — idempotent retry", mint, kind)
                else:
                    log.info("restart reconcile: %s has an unresolved %s intent (idempotent on retry)",
                             mint, kind)
        except Exception:
            log.exception("restart intent reconcile failed")

    def _held_tokens(self, mint: str) -> Optional[float]:
        """Real held token amount for the burner (executor's clients), or None if unavailable."""
        try:
            swap = self.executor._ensure_clients()
            owner = self.executor._owner()
            if owner is None:
                return None
            return swap.token_balance(owner, mint) / (10 ** swap.token_decimals(mint))
        except Exception:
            return None

    # -- live wallet truth (the balance the operator actually owns) ---------- #
    def _wallet_read(self) -> Optional[tuple[float, float]]:
        """(sol, usd_value) of the burner from chain, or None. Network — worker thread only."""
        try:
            with self.executor._state_lock:      # B-5: lazy client init is under the executor's lock
                swap = self.executor._ensure_clients()
                owner = self.executor._owner()
            if owner is None:
                return None
            res = swap._rpc("getBalance", [owner])
            sol = ((res or {}).get("value", 0) or 0) / 1e9
            px = self.executor._sol_usd()
            if px <= 0:
                return None
            return sol, sol * px
        except Exception:
            return None

    async def _refresh_wallet(self) -> None:
        """Write the burner's REAL balance into system_state (the dashboard's wallet line), and on
        the FIRST successful read ANCHOR the live bankroll to it (user report 2026-07-06: "the
        balance says 500"). The $500 config bankroll is paper-measurement fiction — the live book's
        equity must start at what the wallet actually holds. The anchor also purges the 500-based
        bankroll points written between go-live and the anchor. One-shot via live_bankroll_anchored."""
        if self.pipeline is None or getattr(self.executor, "dry_run", True):
            return
        w = await asyncio.to_thread(self._wallet_read)
        if w is None:
            return
        sol, usd = w
        self.state.set_system("wallet_sol", f"{sol:.9f}")   # display line — always refreshed
        self.state.set_system("wallet_usd", f"{usd:.2f}")
        self.state.set_system("wallet_at", utcnow().isoformat())
        if self.state.get_system("live_bankroll_anchored") is not None:
            return
        # B-1: never anchor at $0 (an unfunded/failed read) — funding later would never re-anchor.
        if usd <= 0:
            return
        # B-2: never anchor while capital is already deployed — start would exclude the open stake and
        # permanently understate the account. Retry on the next ~3-min pass once flat.
        if self.state.query("SELECT 1 FROM positions WHERE state IN ('ENTERED','SECURED','RIDING') "
                            "LIMIT 1"):
            return
        # B-3: drop ONLY the engine's $500-fiction heartbeats (expected_equity_usd IS NULL) — a
        # backtest-seeded live DB keeps its seed curve rows.
        self.state.conn.execute("DELETE FROM bankroll_history WHERE expected_equity_usd IS NULL")
        self.state.conn.commit()
        self.state.set_system("bankroll_start_usd", f"{usd:.2f}")
        self.state.set_system("live_bankroll_anchored", utcnow().isoformat())
        log.warning("live bankroll anchored to the real wallet: %.4f SOL ≈ $%.2f", sol, usd)
        self._alert("INFO", "BANKROLL_ANCHORED",
                    f"live bankroll anchored to the real wallet: {sol:.4f} SOL ≈ ${usd:.2f}")

    # -- resilience helpers ------------------------------------------------ #
    def _alert(self, severity: str, kind: str, message: str, *, min_interval_s: float = 300.0) -> None:
        """Record an alert, throttled per-kind so a repeating fault cannot storm the table."""
        import time
        now = time.monotonic()
        if now - self._alert_times.get(kind, 0.0) < min_interval_s:
            return
        self._alert_times[kind] = now
        try:
            self.state.record_alert(severity=severity, kind=kind, message=message)
        except Exception:
            log.exception("failed to record alert %s", kind)

    async def _supervise(self, name: str, factory) -> None:
        """Run a long-lived coroutine forever; on crash log + alert + restart with capped
        backoff, so ONE task's failure never tears down the whole orchestrator (F05). The
        top-level gather used return_exceptions=False, so any single loop's exception killed
        the process — losing coverage on every open position until Railway restarted it."""
        backoff = 1.0
        while True:
            try:
                await factory()
                log.error("task %s returned unexpectedly; restarting in %.0fs", name, backoff)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("task %s crashed; restarting in %.0fs", name, backoff)
                self._alert("CRIT", f"TASK_CRASH_{name}", f"{name} crashed; auto-restarting")
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)

    # -- signal callback --------------------------------------------------- #
    async def _on_call(self, sig) -> None:
        price = None
        try:
            price = (await asyncio.to_thread(self.jc.price, [sig.mint])).get(sig.mint)
        except Exception:
            pass
        now = utcnow()                               # ONE timestamp -> both books share signal_at
        accepted = self.engine.ingest_call(sig, price=price, now=now)
        p_accepted = False
        if self.paper_eng is not None:               # PAPER TWIN: uncapped measurement ingest
            try:
                p_accepted = self.paper_eng.ingest_call(sig, price=price, now=now)
            except Exception:
                log.exception("paper twin ingest failed for %s", sig.mint)
        if accepted:
            # audit reverify-3 A-1 (reproduced): a NEW live watcher must get its own F20 anchor pass
            # even if an earlier paper-only-accepted call (or paper injection) burned the one-shot
            # slot — else the REAL −50% trigger stays on the ingest spot (diverges up to ~24%).
            self._anchored.discard(sig.mint)
        if accepted or p_accepted:
            self.shadow.ingest(sig.mint, price, utcnow().timestamp(), ticker=sig.ticker)
            # F20: pin the anchor to the FIRST 1m candle OPEN at/after the signal BEFORE the
            # first spot tick can evaluate the -50% dip. Otherwise a dip arriving in the first
            # ~60s (before the reconciler's anchor pass) is measured off the transient
            # ingest-time SPOT price (which diverged up to 24% from the backtest's anchor).
            try:
                await self._maybe_reanchor(sig.mint)
            except Exception:
                log.exception("ingest-time anchor failed for %s", sig.mint)
            self.feed.track(sig.mint)
            log.info("WATCHING %s (%s) @ %s (live=%s paper=%s)",
                     sig.ticker, sig.mint, price, accepted, p_accepted)

    # -- periodic sampler -------------------------------------------------- #
    async def _sampler(self, period_s: float = 30.0) -> None:
        while True:
            now = utcnow()
            try:
                # expire WATCHING positions whose 48h dip window has passed
                for pos in self.state.active_positions():
                    if pos["state"] == "WATCHING":
                        mint = pos["mint"]
                        # audit #18: a failed/unconfirmed direct-buy leaves a WATCHING/algo row with NO
                        # rider and NULL dip_deadline; finalize_token no-ops without a rider, so it never
                        # expires and (post-restart) becomes an autonomous dip-rider. Reap the true orphan
                        # (no rider, not in-flight, no resting order to fill it — so no capital/balance).
                        if (mint not in self.engine.riders and mint not in self.engine._pending
                                and mint not in self.engine.manual_pids
                                and not self.state.open_orders(mint)):
                            self.state.update_position(mint, state="EXPIRED", closed_at=now.isoformat(),
                                                       close_reason="never_entered")
                            if not self._mint_needed(mint):
                                self.feed.untrack(mint)
                            continue
                        dl = from_iso(pos["dip_deadline"])
                        if dl and now > dl:
                            self.engine.finalize_token(mint, pos["signal_price"] or 0.0, now)
                            if not self._mint_needed(mint):
                                self.feed.untrack(mint)
                self.engine.sample_bankroll(now=now)
                # F33: the feed heartbeat must reflect REAL feed liveness (the feed's last
                # successful poll), NOT an unconditional sampler tick — else check_feed can
                # never see an outage. A failing price API leaves feed.last_ok_ts stale, so
                # monitor.check_feed fires FEED_OUTAGE.
                if self.feed.last_ok_ts is not None:
                    self.state.set_system("last_feed_ok_ts", self.feed.last_ok_ts.isoformat())
                if self.pipeline is not None:      # live: surface in-flight swap count (observability)
                    self.state.set_system("exec_pending", str(len(self.engine._pending)))
                # MANUAL: refresh the set of mints kept tracked for orders/watchlist, ensure the
                # feed tracks them, and expire stale orders (even for mints that stopped ticking).
                self._manual_desk_mints = (set(self.state.mints_with_open_orders())
                                           | {w["mint"] for w in self.state.watchlist()})
                self.orderbook.sweep()
                self._process_manual_signals_for(self.state, self.engine, shadow=True)
                self._controller_rev = self._reconcile_controllers_for(
                    self.engine, self.state, self._controller_rev)
                # LIVE self-awareness + research run BEFORE any paper work, so a paper-DB fault can
                # never starve them (audit reverify-3: paper legs isolated in their own try below).
                self.monitor.run_once(now=now)
                self._maybe_launch_research(now)
                self._maybe_refresh_customs()
            except Exception:
                log.exception("sampler pass failed")
                self._alert("WARN", "SAMPLER_ERROR", "sampler pass failed; continuing")
            # PAPER twin maintenance — fully isolated: its failure degrades only the practice book.
            if self.paper_eng is not None:
                try:
                    # expire the twin's stale watchers (+ reap rider-less orphans, audit #18)
                    for pos in self.paper_state.active_positions():
                        if pos["state"] != "WATCHING":
                            continue
                        m = pos["mint"]
                        if (m not in self.paper_eng.riders and m not in self.paper_eng.manual_pids
                                and not self.paper_state.open_orders(m)):
                            self.paper_state.update_position(m, state="EXPIRED",
                                                             closed_at=now.isoformat(),
                                                             close_reason="never_entered")
                            if not self._mint_needed(m):
                                self.feed.untrack(m)
                            continue
                        dl = from_iso(pos["dip_deadline"])
                        if dl and now > dl:
                            self.paper_eng.finalize_token(m, pos["signal_price"] or 0.0, now)
                            if not self._mint_needed(m):
                                self.feed.untrack(m)
                    self.paper_eng.sample_bankroll(now=now)
                    self._manual_desk_mints |= (set(self.paper_state.mints_with_open_orders())
                                                | {w["mint"] for w in self.paper_state.watchlist()})
                    self.paper_orderbook.sweep()
                    self._process_manual_signals_for(self.paper_state, self.paper_eng, shadow=False)
                    self._paper_controller_rev = self._reconcile_controllers_for(
                        self.paper_eng, self.paper_state, self._paper_controller_rev)
                except Exception:
                    log.exception("paper twin sampler pass failed — practice book degraded; live ok")
            await asyncio.sleep(period_s)

    def _reconcile_controllers(self) -> None:
        self._controller_rev = self._reconcile_controllers_for(
            self.engine, self.state, self._controller_rev)
        if self.paper_eng is not None:               # practice take-over/release on the paper book
            self._paper_controller_rev = self._reconcile_controllers_for(
                self.paper_eng, self.paper_state, self._paper_controller_rev)

    def _reconcile_controllers_for(self, engine, state, cached_rev: str) -> str:
        """Apply dashboard take-over / release requests (bumped controller_rev) for ONE book: move an
        algo rider to manual (TailRider stops), or rehydrate a TailRider for a released manual position
        (the algo resumes as a config-#1 position from its current state). Returns the new cached rev.
        ≤3s latency via the controller loop — and the position stays valid under either owner meanwhile."""
        rev = state.get_system("controller_rev") or "0"
        if rev == cached_rev:
            return cached_rev
        # H1: NEVER flip ownership for a mint with an in-flight swap — the confirming leg would then
        # apply to the wrong owner (a dropped algo sell, or a resurrected closed manual position).
        # Defer those and DON'T advance the rev, so the next sampler pass retries once it resolves.
        # (The paper twin has no pipeline, so its _pending is always empty — never defers.)
        deferred = False
        for mint in list(engine.riders):                      # algo -> manual
            pos = state.get_position(mint)
            if pos and pos.get("controller") == "manual":
                if mint in engine._pending:
                    deferred = True
                    continue
                engine.riders.pop(mint, None)
                engine.pids.pop(mint, None)
                if pos["state"] in ("ENTERED", "SECURED", "RIDING"):
                    engine.manual_pids[mint] = pos["id"]
                log.info("take-over: %s now manual (algo rider stopped)", mint)
        for mint in list(engine.manual_pids):                 # manual -> algo
            pos = state.get_position(mint)
            if pos and pos.get("controller") == "algo" and pos["state"] in ("ENTERED", "SECURED", "RIDING"):
                if mint in engine._pending:
                    deferred = True
                    continue
                engine.riders[mint] = TailRider.restore(
                    engine.cfg, engine._pos_snapshot(pos, engine.cfg))
                engine.pids[mint] = pos["id"]
                engine.manual_pids.pop(mint, None)
                self.feed.track(mint)
                log.info("release: %s back to the algo", mint)
        return cached_rev if deferred else rev

    def _process_manual_signals(self) -> None:
        self._process_manual_signals_for(self.state, self.engine, shadow=True)
        if self.paper_eng is not None:               # practice injections into the paper book
            self._process_manual_signals_for(self.paper_state, self.paper_eng, shadow=False)

    def _process_manual_signals_for(self, state, engine, *, shadow: bool) -> None:
        """Consume human-injected signals for ONE book (dashboard 'add to watchlist'): each becomes a
        WATCHING algo position the machine rides exactly like a channel call. Runs on the loop,
        single-writer. Paper injections skip the shadow race (challengers race the live book's calls)."""
        for s in state.pending_manual_signals():
            mint = s["mint"]
            try:
                ok, msg = engine.inject_signal(mint, price=s["price"], ticker=s["ticker"],
                                               note="manual signal")
                if ok:
                    if shadow:
                        self.shadow.ingest(mint, s["price"], utcnow().timestamp(), ticker=s["ticker"])
                    self.feed.track(mint)
                    # audit #24: mirror the channel path (_on_call) — pin the anchor to the first 1m
                    # candle open BEFORE a tick can evaluate the -50% dip off the (up to ~24% off)
                    # add-time spot. Idempotent via self._anchored; a no-op once entered.
                    try:
                        asyncio.get_running_loop().create_task(self._maybe_reanchor(mint))
                    except RuntimeError:
                        pass    # no running loop (unit test) — the reconciler re-anchors within ~60s
                    state.mark_manual_signal(s["id"], "done", msg)
                    log.info("injected manual signal %s (%s) @ %s", s["ticker"], mint, s["price"])
                else:
                    state.mark_manual_signal(s["id"], "rejected", msg)
            except Exception:
                log.exception("manual signal inject failed for %s", mint)
                state.mark_manual_signal(s["id"], "rejected", "engine error")

    async def _controller_loop(self, period_s: float = 3.0) -> None:
        """Fast take-over/release application (Codex review): shrinks the coverage gap from the 30s
        sampler cadence to ~3s so a handed-over/handed-back real position is never left undriven (nor
        double-driven) for long. Cheap: _reconcile_controllers reads ONE system_state key and
        early-returns when controller_rev is unchanged."""
        while True:
            await asyncio.sleep(period_s)
            try:
                self._reconcile_controllers()
            except Exception:
                log.exception("controller reconcile pass failed")

    def _maybe_refresh_customs(self) -> None:
        """Pick up dashboard-added/-deleted custom challengers (forward-only: riders
        spawn for calls ingested AFTER the refresh; existing races are untouched)."""
        rev = self.state.get_system(CUSTOM_REV_KEY) or "0"
        if rev == self._custom_rev:
            return
        self._custom_rev = rev
        customs = load_custom_challengers(self.state)
        self.shadow.configs = list(CHALLENGERS) + list(customs)
        self.shadow.by_id = {c.id: c for c in self.shadow.configs}
        # Prune zombie riders of DELETED configs — otherwise they keep racing in memory,
        # re-upserting the rows the dashboard just deleted (and a reused id would inherit
        # the old strategy's legs, corrupting the forward evidence).
        for mint, group in list(self.shadow.riders.items()):
            for cid in list(group):
                if cid not in self.shadow.by_id:
                    group.pop(cid, None)
                    self.state.delete_shadow_config(cid)
            if not group:
                self.shadow.riders.pop(mint, None)
        log.info("custom challenger set refreshed (rev %s): %s",
                 rev, [c.id for c in customs] or "none")

    # -- true-candle truth layer (reconciliation / backfill / anchor) ------- #
    def _feed_candle(self, mint: str, candle: Candle) -> bool:
        """Feed one TRUE 1m candle into champion+shadow under the per-mint HIGH-WATER
        rule: only candles STRICTLY NEWER than the last true candle fed pass (an old low
        arriving after a newer TP must never fire a stale stop). Spot ticks bypass this —
        the machine is monotonic and level-triggered, so overlapping coverage is safe."""
        hw = self._candle_hw.get(mint)
        if hw is not None and candle.ts <= hw:
            return False
        self._candle_hw[mint] = candle.ts
        self.engine.on_candle(mint, candle)
        self.shadow.on_candle(mint, candle)     # crash-safe inside
        self.orderbook.on_candle(mint, candle)  # MANUAL: intrabar-precise order triggers
        if self.paper_eng is not None:          # PAPER TWIN: true candles drive its book too
            try:
                self.paper_eng.on_candle(mint, candle)
                self.paper_orderbook.on_candle(mint, candle)   # intrabar-precise practice orders
            except Exception:
                log.exception("paper twin true-candle failed for %s", mint)
        return True

    async def _backfill(self) -> None:
        """Once at startup: replay 1m candles across the deploy gap for every rehydrated
        mint (positions.updated_at - 2min -> now). A dip wick that printed while we were
        down is honest per the fill model — a resting order AT the level would have
        filled, identical to the backtest's candle semantics."""
        targets, self._backfill_targets = self._backfill_targets, {}
        for mint, since in targets.items():
            try:
                now = utcnow()
                cands = await asyncio.to_thread(
                    self.charts.fetch_candles, mint, "1_MINUTE", since, now, candles=1000)
                # CLAMP (2026-07-03): datapi can return candles far outside the requested
                # window — an unclamped backfill once replayed a token's ENTIRE history and
                # manufactured pre-signal trades. The engine guard also drops pre-signal
                # candles, but never feed junk in the first place.
                cands = [c for c in cands if since <= c.ts <= now]
                fed = sum(1 for c in cands if self._feed_candle(mint, c))
                log.info("BACKFILL %s: replayed %d/%d 1m candles since %s",
                         mint, fed, len(cands), since.isoformat())
                if not self._mint_needed(mint):
                    self.feed.untrack(mint)
            except Exception:
                log.exception("backfill failed for %s", mint)

    async def _maybe_reanchor(self, mint: str) -> None:
        """Anchor fidelity, exactly once per watcher: re-anchor `sig` from the ingest-time
        spot price to the FIRST 1m candle OPEN at/after signal_at (the backtest's exact
        definition). NEVER after entry or once the dip has already triggered."""
        if mint in self._anchored:
            return

        def _needs(tr) -> bool:                 # a rider that still measures its dip off the spot
            return tr is not None and tr.state is PositionState.WATCHING and tr.entry is None

        lr = self.engine.riders.get(mint)
        pr = self.paper_eng.riders.get(mint) if self.paper_eng is not None else None
        if not _needs(lr) and not _needs(pr):
            self._anchored.add(mint)            # nothing to anchor / too late — done forever
            return
        # PREFER the LIVE row whenever it exists (audit reverify-3 A-3): a failed-ENTER rollback
        # during the fetch await could reset the live rider to WATCHING, and anchoring it from a
        # PAPER row's (possibly minute-skewed) signal_at would mis-set a REAL −50% trigger. A
        # live-rejected call creates no live row, so the paper fallback still covers those mints.
        pos = self.state.get_position(mint) or \
              (self.paper_state.get_position(mint) if self.paper_state is not None else None) or {}
        sig_at = from_iso(pos.get("signal_at"))
        if sig_at is None:
            self._anchored.add(mint)
            return
        cands = await asyncio.to_thread(
            self.charts.fetch_candles, mint, "1_MINUTE",
            sig_at, sig_at + timedelta(minutes=5), candles=10)
        # strict floor = the backtest's exact rule (first candle with ts >= posted_at)
        first = next((c for c in cands if c.ts >= sig_at), None)
        if first is None:
            return                              # datapi hasn't indexed it yet — retry next pass
        # each book's reanchor re-checks WATCHING/no-entry (the rider may have entered during
        # the fetch await); shadow updates non-entered riders only.
        did = self.engine.reanchor(mint, first.open)
        if self.paper_eng is not None:
            try:
                did = self.paper_eng.reanchor(mint, first.open) or did
            except Exception:
                log.exception("paper twin reanchor failed for %s", mint)
        if did:
            self.shadow.reanchor(mint, first.open)
            spot = pos.get("signal_price") or 0.0
            if spot > 0:
                log.info("ANCHOR %s spot=%.8g -> first-1m-open=%.8g (%+.2f%%)",
                         pos.get("ticker") or mint, spot, first.open,
                         (first.open / spot - 1.0) * 100.0)
            else:
                log.info("ANCHOR %s spot=n/a -> first-1m-open=%.8g",
                         pos.get("ticker") or mint, first.open)
        self._anchored.add(mint)

    async def _reconcile_mint(self, mint: str) -> None:
        await self._maybe_reanchor(mint)
        now = utcnow()
        cands = await asyncio.to_thread(
            self.charts.fetch_candles, mint, "1_MINUTE",
            now - timedelta(minutes=3), now, candles=10)
        cands = [c for c in cands if now - timedelta(minutes=4) <= c.ts <= now]  # clamp junk
        if cands:
            self.feed.note_alive(mint)          # F24: datapi still sees it -> reset dead timer
        fed = sum(1 for c in cands if self._feed_candle(mint, c))
        if fed:
            log.debug("RECON %s: fed %d/%d true 1m candles", mint, fed, len(cands))
        if not self._mint_needed(mint):
            self.feed.untrack(mint)

    async def _reconciler(self, period_s: float = 60.0) -> None:
        """Every ~60s: fetch the last ~3min of TRUE 1m candles (datapi trade-based OHLC)
        for every tracked mint, so the machine sees the real intrabar lows/highs between
        spot ticks. Runs the startup backfill FIRST so downtime candles land before
        fresher reconciliation candles raise the high-water mark."""
        try:
            await self._backfill()
        except Exception:
            log.exception("startup backfill failed")
        while True:
            await asyncio.sleep(period_s)
            for mint in self.feed.tracked():
                try:                            # one mint's failure never kills the loop
                    await self._reconcile_mint(mint)
                except Exception:
                    log.exception("reconcile failed for %s", mint)

    # -- adaptive research (weekly + on-demand; one run at a time) ---------- #
    def _research_running_in_db(self, now: datetime) -> bool:
        """Cross-process single-flight: research.py inserts a status='running' research_runs
        row at launch. If one is live (started_at < 2h ago) — e.g. a run that survived an
        orchestrator restart — do NOT double-launch on top of it. Ghost rows older than 2h
        (hard-killed runs) are ignored here; the next run itself supersedes them."""
        import json
        try:
            rows = self.state.query(
                "SELECT ts, verdict_json FROM research_runs WHERE status='running'")
        except Exception:
            return False                            # unreadable table -> don't block launches
        for r in rows:
            started = None
            try:
                started = from_iso(r["ts"])
            except (TypeError, ValueError):
                pass
            try:
                v = json.loads(r["verdict_json"] or "{}")
                if isinstance(v, dict):
                    started = from_iso(v.get("started_at")) or started
            except (TypeError, ValueError):
                pass
            if started and (now - started) < timedelta(hours=2):
                return True
        return False

    def _maybe_launch_research(self, now: datetime) -> None:
        if self._research_task is not None and not self._research_task.done():
            return                                  # guard: only one run at a time (in-process)
        if self._research_running_in_db(now):
            return                # guard: a live 'running' row in the DB (a pending on-demand
            # request stays queued — research_requested is only consumed at actual launch)
        reason = None
        if self.state.get_system("research_requested") == "1":
            self.state.set_system("research_requested", "0")   # orchestrator clears the request
            reason = "on-demand"
        else:
            last = from_iso(self.state.get_system("last_research_at"))
            if last is None or (now - last) > timedelta(days=7):
                reason = "weekly"
        if reason:
            log.info("research launch (%s)", reason)
            self._research_task = asyncio.create_task(self._run_research(reason))

    async def _run_research(self, reason: str) -> None:
        # run_remeasurement never raises (failures -> a status='failed' research_runs row);
        # it runs in a worker thread so pricing/corpus IO cannot stall the tick loop. F15: it
        # gets its OWN LiveState connection — sharing the engine's sqlite3.Connection across
        # threads broke atomicity (a research commit flushed the engine's pending write) and
        # could raise into the tick path. busy_timeout lets the two writers coexist under WAL.
        from memebot.live.research import run_remeasurement
        from memebot.live.state import LiveState
        log.info("research re-measurement starting (%s)", reason)

        def _work():
            rstate = LiveState(self.state.path)
            try:
                return run_remeasurement(
                    rstate, corpus_path=self.research_corpus, cache_dir=self.research_cache)
            finally:
                rstate.close()

        verdict = await asyncio.to_thread(_work)
        log.info("research done: status=%s clears=%s degradation=%s",
                 verdict.get("status"), verdict.get("any_config_clears_gate"),
                 verdict.get("degradation_alert"))

    async def run(self) -> None:
        from memebot.live.listener import run_listener
        log.info("orchestrator starting (mode=%s, stake=$%.2f)", self.state.get_system("mode"),
                 self.risk_cfg.stake_usd)
        # Live execution runs on a worker thread; results apply back on THIS loop.
        if self.pipeline is not None:
            self.pipeline.start(asyncio.get_running_loop())
            # resolve any *_SUBMITTED intent a crash left unapplied, BEFORE candles start driving
            await self._reconcile_submitted_intents()
            await self._refresh_wallet()         # anchor the live bankroll to the REAL wallet at boot
        # Each loop is supervised (F05): a crash in one restarts that loop with backoff and
        # never kills the others. The listener additionally self-reconnects with catch-up (F30).
        tasks = [
            self._supervise("listener",
                            lambda: run_listener(CHANNEL, self._on_call, state=self.state)),
            self._supervise("feed", self.feed.run),
            self._supervise("sampler", self._sampler),
            self._supervise("reconciler", self._reconciler),
            self._supervise("controller", self._controller_loop),   # fast take-over/release (~3s)
        ]
        # LIVE real-send only: the on-chain balance drift + orphan safety net. Gated off in
        # dry-run so the supervisor doesn't log-spam restarting a task that returns immediately.
        if self.pipeline is not None and not getattr(self.executor, "dry_run", True):
            tasks.append(self._supervise("onchain_reconcile", self._reconcile_onchain))
        await asyncio.gather(*tasks)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--log", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import signal
    # a Railway redeploy sends SIGTERM — make it raise KeyboardInterrupt so we drain gracefully.
    try:
        signal.signal(signal.SIGTERM, signal.default_int_handler)
    except (ValueError, OSError):
        pass                                    # not the main thread (e.g. under a test runner)
    orch = Orchestrator(args.db)
    try:
        asyncio.run(orch.run())
    except KeyboardInterrupt:
        log.info("shutdown")
    finally:
        if orch.pipeline is not None:
            log.info("draining execution pipeline (bounded)...")
            orch.pipeline.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
