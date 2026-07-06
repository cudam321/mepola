#!/usr/bin/env python3
"""Study v2 — Stage B: realized PnL of managed-exit policies (the bottom line).

Reuses the Stage-A price cache (data_cache/jupiter_v2), so it runs in seconds. For each
policy: mean realized multiple (EV), win rate, profit factor, and the tail-robustness test
(EV with the top-3 winners removed). Then segments the best policy to locate any +EV pocket.

    PYTHONPATH=src python3 scripts/study_v2_stageb.py
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
from memebot.analysis.excursion import compute_excursion  # noqa: E402
from memebot.analysis.exit_sim import POLICIES, simulate_exit  # noqa: E402
from memebot.data.cache import CachedPriceClient  # noqa: E402
from memebot.data.jupiter import JupiterChartsClient  # noqa: E402

WINDOW_H = 15


def ev(realized):
    a = np.asarray(realized)
    return float(a.mean() - 1.0) if len(a) else float("nan")


def profit_factor(realized):
    a = np.asarray(realized) - 1.0
    gains = a[a > 0].sum()
    losses = -a[a < 0].sum()
    return float(gains / losses) if losses > 0 else float("inf")


def ev_ex_topk(realized, k=3):
    a = np.sort(np.asarray(realized) - 1.0)
    if len(a) <= k:
        return float("nan")
    return float(a[:-k].mean())


def main() -> int:
    allsig = load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json"))
    calls = first_call_per_mint(allsig)
    calls.sort(key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_v2"))

    rows = []  # (signal, features, p_fill_net, series)
    for i, s in enumerate(calls):
        if i % 50 == 0:
            print(f"\r  {i}/{len(calls)}", end="", file=sys.stderr)
        feats = extract_features(s.raw_text)
        try:
            series = client.get_price_series(s.mint, s.posted_at - timedelta(minutes=15), s.posted_at + timedelta(hours=WINDOW_H))
        except Exception:
            continue
        ex = compute_excursion(s, series, window_h=WINDOW_H)
        if ex.priced:
            rows.append((s, feats, ex.p_fill_net, series))
    print(f"\r  cache: {client.hits} hits, {client.misses} fetched; priced calls: {len(rows)}", file=sys.stderr)

    line = "=" * 92
    print(line); print("  STUDY v2 — STAGE B: REALIZED MANAGED-EXIT PnL (pessimistic fills, all costs)")
    print(f"  @your_channel | priced calls={len(rows)} | window={WINDOW_H}h | entry worst-fill+1.5%, exit 1.5% (4% on stops)")
    print(line)
    print(f"\n  {'policy':24} | {'n':>5} | {'win%':>5} | {'med×':>6} | {'EV/trade':>9} | {'PF':>5} | {'EV ex-top3':>10}")
    print("  " + "-" * 84)

    realized_by_policy = {}
    for pol in POLICIES:
        r = [simulate_exit(series, pfn, s.posted_at, pol) for (s, _, pfn, series) in rows]
        realized_by_policy[pol.name] = r
        a = np.asarray(r)
        print(f"  {pol.name:24} | {len(r):>5} | {(a>1).mean()*100:>4.0f}% | {np.median(a):>5.2f}x | "
              f"{ev(r)*100:>+7.1f}% | {profit_factor(r):>5.2f} | {ev_ex_topk(r)*100:>+8.1f}%")
    print("  " + "-" * 84)
    print("  EV/trade = mean realized multiple - 1 (per-call expectancy, equal weight).")
    print("  EV ex-top3 = expectancy with the 3 biggest winners removed (lottery check).")

    # ---- segment the best policy by EV ----
    best = max(POLICIES, key=lambda p: ev(realized_by_policy[p.name]))
    r = realized_by_policy[best.name]
    print(f"\n  SEGMENTATION of best policy: {best.name}  (EV/trade {ev(r)*100:+.1f}%)")

    def seg(label, keyfn, order):
        print(f"\n    by {label}:")
        groups = {}
        for (s, f, _, _), real in zip(rows, r):
            k = keyfn(s, f)
            groups.setdefault(k, []).append(real)
        for k in order:
            if k in groups and len(groups[k]) >= 5:
                a = np.asarray(groups[k])
                print(f"      {str(k):10} n={len(a):4}  win%={(a>1).mean()*100:>3.0f}  EV={ev(groups[k])*100:>+7.1f}%  PF={profit_factor(groups[k]):.2f}")

    seg("signal_type", lambda s, f: f["signal_type"],
        ["main", "smartmoney", "volume", "holding", "buymore", "cto", "other"])

    def late_bucket(s, f):
        x = f["lateness_ratio"]
        return "unknown" if x is None else ("<1.5x" if x < 1.5 else "1.5-3x" if x < 3 else "3-5x" if x < 5 else ">5x")
    seg("lateness (Current/Entry MC)", late_bucket, ["<1.5x", "1.5-3x", "3-5x", ">5x", "unknown"])

    def tse_bucket(s, f):
        x = f["time_since_entry_h"]
        return "unknown" if x is None else ("<15min" if x < 0.25 else "15-60min" if x < 1 else "1-4h" if x < 4 else "4-12h" if x < 12 else ">12h")
    seg("time since entry", tse_bucket, ["<15min", "15-60min", "1-4h", "4-12h", ">12h", "unknown"])

    print("\n" + line)
    best_ev = ev(r); best_ev_ext = ev_ex_topk(r)
    if best_ev > 0 and best_ev_ext > 0:
        print(f"  STAGE-B VERDICT: best policy ({best.name}) is +EV ({best_ev*100:+.1f}%/trade) AND tail-robust")
        print("  (still +EV without top-3). Candidate edge -> proceed to OOS/liquidity-gated Stage C.")
    elif best_ev > 0:
        print(f"  STAGE-B VERDICT: best policy is +EV ({best_ev*100:+.1f}%) but NOT tail-robust (ex-top3 {best_ev_ext*100:+.1f}%)")
        print("  -> a lottery on 1-3 hits, not a repeatable system. Treat as NO-GO for sized trading.")
    else:
        print(f"  STAGE-B VERDICT: NO-GO. No managed-exit policy is +EV even at optimistic (non-liquidity-gated)")
        print(f"  prices. Best = {best.name} at {best_ev*100:+.1f}%/trade. Cutting losers + riding the tail")
        print("  does not overcome the follower haircut + costs. Check segments above for any +EV pocket.")
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
