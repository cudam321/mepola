#!/usr/bin/env python3
"""REFUTATION: is the early-seat GO a bottom-catching artifact?

Re-loads the same 168 tse<=72h first-calls as stage19, recomputes fe (early fill at
t_smart+60s) and fl (late fill at t_post+60s) with the IDENTICAL logic, then runs the
bottom-catching diagnostics.
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


def pctiles(a, ps=(5, 25, 50, 75, 90, 95, 99)):
    a = np.asarray(a, dtype=float)
    return {p: float(np.percentile(a, p)) for p in ps}


def main():
    calls = sorted(first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json"))),
                   key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_earlyseat"))

    rows = []
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

        # forward windows
        te = t_smart + timedelta(seconds=S.LAT_S)
        fwd_e = [c.high for c in ser_e.candles if c.ts >= te]
        mfe_e = max(fwd_e) / fe if fwd_e else 0.0
        fwd_l = [c.high for c in ser_l.candles if c.ts >= t_post]
        mfe_l = max(fwd_l) / fl if fwd_l else 0.0

        # peak BETWEEN t_smart and t_post (the "pre-post" pump the late seat never sees)
        mid = [c.high for c in ser_e.candles if te <= c.ts < t_post]
        peak_mid = max(mid) / fe if mid else 0.0  # MFE achievable before the channel even posts
        # global peak timestamp after t_smart: before or after post?
        if fwd_e:
            gmax = max(ser_e.candles, key=lambda c: c.high if c.ts >= te else -1)
            peak_after_post = gmax.ts >= t_post
        else:
            peak_after_post = None

        # LOCAL-LOW check: fe vs the prices in the minutes/hours AFTER t_smart (excluding the fill window)
        # window 5-30 min after entry; if fe << these, the fill snapped back up = transient low.
        w_5_30 = [c.high for c in ser_e.candles
                  if te + timedelta(minutes=5) <= c.ts <= te + timedelta(minutes=30)]
        snapback_30m = (np.median(w_5_30) / fe) if w_5_30 else np.nan
        # the surrounding-candle floor: min low in +-1h around entry; fe/floor ~1 means we DID buy the low
        sur = [c for c in ser_e.candles if te - timedelta(hours=1) <= c.ts <= te + timedelta(hours=1)]
        sur_min = min((c.low for c in sur), default=np.nan)
        sur_med = np.median([c.high for c in sur]) if sur else np.nan

        # channel Entry-MC implied price (pump.fun: 1e9 supply); ratio fe / entry_price
        entry_mc = f["entry_mc"]
        entry_px = (entry_mc / 1e9) if entry_mc else np.nan

        # n candles available at/before the fill (data sparsity -> stale fallback)
        n_pre = sum(1 for c in ser_e.candles if c.ts <= te)
        n_in_win = sum(1 for c in ser_e.candles if te <= c.ts <= te + timedelta(seconds=90))

        rows.append(dict(
            mint=s.mint, tse=tse, fe=fe, fl=fl,
            ratio_lf=fl / fe, mfe_e=mfe_e, mfe_l=mfe_l,
            peak_mid=peak_mid, peak_after_post=peak_after_post,
            snapback_30m=snapback_30m, sur_min=sur_min, sur_med=sur_med,
            entry_px=entry_px, fe_over_entrypx=(fe / entry_px) if entry_px == entry_px and entry_px > 0 else np.nan,
            n_pre=n_pre, n_in_win=n_in_win,
            moon_e=simulate_exit(ser_e, fe, te, S.P_MOON),
            moon_l=simulate_exit(ser_l, fl, t_post, S.P_MOON),
            t_smart=t_smart, t_post=t_post,
        ))

    n = len(rows)
    print(f"\n=== REFUTE BOTTOM-CATCH | n={n} (tse<=72h, both seats fillable) ===")

    lf = np.array([r["ratio_lf"] for r in rows])
    print(f"\n[1] fl/fe  (realized late/early PRICE ratio — channel claims Current/Entry ~3.03x):")
    print(f"    mean={lf.mean():.3f}  median={np.median(lf):.3f}  std={lf.std():.3f}")
    print(f"    pctiles {pctiles(lf)}")
    print(f"    >> mean ({lf.mean():.2f}) vs median ({np.median(lf):.2f}): "
          f"mean/median = {lf.mean()/np.median(lf):.2f}  (>>1 => a few entries landed FAR below post price)")
    print(f"    share of tokens with fl/fe > 3: {(lf>3).mean()*100:.0f}%   > 5: {(lf>5).mean()*100:.0f}%   "
          f"> 10: {(lf>10).mean()*100:.0f}%")

    me = np.array([r["mfe_e"] for r in rows]); ml = np.array([r["mfe_l"] for r in rows])
    print(f"\n[2] early MFE distribution:")
    print(f"    mean={me.mean():.2f}  median={np.median(me):.3f}  alpha(Hill)={S.hill_alpha(list(me)):.2f}  max={me.max():.0f}x")
    print(f"    late  MFE mean={ml.mean():.2f}  median={np.median(ml):.3f}")
    print(f"    early/late MFE: by MEAN {me.mean()/ml.mean():.2f}x  by MEDIAN {np.median(me)/np.median(ml):.2f}x")
    print(f"    (if peaks were shared, early/late MFE should ~= fl/fe = {np.median(lf):.2f} median / {lf.mean():.2f} mean)")
    order = np.argsort(me)[::-1]
    top3_share = me[order[:3]].sum() / me.sum()
    top10_share = me[order[:10]].sum() / me.sum()
    print(f"    top-3 tokens = {top3_share*100:.0f}% of total early MFE;  top-10 = {top10_share*100:.0f}%")

    pm = np.array([r["peak_mid"] for r in rows])
    paf = [r["peak_after_post"] for r in rows]
    frac_before = np.mean([p is False for p in paf]) * 100
    print(f"\n[3] WHERE is the early peak? (pre-post pump the late seat structurally cannot see)")
    print(f"    tokens whose GLOBAL post-t_smart peak occurs BEFORE the channel posts: {frac_before:.0f}%")
    print(f"    pre-post MFE (max between t_smart and t_post)/fe: mean={pm.mean():.2f} median={np.median(pm):.3f} "
          f"max={pm.max():.0f}x")
    print(f"    tokens that already >=2x'd BEFORE the post: {(pm>=2).mean()*100:.0f}%   >=5x: {(pm>=5).mean()*100:.0f}%")

    sb = np.array([r["snapback_30m"] for r in rows], dtype=float)
    sb = sb[~np.isnan(sb)]
    print(f"\n[4] LOCAL-LOW / snap-back: median(high over +5..30min) / fe   (>>1 => fill was a transient low)")
    print(f"    mean={sb.mean():.3f}  median={np.median(sb):.3f}  pctiles {pctiles(sb)}")
    print(f"    share with snapback>1.5: {(sb>1.5).mean()*100:.0f}%   >3: {(sb>3).mean()*100:.0f}%")

    fep = np.array([r["fe_over_entrypx"] for r in rows], dtype=float)
    fep = fep[~np.isnan(fep)]
    print(f"\n[5] fe vs channel Entry-MC implied price (fe / (EntryMC/1e9)); n={len(fep)}")
    print(f"    mean={fep.mean():.3f}  median={np.median(fep):.3f}  pctiles {pctiles(fep)}")
    print(f"    (>>1 => our early fill is ABOVE the channel's claimed entry; <1 => below/cheaper)")

    npre = np.array([r["n_pre"] for r in rows]); nwin = np.array([r["n_in_win"] for r in rows])
    print(f"\n[6] data sparsity at the early fill:")
    print(f"    candles in the 90s fill window: mean={nwin.mean():.2f} median={np.median(nwin):.0f}  "
          f"share with 0 (stale-fallback fill)={(nwin==0).mean()*100:.0f}%")

    # ---- DECISIVE: neutralize bottom-catching and re-test the GO ----
    print(f"\n[7] DECISIVE — re-test early P_MOON @50x cap under fair-entry adjustments:")
    me_moon = np.array([r["moon_e"] for r in rows])
    all_times = np.array([r["t_post"].timestamp() for r in rows])

    def gate(keep, label):
        keep = list(keep)
        cm = S.cap_mults([me_moon[i] for i in keep], 50.0)
        tt = all_times[keep]
        m, lo, hi = S.mean_ci(cm)
        d3 = S.drop_top(cm, 3)
        g2 = S.fixed_f_growth(cm, 0.02)
        bank = S.single_pass_bankroll(cm, tt, 0.02, float("inf"))
        go = lo > 1 and d3 > 1 and g2 > 0 and bank > 500
        print(f"    {label:46} mean {m:6.3f} CIlo {lo:6.3f} drop3 {d3:6.3f} f2logG {g2:+.4f} "
              f"$500->{bank:9.0f}  {'GO' if go else 'NO-GO'}")
        return go

    gate(range(n), "baseline early P_MOON (the GO)")

    # (a) drop tokens whose peak is pre-post (lookahead pumps) -> keep only peak-after-post
    keep_a = [i for i, r in enumerate(rows) if r["peak_after_post"] is True]
    gate(keep_a, f"(a) drop pre-post-peak tokens (n={len(keep_a)})")

    # (b) drop bottom-catch tokens: fe is a transient low (snapback_30m > 1.5)
    keep_b = [i for i, r in enumerate(rows)
              if not (r["snapback_30m"] == r["snapback_30m"] and r["snapback_30m"] > 1.5)]
    gate(keep_b, f"(b) drop snapback>1.5 fills (n={len(keep_b)})")

    # (c) drop entries far below post (fl/fe > 3) — the lifts that make the median 1.41 -> a fat mean
    keep_c = [i for i, r in enumerate(rows) if r["ratio_lf"] <= 3]
    gate(keep_c, f"(c) drop fl/fe>3 tokens (n={len(keep_c)})")

    # (d) drop both pre-post-peak AND snapback (the cleanest 'no bottom-catch, no lookahead pump')
    keep_d = [i for i in keep_a if i in set(keep_b)]
    gate(keep_d, f"(d) drop pre-post-peak AND snapback (n={len(keep_d)})")

    # (e) FAIR-ENTRY re-anchor: replace fe with the price you'd realistically transact at the post,
    #     i.e. just compare to the late seat already done. Instead, neutralize the entry discount:
    #     re-price each early multiple as if entry were the MEDIAN of +-1h surrounding highs (kills
    #     any single-minute low). moon_e_fair approximated by scaling fe up to sur_med.
    fair = []
    for i, r in enumerate(rows):
        if r["sur_med"] == r["sur_med"] and r["sur_med"] > 0 and r["fe"] > 0:
            # if our fill is below the local hour-band median, we caught a low; penalize by the gap
            disc = max(r["fe"] / r["sur_med"], 1e-9)  # <1 means we filled below local median
            fair.append(r["moon_e"] * disc if disc < 1 else r["moon_e"])
        else:
            fair.append(r["moon_e"])
    fair = np.array(fair)
    cm = S.cap_mults(list(fair), 50.0)
    m, lo, hi = S.mean_ci(cm); d3 = S.drop_top(cm, 3); g2 = S.fixed_f_growth(cm, 0.02)
    bank = S.single_pass_bankroll(cm, all_times, 0.02, float("inf"))
    go = lo > 1 and d3 > 1 and g2 > 0 and bank > 500
    print(f"    {'(e) entry re-anchored to +-1h local-median high':46} mean {m:6.3f} CIlo {lo:6.3f} "
          f"drop3 {d3:6.3f} f2logG {g2:+.4f} $500->{bank:9.0f}  {'GO' if go else 'NO-GO'}")

    # print worst offenders for inspection
    print(f"\n    top-8 tokens by fl/fe (biggest early-entry 'discounts'):")
    for i in np.argsort(lf)[::-1][:8]:
        r = rows[i]
        print(f"      {r['mint'][:8]} tse={r['tse']:5.1f}h fl/fe={r['ratio_lf']:7.1f} fe={r['fe']:.2e} "
              f"mfe_e={r['mfe_e']:7.1f} peak_mid={r['peak_mid']:7.1f} snap30m={r['snapback_30m']:6.1f} "
              f"moon_e={r['moon_e']:6.2f} after_post={r['peak_after_post']} nwin={r['n_in_win']}")


if __name__ == "__main__":
    main()
