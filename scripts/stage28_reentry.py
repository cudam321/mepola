#!/usr/bin/env python3
"""Stage 28 — RE-ENTRY beast: cut the loss with a stop, then RE-ENTER on renewed momentum.

Answers the user's point: a stop cuts you out of ANSEM's -70% dip, but if you RE-ENTER when it breaks
out again, you catch the run. State machine per token: enter -> trail/stop -> if stopped, wait and
re-enter when price breaks +M% above the exit price -> repeat up to K entries -> compound the round-trip
returns into a per-token result. Tests whether re-entry recovers the dip-then-run winners (ANSEM) faster
than it compounds losses on the 73% dead-cat bounces. Full denominator, gate, ANSEM shown explicitly.

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/stage28_reentry.py
"""
from __future__ import annotations
import sys
from datetime import timedelta
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT / "scripts"))
from memebot.ingest.telegram_mcp import load_corpus_json, first_call_per_mint  # noqa: E402
from memebot.data.cache import CachedPriceClient  # noqa: E402
from memebot.data.jupiter import JupiterChartsClient  # noqa: E402
import stage14_untruncated as S  # noqa: E402

CAP = 50.0
ANSEM = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"


def trade_reentry(cands, f0, sm, tr, M, K, stop_cost=0.04, slip=0.012):
    """Compound round-trip returns on one token. Enter f0; trail from peak, hard stop at sm*entry;
    after a stop, re-enter when price breaks +M% above the exit price; up to K entries."""
    cap = 1.0
    entry = f0; peak = f0; in_pos = True; entries = 1; last_exit = None; n_re = 0
    for c in cands:
        if in_pos:
            stop_lvl = max(sm * entry, (1 - tr) * peak)
            if c.low <= stop_lvl:                       # pessimistic: stop (low) first
                cap *= min(stop_lvl / entry, CAP) * (1 - stop_cost)
                in_pos = False; last_exit = stop_lvl
            elif c.high > peak:
                peak = c.high
        else:
            if entries < K and last_exit and c.high >= last_exit * (1 + M):
                entry = last_exit * (1 + M) * (1 + slip)
                peak = entry; in_pos = True; entries += 1; n_re += 1
    if in_pos:
        cap *= min(cands[-1].close / entry, CAP)
    return min(cap, CAP), n_re


def gate(name, M, T):
    a = np.array(M)
    mean, lo, hi = S.mean_ci(list(a)); d3 = S.drop_top(list(a), 3); g2 = S.fixed_f_growth(list(a), 0.02)
    bank = S.single_pass_bankroll(list(a), np.asarray(T), 0.02, float("inf")); win = (a > 1).mean() * 100
    go = lo > 1 and d3 > 1 and g2 > 0 and bank > 500
    print(f"    {name:26} mean={mean:6.3f} CIlo={lo:6.3f} drop3={d3:6.3f} logG={g2:+.4f} win={win:3.0f}% "
          f"$500->{bank:7.0f} {'*** GO ***' if go else ''}")
    return go


def main() -> int:
    calls = sorted([s for s in first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_fresh.json"))) if s.mint],
                   key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_untrunc"))
    toks = []
    for i, s in enumerate(calls):
        if i % 200 == 0:
            print(f"\r  loading {i}/{len(calls)}", end="", file=sys.stderr)
        t = s.posted_at + timedelta(seconds=S.LAT_S)
        try:
            ser = S.series_to_today(client, s.mint, s.posted_at)
        except Exception:
            ser = None
        if not ser or not ser.candles:
            continue
        f0 = S.entry_fill(ser, t)
        if not f0 or f0 <= 0:
            continue
        cands = [c for c in ser.candles if c.ts >= t]
        toks.append((s.mint, cands, f0, s.posted_at.timestamp()))
    print("\r" + " " * 40 + "\r", end="", file=sys.stderr)
    print("=" * 100)
    print(f"  STAGE 28 — RE-ENTRY beast | {len(toks)} calls | stop cuts losers, re-enter on breakout | cap {CAP}x")
    print("=" * 100)

    print("\n  effect of re-entries K (stop -50%, trail 50%, re-enter on +30% breakout):")
    for K in (1, 2, 3, 5):
        res, times, am, rec = [], [], None, []
        for mint, cands, f0, ts in toks:
            cap, n_re = trade_reentry(cands, f0, 0.5, 0.5, 0.30, K)
            res.append(cap); times.append(ts); rec.append(n_re)
            if mint == ANSEM:
                am = cap
        lbl = "single entry (K=1)" if K == 1 else f"up to {K} entries"
        tag = f"  ANSEM={am:.1f}x  avg_reentries={np.mean(rec):.2f}"
        print(f"  [{lbl}]"); gate(f"  K={K}", res, times); print(f"       {tag}")

    print("\n  re-entry aggressiveness sweep (K=5, stop -50%, trail 50%):")
    for M in (0.2, 0.3, 0.5, 1.0):
        res, times, am = [], [], None
        for mint, cands, f0, ts in toks:
            cap, _ = trade_reentry(cands, f0, 0.5, 0.5, M, 5)
            res.append(cap); times.append(ts)
            if mint == ANSEM:
                am = cap
        go = gate(f"  re-enter on +{int(M*100)}% breakout", res, times)
        print(f"       ANSEM={am:.1f}x")

    print("\n  best-case: tighter stop (-30%) + aggressive re-entry (K=5, +20% trigger, trail 40%):")
    res, times, am = [], [], None
    for mint, cands, f0, ts in toks:
        cap, _ = trade_reentry(cands, f0, 0.7, 0.4, 0.20, 5)
        res.append(cap); times.append(ts)
        if mint == ANSEM:
            am = cap
    gate("  aggressive re-entry beast", res, times); print(f"       ANSEM={am:.1f}x")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
