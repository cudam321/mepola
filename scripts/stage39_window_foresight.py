#!/usr/bin/env python3
"""Stage 39 — the $1786 window vs. actually trading forward through all of OOS.

The user is right: #1 turns $500 -> $1786 in window 3 (05-20..06-16). This asks the only question
that decides whether that's a strategy or a mirage: can you CAPTURE window 3 without foreknowledge of
ANSEM? Compares, on the SAME data, same #1 policy, at several bet sizes:
  - trading ONLY window 3 (requires knowing when ANSEM runs -> impossible forward)
  - trading the FULL OOS straight through, in order (what you'd actually do)
  - full OOS minus the single ANSEM token
so the gap = the cost of not knowing the future.

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/stage39_window_foresight.py
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT / "scripts"))
from memebot.ingest.telegram_mcp import load_corpus_json, first_call_per_mint  # noqa: E402
from memebot.data.cache import CachedPriceClient  # noqa: E402
from memebot.data.jupiter import JupiterChartsClient  # noqa: E402
import stage14_untruncated as S  # noqa: E402
from stage38_ansem_dependence import sim  # reuse #1's exact policy  # noqa: E402

ANSEM = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"


def bankroll_fixed_frac(mults, f, start=500.0):
    """Compound a bankroll betting fraction f of CURRENT bankroll on each trade, in order. Never busts."""
    b = start
    for m in mults:
        b = b * (1 - f) + b * f * m
    return b


def bankroll_fixed_dollar(mults, stake, start=500.0):
    """Bet a fixed $stake each trade; if bankroll < stake, bet what's left. Can go to ~0."""
    b = start
    for m in mults:
        s = min(stake, b)
        b = b - s + s * m
        if b <= 1e-6:
            return 0.0
    return b


def main() -> int:
    calls = sorted([s for s in first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_fresh.json"))) if s.mint],
                   key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_untrunc"))
    toks = []
    for k, s in enumerate(calls):
        if k % 200 == 0:
            print(f"\r  loading {k}/{len(calls)}", end="", file=sys.stderr)
        try:
            ser = S.series_to_today(client, s.mint, s.posted_at)
        except Exception:
            ser = None
        if not ser or not ser.candles:
            continue
        cds = [c for c in ser.candles if c.ts >= s.posted_at]
        if not cds or cds[0].open <= 0:
            continue
        H = np.array([c.high for c in cds]); L = np.array([c.low for c in cds])
        C = np.array([c.close for c in cds]); Tt = np.array([c.ts.timestamp() for c in cds])
        legs = sim(H, L, C, Tt, cds[0].open)
        if not legs:
            continue
        toks.append(dict(mint=s.mint, mult=float(legs[0]), ts=Tt[0]))
    print("\r" + " " * 40 + "\r", end="", file=sys.stderr)

    toks.sort(key=lambda d: d["ts"])
    cut = [d["ts"] for d in toks][int(len(toks) * 0.7)]
    oos = [d for d in toks if d["ts"] >= cut]
    q = np.array_split(oos, 4)
    w3 = list(q[2])  # window 3 — the ANSEM window

    full = [d["mult"] for d in oos]
    full_ex = [d["mult"] for d in oos if d["mint"] != ANSEM]
    w3m = [d["mult"] for d in w3]

    print("=" * 96)
    print(f"  STAGE 39 — can you capture the $1786 window without foreknowledge?  (#1, ANSEM in, uncapped)")
    print(f"  OOS = {len(oos)} trades; window 3 = {len(w3)} trades")
    print("=" * 96)

    print("\n  FIXED-FRACTION betting (bet f of current bankroll each trade — mathematically can't bust):")
    print(f"    {'bet size f':>12} | {'window-3 ONLY':>16} | {'FULL OOS (in order)':>20} | {'FULL OOS, no ANSEM':>19}")
    for f in [0.0025, 0.005, 0.01, 0.02, 0.05]:
        a = bankroll_fixed_frac(w3m, f); b = bankroll_fixed_frac(full, f); c = bankroll_fixed_frac(full_ex, f)
        print(f"    {f*100:>10.2f}% | {'$'+format(a,'.0f'):>16} | {'$'+format(b,'.0f'):>20} | {'$'+format(c,'.0f'):>19}")

    print("\n  FIXED-DOLLAR betting (bet a set $ each trade; realistic; can go broke):")
    print(f"    {'stake/trade':>12} | {'window-3 ONLY':>16} | {'FULL OOS (in order)':>20} | {'FULL OOS, no ANSEM':>19}")
    for stake in [5, 10, 25, 50]:
        a = bankroll_fixed_dollar(w3m, stake); b = bankroll_fixed_dollar(full, stake); c = bankroll_fixed_dollar(full_ex, stake)
        print(f"    {'$'+str(stake):>12} | {'$'+format(a,'.0f'):>16} | {'$'+format(b,'.0f'):>20} | {'$'+format(c,'.0f'):>19}")

    print("\n  the point:")
    print("    - 'window-3 ONLY' is the $1786 column. It requires switching on 05-20 and off 06-16 —")
    print("      i.e. knowing ANSEM would run. Forward, window 4 (right after) lost 30%.")
    print("    - 'FULL OOS' is what you get trading straight through, unable to see ANSEM coming.")
    print("    - 'no ANSEM' shows the gap is one token: remove it and even the full run collapses.")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
