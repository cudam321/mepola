#!/usr/bin/env python3
"""ATTACK part 2: temporal clustering / regime check + jackknife fragility.

Q1: What is the date span of the FULL corpus vs the n=168 tse<=72h subset?
    Is the tse<=72h field selecting a single regime window?
Q2: In the FULL late-seat set, did Jan-2026-called tokens do unusually well?
    (if so, the early-seat GO may ride a regime, not an entry-timing edge)
Q3: How concentrated by mint? Jackknife: worst single-token leave-in.
"""
from __future__ import annotations
import sys
from datetime import timedelta
from pathlib import Path
from collections import Counter
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from memebot.ingest.telegram_mcp import load_corpus_json, first_call_per_mint
from memebot.analysis.features import extract_features
import stage14_untruncated as S

MAX_TSE_H = 72.0

def main():
    calls = sorted(first_call_per_mint(load_corpus_json(str(ROOT/"runs"/"your_channel_corpus.json"))),
                   key=lambda s: s.posted_at)
    with_mint = [s for s in calls if s.mint]
    print(f"total first-call mints in corpus: {len(with_mint)}")
    fd = [s.posted_at for s in with_mint]
    print(f"FULL corpus posted_at span: {min(fd).date()} .. {max(fd).date()}")
    # month histogram of full corpus
    mc = Counter((d.year, d.month) for d in fd)
    print("  full-corpus calls by month:")
    for k in sorted(mc):
        print(f"    {k[0]}-{k[1]:02d}: {mc[k]}")

    # tse availability
    has_tse, tse_le72 = [], []
    for s in with_mint:
        f = extract_features(s.raw_text)
        tse = f["time_since_entry_h"]
        if tse is not None:
            has_tse.append((s, tse))
            if 0.0 <= tse <= MAX_TSE_H:
                tse_le72.append((s, tse))
    print(f"\ncalls WITH a Time-Since-Entry field: {len(has_tse)}")
    print(f"calls with tse in [0,72h]: {len(tse_le72)}")
    td = [s.posted_at for s, _ in tse_le72]
    print(f"tse<=72h subset posted_at span: {min(td).date()} .. {max(td).date()}")
    tmc = Counter((d.year, d.month) for d in td)
    print("  tse<=72h subset calls by month:")
    for k in sorted(tmc):
        print(f"    {k[0]}-{k[1]:02d}: {tmc[k]}")
    # day histogram
    tdc = Counter(d.date() for d in td)
    print("  tse<=72h subset calls by DAY:")
    for k in sorted(tdc):
        print(f"    {k}: {tdc[k]}")

    # Load reconstructed multiples
    z = np.load(str(ROOT/"runs"/"attack_smalln.npz"), allow_pickle=True)
    em, lm, times, mints = z["early"], z["late"], z["times"], z["mints"]
    # mint duplication check
    mc2 = Counter(mints.tolist())
    dups = {m: c for m, c in mc2.items() if c > 1}
    print(f"\nduplicate mints in the 168 (should be 0 - first_call dedups): {len(dups)}")

    # Q3: jackknife - drop the single most influential token, recompute the whole gate
    cm = np.array(S.cap_mults(em, 50.0))
    print("\n--- JACKKNIFE: leave-one-out, find the run that most hurts CIlo ---")
    base_m = cm.mean()
    worst = []
    for i in range(len(cm)):
        sub = np.delete(cm, i)
        subt = np.delete(times, i)
        m, lo, hi = S.mean_ci(sub)
        worst.append((lo, m, i))
    worst.sort()
    for lo, m, i in worst[:5]:
        print(f"  drop idx {i:>3} (mult {cm[i]:6.2f}): remaining mean {m:.3f} CIlo {lo:.3f}")
    print(f"  -> even dropping the single most-influential token, CIlo stays {worst[0][0]:.3f}")

    # Q2 proxy: within the 168, is the win driven by the densest day(s)?
    # group by call-day, show per-day mean early multiple
    import collections
    byday = collections.defaultdict(list)
    dts = [np.datetime64(int(t), 's') for t in times]
    for t, e in zip(times, cm):
        import datetime as _dt
        day = _dt.datetime.utcfromtimestamp(t).date()
        byday[day].append(e)
    print("\n--- per-DAY early P_MOON (regime concentration within the window) ---")
    for day in sorted(byday):
        a = np.array(byday[day])
        print(f"  {day}: n={len(a):>3} mean {a.mean():6.3f} median {np.median(a):.2f} max {a.max():6.2f}")

if __name__ == "__main__":
    main()
