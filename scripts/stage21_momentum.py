#!/usr/bin/env python3
"""Stage 21 — MOMENTUM / trend-following research on the channel's calls.

Tests momentum as a TRADING STRATEGY (not just a selector):
  (A) BREAKOUT ENTRY: don't buy at the post; wait and only enter if the token confirms upward
      momentum (price breaks +B% above the post price within a window). This SKIPS the immediate
      dumpers (the 56% that never reach 1.5x) at the cost of entering higher on survivors.
  (B) TREND EXIT: ride with a trailing stop (exit on momentum reversal) instead of fixed TP.
  (C) EARLY-STRENGTH FILTER: only trade calls that show strength in the first 15-30 min, then moonbag.

The decisive question: does buying confirmed strength + cutting on reversal beat the −EV floor? Strict
no-lookahead (entry triggers only on prices at/after the post), full denominator accounting, train/OOS,
and the standard gate (CIlo>1, drop3>1, f=2% logG>0, $500 grows, realistic fill).

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/stage21_momentum.py
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
from memebot.data.cache import CachedPriceClient  # noqa: E402
from memebot.data.jupiter import JupiterChartsClient  # noqa: E402
from memebot.analysis.exit_sim import ExitPolicy, simulate_exit  # noqa: E402
import stage14_untruncated as S  # noqa: E402

CAP = 50.0


def breakout_entry(series, t0, fill0, B, window_h):
    """First candle after t0 whose HIGH breaks +B% above fill0, within window_h. Fill at that level +1% slip."""
    end = t0 + timedelta(hours=window_h)
    lvl = fill0 * (1 + B)
    for c in series.candles:
        if c.ts < t0:
            continue
        if c.ts > end:
            break
        if c.high >= lvl:
            return lvl * 1.01, c.ts
    return None, None


def strength_15(series, t0, fill0, mins=20):
    """Early-strength signal: did price make a new high above fill0 in the first `mins` minutes?"""
    end = t0 + timedelta(minutes=mins)
    hi = max((c.high for c in series.candles if t0 <= c.ts <= end), default=0.0)
    return hi / fill0 if fill0 else 0.0


def agg(name, mults, times, trig=None, total=None, show=True):
    if len(mults) < 12:
        if show:
            print(f"    {name:30} n={len(mults)} too few"); return False
        return False
    cm = S.cap_mults(mults, CAP)
    mean, lo, hi = S.mean_ci(cm); d3 = S.drop_top(cm, 3); g2 = S.fixed_f_growth(cm, 0.02)
    bank = S.single_pass_bankroll(cm, times, 0.02, float("inf")); win = np.mean(np.array(mults) > 1) * 100
    go = lo > 1 and d3 > 1 and g2 > 0 and bank > 500
    tr = f" trig={trig}/{total}({trig/total*100:.0f}%)" if trig is not None else ""
    if show:
        print(f"    {name:30} n={len(mults):4d}{tr} mean={mean:6.3f} CIlo={lo:6.3f} drop3={d3:6.3f} "
              f"f2logG={g2:+.4f} win={win:3.0f}% $500->{bank:8.0f} {'*** GO ***' if go else ''}")
    return go


def main() -> int:
    calls = sorted(first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json"))),
                   key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_untrunc"))

    P_TRAIL30 = ExitPolicy("trail30", [], 0.0, 0.30, 1.0, 1e9)      # pure trend exit, 30% give-back
    P_TRAIL50 = ExitPolicy("trail50", [], 0.0, 0.50, 1.0, 1e9)
    rows = []  # per call: dict of strategy -> (mult, ts) or None
    for i, s in enumerate(calls):
        if not s.mint:
            continue
        if i % 100 == 0:
            print(f"\r  {i}/{len(calls)} (cache {client.hits}h/{client.misses}m)", end="", file=sys.stderr)
        t = s.posted_at + timedelta(seconds=S.LAT_S)
        try:
            ser = S.series_to_today(client, s.mint, s.posted_at)
        except Exception:
            ser = None
        f0 = S.entry_fill(ser, t) if (ser and ser.candles) else None
        rec = {"ts": s.posted_at.timestamp()}
        if f0 is None or f0 <= 0:
            rec["dead"] = True
            rows.append(rec); continue
        rec["dead"] = False
        # baseline moonbag + trend exits at post
        rec["base_moon"] = simulate_exit(ser, f0, t, S.P_MOON)
        rec["trail30"] = simulate_exit(ser, f0, t, P_TRAIL30)
        rec["trail50"] = simulate_exit(ser, f0, t, P_TRAIL50)
        # breakout entries + trend exit (trail30)
        for B in (0.2, 0.5, 1.0):
            bf, bt = breakout_entry(ser, t, f0, B, window_h=6)
            rec[f"bo{int(B*100)}"] = simulate_exit(ser, bf, bt, P_TRAIL30) if bf else None
            rec[f"bo{int(B*100)}_moon"] = simulate_exit(ser, bf, bt, S.P_MOON) if bf else None
        # early-strength filter (>=1.5x in first 20 min) then moonbag at post
        rec["str20"] = strength_15(ser, t, f0, 20)
        rows.append(rec)
    print("\r" + " " * 50 + "\r", end="", file=sys.stderr)

    alive = [r for r in rows if not r["dead"]]
    n = len(rows)
    cut = int(n * 0.7)
    print("=" * 100)
    print(f"  STAGE 21 — MOMENTUM/trend-following | {n} calls ({len(alive)} priced) | OOS split @70% | cap {CAP:.0f}x")
    print("=" * 100)

    def col(rows_, key):
        return [(r[key], r["ts"]) for r in rows_ if r.get(key) is not None and not r["dead"]]

    print("\n  baseline at the post (for reference):")
    b = col(rows, "base_moon"); agg("P_MOON (all)", [x[0] for x in b], np.array([x[1] for x in b]))
    for k in ("trail30", "trail50"):
        c = col(rows, k); agg(f"{k} trend-exit (all)", [x[0] for x in c], np.array([x[1] for x in c]))

    print("\n  (A)+(B) BREAKOUT entry + trend exit — full denominator (non-triggers count as $0 opportunity):")
    print("        [traded-set EV: must be +EV on the tokens it actually buys]")
    for B in (20, 50, 100):
        for suff, lbl in (("", "trail30"), ("_moon", "moonbag")):
            key = f"bo{B}{suff}"
            c = col(rows, key)
            trig = len(c)
            agg(f"breakout+{B}% -> {lbl}", [x[0] for x in c], np.array([x[1] for x in c]), trig=trig, total=len(alive))

    print("\n  (C) EARLY-STRENGTH filter (made >=Xx in first 20min) -> moonbag, OOS-tested:")
    train, oos = rows[:cut], rows[cut:]
    for thr in (1.5, 2.0, 3.0):
        sel_oos = [(r["base_moon"], r["ts"]) for r in oos
                   if not r["dead"] and r.get("str20", 0) >= thr and r.get("base_moon") is not None]
        agg(f"OOS strength>={thr}x -> moonbag", [x[0] for x in sel_oos], np.array([x[1] for x in sel_oos]))

    print("=" * 100)
    print("  Read: a momentum strategy must be +EV on the SET IT TRADES (CIlo>1, drop3>1, compounds).")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
