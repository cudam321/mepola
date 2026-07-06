#!/usr/bin/env python3
"""Stage 16 — the power-law tradeoff: can you 'cut losses fast' AND hold ANSEM to the moon?

The user's strategy IS the right power-law idea: small wins, cut losers fast, ride a moonbag for the
rare ANSEM. The question is whether those two halves can coexist. This makes the tension explicit:

  (1) Trace ANSEM-B's actual path from the FOLLOWER's entry — show it craters BELOW entry before its run.
  (2) Sweep the moonbag drawdown tolerance (trailing-stop width). For each width, show BOTH what ANSEM
      pays AND the full-1263-book EV. Wider = catch more of ANSEM; tighter = cut losers faster. If no
      width makes the book clear 1.0, the two halves are mutually exclusive on this data.

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/stage16_powerlaw_tradeoff.py
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

ANSEM_B = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"


def main() -> int:
    calls = sorted(first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json"))),
                   key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_untrunc"))

    prepared = []  # (mint, ser, fill, t)
    ansem = None
    for i, s in enumerate(calls):
        if not s.mint:
            continue
        if i % 100 == 0:
            print(f"\r  loading {i}/{len(calls)}", end="", file=sys.stderr)
        t = s.posted_at + timedelta(seconds=S.LAT_S)
        try:
            ser = S.series_to_today(client, s.mint, s.posted_at)
        except Exception:
            ser = None
        fill = S.entry_fill(ser, t) if (ser and ser.candles) else None
        prepared.append((s.mint, ser, fill, t))
        if s.mint == ANSEM_B:
            ansem = (ser, fill, t, s.posted_at)
    print("\r" + " " * 40 + "\r", end="", file=sys.stderr)

    print("=" * 100)
    print("  STAGE 16 — power-law tradeoff: cut losses fast  VS  hold ANSEM to the moon")
    print("=" * 100)

    # (1) ANSEM-B path from the follower's entry
    print("\n  (1) ANSEM-B actual path from the FOLLOWER's entry (the 1000x runner):")
    ser, fill, t, posted = ansem
    fwd = [c for c in ser.candles if c.ts >= t]
    # pass 1: global ATH and the early peak (first 6 days)
    ath, ath_ts = fill, t
    early_peak, early_peak_ts = fill, t
    for c in fwd:
        if c.high > ath:
            ath, ath_ts = c.high, c.ts
        if c.ts <= t + timedelta(days=6) and c.high > early_peak:
            early_peak, early_peak_ts = c.high, c.ts
    # pass 2: deepest trough that occurs BEFORE the global ATH (the drawdown you must hold through)
    trough_after_peak, trough_ts = fill, ath_ts
    for c in fwd:
        if c.ts < ath_ts and c.low < trough_after_peak:
            trough_after_peak, trough_ts = c.low, c.ts
    print(f"        entry fill (follower): {fill:.3e}   ({posted.date()} call)")
    print(f"        early peak  : {early_peak/fill:6.1f}x  on {early_peak_ts.date()}")
    print(f"        then CRASHES to: {trough_after_peak/fill:6.2f}x  on {trough_ts.date()}  "
          f"(= {(trough_after_peak/fill-1)*100:+.0f}% from YOUR entry)")
    print(f"        FINAL ATH   : {ath/fill:6.0f}x  on {ath_ts.date()}")
    print(f"        => to catch the {ath/fill:.0f}x you had to HOLD THROUGH a {(trough_after_peak/fill-1)*100:+.0f}% drawdown.")
    print(f"        Any stop-loss tighter than {(trough_after_peak/fill-1)*100:.0f}% ejects you BEFORE the run.")

    # (2) the tradeoff: moonbag drawdown tolerance vs full-book EV + what ANSEM pays
    print("\n  (2) Sweep moonbag drawdown tolerance (sell 50% @2x, then trail the rest):")
    print(f"        {'trail give-back':>16} | {'ANSEM pays':>11} | {'book mean':>9} {'CIlo':>7} {'drop3':>7} "
          f"{'f2logG':>8} | {'$500->':>9} | GO?")
    widths = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.75, 0.90, 0.97]
    CAP = 50.0
    for w in widths:
        pol = ExitPolicy(f"trail{w}", [(2.0, 0.5)], stop_mult=0.0, trail_pct=w, trail_arm_mult=2.0, time_stop_h=24 * 30)
        mults, times, ansem_mult = [], [], None
        for mint, sr, fl, tt in prepared:
            m = 0.0 if (fl is None or fl <= 0) else simulate_exit(sr, fl, tt, pol)
            mults.append(m); times.append(tt.timestamp())
            if mint == ANSEM_B:
                ansem_mult = m
        cm = S.cap_mults(mults, CAP)
        mean, lo, hi = S.mean_ci(cm)
        d3 = S.drop_top(cm, 3); g2 = S.fixed_f_growth(cm, 0.02)
        bank = S.single_pass_bankroll(cm, np.asarray(times), f=0.02, cap=float("inf"))
        go = lo > 1 and d3 > 1 and g2 > 0 and bank > 500
        print(f"        {f'{int(w*100)}% (keep {int((1-w)*100)}%)':>16} | {ansem_mult:>9.1f}x | "
              f"{mean:>9.3f} {lo:>7.3f} {d3:>7.3f} {g2:>+8.4f} | {bank:>9.0f} | {'GO' if go else 'no'}")

    # the two limits
    print("\n  Limits:")
    for nm, pol in [("DIAMOND HOLD (no stop, catch EVERY tail incl ANSEM full)", S.P_HOLD),
                    ("disciplined P_MOON (60% trail)", S.P_MOON)]:
        mults, times, am = [], [], None
        for mint, sr, fl, tt in prepared:
            m = 0.0 if (fl is None or fl <= 0) else simulate_exit(sr, fl, tt, pol)
            mults.append(m); times.append(tt.timestamp())
            if mint == ANSEM_B:
                am = m
        for cap in (50.0, float("inf")):
            cm = S.cap_mults(mults, cap)
            mean, lo, hi = S.mean_ci(cm)
            d3 = S.drop_top(cm, 3)
            tag = "50x cap" if cap == 50 else "UNCAPPED"
            print(f"    {nm:52} [{tag:8}] ANSEM={am:7.1f}x  book mean={mean:.3f}  CIlo={lo:.3f}  drop3={d3:.3f}")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
