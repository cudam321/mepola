"""Live price feed — tick-driven: polls Jupiter for active tokens and emits every spot tick.

`PriceFeed` (async) batches all active mints into one `JupiterClient.price([...])` request per poll
(keyless ~1 req/s) and calls `on_tick(mint, price, ts)` for every present mint immediately — no
minute aggregation in the hot path. The consumer (run.py) wraps each tick in a 1-tick candle
(o=h=l=c) and advances the strategy at once, so dip/stop/TP triggers react at tick latency instead
of waiting up to ~60s for a synthetic minute bar (which was built from these same ticks and carried
zero extra information). Dead/illiquid tokens (absent from `price()` for N consecutive polls) fire
`on_dead(mint, last_price)` — but ONLY for mints that have been priced at least once: a brand-new
mint Jupiter hasn't indexed yet must not be finalized as dead ~40s after ingest (the 48h dip-window
expiry handles those).

Latency budget: poll interval 1.1s (Jupiter keyless min_interval is 1.05s — do not go below it)
plus Jupiter quote freshness => trigger reaction ~1-3s.

The sync httpx client is called via a thread so it never blocks the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from memebot.models import Candle

log = logging.getLogger("memebot.live.pricefeed")


def _floor_minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


class CandleAggregator:
    """Rolls spot ticks into 1-minute OHLC candles per mint. Pure and deterministic.

    NOTE: no longer in the hot path — PriceFeed is tick-driven now. Kept for future
    candle backfill / history use."""

    def __init__(self):
        self._cur: dict[str, dict] = {}

    def add_tick(self, mint: str, price: float, ts: datetime) -> Optional[Candle]:
        """Update the current minute bucket; return the PREVIOUS bar if the minute just rolled over."""
        minute = _floor_minute(ts)
        b = self._cur.get(mint)
        if b is None:
            self._cur[mint] = {"m": minute, "o": price, "h": price, "l": price, "c": price}
            return None
        if b["m"] == minute:
            b["h"] = max(b["h"], price)
            b["l"] = min(b["l"], price)
            b["c"] = price
            return None
        # minute rolled over -> finalize the previous bar, start a fresh one
        finalized = Candle(ts=b["m"], open=b["o"], high=b["h"], low=b["l"], close=b["c"], volume=0.0)
        self._cur[mint] = {"m": minute, "o": price, "h": price, "l": price, "c": price}
        return finalized

    def flush(self, mint: str) -> Optional[Candle]:
        """Emit and drop the current partial bar for a mint (e.g. on close)."""
        b = self._cur.pop(mint, None)
        if b is None:
            return None
        return Candle(ts=b["m"], open=b["o"], high=b["h"], low=b["l"], close=b["c"], volume=0.0)

    def drop(self, mint: str) -> None:
        self._cur.pop(mint, None)


class PriceFeed:
    """Async poller. Calls `on_tick(mint, price, ts)` for every present mint each poll and
    `on_dead(mint, price)` when a priced mint disappears from Jupiter for `dead_after_s`
    of continuous WALL-CLOCK time (not poll count — a ~44s API blip must never finalize a
    live RIDING position and forfeit the one tail the strategy exists to catch)."""

    def __init__(self, jupiter_client, *, on_tick: Callable[[str, float, datetime], None],
                 on_dead: Callable[[str, float], None],
                 interval_s: float = 1.1, dead_after_s: float = 900.0,
                 outlier_up: float = 6.0, outlier_down: float = 0.15):
        self.jc = jupiter_client
        self.on_tick = on_tick
        self.on_dead = on_dead
        self.interval_s = interval_s
        self.dead_after_s = dead_after_s               # TIME-based death threshold (F24)
        self.outlier_up = outlier_up                   # single-poll ratio guard (F28)
        self.outlier_down = outlier_down
        self._active: set[str] = set()
        self._last_price: dict[str, float] = {}
        self._absent_since: dict[str, datetime] = {}   # first poll a priced mint went missing
        self._pending: dict[str, float] = {}           # quarantined outlier awaiting confirmation
        self.last_ok_ts: Optional[datetime] = None     # last successful poll — REAL feed liveness
        self.consecutive_fail: int = 0
        self._stop = asyncio.Event()

    def track(self, mint: str) -> None:
        self._active.add(mint)
        self._absent_since.pop(mint, None)

    def untrack(self, mint: str) -> None:
        self._active.discard(mint)
        self._absent_since.pop(mint, None)
        self._pending.pop(mint, None)

    def tracked(self) -> list[str]:
        """Mints currently being polled (consumed by the true-candle reconciler)."""
        return list(self._active)

    def note_alive(self, mint: str) -> None:
        """External proof the mint still trades (e.g. the true-candle reconciler fed a fresh
        1m candle). Resets the absence timer so a transient price-API gap cannot finalize a
        token the datapi stream still sees — the F24 cross-confirmation."""
        self._absent_since.pop(mint, None)

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            await self._poll_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                pass

    async def _poll_once(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        mints = list(self._active)
        if not mints:
            self.last_ok_ts = now                      # loop is alive; nothing to poll
            return
        try:
            prices = await asyncio.to_thread(self.jc.price, mints)
        except Exception:
            # NEVER silently swallow (F33): count it, and surface a sustained outage in the
            # log. The sampler turns feed.last_ok_ts staleness into a FEED_OUTAGE alert.
            self.consecutive_fail += 1
            if self.consecutive_fail in (5, 30) or (self.consecutive_fail % 120 == 0):
                log.warning("price poll failing: %d consecutive errors", self.consecutive_fail)
            return
        self.consecutive_fail = 0
        self.last_ok_ts = now
        for mint in mints:
            px = prices.get(mint)
            if px is None or px <= 0:
                # dead-guard: only a mint that has EVER been priced can be finalized. A
                # brand-new mint Jupiter hasn't indexed yet is owned by the 48h dip expiry.
                if mint in self._last_price:
                    since = self._absent_since.get(mint)
                    if since is None:
                        self._absent_since[mint] = now
                    elif (now - since).total_seconds() >= self.dead_after_s:
                        self.on_dead(mint, self._last_price.get(mint, 0.0))
                        self._absent_since.pop(mint, None)
                continue
            self._absent_since.pop(mint, None)         # present -> not absent
            # outlier guard (F28): a single stale / cross-pool tick can fire TP1 (removing the
            # -30% stop FOREVER) or a spurious pre-secure stop-out. Quarantine an extreme
            # single-poll move until a second poll confirms it; ordinary moves (incl. the -50%
            # dip and the -30% stop) sit well inside [outlier_down, outlier_up] and pass.
            last = self._last_price.get(mint)
            if last and last > 0:
                ratio = px / last
                if ratio >= self.outlier_up or ratio <= self.outlier_down:
                    pend = self._pending.get(mint)
                    if not (pend and 0.8 <= (px / pend if pend else 0.0) <= 1.25):
                        self._pending[mint] = px       # await confirmation; drop this tick
                        continue
            self._pending.pop(mint, None)
            self._last_price[mint] = px
            try:
                self.on_tick(mint, px, now)            # per-mint guard (F05): one callback
            except Exception:                          # error can't abort the batch or feed
                log.exception("on_tick callback failed for %s", mint)
