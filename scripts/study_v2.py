#!/usr/bin/env python3
"""Study v2 — Stage A: follower-anchored opportunity + Gate 0/1 verdict.

Answers the cheap, decisive questions before any exit optimization:
  * Gate 0: how far below the channel's claimed multiples is the follower's realizable
    ceiling (peak / worst-fill)? If "winners" can't even break even for a follower, the
    channel is structurally unfollowable.
  * Gate 1: E-ratio (ATR-normalized MFE/MAE) > 1 at a horizon a follower can act on?

    PYTHONPATH=src python3 scripts/study_v2.py --corpus runs/your_channel_corpus.json
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memebot.ingest.telegram_mcp import load_corpus_json, first_call_per_mint  # noqa: E402
from memebot.analysis.features import extract_features, claimed_multiple_by_ticker  # noqa: E402
from memebot.analysis.excursion import compute_excursion, e_ratio  # noqa: E402
from memebot.data.cache import CachedPriceClient  # noqa: E402
from memebot.data.jupiter import JupiterChartsClient  # noqa: E402

WINDOW_H = 15
HORIZONS = (1, 4, 12)


def pct(a, q):
    return float(np.percentile(a, q)) if len(a) else float("nan")


def frac_ge(a, x):
    return float((np.asarray(a) >= x).mean()) if len(a) else float("nan")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(ROOT / "runs" / "your_channel_corpus.json"))
    ap.add_argument("--since", default=None)
    ap.add_argument("--limit-tokens", type=int, default=None)
    ap.add_argument("--latency", type=float, default=60.0, help="post-to-fill latency seconds (worst fill)")
    ap.add_argument("--entry-cost", type=float, default=0.015, help="entry leg cost fraction")
    ap.add_argument("--cache-dir", default=str(ROOT / "data_cache" / "jupiter_v2"))
    args = ap.parse_args(argv)

    allsig = load_corpus_json(args.corpus)
    claimed_map = claimed_multiple_by_ticker(allsig)
    calls = first_call_per_mint(allsig)
    if args.since:
        from datetime import datetime, timezone
        cut = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        calls = [c for c in calls if c.posted_at >= cut]
    calls.sort(key=lambda s: s.posted_at)
    if args.limit_tokens:
        calls = calls[-args.limit_tokens:]
    print(f"calls: {len(calls)}  (claimed-multiple tickers: {len(claimed_map)})", file=sys.stderr)

    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), args.cache_dir)
    excs = []
    for i, s in enumerate(calls):
        if i % 25 == 0:
            print(f"\r  {i}/{len(calls)}", end="", file=sys.stderr)
        feats = extract_features(s.raw_text)
        try:
            series = client.get_price_series(s.mint, s.posted_at - timedelta(minutes=15),
                                             s.posted_at + timedelta(hours=WINDOW_H))
        except Exception:
            continue
        ex = compute_excursion(
            s, series, entry_leg_cost=args.entry_cost, latency_s=args.latency,
            horizons_h=HORIZONS, window_h=WINDOW_H,
            claimed_multiple=claimed_map.get(s.ticker),
            lateness_ratio=feats["lateness_ratio"], time_since_entry_h=feats["time_since_entry_h"],
        )
        excs.append(ex)
    print(f"\r  cache: {client.hits} hits, {client.misses} fetched", file=sys.stderr)

    priced = [e for e in excs if e.priced]
    report(calls, excs, priced)
    return 0


def report(calls, excs, priced):
    n = len(calls)
    mfe = np.array([e.mfe_x for e in priced])
    mae = np.array([e.mae_x for e in priced])
    line = "=" * 88
    print(line)
    print("  STUDY v2 — STAGE A: FOLLOWER-ANCHORED OPPORTUNITY + GATE 0/1")
    print(f"  @your_channel | calls={n} priced={len(priced)} "
          f"| window={WINDOW_H}h minute | worst-fill+1.5% entry cost")
    print(line)

    # ---- Follower opportunity (the ceiling: perfect peak exit) ----
    print("\n  FOLLOWER MFE_x  (peak after call / worst-fill entry — UPPER bound on realization)")
    print(f"    median={np.median(mfe):.2f}x  p75={pct(mfe,75):.2f}x  p90={pct(mfe,90):.2f}x  max={mfe.max():.1f}x")
    print("    reachable from YOUR entry:  "
          f">=1.5x {frac_ge(mfe,1.5)*100:.0f}%   >=2x {frac_ge(mfe,2)*100:.0f}%   "
          f">=3x {frac_ge(mfe,3)*100:.0f}%   >=5x {frac_ge(mfe,5)*100:.0f}%   >=10x {frac_ge(mfe,10)*100:.0f}%")
    print(f"    (channel claims ~37% reach >=2x — but measured from THEIR Entry MC)")
    print(f"  FOLLOWER MAE_x  (worst dip / entry):  median={np.median(mae):.2f}x  "
          f"p25={pct(mae,25):.2f}x  (median max-drawdown {(1-np.median(mae))*100:.0f}%)")
    print(f"  time-to-peak median: {np.median([e.time_to_mfe_min for e in priced]):.0f} min")

    # ---- GATE 0: claimed vs follower-realizable ----
    winners = [e for e in priced if e.claimed_multiple]
    print(f"\n  GATE 0 — CLAIMED vs FOLLOWER-REALIZABLE  (n={len(winners)} profit-alert calls priced)")
    if winners:
        claimed = np.array([e.claimed_multiple for e in winners])
        real = np.array([e.mfe_x for e in winners])  # follower ceiling
        delta = claimed - real
        print(f"    channel claimed:    median={np.median(claimed):.1f}x  p90={pct(claimed,90):.0f}x")
        print(f"    follower ceiling:   median={np.median(real):.2f}x  p90={pct(real,90):.1f}x")
        print(f"    claimed - realized: median={np.median(delta):.1f}x  (the lateness/wick haircut)")
        print(f"    of these 'winners': follower could >=2x {frac_ge(real,2)*100:.0f}%   "
              f"could NOT break even (<1x) {(real<1).mean()*100:.0f}%")
        g0_fatal = np.median(real) < 1.3
        print(f"    -> Gate 0 {'FATAL (winners unfollowable)' if g0_fatal else 'survivable'}: "
              f"median follower ceiling on claimed winners = {np.median(real):.2f}x")

    # ---- GATE 1: E-ratio ----
    print("\n  GATE 1 — E-RATIO (ATR-normalized MFE/MAE; >1 = exploitable entry edge)")
    for h in HORIZONS:
        er = e_ratio(priced, h)
        print(f"    E({h}h) = {er:.2f}" + ("   <-- edge" if (er and er > 1) else "   (no edge)" if er else "   n/a"))
    # by lateness bucket at 4h
    print("    E(4h) by lateness bucket (Current/Entry MC):")
    buckets = [("<1.5x", 0, 1.5), ("1.5-3x", 1.5, 3), ("3-5x", 3, 5), (">5x", 5, 1e9)]
    for name, lo, hi in buckets:
        seg = [e for e in priced if e.lateness_ratio and lo <= e.lateness_ratio < hi]
        er = e_ratio(seg, 4)
        print(f"      {name:7} n={len(seg):4}  E(4h)={er:.2f}" if er else f"      {name:7} n={len(seg):4}  E(4h)=n/a")

    # ---- top-k concentration ----
    pos = sorted([max(0.0, e.mfe_x - 1.0) for e in priced], reverse=True)
    tot = sum(pos)
    if tot > 0:
        top3 = sum(pos[:3]) / tot
        top10 = sum(pos[:10]) / tot
        print(f"\n  TAIL CONCENTRATION (of total upside MFE): top-3 = {top3*100:.0f}%   top-10 = {top10*100:.0f}%")

    print("\n" + line)
    er4 = e_ratio(priced, 4)
    g1 = bool(er4 and er4 > 1)
    g0 = bool(winners and np.median([e.mfe_x for e in winners]) >= 1.3)
    if g1 and g0:
        print("  STAGE-A VERDICT: gates 0/1 survivable at the optimistic ceiling -> proceed to")
        print("  Stage B (pessimistic managed-exit simulation) to test real net EV.")
    else:
        print("  STAGE-A VERDICT: NO-GO. Even at the optimistic peak-exit ceiling the entry edge")
        print("  and/or follower-realizable winners are too weak; managed exits cannot rescue it.")
    print(line)


if __name__ == "__main__":
    raise SystemExit(main())
