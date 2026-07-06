#!/usr/bin/env python3
"""Stage 29 — re-entry WITH take-profit strategies (the missing piece from stage28).

stage28's re-entry used a trailing stop only. This adds real TP logic to every entry AND re-entry:
sell-all at a fixed multiple, tight TP, derisk-ladder+moonbag, etc. — combined with the stop + re-entry
state machine. Does banking profit on the re-entries help the book? Full denominator, gate, ANSEM shown.

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/stage29_reentry_tp.py
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


def trade(cands, f0, sm, tp_ladder, trail, M, K, tp_cost=0.015, stop_cost=0.04, slip=0.012):
    """Re-entry state machine with a real TP ladder per round-trip. Compounds round-trip returns."""
    rungs = sorted(tp_ladder)
    cap = 1.0; entries = 1; entry = f0
    remaining = 1.0; proceeds = 0.0; peak = f0; filled = [False] * len(rungs); in_pos = True; last_exit = None
    for c in cands:
        if in_pos:
            stop_lvl = max(sm * entry, (1 - trail) * peak) if (sm > 0 or trail < 1) else 0.0
            if stop_lvl > 0 and c.low <= stop_lvl:                 # pessimistic: stop first
                proceeds += remaining * stop_lvl * (1 - stop_cost); remaining = 0.0; last_exit = stop_lvl
            else:
                for i, (mult, frac) in enumerate(rungs):
                    if not filled[i] and c.high >= mult * entry:
                        sell = min(frac, remaining); proceeds += sell * mult * entry * (1 - tp_cost)
                        remaining -= sell; filled[i] = True
                if c.high > peak:
                    peak = c.high
                if remaining <= 1e-9:
                    last_exit = c.close
            if remaining <= 1e-9:
                cap *= min(proceeds / entry, CAP); in_pos = False
        else:
            if entries < K and last_exit and c.high >= last_exit * (1 + M):
                entry = last_exit * (1 + M) * (1 + slip)
                remaining = 1.0; proceeds = 0.0; peak = entry; filled = [False] * len(rungs); in_pos = True; entries += 1
    if in_pos:
        proceeds += remaining * cands[-1].close
        cap *= min(proceeds / entry, CAP)
    return min(cap, CAP)


def gate(name, M, T, ansem):
    a = np.array(M); mean, lo, hi = S.mean_ci(list(a)); d3 = S.drop_top(list(a), 3)
    g2 = S.fixed_f_growth(list(a), 0.02); bank = S.single_pass_bankroll(list(a), np.asarray(T), 0.02, float("inf"))
    win = (a > 1).mean() * 100; go = lo > 1 and d3 > 1 and g2 > 0 and bank > 500
    print(f"    {name:34} mean={mean:6.3f} CIlo={lo:6.3f} drop3={d3:6.3f} logG={g2:+.4f} win={win:3.0f}% "
          f"$500->{bank:7.0f} ANSEM={ansem:6.1f}x {'*** GO ***' if go else ''}")


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
        toks.append((s.mint, [c for c in ser.candles if c.ts >= t], f0, s.posted_at.timestamp()))
    print("\r" + " " * 40 + "\r", end="", file=sys.stderr)
    print("=" * 104)
    print(f"  STAGE 29 — RE-ENTRY + TAKE-PROFIT | {len(toks)} calls | stop -50%, K=5 re-entries | cap {CAP}x")
    print("=" * 104)

    TP = {
        "trail-only (stage28)": ([], 0.5),
        "sell100%@1.5x": ([(1.5, 1.0)], 1.0),
        "sell100%@2x": ([(2.0, 1.0)], 1.0),
        "sell100%@3x": ([(3.0, 1.0)], 1.0),
        "half@2x + trail rest": ([(2.0, 0.5)], 0.5),
        "ladder 1.5/3x + trail": ([(1.5, 0.34), (3.0, 0.33)], 0.5),
        "derisk half@1.5 + moon": ([(1.5, 0.5)], 0.6),
    }
    for Mlabel, Mval in [("re-enter +30% (aggressive)", 0.30), ("re-enter +100% (catches ANSEM)", 1.0)]:
        print(f"\n  {Mlabel}:")
        for name, (ladder, trail) in TP.items():
            res, times, am = [], [], 0.0
            for mint, cands, f0, ts in toks:
                cap = trade(cands, f0, 0.5, ladder, trail, Mval, 5)
                res.append(cap); times.append(ts)
                if mint == ANSEM:
                    am = cap
            gate(name, res, times, am)
    print("=" * 104)
    print("  Read: TP banks the bounces (helps losers) but caps the re-entry runners (hurts ANSEM) — same wall.")
    print("=" * 104)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
