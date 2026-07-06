"""Live execution pipeline — runs the blocking swap OFF the event loop, applies on it.

The engine (loop thread) DECIDES and submits an `ExecJob`; a single dedicated worker thread runs the
swap (quote → build → send → confirm → parse real amounts), which is where the ~30s of `send+confirm`
lives; the result crosses back to the loop via `loop.call_soon_threadsafe(on_result, ...)` and the
engine COMMITS it there — after confirmation, on the same thread as every other DB write, so the
single-writer invariant holds with no locks. See docs/LIVE_EXECUTION_PIPELINE.md.

This module does NOT import solders or touch SQLite directly — it only calls the injected executor's
`buy`/`sell_event` (pure network + the executor's own dedicated connection) and shuttles dataclasses.
Inert by default: nothing here runs unless the orchestrator built it (mode=live).
"""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from memebot.live.executor import Fill
from memebot.live.strategy import Event

log = logging.getLogger("memebot.live.execution")

_URL_RE = re.compile(r"https?://\S+")


def scrub_secrets(s: Optional[str]) -> Optional[str]:
    """Redact any URL — especially SOLANA_RPC_URL, which embeds the provider api-key — from an error
    string before it becomes LegResult.error and is persisted to orders.note / alerts / logs (audit
    #3). Belt-and-braces behind jupiter_swap._rpc's own scrub, and it also covers any other
    secret-bearing exception a worker might raise."""
    if not s:
        return s
    rpc = os.environ.get("SOLANA_RPC_URL")
    if rpc:
        s = s.replace(rpc, "<rpc>")
    return _URL_RE.sub("<url>", s)

# The strategy events that require an on-chain swap (everything else is bookkeeping).
EXEC_KINDS = ("ENTER", "TP", "RIDE_SELL", "STOP_OUT", "FINALIZE")


@dataclass
class ExecJob:
    """One execution leg for a single mint. In live mode the machine runs in single_exec mode, so
    `events` carries exactly one EXEC_KINDS event — a failed leg can never strand a landed one.

    A MANUAL job (manual=True) is a human order riding the SAME pipeline: a buy is one ENTER leg;
    a sell is one sell leg sized by `target_remaining_tokens` (absolute idempotent target) instead
    of the algo's frac model. `order_id` links it back to the `orders` row for status bookkeeping."""
    mint: str
    pid: int
    stake_usd: float
    entry_price: Optional[float]      # modeled entry (sizing input for sells)
    events: list[Event]               # one EXEC_KINDS event (single_exec); a list for generality
    candle_ts: datetime
    tokens_qty: Optional[float] = None   # REAL entry quantity — keys idempotent fractional sells (F3)
    current_price: Optional[float] = None
    manual: bool = False
    order_id: Optional[int] = None
    target_remaining_tokens: Optional[float] = None   # manual sell: idempotent absolute target


@dataclass
class LegResult:
    event: Event
    ok: bool
    fill: Optional[Fill]
    error: Optional[str] = None


@dataclass
class FillResult:
    mint: str
    pid: int
    ok: bool                          # every leg confirmed
    legs: list[LegResult] = field(default_factory=list)
    current_price: Optional[float] = None   # mark price to persist on apply
    manual: bool = False              # route to the engine's manual-apply path
    order_id: Optional[int] = None    # the originating `orders` row

    @property
    def error(self) -> Optional[str]:
        for leg in self.legs:
            if not leg.ok:
                return leg.error
        return None


class LiveExecutionPipeline:
    """A bounded pool of execution workers draining one queue. `submit` is instant (never blocks
    the loop); `on_result` is invoked on the loop thread with a `FillResult` once a job's swap
    confirms (or fails). Workers run concurrently for DIFFERENT mints — so a dozen simultaneous
    stops don't serialize — while a single mint is never in two workers at once (the engine's
    `_pending` guard, both ops on the loop, admits at most one job per mint in flight). Shared
    state the workers touch is thread-safe: the executor's connection + breaker under its own
    lock, the Jupiter clients' rate gates + decimals cache under theirs; the slow ~30s confirm
    holds no lock. All DB writes stay on the loop via the apply consumer — single-writer intact."""

    def __init__(self, executor, on_result: Callable[[FillResult], None], *, max_workers: int = 8):
        self.executor = executor
        self.on_result = on_result           # engine.apply_fill_result — runs ON THE LOOP
        self.max_workers = max(1, int(max_workers))
        self._q: "queue.Queue[Optional[ExecJob]]" = queue.Queue()
        self._loop = None
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    def start(self, loop) -> None:
        self._loop = loop
        for i in range(self.max_workers):
            t = threading.Thread(target=self._run, name=f"live-exec-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def submit(self, job: ExecJob) -> None:
        self._q.put(job)

    def stop(self) -> None:
        self._stop.set()
        for _ in self._threads:              # unblock every worker
            self._q.put(None)

    def shutdown(self, timeout: float = 35.0) -> None:
        """Graceful drain: stop taking new jobs and give workers a bounded window to FINISH the
        swap they're mid-confirm on (a worker checks _stop only between jobs, so its current
        job runs to completion), then join. Reduces abandoned in-flight swaps on a redeploy —
        the ones that don't finish in time are still safe (restart reconcile adopts a landed
        buy; sells are idempotent)."""
        self.stop()
        for t in self._threads:
            t.join(timeout=timeout)

    def pending(self) -> int:
        return self._q.qsize()

    # -- worker thread ----------------------------------------------------- #
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                break
            try:
                result = self.execute(job)
            except Exception as e:                          # never let the worker thread die
                log.exception("execution worker error for %s", job.mint)
                # L3: propagate the routing fields so a crashed MANUAL job still applies via the
                # manual path (else it mis-routes to the algo apply, which no-ops -> the rider hangs).
                result = FillResult(job.mint, job.pid, False,
                                    [LegResult(job.events[0], False, None,
                                               scrub_secrets(f"worker crash: {e!r}"))]
                                    if job.events else [], current_price=job.current_price,
                                    manual=job.manual, order_id=job.order_id)
            try:
                self._deliver(result)                       # ALWAYS deliver — else the mint stays _pending
            except Exception:
                # if delivery fails the mint stays in engine._pending until the next restart, where the
                # submitted-intent reconcile resolves it (there is no in-process watchdog).
                log.exception("failed to deliver result for %s (cleared by restart reconcile)", job.mint)

    def execute(self, job: ExecJob) -> FillResult:
        """Place the job's swaps in order; stop at the first failure. Pure of DB/shared state."""
        legs: list[LegResult] = []
        ok = True
        for ev in job.events:
            try:
                if ev.kind == "ENTER":
                    fill = self.executor.buy(mint=job.mint, stake_usd=job.stake_usd,
                                             entry_price=job.entry_price, ts=ev.ts)
                else:
                    fill = self.executor.sell_event(mint=job.mint, stake_usd=job.stake_usd,
                                                    entry_price=job.entry_price, event=ev,
                                                    tokens_qty=job.tokens_qty,
                                                    target_remaining_tokens=job.target_remaining_tokens)
                legs.append(LegResult(ev, True, fill))
            except Exception as e:
                legs.append(LegResult(ev, False, None, scrub_secrets(repr(e))))
                ok = False
                break                         # a failed leg aborts the batch -> engine rolls back
        return FillResult(job.mint, job.pid, ok, legs, current_price=job.current_price,
                          manual=job.manual, order_id=job.order_id)

    def _deliver(self, result: FillResult) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.on_result, result)
        else:
            self.on_result(result)            # synchronous fallback (tests without a loop)
