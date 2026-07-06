#!/usr/bin/env python3
"""Post-bump micro-scalp — enter at the post, exit within MINUTES for a small gain.

The premise is DIFFERENT from the moonbag work: the POST itself may cause a short
follower-driven bump. Enter at the post (max-high in the 90s reaction window +1.5% slip,
same conservative fill as stage14), then either take a tiny profit (TP +8/12/20/50%) or
bail via a tight ABSOLUTE time-stop (5/15/30/60 min). Eat the ~99% that do not bump.

Every scalp is measured on the FULL denominator (all 1371 first-call tokens, incl. the
~76% near-total-losses and the uncharted/dead-at-entry -> 0). Honest gate (RESEARCH.md):
an executable policy passes only if CIlo>1 AND drop-top3>1 AND f=2% logG>0 AND $500
single-pass grows (50x cap irrelevant — scalp upside is bounded at the TP). OOS = time split.

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/stage_scalp.py
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import stage14_untruncated as S  # series_to_today, entry_fill, mean_ci, drop_top, ...
from memebot.data.cache import CachedPriceClient
from memebot.data.jupiter import JupiterChartsClient
from memebot.ingest.telegram_mcp import load_corpus_json, first_call_per_mint

LAT_S = 60.0            # channel-follower read->react latency (same as stage14)
TP_COST = 0.015        # limit-sell into a bump: 1.5% cost/slip
EXIT_COST = 0.030      # market/time-stop exit into a (usually dumping) thin pool: 3%
HOLDS = [5, 15, 30, 60]           # absolute time-stop windows, minutes
TPS = [0.08, 0.12, 0.20, 0.50]    # tiny take-profit levels (+8/12/20/50%)


def scalp(candles, entry, t_fill, tp_mult, hold_min, stop_mult=0.0,
          tp_cost=TP_COST, exit_cost=EXIT_COST):
    """Realized multiple for ONE micro-scalp.

    - candles: minute bars (already filtered to ts >= t_fill).
    - Absolute time-stop at hold_min (NOT a trailing/no-new-high stop).
    - PESSIMISTIC intrabar: within a bar, stop(low) is checked before TP(high).
    - TP fill at tp_mult*(1-tp_cost); stop/time-stop fill *(1-exit_cost).
    - Dead / no candles -> 0.
    """
    if entry <= 0 or not candles:
        return 0.0
    deadline = t_fill + timedelta(minutes=hold_min)
    win = [c for c in candles if c.ts <= deadline]
    if not win:
        return 0.0
    hard = stop_mult * entry
    for c in win:
        if stop_mult > 0 and c.low <= hard:
            return stop_mult * (1 - exit_cost)
        if c.high >= tp_mult * entry:
            return tp_mult * (1 - tp_cost)
    # time-stop: exit at the close of the last bar inside the window (market)
    return (win[-1].close / entry) * (1 - exit_cost)


def window_peak(candles, entry, t_fill, hold_min, exit_cost=TP_COST):
    """UPPER BOUND (LOOKAHEAD): sell into the perfect intra-window high. The ~0.77x ceiling."""
    if entry <= 0 or not candles:
        return 0.0
    deadline = t_fill + timedelta(minutes=hold_min)
    win = [c for c in candles if c.ts <= deadline]
    if not win:
        return 0.0
    return (max(c.high for c in win) / entry) * (1 - exit_cost)


def gate_row(mults, times, label):
    a = np.asarray(mults, dtype=float)
    m, lo, hi = S.mean_ci(mults)
    d1, d3 = S.drop_top(mults, 1), S.drop_top(mults, 3)
    g2 = S.fixed_f_growth(mults, 0.02)
    bank = S.single_pass_bankroll(mults, times, 0.02, float("inf"))
    win = float((a > 1).mean()) * 100
    go = (lo > 1) and (d3 > 1) and (g2 > 0) and (bank > 500)
    flag = "  <== GO" if go else ""
    print(f"  {label:28} n={len(a)} win={win:4.1f}% mean={m:.4f} CIlo={lo:.4f} CIhi={hi:.4f} "
          f"d1={d1:.4f} d3={d3:.4f} f2logG={g2:+.5f} $500={bank:7.0f}{flag}")
    return dict(label=label, n=int(len(a)), win_pct=win, mean=m, ci_lo=lo, ci_hi=hi,
                drop1=d1, drop3=d3, f2_logG=g2, bank_500=bank, go=bool(go))


def main():
    calls = first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_fresh.json")))
    calls = sorted(calls, key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4),
                               str(ROOT / "data_cache" / "jupiter_untrunc"))

    # per-token: entry fill + minute candles after entry (warm cache -> instant)
    recs = []  # (mint, posted_ts, entry, candles_after)
    dead = 0
    for i, s in enumerate(calls):
        if i % 100 == 0:
            print(f"\r  pricing {i}/{len(calls)} (cache {client.hits}h/{client.misses}m)",
                  end="", file=sys.stderr)
        if not s.mint:
            continue
        t = s.posted_at + timedelta(seconds=LAT_S)
        try:
            ser = S.series_to_today(client, s.mint, s.posted_at)
        except Exception:
            ser = None
        fill = S.entry_fill(ser, t) if (ser and ser.candles) else None
        if fill is None or fill <= 0:
            recs.append((s.mint, s.posted_at.timestamp(), 0.0, []))
            dead += 1
            continue
        after = [c for c in ser.candles if c.ts >= t]
        recs.append((s.mint, s.posted_at.timestamp(), fill, after))
    print("\r" + " " * 70 + "\r", end="", file=sys.stderr)
    print(f"  priced {len(recs)} tokens | cache {client.hits}h/{client.misses}m | dead-at-entry -> 0: {dead}")

    times = np.array([r[1] for r in recs])
    from datetime import datetime, timezone
    dts = [datetime.fromtimestamp(r[1], tz=timezone.utc) for r in recs]

    # ---- FULL SAMPLE grid ----
    print("\n=== FULL SAMPLE (all 1371) — executable scalp: TP or absolute time-stop, no hard stop ===")
    grid = {}
    for hold in HOLDS:
        for tp in TPS:
            mults = [scalp(r[3], r[2], datetime.fromtimestamp(r[1], tz=timezone.utc), 1 + tp, hold)
                     for r in recs]
            grid[(hold, tp)] = mults
            gate_row(mults, times, f"TP+{int(tp*100)}% / {hold}min")
        print()

    # tight-stop variants for a couple of configs (does a hard stop help the losers?)
    print("=== FULL SAMPLE — with a tight hard stop (-30%) to cut the non-bumpers faster ===")
    for hold in [15, 30]:
        for tp in [0.12, 0.20]:
            mults = [scalp(r[3], r[2], datetime.fromtimestamp(r[1], tz=timezone.utc),
                           1 + tp, hold, stop_mult=0.70) for r in recs]
            gate_row(mults, times, f"TP+{int(tp*100)}%/-30%/{hold}min")
    print()

    # pure time-stop (no TP): just ride the post-bump for H min then market out
    print("=== FULL SAMPLE — pure time-stop, NO TP (ride the reaction, market out at H min) ===")
    for hold in HOLDS:
        mults = [scalp(r[3], r[2], datetime.fromtimestamp(r[1], tz=timezone.utc),
                       1e9, hold) for r in recs]  # TP unreachable -> pure time-stop
        gate_row(mults, times, f"time-stop {hold}min")
    print()

    # ---- LOOKAHEAD upper bound (the ~0.77x perfect-TP ceiling) ----
    print("=== UPPER BOUND (LOOKAHEAD, NOT executable): sell into perfect intra-window high ===")
    for hold in HOLDS:
        mults = [window_peak(r[3], r[2], datetime.fromtimestamp(r[1], tz=timezone.utc), hold)
                 for r in recs]
        a = np.asarray(mults)
        print(f"  peak-in-{hold:>2}min   mean={a.mean():.4f}  median={np.median(a):.4f}  "
              f"win={float((a>1).mean())*100:.1f}%  max={a.max():.1f}x")
    print()

    # ---- best config summary + drop-top3 sensitivity ----
    best = max(grid.items(), key=lambda kv: S.mean_ci(kv[1])[0])
    (bh, btp), bm = best
    print(f"=== BEST-MEAN executable config: TP+{int(btp*100)}% / {bh}min ===")
    am = np.asarray(bm)
    print(f"  full mean={am.mean():.4f}  win={float((am>1).mean())*100:.1f}%  "
          f"n_winners={(am>1).sum()}  max={am.max():.3f}x")
    # winners are all ~ (1+tp)*(1-cost); confirm bounded upside (no fat tail -> cap irrelevant)
    print(f"  distinct winner multiple ~= {(1+btp)*(1-TP_COST):.4f} (bounded; drop-top3 ~ no-op)")

    # ---- OOS: time split (train first 60% of calendar, test last 40%) ----
    print("\n=== OOS TIME SPLIT (chronological; does ANY config hold in BOTH halves?) ===")
    order = np.argsort(times)
    n = len(order)
    cut = int(n * 0.6)
    train_idx = set(order[:cut].tolist())
    test_idx = set(order[cut:].tolist())
    split_ts = dts[order[cut]]
    print(f"  split at {split_ts.date()} | train n={len(train_idx)} test n={len(test_idx)}")
    any_both = False
    for hold in HOLDS:
        for tp in TPS:
            mults = grid[(hold, tp)]
            tr = [mults[j] for j in range(n) if j in train_idx]
            te = [mults[j] for j in range(n) if j in test_idx]
            trm = np.mean(tr); tem = np.mean(te)
            trlo = S.mean_ci(tr)[1]; telo = S.mean_ci(te)[1]
            both = trlo > 1 and telo > 1
            any_both = any_both or both
            mark = "  <== both-CIlo>1" if both else ""
            print(f"  TP+{int(tp*100)}%/{hold}min  train mean={trm:.4f} CIlo={trlo:.4f} | "
                  f"test mean={tem:.4f} CIlo={telo:.4f}{mark}")
        print()

    print("=" * 96)
    print(f"  VERDICT: any executable config passing full gate? "
          f"{'YES' if any(gate_go(grid, HOLDS, TPS, times)) else 'NO'} | any OOS both-halves? "
          f"{'YES' if any_both else 'NO'}")
    print("=" * 96)


def gate_go(grid, holds, tps, times):
    out = []
    for hold in holds:
        for tp in tps:
            mults = grid[(hold, tp)]
            a = np.asarray(mults)
            m, lo, hi = S.mean_ci(mults)
            d3 = S.drop_top(mults, 3)
            g2 = S.fixed_f_growth(mults, 0.02)
            bank = S.single_pass_bankroll(mults, times, 0.02, float("inf"))
            out.append(lo > 1 and d3 > 1 and g2 > 0 and bank > 500)
    return out


if __name__ == "__main__":
    main()
