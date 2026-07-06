#!/usr/bin/env python3
"""Stage 30 — execution-lag test + proper TP optimization across ALL winners.

Two questions:
 (1) Is the loss from OUR execution lag (fixable by fast execution) or from the SIGNAL being late
     (unfixable)? Compare fills from perfect-instant to conservative, on real on-chain data.
 (2) With the BEST realistic execution, what is the OPTIMAL take-profit strategy across the whole
     token distribution (not ANSEM)? Fine grid, maximize full-denominator EV; report % of tokens that
     profit, avg win vs avg loss, and the best config's gate.

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/stage30_execution_tp.py
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
from memebot.analysis.exit_sim import ExitPolicy, simulate_exit  # noqa: E402
import stage14_untruncated as S  # noqa: E402

CAP = 50.0


def entry_candle(ser, t0):
    fwd = [c for c in ser.candles if c.ts >= t0]
    return fwd[0] if fwd else None


def main() -> int:
    calls = sorted([s for s in first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_fresh.json"))) if s.mint],
                   key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_untrunc"))
    toks = []
    for i, s in enumerate(calls):
        if i % 200 == 0:
            print(f"\r  loading {i}/{len(calls)}", end="", file=sys.stderr)
        t0 = s.posted_at  # ZERO latency: the instant the signal fires
        try:
            ser = S.series_to_today(client, s.mint, t0)
        except Exception:
            ser = None
        if not ser or not ser.candles:
            continue
        ec = entry_candle(ser, t0)
        if not ec:
            continue
        toks.append((s.mint, ser, t0, ec, s.posted_at.timestamp()))
    print("\r" + " " * 40 + "\r", end="", file=sys.stderr)
    N = len(toks)
    print("=" * 100)
    print(f"  STAGE 30 — execution test + TP optimization | {N} calls | REAL on-chain OHLCV | cap {CAP}x")
    print("=" * 100)

    # (1) EXECUTION LAG TEST — same exit (moonbag), fills from perfect to conservative
    print("\n  (1) Is the lag OURS (fixable) or the SIGNAL's (not)? Same moonbag exit, vary the FILL:")
    def fill_variants(ser, t0, ec):
        return {
            "PERFECT (buy the candle LOW, 0 slip)": ec.low,
            "instant (candle OPEN, 0 slip)": ec.open,
            "instant (OPEN +1% exec slip)": ec.open * 1.01,
            "fast (close of entry candle +1%)": ec.close * 1.01,
            "current model (max-high 90s +1.5%)": S.entry_fill(ser, t0 + timedelta(seconds=60)) or ec.close,
        }
    P = ExitPolicy("moon", [(2.0, 0.5)], 0.0, 0.6, 2.0, 24 * 14)
    for label in fill_variants(toks[0][1], toks[0][2], toks[0][3]):
        res, times = [], []
        for mint, ser, t0, ec, ts in toks:
            f = fill_variants(ser, t0, ec)[label]
            if not f or f <= 0:
                res.append(0.0); times.append(ts); continue
            res.append(simulate_exit(ser, f, ec.ts, P)); times.append(ts)
        cm = S.cap_mults(res, CAP); mean, lo, hi = S.mean_ci(cm)
        print(f"      {label:38} mean={mean:6.3f}  CIlo={lo:6.3f}  {'GO' if lo>1 else ''}")
    print("      => if PERFECT (0-latency, buy-the-low) is still <1, the lag is the SIGNAL, not execution.")

    # (2) OPTIMAL TP across ALL tokens, using the best realistic fill (instant open +1%)
    print("\n  (2) OPTIMAL take-profit across ALL tokens (best realistic fill = instant open +1%):")
    fills = {}
    for mint, ser, t0, ec, ts in toks:
        fills[mint] = ec.open * 1.01
    best = None
    configs = []
    for tp in [1.1, 1.2, 1.3, 1.5, 1.75, 2.0, 2.5, 3.0]:      # sell-all fixed TP
        for sl in [0.0, 0.5, 0.7]:
            configs.append((f"sell100%@{tp}x SL{int((1-sl)*100) if sl else 0}", ExitPolicy("x", [(tp, 1.0)], sl, 1.0, float("inf"), 1e9)))
    configs.append(("half@1.5 + trail40", ExitPolicy("x", [(1.5, 0.5)], 0.0, 0.4, 1.5, 24*7)))
    configs.append(("ladder1.5/2/3 + trail", ExitPolicy("x", [(1.5, .34), (2, .33), (3, .33)], 0.0, 0.5, 1.5, 24*7)))
    rows = []
    for name, pol in configs:
        res, times = [], []
        for mint, ser, t0, ec, ts in toks:
            res.append(simulate_exit(ser, fills[mint], ec.ts, pol)); times.append(ts)
        cm = S.cap_mults(res, CAP); mean, lo, hi = S.mean_ci(cm); d3 = S.drop_top(cm, 3)
        g2 = S.fixed_f_growth(cm, 0.02); bank = S.single_pass_bankroll(cm, np.asarray(times), 0.02, float("inf"))
        a = np.array(cm); winr = (a > 1).mean() * 100
        rows.append((lo, name, mean, lo, d3, g2, bank, winr, a))
    rows.sort(reverse=True)
    print(f"      {'TP config':26} {'mean':>6} {'CIlo':>6} {'drop3':>6} {'logG':>8} {'win%':>5} {'$500':>7}")
    for lo, name, mean, cil, d3, g2, bank, winr, a in rows[:8]:
        print(f"      {name:26} {mean:>6.3f} {cil:>6.3f} {d3:>6.3f} {g2:>+8.4f} {winr:>4.0f}% {bank:>7.0f} {'GO' if (cil>1 and d3>1 and g2>0 and bank>500) else ''}")

    # (3) address 'tons of tokens make profit' under the BEST config
    lo, name, mean, cil, d3, g2, bank, winr, a = rows[0]
    wins = a[a > 1]; losses = a[a <= 1]
    print(f"\n  (3) under the BEST TP config ({name}): {winr:.0f}% of tokens PROFIT (you're right, it's a lot!)")
    print(f"      avg WIN {wins.mean():.2f}x over {len(wins)} tokens  |  avg LOSS {losses.mean():.2f}x over {len(losses)} tokens")
    print(f"      book = {winr/100:.2f} x {wins.mean():.2f}  +  {1-winr/100:.2f} x {losses.mean():.2f}  = {mean:.3f}x per trade")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
