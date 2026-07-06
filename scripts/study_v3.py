#!/usr/bin/env python3
"""Study v3 — "the value of being early": upper bound on the copy-trade edge.

Re-anchors entry at SMART MONEY's entry moment (t_post - time_since_entry, the price level
the channel reports as Entry MC) and re-runs the managed-exit engine, vs entry at the
channel post. If early-entry is strongly +EV while post-entry is -EV, the alpha is real and
recoverable by being early (then: build on-chain wallet detection). If even perfect early
entry is -EV, the 'smart money' isn't skilled -> stop.

    PYTHONPATH=src python3 scripts/study_v3.py
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memebot.ingest.telegram_mcp import load_corpus_json, first_call_per_mint  # noqa: E402
from memebot.analysis.features import extract_features  # noqa: E402
from memebot.analysis.exit_sim import POLICIES, simulate_exit  # noqa: E402
from memebot.data.cache import CachedPriceClient  # noqa: E402
from memebot.data.jupiter import JupiterChartsClient  # noqa: E402

MAX_TSE_H = 8.0       # cap so the early window stays in minute resolution & post has forward room
FWD_H = 16            # minute window length from the EARLY entry


def worst_fill(series, t, latency_s=60.0, entry_cost=0.015):
    hi = max((c.high for c in series.candles if t <= c.ts <= t + timedelta(seconds=latency_s)), default=None)
    if hi is None:
        prior = [c for c in series.candles if c.ts <= t]
        if not prior:
            return None
        hi = prior[-1].high
    return hi * (1 + entry_cost) if hi and hi > 0 else None


def price_at(series, t):
    prior = [c for c in series.candles if c.ts <= t]
    return prior[-1].close if prior else None


def ev(r):
    a = np.asarray(r)
    return float(a.mean() - 1.0) if len(a) else float("nan")


def boot_lo(mult, n=5000, seed=0):
    """2.5th-percentile of the bootstrap EV distribution (the CI lower bound)."""
    a = np.asarray(mult, dtype=float)
    if len(a) < 2:
        return float("nan")
    rng = np.random.default_rng(seed)
    means = a[rng.integers(0, len(a), size=(n, len(a)))].mean(axis=1) - 1.0
    return float(np.percentile(means, 2.5))


def ev_drop_top(mult, k):
    """EV after removing the top-k winners — tests whether the edge is a tail artifact."""
    a = np.sort(np.asarray(mult, dtype=float))[::-1]
    return ev(a[k:]) if len(a) > k else float("nan")


# Policies a follower can actually execute. P1_buy_and_die is EXCLUDED: it "sells" by dumping
# 100% at the final candle, which is not a tradable decision on an illiquid microcap.
EXECUTABLE = {"P2_principal_out_trail", "P3_ladder_2_3_5", "P5_aggressive_derisk", "P5b_tight_stop_moonbag"}


def main() -> int:
    allsig = load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json"))
    calls = first_call_per_mint(allsig)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_v3"))

    subset = []
    for s in calls:
        f = extract_features(s.raw_text)
        if f["time_since_entry_h"] and 0 < f["time_since_entry_h"] <= MAX_TSE_H:
            subset.append((s, f))
    print(f"subset (time_since_entry 0-{MAX_TSE_H}h parsed): {len(subset)}", file=sys.stderr)

    rows = []  # (signal, feats, series, t_entry_early)
    runups = []  # OHLCV-implied price_post/price_early (proxy validation vs channel lateness)
    for i, (s, f) in enumerate(subset):
        if i % 50 == 0:
            print(f"\r  {i}/{len(subset)}", end="", file=sys.stderr)
        t_early = s.posted_at - timedelta(hours=f["time_since_entry_h"])
        try:
            series = client.get_price_series(s.mint, t_early - timedelta(minutes=15), t_early + timedelta(hours=FWD_H))
        except Exception:
            continue
        if series.empty:
            continue
        rows.append((s, f, series, t_early))
        pe, pp = price_at(series, t_early), price_at(series, s.posted_at)
        if pe and pp and pe > 0:
            runups.append(pp / pe)
    print(f"\r  cache: {client.hits} hits, {client.misses} fetched; usable: {len(rows)}", file=sys.stderr)

    line = "=" * 92
    print(line); print("  STUDY v3 — VALUE OF BEING EARLY (copy smart-money entry vs channel post)")
    print(f"  @your_channel | calls={len(rows)} | entry @ smart-money moment vs @ post | same managed exits")
    print(line)

    # proxy validation
    if runups:
        ru = np.asarray(runups)
        print(f"\n  PROXY CHECK: OHLCV run-up price_post/price_early median={np.median(ru):.2f}x "
              f"(channel-stated Current/Entry MC median ~2.65x) -> "
              f"{'consistent' if 1.5 < np.median(ru) < 5 else 'DIVERGENT (parse/field suspect)'}")

    print(f"\n  {'policy':24} | {'EV early':>9} | {'EV post':>9} | {'edge':>8} | {'CIlo':>8} | {'drop3':>8} | {'win%':>5} | exec")
    print("  " + "-" * 96)
    early_by_pol: dict[str, list[float]] = {}
    for pol in POLICIES:
        re_ = [simulate_exit(series, worst_fill(series, te), te, pol) for (s, f, series, te) in rows if worst_fill(series, te)]
        rp_ = [simulate_exit(series, worst_fill(series, s.posted_at), s.posted_at, pol) for (s, f, series, te) in rows if worst_fill(series, s.posted_at)]
        early_by_pol[pol.name] = re_
        a = np.asarray(re_)
        print(f"  {pol.name:24} | {ev(re_)*100:>+7.1f}% | {ev(rp_)*100:>+7.1f}% | {(ev(re_)-ev(rp_))*100:>+6.1f}pp | "
              f"{boot_lo(re_)*100:>+6.1f}% | {ev_drop_top(re_, 3)*100:>+6.1f}% | {(a>1).mean()*100:>4.0f}% | "
              f"{'yes' if pol.name in EXECUTABLE else 'NO'}")
    print("  " + "-" * 96)
    print("  CIlo = 2.5th pct of bootstrap EV (5000 resamples, seed=0); drop3 = EV after removing the top-3 winners.")
    print("  buy_and_die is NON-executable (dumps 100% at the final candle); shown as an UPPER BOUND only.")

    # Honest GO test: an EXECUTABLE policy must have a positive bootstrap CI lower bound AND survive
    # dropping its top-3 winners (so the edge isn't carried by 1-3 lottery tokens). Point EV / max
    # over policies is NOT enough — that is how the old verdict produced a false GO.
    passing = [p for p in POLICIES if p.name in EXECUTABLE
               and boot_lo(early_by_pol[p.name]) > 0 and ev_drop_top(early_by_pol[p.name], 3) > 0]
    print("\n" + line)
    if passing:
        best = max(passing, key=lambda p: boot_lo(early_by_pol[p.name]))
        m = early_by_pol[best.name]
        print(f"  VERDICT: being EARLY clears the bar (executable {best.name}: EV {ev(m)*100:+.1f}%, "
              f"CIlo {boot_lo(m)*100:+.1f}%, drop-top3 {ev_drop_top(m, 3)*100:+.1f}%).")
        print("  -> worth building on-chain smart-money detection + OOS persistence test + real-time copy.")
    else:
        bd = early_by_pol["P1_buy_and_die"]
        print("  VERDICT: NO executable policy clears the bar even at PERFECT early entry.")
        print(f"  The only +EV number is non-executable buy_and_die (EV {ev(bd)*100:+.1f}%, CIlo {boot_lo(bd)*100:+.1f}%, "
              f"drop-top3 {ev_drop_top(bd, 3)*100:+.1f}%) -- a tail artifact, not a strategy.")
        print("  -> do NOT build copy infra on this channel's smart money: the early-entry edge fails")
        print("  executability + a positive CI lower bound. Re-measure a different wallet source first.")
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
