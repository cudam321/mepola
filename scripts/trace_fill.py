#!/usr/bin/env python3
"""Hand-trace entry fill + P_MOON exit for ANSEM-B and 2 mid-winners."""
from __future__ import annotations
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from memebot.ingest.telegram_mcp import load_corpus_json, first_call_per_mint
from memebot.data.cache import CachedPriceClient
from memebot.data.jupiter import JupiterChartsClient
from stage14_untruncated import series_to_today, entry_fill, LAT_S
from stage4_powerlaw import P_MOON, P_HOLD
from memebot.analysis.exit_sim import simulate_exit

TARGETS = {
    "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump": "ANSEM-B (the runner)",
    "8Jx8AAHj86wbQgUTjGuj6GTTL5Ps3cqxKRTvpaJApump": "mid-winner #1 (MFE 88x)",
    "NV2RYH954cTJ3ckFUpvfqaQXU4ARqqDH3562nFSpump": "mid-winner #2 (MFE 143x)",
}

calls = first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json")))
bymint = {s.mint: s for s in calls if s.mint}
client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_untrunc"))


def trace_moon(series, fill, t_fill, policy):
    """Re-implement simulate_exit step logic but print the trail-stop exit moment."""
    candles = [c for c in series.candles if c.ts >= t_fill]
    remaining = 1.0
    proceeds = 0.0
    rungs = sorted(policy.tp_ladder, key=lambda r: r[0])
    filled = [False] * len(rungs)
    peak = fill
    last_high_ts = t_fill
    hard_stop = policy.stop_mult * fill
    tp_cost, stop_cost = 0.015, 0.04
    exit_reason = None
    for c in candles:
        if remaining <= 1e-9:
            break
        armed = peak >= policy.trail_arm_mult * fill
        trail_level = (1 - policy.trail_pct) * peak if armed else 0.0
        stop_level = max(hard_stop, trail_level)
        if stop_level > 0 and c.low <= stop_level:
            proceeds += remaining * stop_level * (1 - stop_cost)
            print(f"      EXIT(trail/stop) @ ts={c.ts}  bar.low={c.low:.3e} <= stop_level={stop_level:.3e}")
            print(f"        peak so far = {peak:.3e} ({peak/fill:.1f}x entry); trail give-back={policy.trail_pct:.0%}; remaining frac={remaining:.2f}")
            remaining = 0.0
            exit_reason = "trail"
            break
        for i, (mult, frac) in enumerate(rungs):
            if not filled[i] and c.high >= mult * fill:
                sell = min(frac, remaining)
                proceeds += sell * (mult * fill) * (1 - tp_cost)
                remaining -= sell
                filled[i] = True
                print(f"      TP rung {mult:.0f}x hit @ ts={c.ts}  sold {sell:.0%}")
        if c.high > peak:
            peak = c.high
            last_high_ts = c.ts
        if remaining > 1e-9 and (c.ts - last_high_ts) > timedelta(hours=policy.time_stop_h):
            proceeds += remaining * c.close * (1 - tp_cost)
            print(f"      EXIT(time-stop) @ ts={c.ts}  no new high for {policy.time_stop_h}h; close={c.close:.3e}")
            remaining = 0.0
            exit_reason = "time"
            break
    if remaining > 1e-9:
        proceeds += remaining * candles[-1].close * (1 - tp_cost)
        exit_reason = "window-end"
        print(f"      EXIT(window-end) close={candles[-1].close:.3e}")
    return proceeds / fill, exit_reason


for mint, label in TARGETS.items():
    s = bymint.get(mint)
    print("=" * 90)
    print(f"{label}  {mint}")
    if not s:
        print("  not a first-call"); continue
    t0 = s.posted_at
    t_fill = t0 + timedelta(seconds=LAT_S)
    ser = series_to_today(client, mint, t0)
    fill = entry_fill(ser, t_fill)
    print(f"  call posted_at = {t0}   entry (t0+{LAT_S:.0f}s) = {t_fill}")
    # show the 90s reaction window candles
    win = [c for c in ser.candles if t_fill <= c.ts <= t_fill + timedelta(seconds=90)]
    print(f"  reaction-window candles [{t_fill} .. +90s]: {len(win)}")
    for c in win:
        print(f"      ts={c.ts} O={c.open:.3e} H={c.high:.3e} L={c.low:.3e} C={c.close:.3e}")
    print(f"  ENTRY FILL = max(high in window)*1.015 = {fill:.4e}")
    fwd = [c.high for c in ser.candles if c.ts >= t_fill]
    mfe = max(fwd) / fill if fwd else 0.0
    peak_ts = max((c for c in ser.candles if c.ts >= t_fill), key=lambda c: c.high).ts
    print(f"  global peak = {max(fwd):.4e} at ts={peak_ts}  => MFE = {mfe:.1f}x")
    print(f"  --- P_MOON trace (sell 50%@2x, 60% trail armed@2x, 14d time-stop) ---")
    m, reason = trace_moon(ser, fill, t_fill, P_MOON)
    print(f"  P_MOON realized = {m:.3f}x  (exit reason: {reason})")
    h = simulate_exit(ser, fill, t_fill, P_HOLD)
    print(f"  P_HOLD realized = {h:.3f}x")
