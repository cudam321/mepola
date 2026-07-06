#!/usr/bin/env python3
"""ATTACK: small-n robustness + selection bias on stage19 early-seat P_MOON GO.

Reconstructs per-token early P_MOON, late P_MOON, dates, mints for the n=168 tse<=72h set,
then runs aggressive robustness:
  (1) drop-top 1/3/5/10 sensitivity (mean + bootstrap CIlo)
  (2) cumulative top-k contribution
  (3) date-half split: does the GO hold in BOTH halves?
  (4) selection vs timing: late on these 168 vs full-1263 late P_MOON
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
CAP = 50.0  # the realistic cap where the GO lives

def boot_ci(a, n=5000, seed=0):
    a = np.asarray(a, float)
    if len(a) < 2:
        return float(a.mean()), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    bs = a[rng.integers(0, len(a), size=(n, len(a)))].mean(axis=1)
    return float(a.mean()), float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))

def gate(cm, times):
    """Full stage19 gate at given cap-applied multiples."""
    m, lo, hi = S.mean_ci(cm)
    d3 = S.drop_top(cm, 3)
    g2 = S.fixed_f_growth(cm, 0.02)
    bank = S.single_pass_bankroll(cm, times, 0.02, float("inf"))
    go = lo > 1 and d3 > 1 and g2 > 0 and bank > 500
    return dict(mean=m, ci_lo=lo, drop3=d3, f2logG=g2, bank=bank, go=go)

def main():
    calls = sorted(first_call_per_mint(load_corpus_json(str(ROOT/"runs"/"your_channel_corpus.json"))),
                   key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT/"data_cache"/"jupiter_earlyseat"))

    early_moon, late_moon, times, dates, mints = [], [], [], [], []
    for s in calls:
        if not s.mint:
            continue
        f = extract_features(s.raw_text)
        tse = f["time_since_entry_h"]
        if tse is None or not (0.0 <= tse <= MAX_TSE_H):
            continue
        t_post = s.posted_at + timedelta(seconds=S.LAT_S)
        t_smart = s.posted_at - timedelta(hours=tse)
        try:
            ser_e = S.series_to_today(client, s.mint, t_smart)
            ser_l = S.series_to_today(client, s.mint, s.posted_at)
        except Exception:
            ser_e = ser_l = None
        fe = S.entry_fill(ser_e, t_smart + timedelta(seconds=S.LAT_S)) if (ser_e and ser_e.candles) else None
        fl = S.entry_fill(ser_l, t_post) if (ser_l and ser_l.candles) else None
        if fe is None or fe <= 0 or fl is None or fl <= 0:
            continue
        times.append(s.posted_at.timestamp())
        dates.append(s.posted_at)
        mints.append(s.mint)
        early_moon.append(simulate_exit(ser_e, fe, t_smart + timedelta(seconds=S.LAT_S), S.P_MOON))
        late_moon.append(simulate_exit(ser_l, fl, t_post, S.P_MOON))

    times = np.asarray(times)
    em = np.asarray(early_moon, float)
    lm = np.asarray(late_moon, float)
    n = len(em)
    print(f"\n=== RECONSTRUCTED n={n} (target 168) ===")
    cm = np.array(S.cap_mults(em, CAP))
    base = gate(cm, times)
    print(f"BASELINE early P_MOON @50x cap: mean {base['mean']:.3f} CIlo {base['ci_lo']:.3f} "
          f"drop3 {base['drop3']:.3f} f2logG {base['f2logG']:+.4f} bank {base['bank']:.0f} GO={base['go']}")
    print(f"  (stage19 json: mean 1.773 CIlo 1.258 drop3 1.316 bank 4001)")

    # ---- (1) drop-top sensitivity ----
    print("\n--- (1) DROP-TOP SENSITIVITY (recompute mean + bootstrap CIlo after dropping top-k) ---")
    order = np.argsort(cm)[::-1]  # descending
    print(f"  top-10 capped multiples: {np.round(cm[order[:10]],2).tolist()}")
    print(f"  {'k':>3} {'n':>4} {'mean':>7} {'CIlo':>7} {'drop3':>7} {'f2logG':>8} {'bank':>9} {'GO':>5}")
    for k in (0, 1, 3, 5, 10):
        keep = order[k:] if k > 0 else order
        sub = cm[keep]
        subt = times[keep]
        m, lo, hi = boot_ci(sub)
        d3 = S.drop_top(sub, 3)
        g2 = S.fixed_f_growth(sub, 0.02)
        bank = S.single_pass_bankroll(list(sub), subt, 0.02, float("inf"))
        go = lo > 1 and d3 > 1 and g2 > 0 and bank > 500
        print(f"  {k:>3} {len(sub):>4} {m:>7.3f} {lo:>7.3f} {d3:>7.3f} {g2:>+8.4f} {bank:>9.0f} {str(go):>5}")

    # ---- (2) cumulative top-k contribution ----
    print("\n--- (2) CUMULATIVE TOP-K CONTRIBUTION (to total summed capped payoff above mean=1 baseline) ---")
    total = cm.sum()
    excess = (cm - 1.0)  # contribution over breakeven
    pos_excess = excess[excess > 0].sum()
    sorted_excess = np.sort(excess)[::-1]
    print(f"  total summed capped multiple = {total:.1f}; total POSITIVE excess-over-1 = {pos_excess:.1f}")
    for k in (1, 3, 5, 10, 20):
        topk_excess = sorted_excess[:k].sum()
        print(f"  top-{k:>2} tokens = {100*topk_excess/pos_excess:5.1f}% of all positive excess-over-breakeven")
    # how many winners carry it
    nwin = int((cm > 1).sum())
    print(f"  tokens with capped mult > 1 (winners): {nwin}/{n} = {100*nwin/n:.0f}%  | "
          f"tokens == cap ({CAP:.0f}x): {int((cm>=CAP).sum())}")

    # ---- (3) date-half split ----
    print("\n--- (3) DATE-HALF SPLIT (does early P_MOON GO hold in BOTH halves?) ---")
    didx = np.argsort(times)
    half = n // 2
    for label, sel in [("EARLY half", didx[:half]), ("LATE half", didx[half:])]:
        sub = cm[sel]; subt = times[sel]
        r = gate(list(sub), subt)
        d0, d1 = dates[sel[0]], dates[sel[-1]]
        print(f"  {label:11} n={len(sub):>3} [{d0.date()}..{d1.date()}] | mean {r['mean']:.3f} "
              f"CIlo {r['ci_lo']:.3f} drop3 {r['drop3']:.3f} f2logG {r['f2logG']:+.4f} bank {r['bank']:.0f} GO={r['go']}")

    # ---- (4) selection vs timing ----
    print("\n--- (4) SELECTION vs TIMING ---")
    cl = np.array(S.cap_mults(lm, CAP))
    rl = gate(list(cl), times)
    print(f"  LATE seat on THESE 168 @50x: mean {rl['mean']:.3f} CIlo {rl['ci_lo']:.3f} "
          f"drop3 {rl['drop3']:.3f} GO={rl['go']}  (stage19 json late P_MOON mean 0.701)")
    print(f"  early/late mean ratio on same 168 = {base['mean']/rl['mean']:.2f}x  "
          f"(pure timing lift; selection cannot explain this)")

    np.savez(str(ROOT/"runs"/"attack_smalln.npz"), early=em, late=lm, times=times,
             mints=np.array(mints), dates=np.array([str(d) for d in dates]))
    print("\nsaved -> runs/attack_smalln.npz")
    return em, lm, times

if __name__ == "__main__":
    main()
