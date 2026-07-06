#!/usr/bin/env python3
"""Stage 15 — does a proper TP/SL algo capture the pump before the dump?

User's hypothesis: every memecoin pumps 3-10x before going to zero, so a proper stop-loss +
take-profit on EACH token secures profit / cuts loss and turns the book positive. This tests it
directly on the un-truncated full denominator (1263 first-call tokens, warm cache):

  (A) MFE distribution — what fraction of tokens EVER reach 1.5x/2x/3x/5x/10x from the follower's
      entry? (tests the premise "they all pump first").
  (B) PERFECT fixed take-profit curve — sell 100% the instant price touches L (no slippage,
      perfect fill), else ride to end. EV(L) = mean[ L if MFE>=L else hold ]. If max over L < 1,
      NO fixed take-profit can win even with perfect execution. This is the theoretical ceiling
      for "just TP it."
  (C) Realistic TP x SL grid via simulate_exit (pessimistic intrabar, modelled stop slippage):
      sweep take-profit levels x hard stops x ladders. Full-denominator mean / bootstrap CIlo /
      drop-top3 / f=2% log-growth / $500 single-pass, at a realistic 50x cap.

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/stage15_tp_sl_grid.py
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
import stage14_untruncated as S  # series_to_today, entry_fill, metrics  # noqa: E402

CAP = 50.0  # realistic realizable-exit cap


def agg(name, mults, times):
    a = np.asarray(mults, dtype=float)
    cm = S.cap_mults(mults, CAP)
    m, lo, hi = S.mean_ci(cm)
    d3 = S.drop_top(cm, 3)
    g2 = S.fixed_f_growth(cm, 0.02)
    bank = S.single_pass_bankroll(cm, times, f=0.02, cap=float("inf"))
    win = float((a > 1).mean()) * 100
    go = lo > 1 and d3 > 1 and g2 > 0 and bank > 500
    print(f"  {name:30} mean={m:6.3f} CIlo={lo:6.3f} drop3={d3:6.3f} f2logG={g2:+.4f} "
          f"win={win:3.0f}% $500->{bank:8.0f} {'*** GO ***' if go else 'no'}")
    return go


def main() -> int:
    calls = sorted(first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json"))),
                   key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_untrunc"))

    series_by, fill_by, mfe, hold, times = {}, {}, [], [], []
    mints = []
    for i, s in enumerate(calls):
        if not s.mint:
            continue
        if i % 100 == 0:
            print(f"\r  loading {i}/{len(calls)} (cache {client.hits}h/{client.misses}m)", end="", file=sys.stderr)
        t = s.posted_at + timedelta(seconds=S.LAT_S)
        try:
            ser = S.series_to_today(client, s.mint, s.posted_at)
        except Exception:
            ser = None
        fill = S.entry_fill(ser, t) if (ser and ser.candles) else None
        mints.append(s.mint)
        series_by[s.mint] = ser
        fill_by[s.mint] = (ser, fill, t)
        if fill is None or fill <= 0:
            mfe.append(0.0); hold.append(0.0); times.append(t.timestamp()); continue
        fwd = [c.high for c in ser.candles if c.ts >= t]
        mfe.append(max(fwd) / fill if fwd else 0.0)
        hold.append(simulate_exit(ser, fill, t, S.P_HOLD))
        times.append(t.timestamp())
    print("\r" + " " * 60 + "\r", end="", file=sys.stderr)
    times = np.asarray(times)
    mfe = np.asarray(mfe); hold = np.asarray(hold)
    n = len(mfe)
    print("=" * 100)
    print(f"  STAGE 15 — TP/SL grid on {n} un-truncated calls | cap {CAP:.0f}x")
    print("=" * 100)

    # (A) premise check: do they actually pump first?
    print("\n  (A) MFE — fraction of tokens that EVER reach a multiple from the follower's entry:")
    for L in [1.5, 2, 3, 5, 10, 20, 50, 100]:
        print(f"        reach {L:>5.1f}x : {(mfe >= L).mean()*100:5.1f}%   ({int((mfe>=L).sum())} tokens)")
    print(f"        MFE median = {np.median(mfe):.3f}x   (the MEDIAN token's best-ever move)")

    # (B) perfect fixed take-profit ceiling
    print("\n  (B) PERFECT fixed take-profit (sell 100% at first touch of L, else ride to end):")
    best = (0.0, None)
    for L in [1.2, 1.3, 1.5, 1.75, 2, 2.5, 3, 4, 5, 7, 10, 15, 25]:
        ev = np.where(mfe >= L, np.minimum(L, CAP), hold).mean()
        flag = "  <-- best" if ev > best[0] else ""
        if ev > best[0]:
            best = (ev, L)
        print(f"        TP@{L:>5.1f}x : EV = {ev:6.3f}x{flag}")
    print(f"        => BEST perfect-TP EV = {best[0]:.3f}x at L={best[1]}x  "
          f"({'CLEARS 1.0' if best[0] > 1 else 'still < 1.0 — no take-profit can win even with perfect fills'})")

    # (C) realistic TP x SL grid via simulate_exit (pessimistic, modelled slippage)
    print("\n  (C) REALISTIC executable TP x SL grid (pessimistic intrabar, stop slippage modelled):")
    def run(pol):
        out = []
        for mint in mints:
            ser, fill, t = fill_by[mint]
            out.append(0.0 if (fill is None or fill <= 0) else simulate_exit(ser, fill, t, pol))
        return out

    policies = []
    # sell-all take-profits, no stop (TP or ride to zero)
    for L in [1.5, 2, 3, 5, 10]:
        policies.append(ExitPolicy(f"TP{L:g}x_allnoSL", [(L, 1.0)], 0.0, 1.0, float("inf"), 1e9))
    # take-profit + hard stop (cut losers fast — the user's "cut loss everytime")
    for L in [2, 3, 5]:
        for Sl in [0.5, 0.7, 0.8]:
            policies.append(ExitPolicy(f"TP{L:g}x_SL{int((1-Sl)*100)}%", [(L, 1.0)], Sl, 1.0, float("inf"), 1e9))
    # ladders + stop (scale out + moonbag + cut)
    policies.append(ExitPolicy("ladder2/3/5_SL30%", [(2, .4), (3, .3), (5, .3)], 0.7, 1.0, float("inf"), 1e9))
    policies.append(ExitPolicy("ladder2/3/5_trail+SL", [(2, .4), (3, .3), (5, .3)], 0.6, 0.4, 2.0, 24 * 7))
    # tight trailing (best from the bull sweep) + de-risk
    policies.append(ExitPolicy("half@1.5_trail15%_SL40%", [(1.5, .5)], 0.6, 0.15, 1.2, 24 * 7))
    policies.append(ExitPolicy("trail15%_armed_now", [], 0.0, 0.15, 1.0, 1e9))

    any_go = False
    for pol in policies:
        any_go |= agg(pol.name, run(pol), times)

    print("\n" + "=" * 100)
    print(f"  VERDICT: {'A TP/SL POLICY CLEARS THE BAR — investigate!' if any_go else 'NO TP/SL policy clears CIlo>1 + drop3>1 + compounding + $500 grows.'}")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
