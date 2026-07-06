#!/usr/bin/env python3
"""Stage 17 — WHY does following a smart-money filter still lose? The comprehensive breakdown.

The channel DOES filter for smart-money-bought tokens. The question is why that doesn't help a
FOLLOWER. This shows the raw data behind it:
  (A) the full outcome distribution (it's NOT "every bet loses" — 24% win; the wins just don't cover
      the 76% that die),
  (B) the $1-in-each-token arithmetic + profit factor,
  (C) LATENESS: the channel's OWN posts show Current MC vs smart-money Entry MC = how far ABOVE smart
      money you buy,
  (D) smart money is often already SELLING when you see the post (AVG SELL MC vs your entry),
  (E) concrete example tokens (a moonshot, a typical winner, typical deaths).

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/stage17_explain.py
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from memebot.ingest.telegram_mcp import load_corpus_json, first_call_per_mint  # noqa: E402
from memebot.analysis.features import extract_features  # noqa: E402

_SELL = re.compile(r"AVG SELL MC\s*:?\s*\$?\s*([0-9.]+)\s*([KMB])?", re.I)
_HOLD = re.compile(r"HOLDING\s*(?:PERCENT)?\s*:?\s*([0-9.]+)\s*%", re.I)
_MAG = {"K": 1e3, "M": 1e6, "B": 1e9, "": 1.0, None: 1.0}


def main() -> int:
    rows = {r["mint"]: r for r in csv.DictReader(open(ROOT / "runs" / "stage14_pertoken.csv"))}
    calls = first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json")))

    hold = np.array([float(r["hold"]) for r in rows.values()])
    moon = np.array([float(r["moon"]) for r in rows.values()])
    mfe = np.array([float(r["mfe"]) for r in rows.values()])
    n = len(hold)
    print("=" * 96)
    print(f"  STAGE 17 — why a smart-money-filtered channel still loses a FOLLOWER | n={n} calls")
    print("=" * 96)

    # (A) outcome distribution
    def dist(name, a):
        buckets = [(-0.01, 0.1, "~total loss (<0.1x)"), (0.1, 0.5, "-50 to -90%"),
                   (0.5, 1.0, "-0 to -50%"), (1.0, 2.0, "1-2x (small win)"),
                   (2.0, 5.0, "2-5x"), (5.0, 10.0, "5-10x"), (10.0, 1e9, "10x+ (moon)")]
        print(f"\n  (A) outcome distribution — {name}:")
        for lo, hi, lbl in buckets:
            sel = (a >= lo) & (a < hi)
            cnt = int(sel.sum())
            contrib = a[sel].sum()  # $ returned per $1 staked in those tokens
            print(f"        {lbl:22} {cnt:4d} tokens ({cnt/n*100:4.1f}%)  | returns ${contrib:8.1f} of the ${cnt:4d} staked")
        print(f"        WIN RATE (>1x): {(a>1).mean()*100:.0f}%   median outcome: {np.median(a):.3f}x   best: {a.max():.0f}x")

    dist("P_HOLD (diamond hand)", hold)
    dist("P_MOON (sell 50%@2x + trail)", moon)

    # (B) the $1-in-each arithmetic + profit factor
    print("\n  (B) put $1 in EVERY one of the", n, "calls (equal weight):")
    for name, a in [("P_HOLD", hold), ("P_MOON", moon), ("MFE perfect-exit", mfe)]:
        ac = np.minimum(a, 50.0)  # realistic 50x cap
        gp = np.maximum(ac - 1, 0).sum()   # gross profit (per $1)
        gl = np.maximum(1 - ac, 0).sum()   # gross loss
        pf = gp / gl if gl else float("inf")
        print(f"        {name:18}: ${n:>4} in -> ${ac.sum():7.0f} out  | profit factor {pf:.2f}  "
              f"(winners +${gp:.0f}, losers -${gl:.0f})")
    print("        => for every $1 you spread across the channel's calls you get back ~$0.59 (P_MOON) / $0.37 (P_HOLD).")

    # (C) LATENESS — the channel's own posts say how late you are
    lateness, sm_sell_above, holding = [], [], []
    for s in calls:
        if not s.mint or s.mint not in rows:
            continue
        f = extract_features(s.raw_text)
        if f["lateness_ratio"] and 1.0 <= f["lateness_ratio"] < 1e4:
            lateness.append(f["lateness_ratio"])
        sm = _SELL.search(s.raw_text)
        cur = f["current_mc"]
        if sm and cur:
            sell_mc = float(sm.group(1)) * _MAG[(sm.group(2) or "").upper()]
            if sell_mc > 0:
                sm_sell_above.append(cur / sell_mc)  # you buy at cur; smart money sold at sell_mc
        h = _HOLD.search(s.raw_text)
        if h:
            holding.append(float(h.group(1)))
    lateness = np.array(lateness)
    print(f"\n  (C) LATENESS — Current MC (what YOU pay) / smart-money Entry MC, from the channel's OWN posts (n={len(lateness)}):")
    if len(lateness):
        for p in [10, 25, 50, 75, 90]:
            print(f"        p{p:<2}: {np.percentile(lateness,p):.2f}x  ", end="")
        print(f"\n        => the MEDIAN call already shows you paying {np.median(lateness):.2f}x what smart money paid. The pump the filter found is BEHIND the post.")

    # (D) smart money already exiting
    if sm_sell_above:
        sa = np.array(sm_sell_above)
        print(f"\n  (D) SMART MONEY ALREADY SELLING (n={len(sa)} posts with AVG SELL MC):")
        print(f"        you buy at a median {np.median(sa):.2f}x of where smart money AVG-SOLD; "
              f"{(sa>1).mean()*100:.0f}% of these you buy ABOVE smart money's own exit.")
    if holding:
        print(f"        smart-money HOLDING % at post: median {np.median(holding):.0f}% "
              f"(they've already offloaded the rest before you see it).")

    # (E) concrete examples
    print("\n  (E) concrete tokens (entry_mc = follower entry shown by channel):")
    items = sorted(rows.values(), key=lambda r: float(r["hold"]), reverse=True)
    show = items[:2] + items[len(items)//2-1:len(items)//2+1] + items[-2:]
    for r in show:
        print(f"        {r['mint'][:8]}.. {r['date']}  entry≈${float(r['entry_mc'])/1e3:6.0f}K  "
              f"peak(MFE)={float(r['mfe']):7.1f}x  ->  P_MOON ends {float(r['moon']):6.2f}x | P_HOLD {float(r['hold']):7.2f}x")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
