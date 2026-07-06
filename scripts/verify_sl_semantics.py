#!/usr/bin/env python3
"""Verify what `sl` means in the #1 sim, and which sl value IS config #1.

Runs the exact stage38 sim across sl in {0.3, 0.5, 0.7}, printing for each:
  - the stop level as a % drawdown from entry (this is the plain-English '-X% SL')
  - OOS per-trade mean / drop3
  - ANSEM's realized multiple
#1 is known (from the leaderboard) to have OOS mean ~1.29 and ANSEM = 197.6x. Whichever sl reproduces
that IS #1 -> and its printed drawdown-% is the true stop.

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/verify_sl_semantics.py
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
from stage38_ansem_dependence import sim  # the exact #1 policy  # noqa: E402

ANSEM = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"


def main() -> int:
    calls = sorted([s for s in first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_fresh.json"))) if s.mint],
                   key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_untrunc"))
    toks = []
    for k, s in enumerate(calls):
        if k % 300 == 0:
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
        toks.append((s.mint, H, L, C, Tt, cds[0].open))
    print("\r" + " " * 40 + "\r", end="", file=sys.stderr)

    ts_all = sorted([t[4][0] for t in toks])
    cut = ts_all[int(len(ts_all) * 0.7)]

    print("=" * 78)
    print("  sl semantics: stop fires at  L <= sl*entry   (sl=0.3 -> price at 30% of entry -> -70%)")
    print("=" * 78)
    print(f"  {'sl':>5} | {'stop = -X% from entry':>22} | {'OOS mean':>9} {'OOS drop3':>10} | {'ANSEM':>9}")
    for sl in [0.3, 0.5, 0.7]:
        oos, ansem = [], None
        for mint, H, L, C, Tt, sig in toks:
            legs = sim(H, L, C, Tt, sig, dip=0.5, sl=sl, ftp=3.0, fsell=0.33, reentry=None)
            if not legs:
                continue
            m = float(legs[0])
            if Tt[0] >= cut:
                oos.append(m)
            if mint == ANSEM:
                ansem = m
        a = np.array(oos)
        d3 = np.sort(a)[:-3].mean() if len(a) > 3 else float("nan")
        drawdown = (1 - sl) * 100
        tag = "  <== matches #1 (mean~1.29, ANSEM 197.6x)" if abs(a.mean() - 1.287) < 0.05 else ""
        print(f"  {sl:>5.2f} | {'-'+format(drawdown,'.0f')+'% (stop at '+format(sl,'.2f')+'x entry)':>22} | "
              f"{a.mean():>9.3f} {d3:>10.3f} | {(ansem or 0):>8.1f}x{tag}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
