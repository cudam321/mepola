"""Follower-anchored excursion metrics (Study v2, Stage A).

For each call we measure, FROM THE FOLLOWER'S fill (not the channel's Entry MC):
  * P_fill_net  — worst price in a short post-to-fill latency window, plus an entry
    cost haircut. Conservative on purpose: the follower reads, reacts, and buys into a
    thinning pool.
  * MFE_x       — peak price reached after the call / P_fill_net (the upside *ceiling*
    a managed exit could monetize). This is the UPPER bound on follower realization;
    if even this is poor, no exit cleverness helps (Gate 0/1).
  * MAE_x       — lowest price after the call / P_fill_net (adverse heat a stop must respect).
  * E-ratio inputs — ATR-normalized MFE/MAE at fixed horizons, for the Gate-1 verdict.

CAVEAT: Stage A uses raw candle highs (not yet liquidity-gated) and a minute window, so
MFE_x is an OPTIMISTIC ceiling. If the channel fails Gate 0/1 even at this optimistic
ceiling, the conclusion is robust. Liquidity-gating + pessimistic intrabar fills come in
Stage B/C only if we get past these gates.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from memebot.models import PriceSeries, Signal


@dataclass
class Excursion:
    mint: str
    priced: bool
    p_fill_net: float = 0.0
    n_forward: int = 0
    mfe_x: float = 0.0                 # peak / fill (>=0; 1.0 = breakeven)
    mae_x: float = 1.0                 # trough / fill
    time_to_mfe_min: float = 0.0
    atr: Optional[float] = None
    mfe_at: dict[int, float] = field(default_factory=dict)      # horizon h -> peak/fill - 1
    mae_at: dict[int, float] = field(default_factory=dict)      # horizon h -> 1 - trough/fill
    mfe_atr_at: dict[int, float] = field(default_factory=dict)  # ATR-normalized
    mae_atr_at: dict[int, float] = field(default_factory=dict)
    lateness_ratio: Optional[float] = None
    time_since_entry_h: Optional[float] = None
    claimed_multiple: Optional[float] = None


def _atr(candles, n: int = 14) -> Optional[float]:
    """Average true range over the first ~n forward candles (volatility scale)."""
    if len(candles) < 2:
        return None
    trs = []
    prev_close = candles[0].close
    for c in candles[1:n + 1]:
        tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
        trs.append(tr)
        prev_close = c.close
    return sum(trs) / len(trs) if trs else None


def compute_excursion(
    signal: Signal,
    series: PriceSeries,
    *,
    entry_leg_cost: float = 0.015,
    latency_s: float = 60.0,
    horizons_h: tuple[int, ...] = (1, 4, 12),
    window_h: int = 16,
    claimed_multiple: Optional[float] = None,
    lateness_ratio: Optional[float] = None,
    time_since_entry_h: Optional[float] = None,
) -> Excursion:
    out = Excursion(mint=signal.mint or "", priced=False,
                    claimed_multiple=claimed_multiple, lateness_ratio=lateness_ratio,
                    time_since_entry_h=time_since_entry_h)
    if series.empty:
        return out

    t0 = signal.posted_at
    candles = series.candles
    ts = [c.ts for c in candles]

    # Worst (max) price over the post-to-fill latency window [t0, t0+latency].
    fill_hi = max(
        (c.high for c in candles if t0 <= c.ts <= t0 + timedelta(seconds=latency_s)),
        default=None,
    )
    if fill_hi is None:  # no candle in the latency window -> use covering candle
        i = bisect_left(ts, t0)
        if i >= len(candles):
            return out
        fill_hi = candles[i].high
    if fill_hi <= 0:
        return out
    p_fill = fill_hi * (1 + entry_leg_cost)

    # Forward candles from the call onward.
    start_i = bisect_left(ts, t0)
    forward = candles[start_i:]
    forward = [c for c in forward if c.ts <= t0 + timedelta(hours=window_h)]
    if not forward:
        return out

    out.priced = True
    out.p_fill_net = p_fill
    out.n_forward = len(forward)
    out.atr = _atr(forward)

    peak = max(c.high for c in forward)
    trough = min(c.low for c in forward)
    out.mfe_x = peak / p_fill
    out.mae_x = trough / p_fill
    peak_bar = max(forward, key=lambda c: c.high)
    out.time_to_mfe_min = (peak_bar.ts - t0).total_seconds() / 60.0

    for h in horizons_h:
        seg = [c for c in forward if c.ts <= t0 + timedelta(hours=h)]
        if not seg:
            continue
        p = max(c.high for c in seg)
        tr = min(c.low for c in seg)
        out.mfe_at[h] = p / p_fill - 1.0
        out.mae_at[h] = 1.0 - tr / p_fill
        if out.atr and out.atr > 0:
            out.mfe_atr_at[h] = (p - p_fill) / out.atr
            out.mae_atr_at[h] = (p_fill - tr) / out.atr
    return out


def e_ratio(excursions: list[Excursion], horizon_h: int) -> Optional[float]:
    """Edge ratio at a horizon = mean(MFE/ATR) / mean(MAE/ATR) across calls. >1 = edge."""
    mfe = [e.mfe_atr_at[horizon_h] for e in excursions if horizon_h in e.mfe_atr_at]
    mae = [e.mae_atr_at[horizon_h] for e in excursions if horizon_h in e.mae_atr_at and e.mae_atr_at[horizon_h] > 0]
    if not mfe or not mae:
        return None
    denom = sum(mae) / len(mae)
    return (sum(mfe) / len(mfe)) / denom if denom > 0 else None
