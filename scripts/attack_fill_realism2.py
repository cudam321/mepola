#!/usr/bin/env python3
"""ATTACK part 2: thinness-keyed fill realism + does the moon cluster in the thinnest names?

Builds on attack_fill_realism.py. Three sharper probes:
  1. Finer flat-slip break-point sweep (15..40%).
  2. MC-KEYED heterogeneous slip: thinner entry (lower Entry MC at t_smart) eats MORE slip,
     mirroring "10-40% to enter size". The killer case if winners cluster in thin names.
  3. Correlation: is the P_MOON multiple driven by the THINNEST (lowest-MC) entries? If yes,
     realistic per-name slip on exactly those names guts the edge even when a flat 25% survives.
  4. Entry+exit thin-liquidity (also worsen exit costs) sanity check.

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/attack_fill_realism2.py
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from memebot.ingest.telegram_mcp import load_corpus_json, first_call_per_mint  # noqa: E402
from memebot.analysis.features import extract_features  # noqa: E402
from memebot.data.cache import CachedPriceClient  # noqa: E402
from memebot.data.jupiter import JupiterChartsClient  # noqa: E402
from memebot.analysis.exit_sim import simulate_exit, ExitPolicy  # noqa: E402
import stage14_untruncated as S  # noqa: E402

MAX_TSE_H = 72.0


def base_maxhigh_90s(series, t):
    win = [c for c in series.candles if t <= c.ts <= t + timedelta(seconds=90)]
    if win:
        return max(c.high for c in win)
    prior = [c for c in series.candles if c.ts <= t]
    return prior[-1].high if prior else None


def mc_slip(mc):
    """Thinness-keyed entry slip from Entry MC ('10-40% to enter size')."""
    if mc is None:
        return 0.25
    if mc < 100_000:
        return 0.40
    if mc < 500_000:
        return 0.25
    if mc < 2_000_000:
        return 0.15
    return 0.08


def agg(mults, times, cap=50.0):
    cm = S.cap_mults(mults, cap)
    m, lo, hi = S.mean_ci(cm)
    d3 = S.drop_top(cm, 3)
    g2 = S.fixed_f_growth(cm, 0.02)
    bank = S.single_pass_bankroll(cm, times, 0.02, float("inf"))
    go = lo > 1 and d3 > 1 and g2 > 0 and bank > 500
    return dict(mean=m, ci_lo=lo, drop3=d3, f2logG=g2, bank=bank, go=go)


def main() -> int:
    calls = sorted(first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json"))),
                   key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4),
                               str(ROOT / "data_cache" / "jupiter_earlyseat"))

    toks = []
    n_anchored = 0
    for s in calls:
        if not s.mint:
            continue
        f = extract_features(s.raw_text)
        tse = f["time_since_entry_h"]
        if tse is None or not (0.0 <= tse <= MAX_TSE_H):
            continue
        n_anchored += 1
        if n_anchored % 25 == 0:
            print(f"\r  pricing {n_anchored}", end="", file=sys.stderr)
        t_smart = s.posted_at - timedelta(hours=tse)
        t_post = s.posted_at + timedelta(seconds=S.LAT_S)
        t_e = t_smart + timedelta(seconds=S.LAT_S)
        try:
            ser_e = S.series_to_today(client, s.mint, t_smart)
            ser_l = S.series_to_today(client, s.mint, s.posted_at)
        except Exception:
            ser_e = ser_l = None
        fe = S.entry_fill(ser_e, t_e) if (ser_e and ser_e.candles) else None
        fl = S.entry_fill(ser_l, t_post) if (ser_l and ser_l.candles) else None
        if fe is None or fe <= 0 or fl is None or fl <= 0:
            continue
        toks.append(dict(ser_e=ser_e, t_e=t_e, base=base_maxhigh_90s(ser_e, t_e),
                         ts=s.posted_at.timestamp(), entry_mc=f.get("entry_mc")))
    print("\r" + " " * 40 + "\r", end="", file=sys.stderr)

    n = len(toks)
    times = np.array([t["ts"] for t in toks])
    print("=" * 92)
    print(f"  ATTACK 2: thinness-keyed fill realism | n={n} | P_MOON @ 50x cap")
    print("=" * 92)

    # 1) finer flat-slip break point
    print("\n  (1) FINE FLAT-SLIP SWEEP (50x cap):")
    print(f"  {'slip':>6} | {'mean':>7} {'CIlo':>7} {'drop3':>7} {'f2logG':>9} {'$500':>9}  GO")
    last_go = None
    for s in (0.015, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50):
        mults = [simulate_exit(t["ser_e"], t["base"] * (1 + s), t["t_e"], S.P_MOON) for t in toks]
        r = agg(mults, times)
        flag = " GO" if r["go"] else " no"
        print(f"  {s*100:5.1f}% | {r['mean']:7.3f} {r['ci_lo']:7.3f} {r['drop3']:7.3f} "
              f"{r['f2logG']:+.4f} {r['bank']:9.0f}{flag}")
        if r["go"]:
            last_go = s
    print(f"  --> GO survives up to ~{last_go*100:.0f}% flat slip; breaks above it.")

    # 2) MC distribution + thinness-keyed slip
    mcs = [t["entry_mc"] for t in toks]
    n_known = sum(1 for m in mcs if m is not None)
    known = [m for m in mcs if m is not None]
    print(f"\n  (2) Entry-MC available for {n_known}/{n}. "
          f"MC pctiles: p10={np.percentile(known,10)/1e3:.0f}k p50={np.percentile(known,50)/1e3:.0f}k "
          f"p90={np.percentile(known,90)/1e6:.2f}M")
    buckets = {"<100k": 0, "100-500k": 0, "500k-2M": 0, ">2M": 0, "unknown": 0}
    for m in mcs:
        if m is None:
            buckets["unknown"] += 1
        elif m < 100_000:
            buckets["<100k"] += 1
        elif m < 500_000:
            buckets["100-500k"] += 1
        elif m < 2_000_000:
            buckets["500k-2M"] += 1
        else:
            buckets[">2M"] += 1
    print(f"      slip-bucket counts: {buckets}")
    mults_het = [simulate_exit(t["ser_e"], t["base"] * (1 + mc_slip(t["entry_mc"])), t["t_e"], S.P_MOON)
                 for t in toks]
    r = agg(mults_het, times)
    print(f"      MC-KEYED het slip (8/15/25/40% by thinness) @50x: mean {r['mean']:.3f} "
          f"CIlo {r['ci_lo']:.3f} drop3 {r['drop3']:.3f} f2logG {r['f2logG']:+.4f} $500->{r['bank']:.0f} "
          f"=> {'GO' if r['go'] else 'NO-GO'}")

    # 3) do the moons cluster in the thinnest (lowest-MC) names?
    base_mults = np.array([simulate_exit(t["ser_e"], t["base"] * 1.015, t["t_e"], S.P_MOON) for t in toks])
    pairs = [(t["entry_mc"], base_mults[i]) for i, t in enumerate(toks) if t["entry_mc"] is not None]
    mc_arr = np.array([p[0] for p in pairs]); mu_arr = np.array([p[1] for p in pairs])
    capped = np.minimum(mu_arr, 50.0)
    rho = np.corrcoef(np.log(mc_arr), capped)[0, 1]
    print(f"\n  (3) corr(log Entry-MC, capped P_MOON mult) = {rho:+.3f}  "
          f"(negative => moons cluster in THIN names => realistic slip there is worse)")
    # split by MC median: edge in the thin half vs thick half
    med = np.median(mc_arr)
    thin = capped[mc_arr <= med]; thick = capped[mc_arr > med]
    print(f"      thin half (MC<= {med/1e3:.0f}k, n={len(thin)}): mean {thin.mean():.3f}  "
          f"thick half (n={len(thick)}): mean {thick.mean():.3f}")
    # where do the top-10 multiples sit?
    order = np.argsort(mu_arr)[::-1][:10]
    print("      top-10 P_MOON multiples and their Entry MC:")
    for j in order:
        print(f"        mult {mu_arr[j]:8.2f}x  @ Entry MC ${mc_arr[j]/1e3:8.0f}k")

    # 4) entry+exit thin-liquidity: 25% entry slip AND doubled exit costs
    P_MOON_thinexit = ExitPolicy("P_moon_thinexit", tp_ladder=[(2.0, 0.5)], stop_mult=0.0,
                                 trail_pct=0.60, trail_arm_mult=2.0, time_stop_h=24 * 14)
    mults_be = [simulate_exit(t["ser_e"], t["base"] * 1.25, t["t_e"], P_MOON_thinexit,
                              tp_cost=0.05, stop_cost=0.08) for t in toks]
    r = agg(mults_be, times)
    print(f"\n  (4) 25% entry slip + 5%/8% exit costs (thin both sides) @50x: mean {r['mean']:.3f} "
          f"CIlo {r['ci_lo']:.3f} drop3 {r['drop3']:.3f} => {'GO' if r['go'] else 'NO-GO'}")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
