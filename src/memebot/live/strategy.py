"""The config #1 state machine — a faithful, candle-driven port of the backtest `sim`.

The reference is `scripts/stage38_ansem_dependence.py::sim` (identical loop to
`scripts/stage37_grid.py::sim`) with the locked params
``dip=0.5, sl=0.7, ftp=3.0, fsell=0.33, reentry=None``:

    wait <=48h for a -50% dip from the signal price -> buy -> hard stop at 0.70x
    entry (a -30% stop) UNTIL secured -> at 3x sell 33% (recover stake) and REMOVE
    the stop -> ride, selling 25% of the remainder at 6/12/24/48x (x2 steps) then x3
    -> no re-entry -> the residual rides to the final close (or death).

`sl=0.7` is the stop LEVEL as a fraction of entry (= a -30% stop), verified by
`scripts/verify_sl_semantics.py`. This is NOT a -70% stop.

`TailRider` is a pure, side-effect-free state machine: it consumes `Candle`s and
emits lifecycle `Event`s, and tracks proceeds internally using the sim's exact
cost constants so that a candle-replay reproduces `sim(...)[0]` to floating-point
precision. The arithmetic below is written in the SAME operation order as `sim`
so the equivalence gate is exact, not merely close.

Fidelity note: the sim's costs are hardcoded (entry x1.01, TP x0.985, stop x0.95)
and are DISTINCT from the honest-paper slip (`entry_slip=0.015`). The equivalence
gate uses these sim constants; the live PaperExecutor may re-price the actual fill.
The strategy decides *when / what fraction / at what level*; an executor decides
*at what price* the intents actually fill (see `executor.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from memebot.models import Candle

W48_SECONDS = 48 * 3600  # the sim's dip window, measured from the first candle


class PositionState(str, Enum):
    WATCHING = "WATCHING"   # waiting for the -50% dip (no capital committed)
    ENTERED = "ENTERED"     # filled at the dip; hard stop armed, not yet secured
    SECURED = "SECURED"     # first TP (3x) hit -> sold 33%, stop REMOVED
    RIDING = "RIDING"       # a subsequent rung (>=6x) has filled
    EXITED = "EXITED"       # remainder finalized at the last close / death
    STOPPED = "STOPPED"     # pre-secure stop hit
    EXPIRED = "EXPIRED"     # the dip never arrived within 48h -> no trade


# terminal states: no further processing
_TERMINAL = {PositionState.EXITED, PositionState.STOPPED, PositionState.EXPIRED}
# active (holding a position) states
_ACTIVE = {PositionState.ENTERED, PositionState.SECURED, PositionState.RIDING}


@dataclass(frozen=True)
class TailRiderConfig:
    """Config #1. Defaults reproduce `stage38 sim` EXACTLY (do not change casually)."""
    dip_trigger: float = 0.50        # enter when low <= (1 - dip_trigger) * sig   (a -50% dip)
    dip_window_h: float = 48.0       # give up if the dip doesn't arrive within this window
    stop_level_mult: float = 0.70    # hard stop at this * entry (= a -30% stop), PRE-SECURE ONLY
    tp1_mult: float = 3.0            # first take-profit rung (secures the stake)
    tp1_sell_frac: float = 0.33      # sell this fraction of ORIGINAL notional at tp1, then remove the stop
    ride_sell_frac: float = 0.25     # sell this fraction of the REMAINDER at each subsequent rung
    ride_step_x2: int = 5            # rungs double while n_tp < this, then triple (3->6->12->24->48->144...)
    # --- cost constants: the sim's hardcoded fills; defaults reproduce sim to fp precision ---
    sim_entry_cost: float = 0.01     # entry = dip_level * (1 + this)      (sim: *1.01)
    tp_cost: float = 0.015           # TP proceeds *= (1 - this)           (sim: *0.985)
    stop_cost: float = 0.05          # stop proceeds *= (1 - this)         (sim: *0.95)

    @property
    def window_seconds(self) -> float:
        return self.dip_window_h * 3600.0


@dataclass
class Event:
    """One lifecycle transition emitted by the machine (persisted to position_events)."""
    ts: datetime
    kind: str                    # SIGNAL|ENTER|STOP_OUT|TP|SECURE|RIDE_SELL|MARK|EXPIRE|FINALIZE
    price: float = 0.0           # the level/fill price of the event
    frac: float = 0.0            # fraction of ORIGINAL notional transacted at this event
    rung_mult: Optional[float] = None
    remaining_frac: float = 0.0  # remaining fraction after the event
    proceeds: float = 0.0        # price-units contribution to `pr` (frac * price * cost-factor)
    note: str = ""


@dataclass
class TailRider:
    """One instance per token. Pure state machine; no network, DB, or clock access.

    Construct with the config and (optionally) the signal price/time. If `sig`/`t0`
    are not given they are taken from the first candle fed — matching the backtest,
    which uses `sig = cds[0].open` and `T[0] = cds[0].ts` (the first candle at/after
    the call). Feed candles in time order via `on_candle`, then call `finalize`.
    """
    cfg: TailRiderConfig = field(default_factory=TailRiderConfig)
    sig: Optional[float] = None            # signal price (first candle open)
    t0: Optional[float] = None             # first-candle epoch seconds (dip window anchor)

    state: PositionState = PositionState.WATCHING
    entry: Optional[float] = None          # filled entry price = dip_level * (1 + sim_entry_cost)
    stop_price: Optional[float] = None      # stop_level_mult * entry while unsecured, else None
    rem: float = 1.0                        # remaining fraction of the original notional
    pr: float = 0.0                         # accumulated proceeds in price-units (sim's `pr`)
    n_tp: int = 0                           # take-profits taken (sim's `ntp`)
    lvl: float = 0.0                        # next rung multiple (sim's `lvl`); set to tp1_mult at entry
    secured: bool = False                   # first TP taken -> stop removed (sim's `sec`)
    peak_price: float = 0.0
    low_price: Optional[float] = None       # lowest low seen (the dip watermark; display-only)

    def __post_init__(self) -> None:
        self.lvl = self.cfg.tp1_mult

    # ------------------------------------------------------------------ #
    @property
    def realized_multiple(self) -> Optional[float]:
        """The trade's realized multiple `pr/entry`, or None if it never entered
        (mirrors `sim` returning None when the dip never triggers)."""
        if self.entry is None or self.entry <= 0:
            return None
        return self.pr / self.entry

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL

    # ------------------------------------------------------------------ #
    def on_candle(self, c: Candle, *, single_exec: bool = False) -> list[Event]:
        """Advance the machine by one bar. Returns the events produced this bar.

        `single_exec` (LIVE only, default off → paper/sim unchanged): emit AT MOST ONE
        execution-bearing event (ENTER or one TP/RIDE_SELL rung or STOP_OUT) per call, so the
        live pipeline places exactly one swap per job. The caller re-feeds the same candle to
        collect the next leg, so a bar that clears several rungs is executed one confirmed swap
        at a time — a failed leg can never strand an already-landed one."""
        events: list[Event] = []
        if self.is_terminal:
            return events

        ts = c.ts.timestamp()
        if self.t0 is None:
            self.t0 = ts
        if self.sig is None:
            self.sig = c.open
        if self.low_price is None or c.low < self.low_price:
            self.low_price = c.low          # passive watermark; never affects decisions

        if self.state is PositionState.WATCHING:
            # sim dip-scan: `if T[j]-T[0] > W48: break` (checked BEFORE the dip on that bar)
            if ts - self.t0 > self.cfg.window_seconds:
                self.state = PositionState.EXPIRED
                events.append(Event(c.ts, "EXPIRE", note="dip did not arrive within window"))
                return events
            dip_level = (1.0 - self.cfg.dip_trigger) * self.sig
            if c.low <= dip_level:
                # ENTER at the dip; sim: entry = (1-dip)*sig*1.01
                self.entry = dip_level * (1.0 + self.cfg.sim_entry_cost)
                self.stop_price = self.cfg.stop_level_mult * self.entry
                self.peak_price = self.entry
                self.state = PositionState.ENTERED
                events.append(Event(c.ts, "ENTER", price=self.entry, remaining_frac=self.rem,
                                    note="-50% dip filled; hard stop armed"))
                if single_exec:
                    return events        # LIVE: defer this bar's stop/TP to the re-fed candle
                # fall through: the sim's leg loop processes THIS same candle (j == start)
            else:
                return events

        if self.state in _ACTIVE:
            events.extend(self._process_bar(c, single_rung=single_exec))
        return events

    def _process_bar(self, c: Candle, *, single_rung: bool = False) -> list[Event]:
        """Stop-before-TP within one bar (pessimistic), matching `sim`'s inner loop."""
        events: list[Event] = []
        if self.rem <= 1e-9:
            return events
        if c.high > self.peak_price:
            self.peak_price = c.high

        # (1) pre-secure hard stop — checked before any take-profit (pessimistic)
        if (not self.secured) and self.cfg.stop_level_mult > 0 and c.low <= self.cfg.stop_level_mult * self.entry:
            # sim: pr += rem * sl * entry * 0.95
            proceeds = self.rem * self.cfg.stop_level_mult * self.entry * (1.0 - self.cfg.stop_cost)
            stop_px = self.cfg.stop_level_mult * self.entry
            self.pr += proceeds
            events.append(Event(c.ts, "STOP_OUT", price=stop_px, frac=self.rem, remaining_frac=0.0,
                                proceeds=proceeds, note="-30% stop hit (unsecured)"))
            self.rem = 0.0
            self.stop_price = None
            self.state = PositionState.STOPPED
            return events

        # (2) take-profit rungs — may fill several in one bar
        while self.rem > 1e-9 and c.high >= self.lvl * self.entry:
            # sim: s = min(fsell if ntp==0 else 0.25*rem, rem)
            frac = min(self.cfg.tp1_sell_frac if self.n_tp == 0 else self.cfg.ride_sell_frac * self.rem, self.rem)
            exit_px = self.lvl * self.entry
            # sim: pr += s * lvl * entry * 0.985   (same operation order)
            proceeds = frac * self.lvl * self.entry * (1.0 - self.cfg.tp_cost)
            self.pr += proceeds
            self.rem -= frac
            self.n_tp += 1
            rung = self.lvl
            if self.n_tp == 1:
                self.secured = True
                self.stop_price = None       # stop REMOVED after securing
                self.state = PositionState.SECURED
                events.append(Event(c.ts, "TP", price=exit_px, frac=frac, rung_mult=rung,
                                    remaining_frac=self.rem, proceeds=proceeds,
                                    note="secured: sold 33% at 3x, stop removed"))
            else:
                self.state = PositionState.RIDING
                events.append(Event(c.ts, "RIDE_SELL", price=exit_px, frac=frac, rung_mult=rung,
                                    remaining_frac=self.rem, proceeds=proceeds,
                                    note=f"sold {self.cfg.ride_sell_frac:.0%} of remainder at {rung:g}x"))
            # sim: lvl = lvl*2 if ntp < 5 else lvl*3
            self.lvl = self.lvl * 2 if self.n_tp < self.cfg.ride_step_x2 else self.lvl * 3
            if single_rung:
                break                    # LIVE: one rung per call (re-fed candle collects the next)

        if self.rem <= 1e-9:
            self.state = PositionState.EXITED
        return events

    def finalize(self, last_close: float, ts: Optional[datetime] = None) -> list[Event]:
        """Value any residual at the last close (sim: `if rem>1e-9: pr += rem*C[-1]`).

        Call once after the last candle (or on token death, passing the last price).
        A never-entered machine (still WATCHING) closes to EXPIRED with no trade.
        """
        events: list[Event] = []
        if self.state in _ACTIVE and self.rem > 1e-9:
            proceeds = self.rem * last_close
            self.pr += proceeds
            events.append(Event(ts or _NO_TS, "FINALIZE", price=last_close, frac=self.rem,
                                remaining_frac=0.0, proceeds=proceeds, note="residual valued at last close"))
            self.rem = 0.0
            self.state = PositionState.EXITED
        elif self.state is PositionState.WATCHING:
            self.state = PositionState.EXPIRED
            events.append(Event(ts or _NO_TS, "EXPIRE", note="series ended before the dip arrived"))
        return events

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        """Serialize live state for SQLite persistence / restart rebuild."""
        return {
            "state": self.state.value, "sig": self.sig, "t0": self.t0,
            "entry": self.entry, "stop_price": self.stop_price, "rem": self.rem,
            "pr": self.pr, "n_tp": self.n_tp, "lvl": self.lvl,
            "secured": self.secured, "peak_price": self.peak_price,
            "low_price": self.low_price,
        }

    @classmethod
    def restore(cls, cfg: TailRiderConfig, snap: dict) -> "TailRider":
        tr = cls(cfg=cfg, sig=snap.get("sig"), t0=snap.get("t0"))
        tr.state = PositionState(snap["state"])
        tr.entry = snap.get("entry")
        tr.stop_price = snap.get("stop_price")
        tr.rem = snap.get("rem", 1.0)
        tr.pr = snap.get("pr", 0.0)
        tr.n_tp = snap.get("n_tp", 0)
        tr.lvl = snap.get("lvl", cfg.tp1_mult)
        tr.secured = snap.get("secured", False)
        tr.peak_price = snap.get("peak_price", 0.0)
        tr.low_price = snap.get("low_price")
        return tr


# A sentinel datetime is never actually used for arithmetic; finalize callers pass a real ts.
from datetime import timezone as _tz  # noqa: E402
_NO_TS = datetime(1970, 1, 1, tzinfo=_tz.utc)
