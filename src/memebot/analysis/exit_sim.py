"""Stage B: event-driven managed-exit simulator (triple-barrier, pessimistic fills).

Per call, walk the post-fill candles and apply an exit policy: take-profit ladder +
trailing stop (moon bag) + hard stop + time stop. PESSIMISTIC intrabar convention — within
a bar we check the STOP (low) before any take-profit (high), so an ambiguous bar that spans
both resolves against us (no lookahead optimism). Per-leg exit costs; selling into a dump
(a stop hit) costs more than a clean take-profit. Dead tokens exit at their last traded
price (≈0). The output is the realized multiple the follower actually walks away with.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from memebot.models import PriceSeries


@dataclass
class ExitPolicy:
    name: str
    tp_ladder: list[tuple[float, float]]   # (price multiple of entry, fraction to sell)
    stop_mult: float                       # hard stop as fraction of entry (0.6 = -40%); 0 = no stop
    trail_pct: float                       # trailing give-back from peak (0.35 = sell 35% off the high)
    trail_arm_mult: float                  # arm trailing only after peak >= this * entry (inf = never)
    time_stop_h: float                     # exit remainder after this many hours with no new high


def simulate_exit(series: PriceSeries, p_fill_net: float, t_fill: datetime, policy: ExitPolicy,
                  *, tp_cost: float = 0.015, stop_cost: float = 0.04) -> float:
    """Return the realized multiple (proceeds / entry) under ``policy``."""
    candles = [c for c in series.candles if c.ts >= t_fill]
    if not candles or p_fill_net <= 0:
        return 0.0
    remaining = 1.0
    proceeds = 0.0
    rungs = sorted(policy.tp_ladder, key=lambda r: r[0])
    filled = [False] * len(rungs)
    peak = p_fill_net
    last_high_ts = t_fill
    hard_stop = policy.stop_mult * p_fill_net

    for c in candles:
        if remaining <= 1e-9:
            break
        # 1) PESSIMISTIC: stops (bar low) before take-profits.
        armed = peak >= policy.trail_arm_mult * p_fill_net
        trail_level = (1 - policy.trail_pct) * peak if armed else 0.0
        stop_level = max(hard_stop, trail_level)
        if stop_level > 0 and c.low <= stop_level:
            proceeds += remaining * stop_level * (1 - stop_cost)
            remaining = 0.0
            break
        # 2) take-profit rungs (bar high)
        for i, (mult, frac) in enumerate(rungs):
            if not filled[i] and c.high >= mult * p_fill_net:
                sell = min(frac, remaining)
                proceeds += sell * (mult * p_fill_net) * (1 - tp_cost)
                remaining -= sell
                filled[i] = True
        # 3) update trailing peak
        if c.high > peak:
            peak = c.high
            last_high_ts = c.ts
        # 4) time stop on the remainder
        if remaining > 1e-9 and (c.ts - last_high_ts) > timedelta(hours=policy.time_stop_h):
            proceeds += remaining * c.close * (1 - tp_cost)
            remaining = 0.0
            break

    if remaining > 1e-9:  # token died / window ended -> exit at last traded price
        proceeds += remaining * candles[-1].close * (1 - tp_cost)
    return proceeds / p_fill_net


# Representative policies from the research (P1 baseline + P2/P3/P5).
POLICIES: list[ExitPolicy] = [
    ExitPolicy("P1_buy_and_die", [], stop_mult=0.0, trail_pct=1.0, trail_arm_mult=float("inf"), time_stop_h=1e9),
    ExitPolicy("P2_principal_out_trail", [(2.0, 0.5)], stop_mult=0.5, trail_pct=0.35, trail_arm_mult=1.5, time_stop_h=12),
    ExitPolicy("P3_ladder_2_3_5", [(2.0, 0.25), (3.0, 0.25), (5.0, 0.25)], stop_mult=0.5, trail_pct=0.35, trail_arm_mult=1.5, time_stop_h=12),
    ExitPolicy("P5_aggressive_derisk", [(2.0, 0.5), (4.0, 0.3)], stop_mult=0.6, trail_pct=0.40, trail_arm_mult=1.5, time_stop_h=6),
    ExitPolicy("P5b_tight_stop_moonbag", [(2.0, 0.5), (4.0, 0.25)], stop_mult=0.7, trail_pct=0.45, trail_arm_mult=2.0, time_stop_h=6),
]
