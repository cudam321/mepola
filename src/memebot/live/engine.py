"""LiveEngine — the autonomous core: drives config #1 state machines from live price candles.

Pure orchestration (no asyncio, no network): ingest a Call -> WATCHING position; feed candles ->
advance each `TailRider` -> route its events through the executor -> persist to SQLite -> close
terminal positions and update the bankroll. `run.py` wraps this in an async loop with the Telethon
listener and the Jupiter price feed; this class is fully unit-testable by feeding synthetic candles.

Because the `TailRider` embodies config #1's fill model and `PaperExecutor` realizes it exactly,
a paper run over a token's real candles reproduces the backtest — the same guarantee the equivalence
gate proves. In live mode the same logic runs against `LiveExecutor` (Phase D, gated).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from memebot.live.execution import EXEC_KINDS, ExecJob, FillResult, LegResult
from memebot.live.executor import Executor, Fill, PaperExecutor
from memebot.live.risk import STAKE_HARD_CAP_USD, RiskGovernor
from memebot.live.state import LiveState, from_iso, utcnow
from memebot.live.strategy import Event, PositionState, TailRider, TailRiderConfig
from memebot.models import Candle, Signal, SignalSide

log = logging.getLogger("memebot.live.engine")

_SELL_KINDS = ("TP", "RIDE_SELL", "STOP_OUT", "FINALIZE")
_MANUAL_ACTIVE = ("ENTERED", "SECURED", "RIDING")
_TERMINAL_STATES = ("EXITED", "STOPPED", "EXPIRED")
_MANUAL_DUST = 1e-9


class LiveEngine:
    def __init__(self, state: LiveState, risk: RiskGovernor, *, executor: Optional[Executor] = None,
                 cfg: Optional[TailRiderConfig] = None, pipeline=None):
        self.state = state
        self.risk = risk
        self.executor = executor or PaperExecutor()
        self.cfg = cfg or TailRiderConfig()
        # LIVE-only: an off-loop execution pipeline. When set (mode=live), execution-bearing events
        # are submitted to it and the DB advances only after a confirmed fill (apply_fill_result).
        # When None (paper), on_candle applies inline exactly as before — the equivalence path.
        self.pipeline = pipeline
        self._pending: set[str] = set()          # mints with an in-flight swap job (skip their candles)
        self._buffered: dict[str, Candle] = {}   # latest candle seen while a mint was pending
        self.riders: dict[str, TailRider] = {}
        self.pids: dict[str, int] = {}
        # MANUAL layer: human-controlled positions have NO TailRider — they are driven by the
        # OrderBook + direct actions. manual_pids maps an active manual mint -> position id;
        # _manual_pending holds the metadata of an in-flight manual swap so apply_fill_result
        # can book the confirmed fill onto the right position/order.
        self.manual_pids: dict[str, int] = {}
        self._manual_pending: dict[str, dict] = {}
        self._dead_finalize_fails: dict[str, int] = {}   # audit #7: consecutive unroutable FINALIZEs
        self._dead_manual_tries: dict[str, int] = {}     # H5: real-swap attempts before a manual writeoff
        self._sells_blocked_alerted = False              # M1: page once when gates block a stop
        self._algo_fail_seen: set[str] = set()           # audit #16: alert-once-per-mint on algo fail
        # in-flight (submitted-but-unconfirmed) BUY notionals per mint — counted by can_enter so the
        # deployed/concurrency caps hold under a one-sweep correlated dip (audit re-verify #1/#2).
        self._pending_buy_usd: dict[str, float] = {}
        self._rehydrate()

    def _reserved_buys(self) -> tuple[int, float]:
        """(count, Σ notional) of in-flight buys — the reservation the cap gate adds to confirmed use."""
        return len(self._pending_buy_usd), sum(self._pending_buy_usd.values())

    # -- restart rebuild --------------------------------------------------- #
    @staticmethod
    def _pos_snapshot(pos: dict, cfg: TailRiderConfig) -> dict:
        return {
            "state": pos["state"], "sig": pos["signal_price"], "t0": pos["t0_epoch"],
            "entry": pos["entry_price"], "stop_price": pos["stop_price"],
            "rem": pos["remaining_frac"] if pos["remaining_frac"] is not None else 1.0,
            "pr": pos["proceeds_units"] or 0.0, "n_tp": pos["n_tp"] or 0,
            "lvl": pos["next_rung_mult"] or cfg.tp1_mult,
            "secured": bool(pos["secured"]), "peak_price": pos["peak_price"] or 0.0,
            "low_price": pos["low_price"],
        }

    def _rehydrate(self) -> None:
        for pos in self.state.active_positions():
            if pos.get("controller") == "manual":
                # A manual position is driven by orders, not a TailRider — track its pid only.
                self.manual_pids[pos["mint"]] = pos["id"]
                continue
            self.riders[pos["mint"]] = TailRider.restore(self.cfg, self._pos_snapshot(pos, self.cfg))
            self.pids[pos["mint"]] = pos["id"]

    # -- ingest a signal --------------------------------------------------- #
    def ingest_call(self, sig: Signal, *, price: Optional[float] = None,
                    now: Optional[datetime] = None) -> bool:
        """Register a first-call BUY as a WATCHING position (no capital committed yet)."""
        now = now or utcnow()
        if not sig.mint or self.state.is_seen(sig.mint):
            self.state.record_signal(ts=now, source_channel=sig.source_channel, message_id=sig.message_id,
                                     ticker=sig.ticker, mint=sig.mint, side="buy",
                                     parse_confidence=sig.parse_confidence, is_first_call=False,
                                     accepted=False, reject_reason="dup_or_no_mint")
            return False
        decision = self.risk.can_enter()
        self.state.record_signal(ts=now, source_channel=sig.source_channel, message_id=sig.message_id,
                                 ticker=sig.ticker, mint=sig.mint, side="buy",
                                 parse_confidence=sig.parse_confidence, is_first_call=True,
                                 accepted=decision.ok, reject_reason=decision.reason,
                                 raw_text=(sig.raw_text or "")[:400])
        if not decision.ok:
            # F27: do NOT mark_seen on a capacity/kill rejection — the is_seen() guard above
            # would then permanently blacklist the mint, so a token declined while
            # max_concurrent was full (or the kill-switch was on) could never be entered on a
            # later re-call. The rejection is already captured in `signals` (accepted=False)
            # for the audit trail; leaving the mint eligible matches the oracle, which has no
            # capacity model. (A re-call re-anchors at the new call price — accepted residual.)
            return False
        self.state.mark_seen(sig.mint, ticker=sig.ticker, source_channel=sig.source_channel,
                             message_id=sig.message_id, first_seen_at=now, signal_price=price,
                             outcome="positioned")
        tr = TailRider(cfg=self.cfg, sig=price, t0=now.timestamp())
        deadline = now + timedelta(hours=self.cfg.dip_window_h)
        # re-audit #4 (M7 completion): a TERMINAL row can exist for an unseen mint — a failed
        # direct buy that the reaper EXPIRED and then unmarked. positions.mint is UNIQUE, so a
        # plain INSERT raises IntegrityError (losing the call and, from catch-up, wedging the
        # replay). Reset the dead row back to WATCHING with the new signal's fields instead.
        stale = self.state.get_position(sig.mint)
        if (stale is not None and stale["state"] == "EXPIRED"
                and (stale.get("close_reason") or "") == "never_entered"):
            # only never-entered rows are reusable — a row with real trades keeps its history
            pid = stale["id"]
            self.state.update_position(
                sig.mint, state="WATCHING", ticker=sig.ticker, signal_at=now.isoformat(),
                signal_price=price or 0.0, dip_deadline=deadline.isoformat(),
                t0_epoch=now.timestamp(), entry_at=None, entry_price=None, stake_usd=None,
                tokens_qty=None, stop_price=None, remaining_frac=None, secured=0, n_tp=0,
                next_rung_mult=None, next_rung_price=None, peak_price=None, current_price=price,
                current_multiple=None, realized_multiple=None, realized_pnl_usd=None,
                closed_at=None, close_reason=None, controller="algo")
        else:
            pid = self.state.create_position(mint=sig.mint, ticker=sig.ticker, signal_at=now,
                                             signal_price=price or 0.0, state="WATCHING",
                                             dip_deadline=deadline,
                                             source_channel=sig.source_channel,
                                             message_id=sig.message_id, t0_epoch=now.timestamp())
        self.state.append_event(position_id=pid, mint=sig.mint, ts=now, event_type="SIGNAL",
                                price=price, note="first-call BUY -> WATCHING")
        self.riders[sig.mint] = tr
        self.pids[sig.mint] = pid
        return True

    def inject_signal(self, mint: str, *, price: float, ticker: Optional[str] = None,
                      note: str = "manual signal") -> tuple[bool, str]:
        """Inject a token as a CALL (the human's own intel) — treated EXACTLY like a channel signal:
        a WATCHING algo position that config #1 then rides (waits for the −50% dip, buys $3, etc.).
        This is the 'add to watchlist' action."""
        if not mint:
            return False, "no mint"
        pos = self.state.get_position(mint)
        if pos and pos["state"] in ("WATCHING", "ENTERED", "SECURED", "RIDING"):
            return False, "already tracked"
        sig = Signal(source_channel="manual", message_id=0, posted_at=utcnow(), raw_text=note,
                     side=SignalSide.BUY, mint=mint, ticker=ticker, parse_confidence=1.0)
        if self.ingest_call(sig, price=price):
            return True, "watching"
        return False, "rejected (duplicate or at capacity)"

    # -- advance one token by one candle ----------------------------------- #
    def on_candle(self, mint: str, candle: Candle) -> None:
        tr = self.riders.get(mint)
        if tr is None:
            return
        # INVARIANT (2026-07-03): never process a candle from before the signal existed.
        # A backfill bug replayed pre-signal history and manufactured an entry dated weeks
        # before the call. Matches the backtest exactly: it only feeds candles ts >= posted_at.
        if tr.t0 is not None and candle.ts.timestamp() < tr.t0:
            return
        # FAIL-SAFE (audit #10): an EXPLICIT dashboard take-over writes controller='manual' out-of-band
        # (separate process); the algo rider is only dropped ~3s later by _reconcile_controllers. Never
        # let a stale algo rider drive a position the human now owns (would submit an algo swap on a
        # manual position), nor one already closed (would resurrect it into a 2nd closed_trades row).
        # BUT NEVER during an in-flight swap (H1): a pending sell's confirmed fill must still apply to
        # THIS algo rider — dropping it here would lose the TP/RIDE_SELL/STOP_OUT proceeds. Fall through
        # to the buffer-and-return path; the take-over hand-off happens after the swap resolves.
        if mint not in self._pending:
            pos = self.state.get_position(mint)
            if pos is None or pos.get("controller") == "manual" or pos.get("state") in _TERMINAL_STATES:
                if pos is not None and pos.get("controller") == "manual":
                    self.riders.pop(mint, None)  # drop the orphan algo rider now that it's manual
                    self.pids.pop(mint, None)
                    self._algo_fail_seen.discard(mint)
                    # COMPLETE the hand-off (audit re-verify): if the fail-safe wins the race with
                    # _reconcile_controllers, register the active position under manual_pids so it stays
                    # feed-tracked (dead-detection + marks) and can be released back. Idempotent w/ run.py.
                    if pos.get("state") in _MANUAL_ACTIVE:
                        self.manual_pids[mint] = pos["id"]
                return
        if self.pipeline is not None:
            if mint in self._pending:
                self._buffer_candle(mint, candle)   # a swap is in flight — accumulate, don't act
                return
            # LIVE: single_exec -> at most one execution event per call, so every job is one leg.
            # Pass the candle as refeed so, after this leg confirms, the SAME candle is re-fed to
            # collect the next rung (a bar that clears several rungs executes one swap at a time).
            self._drive_live(mint, self.pids[mint], tr, tr.on_candle(candle, single_exec=True),
                             current_price=candle.close, ts=candle.ts, refeed_candle=candle)
            return
        # PAPER — apply inline exactly as before (the equivalence path, untouched).
        self._drive_paper(mint, self.pids[mint], tr, tr.on_candle(candle),
                          current_price=candle.close, ts=candle.ts)

    def finalize_token(self, mint: str, last_price: float, ts: Optional[datetime] = None) -> None:
        """Close out a token (dead/illiquid/horizon) at its last observed price."""
        tr = self.riders.get(mint)
        if tr is None:
            return
        if self.pipeline is not None:
            if mint in self._pending:
                return       # a swap is in flight; let it resolve — the dead-check retries next pass
            self._drive_live(mint, self.pids[mint], tr, tr.finalize(last_price, ts),
                             current_price=last_price, ts=ts or utcnow())
            return
        self._drive_paper(mint, self.pids[mint], tr, tr.finalize(last_price, ts),
                          current_price=last_price, ts=ts or utcnow())

    def _buffer_candle(self, mint: str, candle: Candle) -> None:
        """Accumulate candles seen while a mint is pending into ONE covering candle (min low, max
        high) — so a stop-low or rung-high that printed during the ~30s swap window is NOT lost
        (last-tick-only buffering would drop it). Re-fed once the pending swap resolves."""
        b = self._buffered.get(mint)
        if b is None:
            self._buffered[mint] = candle
        else:
            self._buffered[mint] = Candle(ts=candle.ts, open=b.open,
                                          high=max(b.high, candle.high), low=min(b.low, candle.low),
                                          close=candle.close, volume=0.0)

    # -- paper: apply inline (unchanged equivalence path) ------------------ #
    def _drive_paper(self, mint: str, pid: int, tr: TailRider, events, *,
                     current_price: float, ts: datetime) -> None:
        for ev in events:
            self._apply_event(mint, pid, tr, ev)
        self._persist_position(mint, pid, tr, current_price=current_price)
        if tr.is_terminal:
            self._finalize(mint, pid, tr, ts)

    # -- live: decide on the loop, execute off it (ONE leg per job) --------- #
    def _drive_live(self, mint: str, pid: int, tr: TailRider, events, *,
                    current_price: float, ts: datetime, refeed_candle: Optional[Candle] = None) -> None:
        events = list(events)
        exec_events = [e for e in events if e.kind in EXEC_KINDS]     # <= 1 (single_exec)
        for e in events:
            if e.kind not in EXEC_KINDS:         # bookkeeping events persist immediately
                self.state.append_event(position_id=pid, mint=mint, ts=e.ts, event_type=e.kind,
                                        price=e.price, rung_mult=e.rung_mult, frac=e.frac,
                                        remaining_frac=e.remaining_frac, note=e.note)
        if not exec_events:
            self._persist_position(mint, pid, tr, current_price=current_price)
            if tr.is_terminal:                   # EXPIRED never-entered etc. — no swap needed
                self._finalize(mint, pid, tr, ts)
            return
        e = exec_events[0]
        side = "buy" if e.kind == "ENTER" else "sell"
        if not self._can_send_live(side):
            # M1 (audit 2026-07-07): a blocked SELL means every stop is dead while the operator
            # gates/mode stay unset — that must PAGE, not silently retry each candle. Alerted on
            # the transition only; cleared when a sell goes through again.
            if side == "sell" and not self._sells_blocked_alerted:
                self._sells_blocked_alerted = True
                try:
                    self.state.record_alert(
                        severity="CRIT", kind="SELLS_DISABLED",
                        message=f"a live SELL for {mint[:6]}… was blocked by mode/arming/gates — "
                                "ALL stops are disabled while this holds; restore the gates")
                except Exception:
                    pass
            self._rollback(mint)                 # not armed / gated — stay confirmed, retry next candle
            return
        if side == "sell" and self._sells_blocked_alerted:
            self._sells_blocked_alerted = False
        pos = self.state.get_position(mint) or {}
        stake = self.risk.size_for(self._realized_equity()) if e.kind == "ENTER" \
            else (pos.get("stake_usd") or self.risk.size_for())
        if e.kind == "ENTER":
            # BLOCKER #2: the deployed/concurrency caps are evaluated at signal-ingest (can_enter in
            # ingest_call), but WATCHING costs nothing — a correlated -50% dip fills many at once.
            # Re-gate the caps HERE, at the capital-committing ENTER (live-only; can_enter is uncapped
            # in paper so equivalence is untouched). Count in-flight buys (reserved) so a one-sweep
            # correlated dip can't fire N ENTERs that each see deployed unchanged and overshoot. On
            # breach stay WATCHING and retry next candle. NEVER gate sells (risk-reducing).
            rn, ru = self._reserved_buys()
            decision = self.risk.can_enter(prospective_stake=stake, reserved_n=rn, reserved_usd=ru)
            if not decision.ok:
                self._rollback(mint)
                return
        self._pending.add(mint)
        if e.kind == "ENTER":
            self._pending_buy_usd[mint] = stake      # reserve this in-flight buy against the cap
        if refeed_candle is not None:            # re-feed after this leg to collect the next rung
            self._buffer_candle(mint, refeed_candle)
        self.state.append_event(position_id=pid, mint=mint, ts=e.ts,     # durable restart intent marker
                                event_type=e.kind + "_SUBMITTED", price=e.price, frac=e.frac,
                                rung_mult=e.rung_mult, remaining_frac=e.remaining_frac,
                                note="live intent submitted (awaiting confirm)")
        self.pipeline.submit(ExecJob(mint=mint, pid=pid, stake_usd=stake, entry_price=tr.entry,
                                     events=[e], candle_ts=ts, tokens_qty=pos.get("tokens_qty"),
                                     current_price=current_price))

    def _can_send_live(self, side: str) -> bool:
        """Whether a live swap of `side` may be submitted right now (read on the loop thread)."""
        if self.state.get_system("mode") != "live":
            return False
        if not getattr(self.executor, "armed", False):
            return False
        if side == "buy" and self.state.get_system("kill_switch") == "on":
            return False            # F04: kill blocks NEW buys; sells always allowed
        if not getattr(self.executor, "dry_run", True):
            # real sends need both operator gates (F11)
            if self.state.get_system("equivalence_ok") != "1":
                return False
            if self.state.get_system("dust_reconciled") != "1":
                return False
        return True

    def _rollback(self, mint: str) -> None:
        """Discard the optimistic in-memory advance; rebuild the rider from the authoritative DB
        row (the last CONFIRMED state). Used when a swap can't be sent or a job failed."""
        pos = self.state.get_position(mint)
        if pos:
            self.riders[mint] = TailRider.restore(self.cfg, self._pos_snapshot(pos, self.cfg))

    # -- apply a confirmed/failed job (ON THE LOOP THREAD) ----------------- #
    def apply_fill_result(self, result: FillResult) -> None:
        mint, pid = result.mint, result.pid
        self._pending.discard(mint)
        self._pending_buy_usd.pop(mint, None)        # release the in-flight buy reservation
        if result.manual:
            self._apply_manual_result(result)
            return
        tr = self.riders.get(mint)
        if tr is None:
            return
        if result.ok:
            self._dead_finalize_fails.pop(mint, None)
            self._algo_fail_seen.discard(mint)
            self._apply_confirmed(mint, pid, tr, result)
            # re-feed the buffered candle to collect the NEXT leg of the same bar (multi-rung)
            buf = self._buffered.pop(mint, None)
            if buf is not None and mint in self.riders and mint not in self._pending:
                self.on_candle(mint, buf)
        else:
            log.warning("live job for %s failed: %s — rolling back", mint, result.error)
            self._buffered.pop(mint, None)       # do NOT re-feed on failure — the next tick retries
            # audit #7: a FINALIZE that keeps failing means the token is rugged/unroutable (a routable
            # token's FINALIZE would land + close). Retrying forever zombies the slot + never books the
            # loss. After N failures, write it off with NO swap (the money is already gone).
            if any(leg.event.kind == "FINALIZE" for leg in result.legs):
                n = self._dead_finalize_fails.get(mint, 0) + 1
                self._dead_finalize_fails[mint] = n
                if n >= 3:
                    self._dead_writeoff(mint, pid, result.current_price or 0.0)
                    return
            # audit #16: surface a persistent algo failure ONCE per mint (a confirmed-swap failure
            # trips the executor breaker, but a PRE-SEND failure — a quote no-route / RPC error — does
            # NOT, so it would otherwise retry silently every tick). Throttled via _algo_fail_seen;
            # cleared on the next success.
            if mint not in self._algo_fail_seen:
                self._algo_fail_seen.add(mint)
                try:
                    kind = result.legs[0].event.kind if result.legs else "swap"
                    # a raw RPC error repr can be a 7KB program-log dump — alert with the
                    # headline only (the full text is in the engine log / orders.note)
                    err = str(result.error or "")
                    if len(err) > 300:
                        err = err[:300].rstrip() + " … [truncated]"
                    self.state.record_alert(severity="WARN", kind="ALGO_ORDER_FAILED",
                                            message=f"algo {kind} {mint[:6]}… failed: {err} "
                                                    "— retrying next tick")
                except Exception:
                    pass
            self._rollback(mint)                 # rebuild the rider from the last CONFIRMED DB state

    def _dead_writeoff(self, mint: str, pid: int, last_price: float) -> None:
        """Audit #7: close a rugged/unroutable algo position with NO swap (mirror finalize_manual).
        Value the residual at last_price (~0 for a dead token), book the real (~total) loss, free the
        slot + budget, and alert. Reached only after repeated FINALIZE-swap failures = true unsellability."""
        # AUDIT B3 (2026-07-07): the rider reaching this point is the OPTIMISTICALLY-advanced one
        # (tr.finalize already ran for the failed job: rem=0, terminal) because the 3rd failure
        # returns before the rollback. Writing off from it values the residual at $0, appends NO
        # finalize event, and the dead_writeoff tag then suppresses the orphan alert for a bag that
        # may be worth real money. Restore the last CONFIRMED state first, then write off honestly.
        self._rollback(mint)
        tr = self.riders.get(mint)
        if tr is None:
            self._dead_finalize_fails.pop(mint, None)
            return
        pos = self.state.get_position(mint) or {}
        px = last_price if last_price and last_price > 0 else 0.0
        residual_usd = tr.rem * (pos.get("tokens_qty") or 0.0) * px
        ts = utcnow()
        for ev in tr.finalize(px, ts):           # advances the rider to EXITED, books residual into pr
            self.state.append_event(position_id=pid, mint=mint, ts=ev.ts, event_type=ev.kind,
                                    price=ev.price, frac=ev.frac,
                                    proceeds_usd=(residual_usd if ev.kind == "FINALIZE" else None),
                                    remaining_frac=ev.remaining_frac,
                                    note="dead/unroutable — written off with no swap (audit #7)")
        self._persist_position(mint, pid, tr, current_price=px)
        # reason='dead_writeoff' labels BOTH positions and closed_trades consistently (audit re-verify),
        # and the tag lets the on-chain orphan reconcile skip the KNOWN written-off dust (#7) instead of
        # CRIT-spamming ORPHAN_BALANCE every 180s and masking a real orphan.
        self._finalize_live(mint, pid, tr, ts, reason="dead_writeoff")
        self._dead_finalize_fails.pop(mint, None)
        try:
            self.state.record_alert(severity="WARN", kind="DEAD_WRITEOFF",
                                    message=f"{mint[:6]}… FINALIZE unroutable after retries — written off "
                                            f"at {px:.6g} (no swap); slot freed")
        except Exception:
            pass

    def _apply_confirmed(self, mint: str, pid: int, tr: TailRider, result: FillResult) -> None:
        enter = next((leg for leg in result.legs if leg.event.kind == "ENTER"), None)
        if enter is not None and enter.fill is not None:
            f = enter.fill
            self.state.update_position(mint, state=tr.state.value, entry_at=enter.event.ts.isoformat(),
                                       entry_price=tr.entry, stake_usd=f.usd, tokens_qty=f.tokens,
                                       stop_price=tr.stop_price, remaining_frac=tr.rem)
        for leg in result.legs:                  # record each leg's REAL proceeds (F03)
            ev, f = leg.event, leg.fill
            self.state.append_event(position_id=pid, mint=mint, ts=ev.ts, event_type=ev.kind,
                                    price=ev.price, rung_mult=ev.rung_mult, frac=ev.frac,
                                    proceeds_usd=(f.usd if (f and ev.kind in _SELL_KINDS) else None),
                                    remaining_frac=ev.remaining_frac,
                                    note=(f.note if f else ev.note))
        last_ts = result.legs[-1].event.ts if result.legs else utcnow()
        self._persist_position(mint, pid, tr,
                               current_price=(result.current_price or tr.peak_price or tr.entry or 0.0))
        if tr.is_terminal:
            self._finalize_live(mint, pid, tr, last_ts)

    def _finalize_live(self, mint: str, pid: int, tr: TailRider, ts: datetime,
                       *, reason: Optional[str] = None) -> None:
        """Close a LIVE position with REAL P&L (F03): summed actual sell proceeds − real cost,
        not the modeled tr.realized_multiple. `reason` overrides the default close_reason so callers
        (e.g. the dead-writeoff) label BOTH positions and closed_trades consistently."""
        pos = self.state.get_position(mint) or {}
        stake = pos.get("stake_usd") or 0.0
        if tr.state is PositionState.EXPIRED or tr.realized_multiple is None:
            self.state.update_position(mint, state="EXPIRED", closed_at=ts.isoformat(),
                                       close_reason="no_dip_within_48h")
        else:
            rows = self.state.query(
                "SELECT COALESCE(SUM(proceeds_usd),0) AS p FROM position_events "
                "WHERE position_id=? AND event_type IN "
                "('TP','RIDE_SELL','STOP_OUT','FINALIZE','MANUAL_SELL')", (pid,))    # incl. manual legs
            real_proceeds = float(rows[0]["p"] or 0.0)
            real_pnl = real_proceeds - stake
            real_mult = (real_proceeds / stake) if stake else (tr.realized_multiple or 0.0)
            entry_at = from_iso(pos.get("entry_at"))
            held = ((ts - entry_at).total_seconds() / 3600.0) if entry_at else None
            reason = reason or ("stopped" if tr.state is PositionState.STOPPED else "rode_to_horizon")
            self.state.update_position(mint, state=tr.state.value, realized_multiple=real_mult,
                                       current_multiple=real_mult, realized_pnl_usd=real_pnl,
                                       closed_at=ts.isoformat(), close_reason=reason)
            self.state.record_close(position_id=pid, mint=mint, ticker=pos.get("ticker"),
                                    entry_at=entry_at, entry_price=tr.entry, stake_usd=stake, exit_at=ts,
                                    close_reason=reason, realized_multiple=real_mult, pnl_usd=real_pnl,
                                    peak_multiple=(tr.peak_price / tr.entry if tr.entry else None),
                                    held_hours=held, n_tp=tr.n_tp,
                                    was_stopped=(tr.state is PositionState.STOPPED), was_secured=tr.secured)
            self.risk.record_realized_pnl(real_pnl)
        self._cancel_resting_orders(mint)            # audit #12: close cancels its resting orders
        self.riders.pop(mint, None)
        self.pids.pop(mint, None)

    def reconcile_landed_algo_sell(self, mint: str, kind: str, ev_price: float,
                                   real_tokens: float) -> bool:
        """Restart reconcile (audit #5): an algo sell leg (TP/RIDE_SELL/STOP_OUT/FINALIZE) landed
        on-chain but its loop-apply was lost (crash or confirm-timeout rollback). The rehydrated rider
        is at the PRE-sell state, so the idempotent retry would re-fire the leg and book $0 proceeds —
        zeroing exactly the TP1-secure leg config #1's EV lives in. Instead, re-drive the rider through
        the SAME leg (a synthetic candle at the trigger, or finalize) and book the REAL proceeds from
        the on-chain bag delta. Returns True iff a landed leg was reconciled (False = did not land)."""
        tr = self.riders.get(mint)
        pos = self.state.get_position(mint)
        if tr is None or pos is None or not pos.get("tokens_qty"):
            return False
        tokens_qty = pos["tokens_qty"]
        rem = pos["remaining_frac"] if pos["remaining_frac"] is not None else 1.0
        pre_bag = tokens_qty * rem
        sold = pre_bag - real_tokens
        if pre_bag <= 0 or sold <= pre_bag * 1e-6:   # bag did not shrink -> the sell did NOT land
            return False
        ts = utcnow()
        proceeds = sold * (ev_price or 0.0)          # real proceeds estimate (bag delta x trigger px)
        if kind == "FINALIZE":
            events = tr.finalize(ev_price, ts)
        else:
            entry = tr.entry or 1.0
            if kind == "STOP_OUT":
                candle = Candle(ts=ts, open=entry, high=entry, low=ev_price, close=ev_price, volume=0.0)
            else:                                    # TP / RIDE_SELL — a high that clears the rung
                candle = Candle(ts=ts, open=entry, high=ev_price, low=entry, close=ev_price, volume=0.0)
            events = tr.on_candle(candle, single_exec=True)
        exec_ev = next((e for e in events if e.kind in EXEC_KINDS), None)
        if exec_ev is None:
            return False
        fill = Fill(mint, "SELL", exec_ev.price, sold, proceeds, ts=ts,
                    note="restart-reconciled landed sell (proceeds estimated — verify on-chain)")
        result = FillResult(mint, pos["id"], True, [LegResult(exec_ev, True, fill)],
                            current_price=ev_price)
        self._apply_confirmed(mint, pos["id"], tr, result)
        return True

    # ==================================================================== #
    # MANUAL layer — human discretionary control, riding the SAME safe money path.
    #
    # A manual position uses the identical (entry, stake, tokens_qty, remaining_frac rem,
    # proceeds_units pr) accounting as the algo, so current_multiple = (pr + rem*price)/entry
    # and the closed realized_multiple = pr/entry — the dashboard's account/hero/stats math is
    # unchanged. The difference is purely control: no TailRider drives it; the OrderBook + direct
    # actions do. Every real-money manual swap goes through _can_send_live + the pipeline (live)
    # or the PaperExecutor (paper), with the arming gates, kill-switch (buys), idempotent sells,
    # failure breaker, and burner allowlist all intact. A direct buy is clamped by the per-order
    # fat-finger cap AND the survival hard cap, and — because it books controller='algo' — is gated
    # by the algo's own total_deployed_cap_usd / max_concurrent via risk.can_enter (BLOCKER #1).
    # manual_cap_usd is the direct-buy ENABLE switch (0 = off), not a separate aggregate ceiling.
    # ==================================================================== #
    def _manual_caps(self) -> tuple[float, float]:
        hard = float(self.state.get_system("manual_trade_hard_cap_usd") or 0.0)
        total = float(self.state.get_system("manual_cap_usd") or 0.0)
        return hard, total

    def _prepare_direct_buy_row(self, mint, price, ticker) -> tuple[bool, str, Optional[int]]:
        """Ensure a position row exists to receive a DIRECT BUY. The result stays ALGO-managed
        (config #1 rides it — the user's choice), so we NEVER flip controller to 'manual'. A WATCHING
        position is entered now (skip the −50% dip wait); an active hold is refused (no averaging)."""
        pos = self.state.get_position(mint)
        if pos is None:
            now = utcnow()
            pid = self.state.create_position(mint=mint, ticker=ticker, signal_at=now,
                                             signal_price=price, state="WATCHING",
                                             t0_epoch=now.timestamp())
            self.state.mark_seen(mint, ticker=ticker, first_seen_at=now, signal_price=price,
                                 outcome="positioned")
            return True, "", pid
        if pos["state"] in _MANUAL_ACTIVE:
            return False, "already holding this position", pos["id"]
        if pos["state"] in ("EXITED", "STOPPED", "EXPIRED"):
            return False, "position already closed", pos["id"]
        return True, "", pos["id"]                   # WATCHING → buy it now, keep it algo-managed

    def direct_buy(self, mint: str, *, usd: float, price: float, ts: Optional[datetime] = None,
                   ticker: Optional[str] = None, order_id: Optional[int] = None,
                   note: str = "") -> tuple[bool, str]:
        """A human DIRECT BUY (market or a filled limit). The bought position is ALGO-managed — the
        config #1 rider rides it from the buy price (−30% stop → 3× secure → ride). Clamped to the
        per-order + exposure caps; kill-switch blocks it; live requires the arming gates. Paper sims."""
        ts = ts or utcnow()
        if not price or price <= 0:
            return False, "no live price for this mint"
        if self.state.get_system("kill_switch") == "on":
            return False, "kill-switch on (new buys blocked)"
        hard, total = self._manual_caps()
        if hard > 0:
            usd = min(usd, hard)                      # per-order fat-finger clamp
        usd = min(usd, STAKE_HARD_CAP_USD)            # + the researched survival ceiling (never exceed)
        if usd <= 0:
            return False, "size must be > 0"
        if total <= 0:                               # manual_cap_usd = 0 -> direct buys disabled
            return False, "direct buys disabled (manual cap = 0)"
        if mint in self._pending:
            return False, "a swap is already in flight for this mint"
        if self.pipeline is not None and not self._can_send_live("buy"):
            return False, "live buys not available (mode/arming/gates)"
        # BLOCKER #1: a confirmed direct buy becomes a controller='algo' position, so it MUST honor the
        # same aggregate ceilings as an algo entry. Gate through can_enter (counting in-flight buys) —
        # this is what actually binds total_deployed_cap_usd + max_concurrent on the direct-buy path
        # (uncapped in paper). Returns a NON-terminal reason so a resting limit buy retries, not cancels.
        rn, ru = self._reserved_buys()
        decision = self.risk.can_enter(prospective_stake=usd, reserved_n=rn, reserved_usd=ru)
        if not decision.ok:
            return False, f"risk cap ({decision.reason}) — retry when a slot frees"
        ok, reason, pid = self._prepare_direct_buy_row(mint, price, ticker)
        if not ok:
            return False, reason
        # H3: CLAIM the order (open -> submitted) before anything fires. The dashboard is a
        # separate process — without the compare-and-swap, a user's just-written 'cancelled'
        # is overwritten here and the cancelled order still buys.
        if order_id and not self.state.claim_order(order_id, "open", "submitted"):
            return False, "order no longer open (cancelled/claimed) — not fired"
        if self.pipeline is not None:                # LIVE — off-loop, confirm-then-commit
            self._pending.add(mint)
            self._pending_buy_usd[mint] = usd        # reserve this in-flight buy against the cap
            self._manual_pending[mint] = {"op": "buy", "order_id": order_id, "ticker": ticker,
                                          "note": note}
            self.state.append_event(position_id=pid, mint=mint, ts=ts,
                                    event_type="MANUAL_BUY_SUBMITTED", price=price,
                                    proceeds_usd=usd, note="direct buy submitted (awaiting confirm)")
            self.pipeline.submit(ExecJob(
                mint=mint, pid=pid, stake_usd=usd, entry_price=price,
                events=[Event(ts, "ENTER", price=price, remaining_frac=1.0, note="direct buy")],
                candle_ts=ts, current_price=price, manual=True, order_id=order_id))
            return True, "submitted"
        # PAPER — simulate inline against the recorder
        fill = self.executor.buy(mint=mint, stake_usd=usd, entry_price=price, ts=ts)
        self._direct_buy_book(mint, pid, fill, ts, order_id=order_id, ticker=ticker, note=note)
        return True, "filled"

    def _ensure_manual_control(self, mint: str) -> None:
        """Implicit take-over: the FIRST manual override on an algo position (a hand sell / a
        TP-SL edit) hands the human the wheel — stop the algo rider on this ONE position. No toggle
        (the act IS the take-over). A crash-safe DB flip + rider drop, all on the loop thread."""
        pos = self.state.get_position(mint)
        if pos is None or pos.get("controller") == "manual":
            return
        if pos["state"] not in _MANUAL_ACTIVE:
            return
        self.state.update_position(mint, controller="manual")
        self.riders.pop(mint, None)
        self.pids.pop(mint, None)
        self.manual_pids[mint] = pos["id"]
        self.state.set_system("controller_rev",
                              str(int(self.state.get_system("controller_rev") or "0") + 1))

    def manual_sell(self, mint: str, *, price: Optional[float] = None,
                    ts: Optional[datetime] = None, sell_frac: Optional[float] = None,
                    sell_tokens: Optional[float] = None, close: bool = False,
                    order_id: Optional[int] = None, is_stop: bool = False,
                    note: str = "") -> tuple[bool, str]:
        """Human sell of a manual position (risk-reducing — never kill-blocked). Size by fraction
        of current holdings, absolute tokens, or `close` (dump the bag). Idempotent in live."""
        ts = ts or utcnow()
        pos = self.state.get_position(mint)
        if pos is None or pos["state"] not in _MANUAL_ACTIVE:
            return False, "no open position to sell"
        if mint in self._pending:
            return False, "a swap is already in flight for this mint"
        self._ensure_manual_control(mint)            # implicit take-over: acting IS the override
        pos = self.state.get_position(mint)          # re-read after the (possible) take-over
        if not price or price <= 0:
            price = pos.get("current_price") or pos.get("entry_price")
        if not price or price <= 0:
            return False, "no price to sell at"
        tokens_qty = pos.get("tokens_qty") or 0.0
        rem = pos["remaining_frac"] if pos["remaining_frac"] is not None else 1.0
        held = rem * tokens_qty
        if held <= _MANUAL_DUST:
            return False, "nothing held"
        if close:
            sell_tok, target_rem_tokens = held, 0.0
        elif sell_tokens is not None:
            sell_tok = min(max(0.0, sell_tokens), held)
            target_rem_tokens = held - sell_tok
        elif sell_frac is not None:
            g = min(max(0.0, sell_frac), 1.0)
            sell_tok = held * g
            target_rem_tokens = held - sell_tok
        else:
            return False, "specify sell_frac, sell_tokens, or close"
        if sell_tok <= _MANUAL_DUST:
            return False, "sell amount too small"
        if self.pipeline is not None:                # LIVE
            if not self._can_send_live("sell"):
                return False, "live sells not available (mode/arming/gates)"
            # H3: claim before firing (see direct_buy) — a cancelled stop must never still sell
            if order_id and not self.state.claim_order(order_id, "open", "submitted"):
                return False, "order no longer open (cancelled/claimed) — not fired"
            self._pending.add(mint)
            self._manual_pending[mint] = {"op": "sell", "order_id": order_id, "is_stop": is_stop,
                                          "note": note, "close": close}
            self.state.append_event(position_id=pos["id"], mint=mint, ts=ts,
                                    event_type="MANUAL_SELL_SUBMITTED", price=price,
                                    note="manual sell submitted (awaiting confirm)")
            frac_orig = (sell_tok / tokens_qty) if tokens_qty else 0.0
            ev = Event(ts, "MANUAL_SELL", price=price, frac=frac_orig,
                       remaining_frac=max(0.0, rem - frac_orig))
            # Pass CURRENT held (not the original notional): the real executor sizes against the
            # on-chain balance and ignores this, but the dry-run path models sold = held - target.
            self.pipeline.submit(ExecJob(
                mint=mint, pid=pos["id"], stake_usd=pos.get("stake_usd") or 0.0,
                entry_price=pos.get("entry_price"), events=[ev], candle_ts=ts,
                tokens_qty=held, current_price=price, manual=True, order_id=order_id,
                target_remaining_tokens=target_rem_tokens))
            return True, "submitted"
        # PAPER
        if order_id and not self.state.claim_order(order_id, "open", "submitted"):
            return False, "order no longer open (cancelled/claimed) — not fired"    # H3
        fill = self.executor.sell_manual(mint=mint, tokens=sell_tok, price=price, ts=ts)
        self._manual_book_sell(mint, pos["id"], fill, price, ts, order_id=order_id,
                               is_stop=is_stop, note=note)
        return True, "filled"

    def mark_manual(self, mint: str, price: float) -> None:
        """Mark-to-market a manual position on a tick (no state change) so its P&L stays live."""
        pos = self.state.get_position(mint)
        if pos is None or pos.get("controller") != "manual" or pos["state"] not in _MANUAL_ACTIVE:
            return
        entry = pos.get("entry_price")
        if not entry:
            return
        rem = pos["remaining_frac"] if pos["remaining_frac"] is not None else 1.0
        pr = pos.get("proceeds_units") or 0.0
        peak = max(pos.get("peak_price") or 0.0, price)
        cm = (pr + rem * price) / entry
        stake = pos.get("stake_usd") or 0.0
        self.state.update_position(mint, current_price=price, current_multiple=cm, peak_price=peak,
                                   realized_pnl_usd=stake * (cm - 1.0))

    def finalize_manual(self, mint: str, last_price: float, ts: Optional[datetime] = None) -> None:
        """Close a dead/illiquid MANUAL position (review M4) so it can't linger holding cap/equity.

        H5 (audit 2026-07-07): on a LIVE book this must first attempt the REAL swap — booking
        held×price with no swap invents proceeds the wallet never received (a token flagged dead
        with a residual price would print fictional realized P&L). Only after repeated failed
        real-swap attempts (truly unroutable = rugged) is the residual written off with no swap,
        mirroring the algo's _dead_writeoff, with a WARN so the operator can verify on-chain."""
        ts = ts or utcnow()
        pos = self.state.get_position(mint)
        if pos is None or pos.get("controller") != "manual" or pos["state"] not in _MANUAL_ACTIVE:
            return
        if mint in self._pending:
            return                       # a swap is resolving — let it; the dead-check retries
        rem = pos["remaining_frac"] if pos["remaining_frac"] is not None else 1.0
        held = rem * (pos.get("tokens_qty") or 0.0)
        if held <= _MANUAL_DUST:
            return
        price = last_price if last_price and last_price > 0 else 0.0
        if self.pipeline is not None:                       # LIVE: try to actually sell it
            tries = self._dead_manual_tries.get(mint, 0)
            if tries < 3 and price > 0:
                if not self._can_send_live("sell"):
                    # re-audit #2: gates/mode down must DEFER, not skip straight to a no-swap
                    # writeoff with phantom held×price proceeds — sells will come back (M1
                    # already pages SELLS_DISABLED); the dead-check retries.
                    return
                self._dead_manual_tries[mint] = tries + 1
                ok, msg = self.manual_sell(mint, price=price, ts=ts, close=True,
                                           note="dead/illiquid finalize (real swap)")
                if ok:
                    return               # confirmed proceeds book through _apply_manual_result
                log.info("dead manual finalize swap for %s not fired (%s) — attempt %d/3",
                         mint, msg, tries + 1)
                return                   # the dead-check retries; write off only after 3 tries
            try:
                self.state.record_alert(
                    severity="WARN", kind="DEAD_MANUAL_WRITEOFF",
                    message=f"{mint[:6]}… manual bag written off with NO swap after "
                            f"{tries} failed sell attempts (residual {held:.6g} tokens at "
                            f"{price:.8g}) — verify on-chain")
            except Exception:
                pass
        self._dead_manual_tries.pop(mint, None)
        fill = Fill(mint, "SELL", price, held, held * price, ts=ts, note="dead/illiquid finalize")
        # close_reason=dead_writeoff so the orphan backstop treats the residue as KNOWN
        # (mirrors the algo path) instead of CRIT-ing every 6 minutes forever
        self._manual_book_sell(mint, pos["id"], fill, price, ts, is_stop=False,
                               note="dead/illiquid finalize", close_reason="dead_writeoff")

    # -- manual apply (ON THE LOOP) + booking ------------------------------ #
    def _apply_manual_result(self, result: FillResult) -> None:
        mint, meta = result.mint, self._manual_pending.pop(result.mint, {})
        order_id = result.order_id
        # M5 (audit 2026-07-07): a manual fill invalidates any candle buffered for the algo path —
        # re-feeding a stale bar hours later would merge its extremes with a fresh entry's and
        # fire a phantom instant stop/TP.
        self._buffered.pop(mint, None)
        if not result.ok:
            log.warning("manual %s job for %s failed: %s — resetting to retry",
                        meta.get("op"), mint, result.error)
            if order_id:
                # CRITICAL (Codex review): a transient swap failure must NEVER permanently abandon a
                # manual order — a stop-loss that fails once would then leave a real bag unprotected.
                # Reset to 'open' so the OrderBook retries it next candle (mirrors the algo's
                # rollback-and-retry). The consecutive-failure breaker bounds buy retries (it trips
                # the kill-switch, which blocks buys); risk-reducing SELLS keep retrying to protect.
                # H3: compare-and-swap — if the user CANCELLED while the swap was in flight, the
                # reset must not resurrect the order.
                if not self.state.claim_order(order_id, "submitted", "open",
                                              note=(f"retrying after failure: {result.error}")[:200]):
                    log.info("order %s was cancelled while in flight — not resurrecting", order_id)
            try:
                self.state.record_alert(severity="WARN", kind="MANUAL_ORDER_FAILED",
                                        message=f"manual {meta.get('op','order')} {mint[:6]}… failed "
                                                f"(will retry): {result.error}")
            except Exception:
                pass
            return
        leg = result.legs[0] if result.legs else None
        if leg is None or leg.fill is None:
            return
        fill = leg.fill
        ts = utcnow()
        if meta.get("op") == "buy":
            self._direct_buy_book(mint, result.pid, fill, ts, order_id=order_id,
                                  ticker=meta.get("ticker"), note=meta.get("note", ""))
        else:
            price = result.current_price or fill.price
            self._manual_book_sell(mint, result.pid, fill, price, ts, order_id=order_id,
                                   is_stop=meta.get("is_stop", False), note=meta.get("note", ""),
                                   force_close=meta.get("close", False))

    def _direct_buy_book(self, mint, pid, fill: Fill, ts: datetime, *, order_id=None,
                         ticker=None, note="") -> None:
        # A DIRECT BUY is a human-chosen ENTRY that the ALGO then rides per config #1 (user's choice):
        # −30% stop until secured → 3× sell 33% → ride. So we book it ENTERED + controller='algo' and
        # attach a rider in the ENTERED state from the buy price.
        entry = fill.price if fill.price and fill.price > 0 else (
            fill.usd / fill.tokens if fill.tokens else 0.0)
        tokens, stake = fill.tokens, fill.usd
        stop = self.cfg.stop_level_mult * entry
        self.state.update_position(
            mint, controller="algo", state="ENTERED", entry_at=ts.isoformat(),
            entry_price=entry, stake_usd=stake, tokens_qty=tokens, remaining_frac=1.0,
            proceeds_units=0.0, secured=0, n_tp=0, stop_price=stop,
            next_rung_mult=self.cfg.tp1_mult, next_rung_price=self.cfg.tp1_mult * entry,
            peak_price=entry, current_price=entry, current_multiple=1.0, realized_pnl_usd=0.0)
        self.state.append_event(position_id=pid, mint=mint, ts=ts, event_type="ENTER",
                                price=entry, remaining_frac=1.0,
                                note=note or fill.note or "direct buy — algo rides it (config #1)")
        snap = {"state": "ENTERED", "sig": entry, "t0": ts.timestamp(), "entry": entry,
                "stop_price": stop, "rem": 1.0, "pr": 0.0, "n_tp": 0, "lvl": self.cfg.tp1_mult,
                "secured": False, "peak_price": entry, "low_price": None}
        self.riders[mint] = TailRider.restore(self.cfg, snap)
        self.pids[mint] = pid
        self.manual_pids.pop(mint, None)
        if order_id:
            self.state.update_order(order_id, status="filled", filled_at=ts.isoformat(),
                                    position_id=pid)
        try:
            self.state.record_alert(severity="INFO", kind="MANUAL_FILL",
                                    message=f"direct BUY {ticker or mint[:6]}… ${stake:.2f} @ {entry:.6g} — algo riding")
        except Exception:
            pass

    def _manual_book_sell(self, mint, pid, fill: Fill, price: float, ts: datetime, *,
                          order_id=None, is_stop=False, note="", force_close=False,
                          close_reason: Optional[str] = None) -> None:
        pos = self.state.get_position(mint) or {}
        entry = pos.get("entry_price") or 0.0
        tokens_qty = pos.get("tokens_qty") or 0.0
        rem = pos["remaining_frac"] if pos.get("remaining_frac") is not None else 1.0
        pr = pos.get("proceeds_units") or 0.0
        stake = pos.get("stake_usd") or 0.0
        sold, usd = fill.tokens, fill.usd
        frac_orig = min((sold / tokens_qty) if tokens_qty > 0 else 0.0, rem)
        pr_new = pr + (usd / tokens_qty if tokens_qty > 0 else 0.0)
        rem_new = max(0.0, rem - frac_orig)
        # H5 (audit 2026-07-07): a CLOSE dumped everything the wallet actually held. If the fill
        # is smaller than the book's bag (operator sold some externally), the difference is a
        # PHANTOM — leaving the position open on it would mark ghost tokens forever. Close it,
        # book only the real proceeds, and tell the operator the book and wallet had diverged.
        if force_close and rem_new > _MANUAL_DUST:
            try:
                self.state.record_alert(
                    severity="WARN", kind="EXTERNAL_SELL_RECONCILED",
                    message=f"{mint[:6]}… close sold {sold:.6g} tokens but the book expected "
                            f"{rem * tokens_qty:.6g} — the difference was not in the wallet "
                            "(external sell?); position closed on real proceeds")
            except Exception:
                pass
            frac_orig, rem_new = rem, 0.0
        peak = max(pos.get("peak_price") or 0.0, price or 0.0)
        self.state.append_event(position_id=pid, mint=mint, ts=ts, event_type="MANUAL_SELL",
                                price=price, frac=frac_orig, proceeds_usd=usd,
                                remaining_frac=rem_new, note=note or fill.note or "manual sell")
        # re-audit #1: force_close alone is sufficient to CLOSE — a close whose fill was a $0
        # idempotent skip (externally emptied bag) must not fall through to the open-position
        # branch and leave a zombie (state ACTIVE, rem=0, no closed_trades row, unfreeable slot).
        if rem_new <= _MANUAL_DUST and (sold > _MANUAL_DUST or usd > 0 or force_close):
            # REAL-dollar P&L (review M1): sum the ACTUAL proceeds booked across this position's
            # sell legs — for a taken-over algo position those legs carry real on-chain proceeds,
            # so pr/entry (which mixes the algo's MODELED pr) would misstate the realized dollars.
            # This subsumes the pure-manual case (its event proceeds equal pr*entry by construction).
            prows = self.state.query(
                "SELECT COALESCE(SUM(proceeds_usd),0) AS p FROM position_events "
                "WHERE position_id=? AND event_type IN "
                "('TP','RIDE_SELL','STOP_OUT','FINALIZE','MANUAL_SELL')", (pid,))
            real_proceeds = float(prows[0]["p"] or 0.0)
            realized_mult = (real_proceeds / stake) if stake else ((pr_new / entry) if entry else 0.0)
            pnl = real_proceeds - stake
            reason = close_reason or ("manual_stop" if is_stop else "manual_close")
            st = "STOPPED" if is_stop else "EXITED"
            entry_at = from_iso(pos.get("entry_at"))
            held_h = ((ts - entry_at).total_seconds() / 3600.0) if entry_at else None
            self.state.update_position(mint, state=st, remaining_frac=0.0, proceeds_units=pr_new,
                                       realized_multiple=realized_mult, current_multiple=realized_mult,
                                       realized_pnl_usd=pnl, peak_price=peak, current_price=price,
                                       closed_at=ts.isoformat(), close_reason=reason)
            self.state.record_close(position_id=pid, mint=mint, ticker=pos.get("ticker"),
                                    entry_at=entry_at, entry_price=entry, stake_usd=stake, exit_at=ts,
                                    close_reason=reason, realized_multiple=realized_mult, pnl_usd=pnl,
                                    peak_multiple=(peak / entry if entry else None), held_hours=held_h,
                                    n_tp=(pos.get("n_tp") or 0), was_stopped=is_stop,
                                    was_secured=bool(pos.get("secured")))   # L1: carry real ladder state
            self.risk.record_realized_pnl(pnl)
            self.manual_pids.pop(mint, None)
            self._dead_manual_tries.pop(mint, None)       # H5: closed — retry counter is done
            self.riders.pop(mint, None)                   # audit #10: no algo rider may outlive a close
            self.pids.pop(mint, None)
            self._cancel_resting_orders(mint)             # a closed position cancels its resting orders
        else:
            cm = ((pr_new + rem_new * price) / entry) if entry else None
            self.state.update_position(mint, remaining_frac=rem_new, proceeds_units=pr_new,
                                       peak_price=peak, current_price=price, current_multiple=cm,
                                       realized_pnl_usd=(stake * (cm - 1.0) if cm is not None else 0.0))
        if order_id:
            self.state.update_order(order_id, status="filled", filled_at=ts.isoformat())
        try:
            self.state.record_alert(severity="INFO", kind="MANUAL_FILL",
                                    message=f"manual SELL {pos.get('ticker') or mint[:6]}… "
                                            f"{frac_orig:.0%} → ${usd:.2f}")
        except Exception:
            pass

    def mark(self, mint: str, price: float) -> None:
        """Mark-to-market an open position without advancing the state machine."""
        tr = self.riders.get(mint)
        if tr is None or tr.entry is None:
            return
        self._persist_position(mint, self.pids[mint], tr, current_price=price)

    def reanchor(self, mint: str, sig: float) -> bool:
        """Anchor fidelity: replace the ingest-time SPOT anchor with the first 1m candle
        OPEN at/after the signal — the backtest's exact `sig` definition (spot diverged
        up to 24% on real tokens). Applies ONLY while still WATCHING with no entry;
        NEVER after the dip has triggered."""
        tr = self.riders.get(mint)
        if tr is None or tr.state is not PositionState.WATCHING or tr.entry is not None:
            return False
        # SANITY GUARD: the first-1m-open should be CLOSE to the ingest spot ("up to 24%"). A wild
        # divergence means datapi returned garbage OHLC — trusting it corrupts BOTH the CALL-line
        # display and the −50% trigger anchor (this mis-anchored SAKURA to ~half its call price).
        # Keep the ingest spot in that case.
        spot = tr.sig
        if spot and spot > 0 and abs(sig / spot - 1.0) > 0.40:
            return False
        tr.sig = sig
        # Reset the dip watermark with the anchor: pre-anchor lows were measured against
        # the OLD spot anchor — kept against the new sig they produce >=100% dip readings
        # on tokens that never triggered (the "low 100%" WATCHING row).
        tr.low_price = None
        self.state.update_position(mint, signal_price=sig, low_price=None)
        return True

    # -- internals --------------------------------------------------------- #
    def _cancel_resting_orders(self, mint: str, reason: str = "position closed") -> None:
        """Cancel every resting order on a mint whose position just closed (audit #12). Without this
        an algo close (stop/finalize) leaves human sell/limit orders that fire forever against the
        closed row — a dead-token polling leak + a phantom 'open' order on the dashboard."""
        self._algo_fail_seen.discard(mint)           # audit re-verify #16: release the alert-once flag
        for o in self.state.open_orders(mint):
            self.state.update_order(o["id"], status="cancelled", note=reason)

    def _apply_event(self, mint, pid, tr: TailRider, ev) -> None:
        usd = None
        if ev.kind == "ENTER":
            stake = self.risk.size_for(self._realized_equity())
            fill = self.executor.buy(mint=mint, stake_usd=stake, entry_price=tr.entry, ts=ev.ts)
            self.state.update_position(mint, state=tr.state.value, entry_at=ev.ts.isoformat(),
                                       entry_price=tr.entry, stake_usd=stake, tokens_qty=fill.tokens,
                                       stop_price=tr.stop_price, remaining_frac=tr.rem)
        elif ev.kind in ("TP", "RIDE_SELL", "STOP_OUT", "FINALIZE"):
            pos = self.state.get_position(mint) or {}
            stake = pos.get("stake_usd") or self.risk.size_for()
            usd = self.executor.sell_event(mint=mint, stake_usd=stake, entry_price=tr.entry, event=ev).usd
        self.state.append_event(position_id=pid, mint=mint, ts=ev.ts, event_type=ev.kind,
                                price=ev.price, rung_mult=ev.rung_mult, frac=ev.frac,
                                proceeds_usd=usd, remaining_frac=ev.remaining_frac, note=ev.note)

    def _persist_position(self, mint, pid, tr: TailRider, *, current_price: float) -> None:
        pos = self.state.get_position(mint) or {}
        stake = pos.get("stake_usd")
        cur_mult = None
        if tr.entry:
            cur_mult = (tr.pr + tr.rem * current_price) / tr.entry
        self.state.update_position(
            mint, state=tr.state.value, secured=int(tr.secured), n_tp=tr.n_tp,
            next_rung_mult=tr.lvl, next_rung_price=(tr.lvl * tr.entry if tr.entry else None),
            remaining_frac=tr.rem, proceeds_units=tr.pr, peak_price=tr.peak_price,
            low_price=tr.low_price,
            stop_price=tr.stop_price, current_price=current_price, current_multiple=cur_mult,
            realized_pnl_usd=(stake * (cur_mult - 1.0) if (stake and cur_mult is not None) else 0.0),
        )

    def _finalize(self, mint, pid, tr: TailRider, ts: datetime) -> None:
        pos = self.state.get_position(mint) or {}
        stake = pos.get("stake_usd") or 0.0
        mult = tr.realized_multiple
        if tr.state is PositionState.EXPIRED or mult is None:
            self.state.update_position(mint, state="EXPIRED", closed_at=ts.isoformat(),
                                       close_reason="no_dip_within_48h")
        else:
            pnl = stake * (mult - 1.0)
            entry_at = from_iso(pos.get("entry_at"))
            held = ((ts - entry_at).total_seconds() / 3600.0) if entry_at else None
            reason = "stopped" if tr.state is PositionState.STOPPED else "rode_to_horizon"
            self.state.update_position(mint, state=tr.state.value, realized_multiple=mult,
                                       current_multiple=mult, realized_pnl_usd=pnl,
                                       closed_at=ts.isoformat(), close_reason=reason)
            self.state.record_close(position_id=pid, mint=mint, ticker=pos.get("ticker"),
                                    entry_at=entry_at, entry_price=tr.entry, stake_usd=stake, exit_at=ts,
                                    close_reason=reason, realized_multiple=mult, pnl_usd=pnl,
                                    peak_multiple=(tr.peak_price / tr.entry if tr.entry else None),
                                    held_hours=held, n_tp=tr.n_tp, was_stopped=(tr.state is PositionState.STOPPED),
                                    was_secured=tr.secured)
            self.risk.record_realized_pnl(pnl)
        self._cancel_resting_orders(mint)            # audit #12: close cancels its resting orders
        self.riders.pop(mint, None)
        self.pids.pop(mint, None)

    # -- bankroll ---------------------------------------------------------- #
    def _live_closed_pnl(self) -> float:
        """P&L of LIVE trades only, by provenance: the seed replay marks
        seen_mints.outcome='seen' while this engine always marks 'positioned'.
        Seed-replay P&L must never leak into the live account (it did on
        2026-07-03 — the equity curve jumped ~+$394 at the seed/live seam)."""
        rows = self.state.query(
            "SELECT COALESCE(SUM(c.pnl_usd),0) AS p FROM closed_trades c "
            "JOIN seen_mints s ON s.mint = c.mint WHERE s.outcome != 'seen'")
        return float(rows[0]["p"] or 0.0)

    def _realized_equity(self) -> float:
        start = float(self.state.get_system("bankroll_start_usd", "500") or 500)
        return start + self._live_closed_pnl()

    def sample_bankroll(self, *, now: Optional[datetime] = None) -> None:
        start = float(self.state.get_system("bankroll_start_usd", "500") or 500)
        realized = start + self._live_closed_pnl()
        open_pos = self.state.active_positions()
        deployed = sum((p["stake_usd"] or 0.0) for p in open_pos if p["state"] != "WATCHING")
        unreal = 0.0
        for p in open_pos:
            if p["current_multiple"] is not None and p["stake_usd"]:
                unreal += p["stake_usd"] * (p["current_multiple"] - 1.0)
        n_open = sum(1 for p in open_pos if p["state"] in ("ENTERED", "SECURED", "RIDING"))
        n_watch = sum(1 for p in open_pos if p["state"] == "WATCHING")
        # skip redundant rows: write only when something changed, or every 10 min as a heartbeat
        key = (round(realized, 2), round(unreal, 2), round(deployed, 2), n_open, n_watch)
        ts_now = (now or utcnow()).timestamp()
        if key == getattr(self, "_bk_key", None) and ts_now - getattr(self, "_bk_ts", 0.0) < 600:
            return
        self._bk_key, self._bk_ts = key, ts_now
        self.state.sample_bankroll(
            ts=now, realized_equity_usd=realized, unrealized_equity_usd=realized + unreal,
            deployed_usd=deployed, dry_powder_usd=realized - deployed, n_open=n_open,
            n_watching=n_watch, realized_pnl_cum_usd=realized - start)
