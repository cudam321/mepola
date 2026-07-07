"""OrderBook — evaluates human `orders` rows against live candles and fires them through the engine.

The dashboard API is a SEPARATE process from the engine; it can only WRITE `orders` rows. This
component (owned by the engine loop) reads those rows and executes them, so every manual order —
market or conditional — funnels through the exact same safe money path as the algo (engine.manual_buy
/ manual_sell → the off-loop pipeline in live, the PaperExecutor in paper). A "market" order is just
`trigger_type='now'`; limit/TP/SL/trailing are price/mult/drawdown triggers evaluated intrabar with
the SAME stop-before-take-profit pessimism the TailRider uses.

Discipline mirrored from the algo:
- ONE order fires per mint per candle (single_exec); the next tick/candle collects the next. A swap
  in flight for a mint (engine._pending) blocks any further firing for that mint.
- Stops fire before take-profits within a bar (pessimistic). Sells before buys.
- Fills are modeled AT the trigger level (paper); live re-prices via the real quote.
- Crash-safe: `status='submitted'` is the durable in-flight marker; the boot reconcile resolves it.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from memebot.live.state import from_iso, utcnow
from memebot.models import Candle

log = logging.getLogger("memebot.live.orders")

_ACTIVE = ("ENTERED", "SECURED", "RIDING")
_STOP_KINDS = ("stop_loss", "trailing_stop")
# reasons (substring-matched) that mean an order can NEVER fill -> cancel it (vs a transient
# gate/price/in-flight -> retry). Must match the strings engine.manual_sell / direct_buy return.
_TERMINAL_REASONS = ("no open position to sell", "nothing held", "already holding",
                     "position already closed", "already closed", "direct buys disabled",
                     "averaging-in", "sell amount too small")   # dust can never grow — cancel


class OrderBook:
    def __init__(self, state, engine, *, track_fn: Optional[Callable[[str], None]] = None):
        self.state = state
        self.engine = engine
        self.track_fn = track_fn          # ask the price feed to start tracking a mint (sweep)

    # -- per-candle evaluation (driven from the tick + true-candle paths) -- #
    def on_candle(self, mint: str, candle: Candle) -> None:
        """Mark the manual position (if any) and fire at most one triggered order for this mint."""
        try:
            if mint in self.engine._pending:
                return                     # a swap is in flight — don't stack another
            pos = self.state.get_position(mint)
            if pos and pos.get("controller") == "manual" and pos["state"] in _ACTIVE:
                self.engine.mark_manual(mint, candle.close)
            orders = self.state.open_orders(mint)
            if not orders:
                return
            live_orders = self._drop_expired(orders)
            fired: list[tuple[int, dict, float]] = []
            for o in live_orders:
                fp = self._evaluate(o, candle, pos)
                if fp is not None and fp > 0:
                    fired.append((self._priority(o), o, fp))
            if not fired:
                return
            fired.sort(key=lambda x: x[0])            # stops first, then sells, then buys
            _, order, fill_price = fired[0]
            self._fire(order, fill_price, candle.ts, pos)
        except Exception:
            log.exception("orderbook on_candle failed for %s", mint)

    # -- periodic maintenance (from the sampler) --------------------------- #
    def sweep(self) -> None:
        """Ensure every open-order + watchlist mint is tracked by the feed (so ticks drive them),
        and expire stale orders even for mints that stopped ticking."""
        try:
            mints = set(self.state.mints_with_open_orders())
            for w in self.state.watchlist():
                mints.add(w["mint"])
            if self.track_fn:
                for m in mints:
                    try:
                        self.track_fn(m)
                    except Exception:
                        pass
            self._drop_expired(self.state.open_orders())   # global expiry pass
        except Exception:
            log.exception("orderbook sweep failed")

    # -- internals --------------------------------------------------------- #
    def _drop_expired(self, orders: list[dict]) -> list[dict]:
        now = utcnow()
        live: list[dict] = []
        for o in orders:
            exp = from_iso(o["expires_at"]) if o.get("expires_at") else None
            # re-audit: never expire a 'submitted' (in-flight) order — stamping it 'expired'
            # mid-swap would break the failure-retry path (claim submitted->open misses) and
            # lie about a swap that may land. CAS from 'open' only.
            if exp and now > exp and o["status"] == "open":
                if self.state.claim_order(o["id"], "open", "expired",
                                          note="expired (order window passed)"):
                    continue
            live.append(o)
        return live

    @staticmethod
    def _priority(order: dict) -> int:
        if order["side"] == "sell":
            return 0 if order["kind"] in _STOP_KINDS else 1
        return 2                                        # buys last

    def _evaluate(self, order: dict, candle: Candle, pos: Optional[dict]) -> Optional[float]:
        """Return the modeled fill price if `order` triggers on this candle, else None."""
        tt, tv = order["trigger_type"], order["trigger_value"]
        o, hi, lo, close = candle.open, candle.high, candle.low, candle.close
        # Modeled fills are clamped to what the bar could actually give (re-audit): a trigger
        # already passed at the OPEN fills at the open, never at an off-market trigger price —
        # else a stop set 10x above market books paper proceeds at a price that never traded.
        if tt == "now":
            return close
        if tt == "price_at_or_below":
            return min(tv, o) if (tv is not None and lo <= tv) else None
        if tt == "price_at_or_above":
            return max(tv, o) if (tv is not None and hi >= tv) else None
        if tt == "mult_at_or_above":
            entry = (pos or {}).get("entry_price")
            if not entry or tv is None:
                return None
            level = tv * entry
            return max(level, o) if hi >= level else None
        if tt == "peak_drawdown_pct":
            if not pos or not pos.get("entry_price") or tv is None:
                return None
            # M10 (audit 2026-07-07): a FRESH trailing stop arms from the CURRENT price, never
            # the position's all-time peak — seeding from peak_price on a token already far off
            # its high would market-dump the full bag the instant the order is placed. First
            # candle seeds the watermark; the trail fires from the next candle on.
            if order.get("hwm") is None:
                self.state.update_order(order["id"], hwm=close)
                return None
            base = order["hwm"]
            new_hwm = max(base, hi)
            if new_hwm > base:
                self.state.update_order(order["id"], hwm=new_hwm)
            level = new_hwm * (1.0 - tv)
            return min(level, o) if lo <= level else None    # gapped-below fills at the open
        return None

    def _fire(self, order: dict, fill_price: float, ts, pos: Optional[dict]) -> None:
        oid, mint = order["id"], order["mint"]
        label = order["kind"]
        if order["side"] == "buy":
            usd = order["size_value"]                   # buys are always size_kind='usd'
            ok, msg = self.engine.direct_buy(mint, usd=usd, price=fill_price, ts=ts,
                                             ticker=order.get("ticker"), order_id=oid,
                                             note=f"direct {label}")
        else:
            is_stop = order["kind"] in _STOP_KINDS
            sk, sv = order["size_kind"], order["size_value"]
            kw: dict = {}
            if sk == "token_frac":
                if sv >= 0.999:
                    kw["close"] = True
                else:
                    kw["sell_frac"] = sv
            elif sk == "token_abs":
                kw["sell_tokens"] = sv
            else:                                       # usd size on a sell -> treat as close guard
                kw["close"] = True
            ok, msg = self.engine.manual_sell(mint, price=fill_price, ts=ts, order_id=oid,
                                              is_stop=is_stop, note=f"manual {label}", **kw)
        if not ok:
            # transient (kill/gate/price/in-flight) -> leave open to retry; terminal -> cancel
            if any(r in (msg or "").lower() for r in _TERMINAL_REASONS):
                self.state.update_order(oid, status="cancelled", note=msg)
            else:
                log.info("order %d not fired (%s) — will retry", oid, msg)
