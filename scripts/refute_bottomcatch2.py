#!/usr/bin/env python3
"""REFUTE part 2: re-sim early P_MOON with a ROBUST entry fill.

The stage19 early fill = max-high over a 90s window (+1.5% slip). But the early fills land
on THIN candles (mean 1.46 candles / 90s window; 7% have zero). A single thin low print is
not a price you could accumulate a position at. Replace it with realistic fills:
  - fill_15m_max : worst (max-high) price over the first 15 min after t_smart
  - fill_30m_med : median high over the first 30 min (a realistic average accumulation price)
A genuine early-but-stable entry is unaffected (flat price); a bottom-catch is corrected.
If the GO dies under either robust fill, the edge was bottom-catching.
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
from memebot.analysis.features import extract_features
from memebot.data.cache import CachedPriceClient
from memebot.data.jupiter import JupiterChartsClient
from memebot.analysis.exit_sim import simulate_exit
import stage14_untruncated as S

MAX_TSE_H = 72.0


def gate(mults, times, label):
    cm = S.cap_mults(list(mults), 50.0)
    m, lo, hi = S.mean_ci(cm)
    d3 = S.drop_top(cm, 3)
    g2 = S.fixed_f_growth(cm, 0.02)
    bank = S.single_pass_bankroll(cm, np.asarray(times), 0.02, float("inf"))
    go = lo > 1 and d3 > 1 and g2 > 0 and bank > 500
    print(f"  {label:40} n={len(cm):3d} mean {m:6.3f} CIlo {lo:6.3f} drop3 {d3:6.3f} "
          f"f2logG {g2:+.4f} $500->{bank:9.0f}  {'*** GO ***' if go else 'NO-GO'}")
    return go


def main():
    calls = sorted(first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json"))),
                   key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_earlyseat"))

    moon_90s, moon_15m_max, moon_30m_med, moon_5m_max = [], [], [], []
    times = []
    for s in calls:
        if not s.mint:
            continue
        f = extract_features(s.raw_text)
        tse = f["time_since_entry_h"]
        if tse is None or not (0.0 <= tse <= MAX_TSE_H):
            continue
        t_smart = s.posted_at - timedelta(hours=tse)
        te = t_smart + timedelta(seconds=S.LAT_S)
        try:
            ser_e = S.series_to_today(client, s.mint, t_smart)
        except Exception:
            ser_e = None
        if not (ser_e and ser_e.candles):
            continue
        fe_90s = S.entry_fill(ser_e, te)  # original
        # also need fl to apply the same 168-token filter as stage19
        try:
            ser_l = S.series_to_today(client, s.mint, s.posted_at)
        except Exception:
            ser_l = None
        fl = S.entry_fill(ser_l, s.posted_at + timedelta(seconds=S.LAT_S)) if (ser_l and ser_l.candles) else None
        if fe_90s is None or fe_90s <= 0 or fl is None or fl <= 0:
            continue

        w15 = [c.high for c in ser_e.candles if te <= c.ts <= te + timedelta(minutes=15)]
        w30 = [c.high for c in ser_e.candles if te <= c.ts <= te + timedelta(minutes=30)]
        w5 = [c.high for c in ser_e.candles if te <= c.ts <= te + timedelta(minutes=5)]
        fe_15m = max(w15) * 1.015 if w15 else fe_90s
        fe_30m_med = float(np.median(w30)) * 1.015 if w30 else fe_90s
        fe_5m = max(w5) * 1.015 if w5 else fe_90s

        times.append(s.posted_at.timestamp())
        moon_90s.append(simulate_exit(ser_e, fe_90s, te, S.P_MOON))
        moon_15m_max.append(simulate_exit(ser_e, fe_15m, te, S.P_MOON))
        moon_30m_med.append(simulate_exit(ser_e, fe_30m_med, te, S.P_MOON))
        moon_5m_max.append(simulate_exit(ser_e, fe_5m, te, S.P_MOON))

    print(f"\n=== ROBUST-ENTRY RE-SIM of early P_MOON @50x cap | n={len(times)} ===\n")
    gate(moon_90s, times, "fill = 90s max-high (ORIGINAL, the GO)")
    gate(moon_5m_max, times, "fill = 5min max-high")
    gate(moon_15m_max, times, "fill = 15min max-high")
    gate(moon_30m_med, times, "fill = 30min MEDIAN high")
    print("\n  (A genuine flat early entry is unchanged by widening the fill window;")
    print("   only fills that landed on a thin transient LOW move. If widening kills the GO,")
    print("   the early seat was buying sub-minute lows = bottom-catching.)")


if __name__ == "__main__":
    main()
