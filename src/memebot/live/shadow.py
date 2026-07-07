"""Champion/challenger FORWARD shadow race — honest adaptation, part (a).

Every challenger config sees the SAME live ticks as the champion, at the same moment,
with the same fill model. Nothing here can look ahead, because the future hasn't happened
yet when a rider decides. This is the only form of "which config is better?" evidence this
project trusts without a fight: this research killed SIX overfit artifacts (lookahead,
undefined-mean lottery, bottom-catching, resampling, single-regime, fill-fragility), and
every one of them would have been impossible in a forward race.

What this module deliberately does NOT do:
  * It never trades. Challengers are bookkeeping only; the champion engine is untouched.
  * It never promotes. Comparing forward records is the dashboard human's job, gated by
    the same measurement bar as the research (see live/research.py and docs/ADAPTIVE.md).
  * It never breaks the champion: every public entry point is exception-proof (log +
    one alert, continue). A poisoned rider is dropped, not allowed to poison the tick path.

Semantics are pinned to the golden `sim` (tests/sim_oracle.py == stage37/38's `sim`):
  * dip>0 configs delegate to the live `TailRider` (already fp-equal to `sim`).
  * dip==0 ("chase" control): enter on the FIRST candle at `sig * 1.01` (sim:
    `if dip == 0: start = 0; entry = sig * 1.01`) — no dip wait, no 48h window.
  * reentry=R: after a pre-secure STOP_OUT at price `stop_px`, wait (starting the NEXT
    candle, sim's `k = eidx + 1`) for `high >= R * stop_px`, then re-enter at
    `(R * stop_px) * 1.01`; max 8 legs total (sim's `len(legs) < 8`).

shadow_trades representation: ONE ROW PER LEG. Each leg is an independent 1.0-notional
entry and its `realized_multiple` is that leg's `pr/entry` — exactly the unit stage37
`extend`s into its train/OOS lists. Config-level stats treat every row as one trade.

v2 (CHALLENGER_SET_VERSION = 2) widens the race beyond the sim's 5-param space to every
lever the research ever touched (docs/STRATEGY_AND_FINDINGS.md). C1..C10 are UNCHANGED.
  * family="entry": deeper/shallower dips (C11/C12) and the honest 1h-delay momentum-entry
    control (C13, measured 0.80x in backtest) — enter at the FIRST candle at/after
    t0 + 1h, at THAT candle's open * 1.01, no dip wait.
  * family="exit": `TrailExit` configs (C14/C15/C16) — an incremental candle-driven port
    of `analysis/exit_sim.simulate_exit` (TP ladder + trailing give-back + hard stop +
    no-new-high time stop), pinned to `simulate_exit` EXACTLY. One documented divergence
    from `sim`: exit_sim NEVER removes the hard stop after a TP; once the trail arms it
    dominates via max(hard, trail). C18 stays in the sim space (secure 1.5x / sell 50%).
  * family="gate": C17 runs #1's params but only ACTIVATES when the market is hot.
    Heat is FORWARD-SAFE by construction: a rolling record (system_state['shadow_heat'])
    of each ingested mint's first-24h peak-multiple-vs-sig, updated opportunistically
    from candles the engine already receives; heat = mean over records RESOLVED
    (ingested >24h ago) within a 14d window. Fewer than 5 resolved records -> the gate
    ABSTAINS. Gated-out calls write NO rider, so C17's n_trades lags BY DESIGN.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

from memebot.live.state import LiveState, from_iso, utcnow
from memebot.live.strategy import PositionState, TailRider, TailRiderConfig
from memebot.models import Candle

log = logging.getLogger("memebot.live.shadow")

MAX_LEGS = 8                    # sim: `while i < n and len(legs) < 8`
# audit #31: there is NO version-skip on rehydrate (restore() never reads snap['v']). FROZEN configs
# are FROZEN: changing an EXISTING id's management params (sl/ftp/fsell) corrupts its in-flight
# forward-race evidence — it needs a manual migration/wipe of that config's rows, not a version bump.
CHALLENGER_SET_VERSION = 2      # informational only (snapshotted into rows; not enforced)
DELAY_ENTRY_SECONDS = 3600.0    # delay_1h: enter at the first candle at/after t0 + 1h

# -- forward-safe market heat (the C17 regime gate) --------------------------------- #
HEAT_STATE_KEY = "shadow_heat"        # system_state key holding the rolling records
HEAT_RESOLVE_SECONDS = 24 * 3600      # a record resolves once its first 24h have fully passed
HEAT_WINDOW_SECONDS = 14 * 24 * 3600  # heat = mean over resolved records within this window
HEAT_MIN_RESOLVED = 5                 # fewer resolved records -> the gate ABSTAINS


@dataclass(frozen=True)
class Challenger:
    """One entry in the fixed, versioned challenger set.

    v1 was the sim's 5-param space. v2 adds `family` (dashboard grouping), `entry_mode`
    ("dip" = the sim's dip/chase entry; "delay_1h" = the honest 1h-mark momentum entry),
    an optional `exit_policy` (TrailExit params — replaces the TailRider ladder entirely;
    `sl/ftp/fsell` are UNUSED when set), and `heat_min` (regime gate: the rider only
    opens when the engine's forward market heat >= this at ingest; None = always).
    """
    id: str
    label: str
    dip: float                  # dip trigger (0 = no-dip immediate chase entry)
    sl: float                   # stop LEVEL as a fraction of entry (0.7 = -30% stop; 0 = no stop)
    ftp: float                  # first take-profit multiple (secures the stake)
    fsell: float                # fraction of original notional sold at ftp
    reentry: Optional[float]    # after a stop: re-enter when high >= reentry * stop price (None = never)
    family: str = "core"        # "core" | "entry" | "exit" | "gate"
    entry_mode: str = "dip"     # "dip" | "none" | "delay_1h"
    exit_policy: Optional[dict] = None   # TrailExit params: tp_ladder/stop_mult/trail_pct/trail_arm_mult/time_stop_h
    heat_min: Optional[float] = None     # activate only if market heat >= this at ingest

    @property
    def mode(self) -> str:
        """Effective entry mode; "none" = immediate chase (the sim's dip==0 branch)."""
        if self.entry_mode == "delay_1h":
            return "delay_1h"
        return "none" if self.entry_mode == "none" or self.dip == 0 else "dip"

    def tailrider_config(self) -> TailRiderConfig:
        return TailRiderConfig(dip_trigger=self.dip, stop_level_mult=self.sl,
                               tp1_mult=self.ftp, tp1_sell_frac=self.fsell)


# The FIXED challenger set. C1..C10 are the v1 core (family="core" by default, UNCHANGED —
# ids and behavior stable). C1 is the live champion; C7/C10 are controls chosen to
# DOCUMENT failure modes (chasing loses; pure hold rides ~99% of tokens to zero), not to win.
# C11..C18 are the v2 widening (see module docstring).
CHALLENGERS: tuple[Challenger, ...] = (
    Challenger("C1",  "champion #1",        dip=0.5, sl=0.7, ftp=3.0, fsell=0.33, reentry=None),
    Challenger("C2",  "#1 + re-entry",      dip=0.5, sl=0.7, ftp=3.0, fsell=0.33, reentry=3.0),
    Challenger("C3",  "deep stop -50%",     dip=0.5, sl=0.5, ftp=3.0, fsell=0.33, reentry=None),
    Challenger("C4",  "no stop",            dip=0.5, sl=0.0, ftp=3.0, fsell=0.33, reentry=None),
    Challenger("C5",  "shallow dip -30%",   dip=0.3, sl=0.7, ftp=3.0, fsell=0.33, reentry=None),
    Challenger("C6",  "early secure 2x/50", dip=0.5, sl=0.7, ftp=2.0, fsell=0.5,  reentry=None),
    Challenger("C7",  "no dip (chase)",     dip=0.0, sl=0.7, ftp=3.0, fsell=0.33, reentry=None),
    Challenger("C8",  "fast secure 1.5x",   dip=0.5, sl=0.7, ftp=1.5, fsell=0.33, reentry=None),
    Challenger("C9",  "soft everything",    dip=0.3, sl=0.5, ftp=2.0, fsell=0.33, reentry=None),
    Challenger("C10", "diamond hand",       dip=0.5, sl=0.0, ftp=1e9, fsell=0.33, reentry=None),
    # -- v2: entry family — the dip depth lever + the honest 1h-delay control ------- #
    Challenger("C11", "dip -40%",           dip=0.4, sl=0.7, ftp=3.0, fsell=0.33, reentry=None,
               family="entry"),
    Challenger("C12", "dip -60%",           dip=0.6, sl=0.7, ftp=3.0, fsell=0.33, reentry=None,
               family="entry"),
    Challenger("C13", "delay 1h entry",     dip=0.0, sl=0.7, ftp=3.0, fsell=0.33, reentry=None,
               family="entry", entry_mode="delay_1h"),   # momentum-entry control: 0.80x in backtest
    # -- v2: exit family — TrailExit ports of the research's managed exits ---------- #
    Challenger("C14", "P_MOON moonbag",     dip=0.5, sl=0.0, ftp=0.0, fsell=0.0, reentry=None,
               family="exit",
               exit_policy={"tp_ladder": [(2.0, 0.5)], "stop_mult": 0.0, "trail_pct": 0.60,
                            "trail_arm_mult": 2.0, "time_stop_h": 336.0}),
    # NOTE (exit_sim semantics, mirrored EXACTLY): the 0.7x hard stop is NEVER removed after
    # the 3x TP — exit_sim has no "secure removes the stop". Once the trail arms at 3x, its
    # level ((1-0.50)*peak >= 1.5x entry) dominates the hard stop via max(hard, trail), so in
    # practice the hard stop only bites PRE-arm — the intended "secure at 3x then trail" shape.
    Challenger("C15", "#1 + trail",         dip=0.5, sl=0.0, ftp=0.0, fsell=0.0, reentry=None,
               family="exit",
               exit_policy={"tp_ladder": [(3.0, 0.33)], "stop_mult": 0.7, "trail_pct": 0.50,
                            "trail_arm_mult": 3.0, "time_stop_h": 1e9}),
    Challenger("C16", "time discipline",    dip=0.5, sl=0.0, ftp=0.0, fsell=0.0, reentry=None,
               family="exit",               # trail_pct=1.0 -> trail level 0 (never fires)
               exit_policy={"tp_ladder": [(3.0, 0.33)], "stop_mult": 0.7, "trail_pct": 1.0,
                            "trail_arm_mult": 1e9, "time_stop_h": 168.0}),
    # -- v2: gate family — #1 traded only in a hot regime (forward-safe heat) -------- #
    Challenger("C17", "regime-gated #1",    dip=0.5, sl=0.7, ftp=3.0, fsell=0.33, reentry=None,
               family="gate", heat_min=1.5),
    # -- v2: one more sim-space exit variant ----------------------------------------- #
    Challenger("C18", "secure 1.5x/50%",    dip=0.5, sl=0.7, ftp=1.5, fsell=0.5,  reentry=None,
               family="exit"),
)

# -- user-defined challengers (dashboard "add strategy" form) ----------------------- #
# Stored as a JSON list under system_state[CUSTOM_KEY]; ids are X1, X2, ... so they can
# never collide with the fixed set. They race forward-only (riders spawn for NEW calls)
# and can never be promoted to champion (the promotion allowlist stays C1..C10).
CUSTOM_KEY = "custom_challengers"
CUSTOM_REV_KEY = "custom_challengers_rev"   # bumped on every add/delete; engine polls it
MAX_CUSTOM = 8


def challenger_from_dict(d: dict) -> Challenger:
    """Validate a user-supplied strategy definition and build a Challenger.

    Raises ValueError with a human-readable message on any bad knob. Only the sim's
    5-param space + entry_mode is exposed — exit_policy/heat gates stay code-defined.
    """
    cid = str(d.get("id") or "")
    if not (cid.startswith("X") and cid[1:].isdigit()):
        raise ValueError("id must be X<number>")
    label = str(d.get("label") or "").strip()
    if not (1 <= len(label) <= 24):
        raise ValueError("label must be 1-24 characters")
    try:
        dip = float(d.get("dip", 0.5))
        sl = float(d.get("sl", 0.7))
        ftp = float(d.get("ftp", 3.0))
        fsell = float(d.get("fsell", 0.33))
        reentry = None if d.get("reentry") in (None, "", 0) else float(d["reentry"])
    except (TypeError, ValueError):
        raise ValueError("dip/sl/ftp/fsell/reentry must be numbers") from None
    entry_mode = str(d.get("entry_mode") or "dip")
    if entry_mode not in ("dip", "none", "delay_1h"):
        raise ValueError("entry_mode must be dip / none / delay_1h")
    if not 0.0 <= dip <= 0.9:
        raise ValueError("dip must be in [0, 0.9] (0 = no dip, chase entry)")
    if sl != 0.0 and not 0.1 <= sl <= 0.95:
        raise ValueError("sl must be 0 (no stop) or in [0.10, 0.95] of entry")
    if not 1.01 <= ftp <= 1e9:
        raise ValueError("ftp (first take-profit multiple) must be >= 1.01")
    if not 0.0 < fsell <= 1.0:
        raise ValueError("fsell must be in (0, 1]")
    if reentry is not None and not 1.1 <= reentry <= 20.0:
        raise ValueError("reentry must be empty (never) or in [1.1, 20]")
    return Challenger(id=cid, label=label, dip=dip, sl=sl, ftp=ftp, fsell=fsell,
                      reentry=reentry, family="custom", entry_mode=entry_mode)


def load_custom_challengers(state: "LiveState") -> tuple[Challenger, ...]:
    """Read + validate the stored custom set; invalid entries are skipped with a log."""
    try:
        raw = state.get_system(CUSTOM_KEY)
        if not raw:
            return ()
        out = []
        for d in json.loads(raw):
            try:
                out.append(challenger_from_dict(d))
            except ValueError as e:
                log.warning("skipping invalid custom challenger %r: %s", d.get("id"), e)
        return tuple(out)
    except Exception:
        log.exception("failed to load custom challengers")
        return ()


class TrailExit:
    """Incremental, candle-driven port of `analysis/exit_sim.simulate_exit` — EXACT semantics.

    Created AT the fill (entry price + fill-bar epoch); feed it every candle from the fill
    bar on. Per bar, in `simulate_exit`'s exact order:
      1) PESSIMISTIC: stop (bar low) before take-profits. stop_level = max(hard, trail)
         where hard = stop_mult*entry (never removed) and trail = (1-trail_pct)*peak,
         armed only once peak >= trail_arm_mult*entry (peak starts AT the entry price).
         low <= stop_level -> the remainder exits at stop_level*(1-stop_cost=0.04).
      2) each tp_ladder rung (mult, frac) fills ONCE when high >= mult*entry, at
         mult*entry*(1-tp_cost=0.015).
      3) a new bar high raises `peak` and resets the no-new-high clock (emits "MARK" so
         the engine persists the decision-critical watermark).
      4) time stop: strictly more than time_stop_h hours since the last new high -> the
         remainder exits at the bar CLOSE*(1-tp_cost).
    `finalize(last_close)` mirrors exit_sim's end-of-series exit: last_close*(1-tp_cost).
    Parity is pinned by tests/test_shadow.py against `simulate_exit` on synthetic series.
    """

    def __init__(self, policy: dict, *, entry: float, t_fill: float,
                 tp_cost: float = 0.015, stop_cost: float = 0.04):
        self.entry = float(entry)
        self.tp_cost = tp_cost
        self.stop_cost = stop_cost
        self.rungs = sorted(((float(m), float(f)) for m, f in policy["tp_ladder"]),
                            key=lambda r: r[0])            # exit_sim sorts the ladder
        self.stop_mult = float(policy["stop_mult"])
        self.trail_pct = float(policy["trail_pct"])
        self.trail_arm_mult = float(policy["trail_arm_mult"])
        self.time_stop_h = float(policy["time_stop_h"])
        self.filled = [False] * len(self.rungs)
        self.rem = 1.0                                     # exit_sim's `remaining`
        self.pr = 0.0                                      # exit_sim's `proceeds` (price-units)
        self.peak = float(entry)                           # exit_sim: peak = p_fill_net
        self.last_high_ts = float(t_fill)                  # exit_sim: last_high_ts = t_fill
        self.done = False
        self.close_reason: Optional[str] = None

    @property
    def realized_multiple(self) -> float:
        return self.pr / self.entry

    def on_candle(self, c: Candle) -> list[str]:
        if self.done:
            return []
        kinds: list[str] = []
        # 1) PESSIMISTIC: stops (bar low) before take-profits — exit_sim step 1
        armed = self.peak >= self.trail_arm_mult * self.entry
        trail_level = (1.0 - self.trail_pct) * self.peak if armed else 0.0
        stop_level = max(self.stop_mult * self.entry, trail_level)
        if stop_level > 0 and c.low <= stop_level:
            self.pr += self.rem * stop_level * (1.0 - self.stop_cost)
            self.rem = 0.0
            self.done = True
            self.close_reason = "stopped"
            return ["STOP_OUT"]
        # 2) take-profit rungs (bar high) — exit_sim step 2 (each fills once)
        for i, (mult, frac) in enumerate(self.rungs):
            if not self.filled[i] and c.high >= mult * self.entry:
                sell = min(frac, self.rem)
                self.pr += sell * (mult * self.entry) * (1.0 - self.tp_cost)
                self.rem -= sell
                self.filled[i] = True
                kinds.append("TP")
        # 3) trailing peak — exit_sim step 3
        if c.high > self.peak:
            self.peak = c.high
            self.last_high_ts = c.ts.timestamp()
            kinds.append("MARK")           # force a snapshot persist: peak drives the trail
        # 4) time stop on the remainder — exit_sim step 4 (strict >)
        if self.rem > 1e-9 and (c.ts.timestamp() - self.last_high_ts) > self.time_stop_h * 3600.0:
            self.pr += self.rem * c.close * (1.0 - self.tp_cost)
            self.rem = 0.0
            self.done = True
            self.close_reason = "time_stop"
            kinds.append("TIME_STOP")
            return kinds
        if self.rem <= 1e-9:
            self.done = True
            self.close_reason = "sold_out"
        return kinds

    def finalize(self, last_close: float) -> list[str]:
        """exit_sim's tail: the remainder exits at the last traded price * (1 - tp_cost)."""
        if self.done:
            return []
        self.pr += self.rem * last_close * (1.0 - self.tp_cost)
        self.rem = 0.0
        self.done = True
        self.close_reason = "rode_to_horizon"
        return ["FINALIZE"]

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        return {"entry": self.entry, "rem": self.rem, "pr": self.pr,
                "peak": self.peak, "last_high_ts": self.last_high_ts,
                "filled": list(self.filled), "done": self.done,
                "close_reason": self.close_reason}

    @classmethod
    def restore(cls, policy: dict, snap: dict) -> "TrailExit":
        t = cls(policy, entry=snap["entry"], t_fill=snap.get("last_high_ts", 0.0))
        t.rem = snap.get("rem", 1.0)
        t.pr = snap.get("pr", 0.0)
        t.peak = snap.get("peak", t.entry)
        t.last_high_ts = snap.get("last_high_ts", 0.0)
        filled = snap.get("filled")
        if filled is not None:
            t.filled = [bool(x) for x in filled]
        t.done = bool(snap.get("done"))
        t.close_reason = snap.get("close_reason")
        return t


class ShadowRider:
    """One (config, token) shadow position. Wraps `TailRider` to add the sim behaviours
    it doesn't natively support: dip==0 immediate entry, post-stop re-entry legs, the
    1h-delay entry, and `TrailExit`-based exits (exit_policy configs run the entry watch
    here and hand the position to a `TrailExit` instead of the TailRider ladder).

    Plain configs (dip>0, reentry=None, no exit_policy) delegate 1:1 to a single
    `TailRider` leg, so the champion challenger C1 is BY CONSTRUCTION identical to the
    live engine's machine.
    """

    def __init__(self, cc: Challenger, *, sig: Optional[float] = None,
                 t0: Optional[float] = None, ticker: Optional[str] = None):
        self.cc = cc
        self.cfg = cc.tailrider_config()
        self.sig = sig
        self.t0 = t0
        self.ticker = ticker
        self.legs: list[dict] = []                 # completed legs (multiple/entered_at/closed_at/reason)
        self.flushed = 0                           # legs[:flushed] already written to shadow_trades
        self.cur: Optional[TailRider] = None       # the active leg's machine (ladder exits)
        self.trail: Optional[TrailExit] = None     # the active TrailExit (exit_policy configs)
        self.cur_entered_at: Optional[datetime] = None
        self.awaiting_target: Optional[float] = None   # re-entry trigger price, if waiting
        self.done = False
        if cc.exit_policy is None and cc.mode == "dip":
            self.cur = TailRider(cfg=self.cfg, sig=sig, t0=t0)
        # mode "none": entry is forced on the first candle fed (needs its open if sig unknown)
        # mode "delay_1h": entry is forced on the first candle at/after t0 + 1h
        # exit_policy set: this class runs the entry watch itself; TrailExit runs the exit

    # ------------------------------------------------------------------ #
    @property
    def status(self) -> str:
        if self.done:
            return "DONE"
        if self.trail is not None:
            return "TRAIL_ACTIVE"
        if self.cur is not None:
            return self.cur.state.value
        if self.awaiting_target is not None:
            return "AWAIT_REENTRY"
        return "PENDING_ENTRY"                     # chase/delay/trail-watch before entry

    def _force_enter(self, entry_price: float, ts: datetime) -> None:
        """Start a leg already ENTERED at `entry_price` (sim's dip==0 / re-entry entries)."""
        tr = TailRider(cfg=self.cfg, sig=self.sig, t0=self.t0)
        tr.entry = entry_price
        tr.stop_price = (self.cfg.stop_level_mult * entry_price
                         if self.cfg.stop_level_mult > 0 else None)
        tr.peak_price = entry_price
        tr.state = PositionState.ENTERED
        self.cur = tr
        self.cur_entered_at = ts

    def _record_leg(self, tr: TailRider, ts: Optional[datetime], reason: str) -> None:
        self._record_leg_value(tr.realized_multiple, ts, reason)

    def _record_leg_value(self, multiple: Optional[float], ts: Optional[datetime], reason: str) -> None:
        self.legs.append({
            "multiple": multiple,
            "entered_at": self.cur_entered_at.isoformat() if self.cur_entered_at else None,
            "closed_at": ts.isoformat() if ts else None,
            "close_reason": reason,
        })
        self.cur_entered_at = None

    def _settle_leg(self, ts: datetime) -> None:
        """After feeding a candle: harvest a leg that just went terminal, arm re-entry."""
        tr = self.cur
        if tr is None or not tr.is_terminal:
            return
        if tr.state is PositionState.STOPPED:
            self._record_leg(tr, ts, "stopped")
            stop_px = self.cc.sl * tr.entry        # sim: expx = sl * entry
            self.cur = None
            if self.cc.reentry is not None and len(self.legs) < MAX_LEGS:
                # sim: tgt = reentry * expx; the scan starts at the NEXT candle (k = eidx + 1),
                # which holds here because the stop candle has already been fully consumed.
                self.awaiting_target = self.cc.reentry * stop_px
            else:
                self.done = True
        elif tr.state is PositionState.EXITED:
            # remainder sold to ~0 through the ladder — sim: stp=False -> break (no re-entry)
            self._record_leg(tr, ts, "sold_out")
            self.cur = None
            self.done = True
        elif tr.state is PositionState.EXPIRED:
            # the dip never arrived (first leg only) — no trade at all
            self.cur = None
            self.done = True

    # ------------------------------------------------------------------ #
    def on_candle(self, c: Candle) -> list[str]:
        """Advance by one bar. Returns the event kinds produced (empty = uneventful tick)."""
        if self.done:
            return []
        # INVARIANT (2026-07-03): never process a candle from before the signal existed
        # (same guard as LiveEngine — a backfill bug once replayed pre-signal history).
        # Applies to EVERY entry mode (dip / chase / delay_1h / TrailExit watch).
        if self.t0 is not None and c.ts.timestamp() < self.t0:
            return []
        if self.t0 is None:
            self.t0 = c.ts.timestamp()
        if self.cc.exit_policy is not None:
            return self._trail_tick(c)
        kinds: list[str] = []
        if self.cur is None and self.awaiting_target is None and not self.legs:
            if self.cc.mode == "delay_1h":
                # honest momentum-entry control: first candle at/after t0+1h, at ITS open +1%
                if c.ts.timestamp() < self.t0 + DELAY_ENTRY_SECONDS:
                    return []
                self._force_enter(c.open * 1.01, c.ts)
                kinds.append("ENTER")
            else:
                # dip==0 first-leg entry: sim `if dip == 0: start = 0; entry = sig * 1.01`
                if self.sig is None:
                    self.sig = c.open               # mirror the backtest's sig = cds[0].open
                self._force_enter(self.sig * 1.01, c.ts)
                kinds.append("ENTER")
        elif self.cur is None and self.awaiting_target is not None:
            if c.high >= self.awaiting_target:
                self._force_enter(self.awaiting_target * 1.01, c.ts)   # sim: entry = tgt * 1.01
                self.awaiting_target = None
                kinds.append("REENTER")
            else:
                return []
        if self.cur is not None:
            evs = self.cur.on_candle(c)
            for ev in evs:
                if ev.kind == "ENTER":
                    self.cur_entered_at = c.ts
            kinds.extend(ev.kind for ev in evs)
            self._settle_leg(c.ts)
        return kinds

    def _trail_tick(self, c: Candle) -> list[str]:
        """One bar for an exit_policy config: entry watch here, exits inside `TrailExit`."""
        kinds: list[str] = []
        ts = c.ts.timestamp()
        if self.trail is None:                     # not yet entered
            mode = self.cc.mode
            if mode == "delay_1h":
                if ts < self.t0 + DELAY_ENTRY_SECONDS:
                    return []
                entry = c.open * 1.01
            elif mode == "none":
                if self.sig is None:
                    self.sig = c.open
                entry = self.sig * 1.01
            else:
                # dip watch, sim semantics: the window is checked BEFORE the dip on the bar
                if ts - self.t0 > self.cfg.window_seconds:
                    self.done = True               # dip never arrived -> no trade at all
                    return []
                if self.sig is None:
                    self.sig = c.open
                dip_level = (1.0 - self.cfg.dip_trigger) * self.sig
                if c.low > dip_level:
                    return []
                entry = dip_level * 1.01           # sim entry cost: (1-dip)*sig*1.01
            self.trail = TrailExit(self.cc.exit_policy, entry=entry, t_fill=ts)
            self.cur_entered_at = c.ts
            kinds.append("ENTER")
            # fall through: the fill bar is processed (exit_sim includes c.ts >= t_fill)
        kinds.extend(self.trail.on_candle(c))
        if self.trail.done:
            self._record_leg_value(self.trail.realized_multiple, c.ts,
                                   self.trail.close_reason or "closed")
            self.trail = None
            self.done = True                       # exit_policy configs are single-leg
        return kinds

    def finalize(self, last_price: float, ts: Optional[datetime] = None) -> list[str]:
        """Value any residual at the last price (token death / horizon). Terminal after this."""
        if self.done:
            return []
        kinds: list[str] = []
        if self.trail is not None:
            kinds.extend(self.trail.finalize(last_price))
            self._record_leg_value(self.trail.realized_multiple, ts, "rode_to_horizon")
            self.trail = None
        elif self.cur is not None:
            evs = self.cur.finalize(last_price, ts)
            kinds.extend(ev.kind for ev in evs)
            if self.cur.state is PositionState.EXITED:
                self._record_leg(self.cur, ts, "rode_to_horizon")
            # EXPIRED (still WATCHING) -> never entered -> no trade row
            self.cur = None
        # if awaiting re-entry when the series ends: sim's `if k >= n: break` — no extra leg
        self.awaiting_target = None
        self.done = True
        return kinds

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        return {
            "v": CHALLENGER_SET_VERSION, "config_id": self.cc.id,
            "sig": self.sig, "t0": self.t0, "ticker": self.ticker,
            "legs": self.legs, "flushed": self.flushed,
            "awaiting_target": self.awaiting_target, "done": self.done,
            "cur": self.cur.snapshot() if self.cur is not None else None,
            "trail": self.trail.snapshot() if self.trail is not None else None,
            "cur_entered_at": self.cur_entered_at.isoformat() if self.cur_entered_at else None,
        }

    @classmethod
    def restore(cls, cc: Challenger, snap: dict) -> "ShadowRider":
        r = cls(cc, sig=snap.get("sig"), t0=snap.get("t0"), ticker=snap.get("ticker"))
        r.legs = list(snap.get("legs") or [])
        r.flushed = int(snap.get("flushed") or 0)  # pre-flush snapshots default to 0 -> legs flush once
        r.awaiting_target = snap.get("awaiting_target")
        r.done = bool(snap.get("done"))
        cur = snap.get("cur")
        r.cur = TailRider.restore(r.cfg, cur) if cur else None
        trail = snap.get("trail")
        r.trail = TrailExit.restore(cc.exit_policy, trail) if trail else None
        r.cur_entered_at = from_iso(snap.get("cur_entered_at"))
        return r


class ShadowEngine:
    """Runs every challenger against every live token, persistently and crash-safely.

    Cheap by design: rider snapshots are upserted only on event-bearing ticks, and a
    terminal rider flushes its legs to `shadow_trades` (one row PER LEG) then frees its
    `shadow_riders` row. An EXPIRED-never-entered rider writes nothing.

    Crash-safe by design: no exception escapes into the champion path — first failure
    raises one SHADOW_ERROR alert, everything after is log-only, and a poisoned rider
    is dropped rather than re-poisoning every tick.
    """

    def __init__(self, state: LiveState, configs: Sequence[Challenger] = CHALLENGERS):
        self.state = state
        self.configs = list(configs)
        self.by_id = {c.id: c for c in self.configs}
        self.riders: dict[str, dict[str, ShadowRider]] = {}   # mint -> config_id -> rider
        self._alerted = False
        self._heat: dict[str, dict] = {}   # mint -> {"t0","sig","peak"} (system_state-backed)
        try:
            raw = state.get_system(HEAT_STATE_KEY)
            if raw:
                self._heat = json.loads(raw)
        except Exception:
            self._trip("heat load")
        try:
            self._rehydrate()
        except Exception:
            self._trip("rehydrate")

    # -- restart rebuild (same pattern as LiveEngine._rehydrate) ----------- #
    def _rehydrate(self) -> None:
        for row in self.state.load_shadow_riders():
            cc = self.by_id.get(row["config_id"])
            if cc is None:
                log.info("skipping shadow rider for unknown config %r (older challenger set?)",
                         row["config_id"])
                continue
            try:
                snap = json.loads(row["snapshot_json"] or "{}")
                r = ShadowRider.restore(cc, snap)
                # Migration + safety net: settle any legs a pre-flush snapshot buffered
                # (re-entry riders used to hoard stopped legs until final retire), and
                # retire a rider that already finished (e.g. a finalize that failed to
                # delete its row) instead of carrying it as a zombie.
                if len(r.legs) > r.flushed:
                    self._flush_legs(cc.id, row["mint"], r)
                    self.state.upsert_shadow_rider(cc.id, row["mint"], r.snapshot(), r.status)
                if r.done:
                    self._retire(cc.id, row["mint"], r)
                    continue
                self.riders.setdefault(row["mint"], {})[cc.id] = r
            except Exception:
                log.exception("failed to rehydrate shadow rider %s/%s", row["config_id"], row["mint"])

    def has_active(self, mint: str) -> bool:
        """True while any challenger still needs ticks for this mint (feed keep-alive)."""
        try:
            return bool(self.riders.get(mint))
        except Exception:
            return False

    # -- forward-safe market heat (the C17 regime gate) ---------------------- #
    def market_heat(self, now_epoch: Optional[float] = None) -> Optional[float]:
        """Mean first-24h peak-multiple-vs-sig over RESOLVED records (ingested >24h ago)
        within the 14d window; None (= ABSTAIN) with fewer than HEAT_MIN_RESOLVED.
        Forward-safe by construction: at decision time only fully-elapsed 24h windows
        of already-received candles contribute — nothing about the new call is used."""
        now = now_epoch if now_epoch is not None else utcnow().timestamp()
        vals = [r.get("peak", 0.0) for r in self._heat.values()
                if HEAT_RESOLVE_SECONDS < now - r.get("t0", 0.0) <= HEAT_WINDOW_SECONDS]
        if len(vals) < HEAT_MIN_RESOLVED:
            return None
        return float(sum(vals) / len(vals))

    def _save_heat(self) -> None:
        self.state.set_system(HEAT_STATE_KEY, json.dumps(self._heat))

    def _heat_on_ingest(self, mint: str, sig_price: Optional[float],
                        t0_epoch: Optional[float]) -> Optional[float]:
        """Compute the heat the gate decides on, then prune + register the new record."""
        now = t0_epoch if t0_epoch is not None else utcnow().timestamp()
        heat = self.market_heat(now)
        cutoff = now - HEAT_WINDOW_SECONDS
        pruned = {m: r for m, r in self._heat.items() if r.get("t0", 0.0) >= cutoff}
        if sig_price and t0_epoch is not None and mint not in pruned:
            pruned[mint] = {"t0": float(t0_epoch), "sig": float(sig_price), "peak": 0.0}
        if pruned != self._heat:
            self._heat = pruned
            self._save_heat()
        return heat

    def _heat_on_candle(self, mint: str, candle: Candle) -> None:
        """Opportunistic peak update from candles the engine already receives."""
        rec = self._heat.get(mint)
        if not rec:
            return
        sig = rec.get("sig") or 0.0
        ts = candle.ts.timestamp()
        if sig <= 0 or not (rec["t0"] <= ts <= rec["t0"] + HEAT_RESOLVE_SECONDS):
            return
        pk = candle.high / sig
        if pk > rec.get("peak", 0.0):
            rec["peak"] = pk
            self._save_heat()

    # -- lifecycle ---------------------------------------------------------- #
    def ingest(self, mint: str, sig_price: Optional[float], t0_epoch: Optional[float],
               *, ticker: Optional[str] = None) -> None:
        """Open one shadow rider per challenger for a newly accepted first-call.

        Gated challengers (heat_min set) only get a rider when the forward heat clears
        their bar; with an unknown heat (<5 resolved records) the gate ABSTAINS. Either
        way a gated-out call writes NO rider row — its n_trades lags by design."""
        try:
            if not mint or mint in self.riders:
                return
            heat = self._heat_on_ingest(mint, sig_price, t0_epoch)
            group: dict[str, ShadowRider] = {}
            for cc in self.configs:
                if cc.heat_min is not None and (heat is None or heat < cc.heat_min):
                    log.debug("heat gate: %s inactive for %s (heat=%s)", cc.id, mint, heat)
                    continue
                r = ShadowRider(cc, sig=sig_price, t0=t0_epoch, ticker=ticker)
                group[cc.id] = r
                self.state.upsert_shadow_rider(cc.id, mint, r.snapshot(), r.status)
            self.riders[mint] = group
        except Exception:
            self._trip(f"ingest {mint}")

    def reanchor(self, mint: str, sig: float) -> None:
        """Anchor fidelity (see run.py): update `sig` for riders that have NOT entered.
        Entered / legged / awaiting-re-entry riders are left untouched — their economics
        are already anchored to a real fill. delay_1h riders are NEVER re-anchored (their
        entry never keys off sig, and after entry it would rewrite real economics). The
        dip low-watermark is reset with the anchor — it was measured against the old sig.
        Crash-safe like every other entry point."""
        try:
            group = self.riders.get(mint)
            if not group:
                return
            for cid, r in group.items():
                if r.done or r.legs or r.awaiting_target is not None:
                    continue                               # already traded / trading
                if r.cc.mode == "delay_1h":
                    continue                               # sig-independent; never re-anchor
                if r.trail is not None:
                    continue                               # TrailExit entered — a real fill
                if r.cur is not None and (r.cur.state is not PositionState.WATCHING
                                          or r.cur.entry is not None):
                    continue                               # entered — never re-anchor
                r.sig = sig
                if r.cur is not None:
                    r.cur.sig = sig
                    r.cur.low_price = None                 # watermark belonged to the old anchor
                self.state.upsert_shadow_rider(cid, mint, r.snapshot(), r.status)
        except Exception:
            self._trip(f"reanchor {mint}")

    def on_candle(self, mint: str, candle: Candle) -> None:
        try:
            self._heat_on_candle(mint, candle)
        except Exception:
            self._trip(f"heat {mint}")
        group = self.riders.get(mint)
        if not group:
            return
        try:
            for cid, r in list(group.items()):
                try:
                    kinds = r.on_candle(candle)
                except Exception:
                    self._trip(f"rider {cid}/{mint}")
                    group.pop(cid, None)                      # drop the poisoned rider
                    try:
                        self._flush_legs(cid, mint, r)        # settled legs must not vanish
                    except Exception:
                        log.exception("flush on poison drop failed (%s/%s)", cid, mint)
                    self.state.delete_shadow_rider(cid, mint)
                    continue
                if r.done:
                    self._retire(cid, mint, r)
                    group.pop(cid, None)
                else:
                    # Flush any leg that just settled (re-entry configs keep racing after a
                    # stop; its loss must land in shadow_trades NOW, not at final retire).
                    flushed_now = len(r.legs) > r.flushed
                    if flushed_now:
                        self._flush_legs(cid, mint, r)
                    if kinds or flushed_now:
                        self.state.upsert_shadow_rider(cid, mint, r.snapshot(), r.status)
            if not group:
                self.riders.pop(mint, None)
        except Exception:
            self._trip(f"on_candle {mint}")

    def finalize(self, mint: str, last_price: float, ts: Optional[datetime] = None) -> None:
        """Close out every challenger for a token (death/horizon) at its last price."""
        group = self.riders.pop(mint, None)
        if not group:
            return
        try:
            ts = ts or utcnow()
            for cid, r in group.items():
                try:
                    r.finalize(last_price, ts)
                    self._retire(cid, mint, r)
                except Exception:
                    self._trip(f"finalize {cid}/{mint}")
                    try:
                        # keep the row (legs + flushed watermark) — the rehydrate safety
                        # net flushes and retires it at the next boot instead of losing it
                        self.state.upsert_shadow_rider(cid, mint, r.snapshot(), r.status)
                    except Exception:
                        self.state.delete_shadow_rider(cid, mint)
        except Exception:
            self._trip(f"finalize {mint}")

    # -- internals ----------------------------------------------------------- #
    def _retire(self, cid: str, mint: str, r: ShadowRider) -> None:
        """Terminal rider: flush any legs not already written per-leg, then free.

        F22: if a leg is still unflushed (the horizon leg finalize() just buffered with a
        wall-clock closed_at), persist the DONE snapshot BEFORE the flush + delete. Then a
        crash in the window leaves a row whose buffered leg already carries THIS closed_at;
        on reboot _rehydrate's `len(r.legs) > r.flushed` net re-flushes the SAME leg with the
        SAME timestamp, so INSERT OR IGNORE dedups it — instead of re-finalizing with a fresh
        utcnow() and double-counting the horizon leg."""
        if len(r.legs) > r.flushed:
            self.state.upsert_shadow_rider(cid, mint, r.snapshot(), r.status)
        self._flush_legs(cid, mint, r)
        self.state.delete_shadow_rider(cid, mint)

    def _flush_legs(self, cid: str, mint: str, r: ShadowRider) -> None:
        """One shadow_trades row PER LEG, written as soon as the leg settles.
        `r.flushed` guards against double-writes across ticks/restarts."""
        for leg in r.legs[r.flushed:]:
            self.state.record_shadow_trade(
                config_id=cid, mint=mint, ticker=r.ticker,
                entered_at=leg.get("entered_at"), closed_at=leg.get("closed_at"),
                realized_multiple=leg.get("multiple"), close_reason=leg.get("close_reason"))
        r.flushed = len(r.legs)

    def _trip(self, where: str) -> None:
        """Log every shadow failure; alert only once — the champion must never notice."""
        log.exception("shadow error (%s) — champion path unaffected", where)
        if not self._alerted:
            self._alerted = True
            try:
                self.state.record_alert(severity="WARN", kind="SHADOW_ERROR",
                                        message=f"shadow engine error at {where}; see logs "
                                                "(champion unaffected; further errors log-only)")
            except Exception:
                pass
