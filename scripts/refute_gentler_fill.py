#!/usr/bin/env python3
"""REFUTATION test: does a GENTLER, still-honest entry fill flip any policy?

Gentler fill = first candle CLOSE at/after t (=posted+60s) * 1.005
               (vs current: max-high in [t,t+90s] * 1.015)
Re-aggregate full denominator (1263) P_MOON & P_HOLD: mean + bootstrap CIlo +
drop-top3 + f=2% log-growth, at caps incl <=50x. Reuses warm cache.
"""
from __future__ import annotations
import sys
from datetime import timedelta
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from memebot.ingest.telegram_mcp import load_corpus_json, first_call_per_mint
from memebot.data.cache import CachedPriceClient
from memebot.data.jupiter import JupiterChartsClient
from memebot.analysis.exit_sim import simulate_exit
from stage14_untruncated import (series_to_today, entry_fill as fill_worst, LAT_S,
                                 mean_ci, drop_top, fixed_f_growth, cap_mults)
from stage4_powerlaw import P_MOON, P_HOLD


def fill_gentle(series, t):
    """First candle CLOSE at/after t, times 1.005 (gentler, still honest: no lookahead)."""
    after = [c for c in series.candles if c.ts >= t]
    if after:
        return after[0].close * 1.005
    prior = [c for c in series.candles if c.ts <= t]
    return prior[-1].close * 1.005 if prior else None


calls = first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json")))
calls = sorted(calls, key=lambda s: s.posted_at)
client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_untrunc"))

CAPS = [10.0, 25.0, 50.0, float("inf")]
rows = []  # (ts, moon_worst, hold_worst, moon_gentle, hold_gentle)
fill_ratios = []
for i, s in enumerate(calls):
    if i % 100 == 0:
        print(f"\r  {i}/{len(calls)}", end="", file=sys.stderr)
    if not s.mint:
        continue
    t = s.posted_at + timedelta(seconds=LAT_S)
    try:
        ser = series_to_today(client, s.mint, s.posted_at)
    except Exception:
        ser = None
    if not (ser and ser.candles):
        rows.append((s.posted_at.timestamp(), 0.0, 0.0, 0.0, 0.0)); continue
    fw = fill_worst(ser, t)
    fg = fill_gentle(ser, t)
    if fw is None or fw <= 0 or fg is None or fg <= 0:
        rows.append((s.posted_at.timestamp(), 0.0, 0.0, 0.0, 0.0)); continue
    fill_ratios.append(fw / fg)  # >1 means gentle is cheaper -> multiples scale up
    mw = simulate_exit(ser, fw, t, P_MOON)
    hw = simulate_exit(ser, fw, t, P_HOLD)
    mg = simulate_exit(ser, fg, t, P_MOON)
    hg = simulate_exit(ser, fg, t, P_HOLD)
    rows.append((s.posted_at.timestamp(), mw, hw, mg, hg))
print("\r" + " " * 30 + "\r", end="", file=sys.stderr)

fr = np.asarray(fill_ratios)
print(f"\n  n={len(rows)}   fill ratio worst/gentle: mean={fr.mean():.3f} median={np.median(fr):.3f} "
      f"p90={np.percentile(fr,90):.3f} max={fr.max():.3f}  (gentle is this-x cheaper => multiples scale up)")


def block(name, vals):
    print(f"\n  === {name} (gentler fill) ===  full-denominator n={len(vals)}")
    a = np.asarray(vals)
    print(f"   raw: mean={a.mean():.3f} median={np.median(a):.4f} win={100*(a>1).mean():.1f}% max={a.max():.1f}x")
    print(f"   {'cap':>6} | {'mean':>7} {'CIlo':>7} {'CIhi':>7} | {'drop3':>7} | {'f2logG':>9} | pass?")
    for cap in CAPS:
        cm = cap_mults(vals, cap)
        m, lo, hi = mean_ci(cm)
        d3 = drop_top(cm, 3)
        g2 = fixed_f_growth(cm, 0.02)
        cl = "inf" if cap == float("inf") else f"{cap:.0f}x"
        ok = (lo > 1 and d3 > 1 and g2 > 0)
        print(f"   {cl:>6} | {m:>7.3f} {lo:>7.3f} {hi:>7.3f} | {d3:>7.3f} | {g2:>+9.4f} | {'GO' if ok else 'no'}")


moon_g = [r[3] for r in rows]
hold_g = [r[4] for r in rows]
moon_w = [r[1] for r in rows]
hold_w = [r[2] for r in rows]
print(f"\n  baseline (worst fill) sanity: P_MOON mean={np.mean(moon_w):.3f}  P_HOLD mean={np.mean(hold_w):.3f}")
block("P_MOON", moon_g)
block("P_HOLD", hold_g)
