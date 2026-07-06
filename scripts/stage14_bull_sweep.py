#!/usr/bin/env python3
"""BULL ANGLE: search for the best executable, no-lookahead, uniformly-applied exit policy
that makes @your_channel +EV on the full 1263-token denominator.

Builds series+fill once per token (warm cache), then sweeps many ExitPolicy configs.
Gate: at a cap <=50x, CIlo>1 AND drop3>1 AND f=2% logG>0 (and $500 grows).
"""
from __future__ import annotations
import sys, time
from datetime import timedelta
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from stage14_untruncated import (series_to_today, entry_fill, LAT_S,
                                 mean_ci, drop_top, fixed_f_growth, cap_mults,
                                 hill_alpha, single_pass_bankroll)
from memebot.ingest.telegram_mcp import load_corpus_json, first_call_per_mint
from memebot.data.cache import CachedPriceClient
from memebot.data.jupiter import JupiterChartsClient
from memebot.analysis.exit_sim import ExitPolicy, simulate_exit

CAPS = [10.0, 25.0, 50.0]


def build():
    calls = first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json")))
    calls = sorted(calls, key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4),
                               str(ROOT / "data_cache" / "jupiter_untrunc"))
    toks = []  # (series, fill, t_fill, time_ts)
    t0 = time.time()
    for s in calls:
        if not s.mint:
            continue
        t = s.posted_at + timedelta(seconds=LAT_S)
        try:
            ser = series_to_today(client, s.mint, s.posted_at)
        except Exception:
            ser = None
        fill = entry_fill(ser, t) if (ser and ser.candles) else None
        toks.append((ser, fill, t, s.posted_at.timestamp()))
    print(f"built {len(toks)} tokens in {time.time()-t0:.1f}s | cache {client.hits}h/{client.misses}m",
          file=sys.stderr)
    return toks


def run_policy(toks, policy):
    mults = []
    for ser, fill, t, _ in toks:
        if fill is None or fill <= 0:
            mults.append(0.0)
            continue
        mults.append(simulate_exit(ser, fill, t, policy))
    return np.asarray(mults)


def evaluate(name, mults, times):
    a = mults
    alpha = hill_alpha(list(a))
    win = float((a > 1).mean()) * 100
    row = {"name": name, "n": len(a), "alpha": alpha, "win": win,
           "median": float(np.median(a)), "max": float(a.max()), "by_cap": {}}
    best_pass = False
    for cap in CAPS:
        cm = cap_mults(list(a), cap)
        m, lo, hi = mean_ci(cm)
        d3 = drop_top(cm, 3)
        g2 = fixed_f_growth(cm, 0.02)
        bank = single_pass_bankroll(cm, times, f=0.02, cap=float("inf"))
        passes = (lo > 1 and d3 > 1 and g2 > 0 and bank > 500)
        best_pass = best_pass or passes
        row["by_cap"][cap] = dict(mean=m, lo=lo, hi=hi, drop3=d3, logG=g2, bank=bank, passes=passes)
    row["passes_any"] = best_pass
    return row


def main():
    toks = build()
    times = np.array([t[3] for t in toks])

    # ---- candidate policies: the bull's best shots ----
    pols = []
    # pure low take-profit (sell 100% at L)
    for L in [1.1, 1.2, 1.3, 1.5, 2.0]:
        pols.append(ExitPolicy(f"TP100@{L}", [(L, 1.0)], 0.0, 1.0, float("inf"), 1e9))
    # de-risk + moonbag (sell half early, trail the rest), varied
    pols.append(ExitPolicy("half@1.3_trail40@1.3", [(1.3, 0.5)], 0.0, 0.40, 1.3, 24 * 14))
    pols.append(ExitPolicy("half@1.5_trail40@1.5", [(1.5, 0.5)], 0.0, 0.40, 1.5, 24 * 14))
    pols.append(ExitPolicy("half@2_trail50@2", [(2.0, 0.5)], 0.0, 0.50, 2.0, 24 * 14))
    # ladders that lock most early, keep a runner
    pols.append(ExitPolicy("ladder_1.2-1.5-2-5", [(1.2, 0.25), (1.5, 0.25), (2.0, 0.25), (5.0, 0.15)],
                           0.0, 0.50, 5.0, 24 * 14))
    pols.append(ExitPolicy("staircase_10x10",
                           [(1.2, 0.1), (1.5, 0.1), (2, 0.1), (3, 0.1), (5, 0.1),
                            (10, 0.1), (20, 0.1), (50, 0.1)], 0.0, 0.50, 10.0, 24 * 14))
    # tight trailing stop armed immediately (ride to near-peak)
    for tp in [0.15, 0.20, 0.25, 0.30, 0.40]:
        pols.append(ExitPolicy(f"trail{tp}_arm1.0", [], 0.0, tp, 1.0, 1e9))
    # trail armed after a small run (let it breathe, then ride peak)
    for arm in [1.2, 1.5, 2.0]:
        for tp in [0.25, 0.35]:
            pols.append(ExitPolicy(f"trail{tp}_arm{arm}", [], 0.0, tp, arm, 1e9))
    # hard stop to cut losers fast + moonbag tail
    for stop in [0.5, 0.7, 0.85]:
        pols.append(ExitPolicy(f"stop{stop}_trail40@2", [(2.0, 0.5)], stop, 0.40, 2.0, 24 * 14))
    # hard stop + pure hold tail (cut losers, ride winners to horizon)
    for stop in [0.5, 0.7]:
        pols.append(ExitPolicy(f"stop{stop}_hold", [], stop, 1.0, float("inf"), 1e9))
    # short time-stop to recycle dead capital faster + moonbag
    for ts in [6, 24, 72]:
        pols.append(ExitPolicy(f"half@2_trail50@2_ts{ts}", [(2.0, 0.5)], 0.0, 0.50, 2.0, ts))
    # de-risk early then diamond the rest (capture tail, cut half the loss)
    pols.append(ExitPolicy("half@1.5_holdrest", [(1.5, 0.5)], 0.0, 1.0, float("inf"), 1e9))
    pols.append(ExitPolicy("third@1.3_third@2_holdrest", [(1.3, 0.34), (2.0, 0.33)], 0.0, 1.0, float("inf"), 1e9))

    results = []
    for p in pols:
        m = run_policy(toks, p)
        results.append(evaluate(p.name, m, times))

    # print table
    hdr = f"{'policy':32} {'cap':>4} {'mean':>7} {'CIlo':>7} {'drop3':>7} {'logG':>9} {'$500':>9} {'P'}"
    print("=" * len(hdr))
    print(hdr)
    print("=" * len(hdr))
    # sort by best CIlo at 50x
    results.sort(key=lambda r: r["by_cap"][50.0]["lo"], reverse=True)
    for r in results:
        for cap in CAPS:
            c = r["by_cap"][cap]
            flag = "GO" if c["passes"] else ""
            nm = r["name"] if cap == CAPS[0] else ""
            print(f"{nm:32} {cap:>4.0f} {c['mean']:>7.3f} {c['lo']:>7.3f} {c['drop3']:>7.3f} "
                  f"{c['logG']:>+9.4f} {c['bank']:>9.1f} {flag}")
        print("-" * len(hdr))

    passing = [r for r in results if r["passes_any"]]
    print(f"\nPOLICIES THAT CLEAR THE GATE (CIlo>1 & drop3>1 & logG>0 & $500>500 at some cap<=50x): "
          f"{len(passing)}")
    for r in passing:
        print("  ", r["name"])
    # best by CIlo at 50x
    best = results[0]
    c50 = best["by_cap"][50.0]
    print(f"\nBEST policy by CIlo@50x: {best['name']}")
    print(f"  @50x: mean={c50['mean']:.3f} CIlo={c50['lo']:.3f} drop3={c50['drop3']:.3f} "
          f"logG={c50['logG']:+.4f} $500->{c50['bank']:.1f} win={best['win']:.0f}% alpha={best['alpha']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
