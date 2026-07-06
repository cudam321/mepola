#!/usr/bin/env python3
"""Stage 13 — is the selected-survivor edge REAL or artifact #3? Decisive verification.

stage12 showed selected-top20% survivors with E[M]=1.53 and $500->$5000 in a resampled bankroll sim.
Red flags: (1) same method gave 0.86 last run (sample-unstable), (2) the sim resampled WITH replacement
(over-counts the tail), (3) likely tail-driven. Here we kill all three: deterministic ORDER BY sampling,
STABILITY across disjoint samples, drop-top robustness, a 10x liquidity cap, and a SINGLE-PASS bankroll
(trade each token ONCE, chronological, no resampling) with and without the single best token.

    set -a && . ./.env && set +a && PYTHONPATH=src python3 scripts/stage13_verify.py
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from memebot.data.dune import DuneClient  # noqa: E402
from memebot.data.cache import CachedPriceClient  # noqa: E402
from memebot.data.jupiter import JupiterChartsClient  # noqa: E402
from memebot.analysis.exit_sim import simulate_exit  # noqa: E402
from stage4_powerlaw import P_MOON  # noqa: E402
from stage7_channel_classifier import ridge_fit  # noqa: E402
from stage9_survivor import parse_ts, H, D, HOLD_DAYS  # noqa: E402
from stage12_bankroll_sim import feats, GAS_PER_SIDE, B0  # noqa: E402

FILL_SLIP = 1.05


def det_grad_sql(ws, we, limit, offset):
    return (f"SELECT basemint AS mint, min(created_at) AS grad FROM pumpswap_solana.pools "
            f"WHERE is_valid_pool = true AND created_at >= TIMESTAMP '{ws}' AND created_at < TIMESTAMP '{we}' "
            f"GROUP BY basemint ORDER BY basemint OFFSET {offset} LIMIT {limit}")


def collect(dune, client, ws, we, limit, offset):
    rows = dune.run_sql(det_grad_sql(ws, we, limit, offset))["rows"]
    X, M, T = [], [], []
    for i, r in enumerate(rows):
        if i % 300 == 0:
            print(f"\r    {ws[:7]}+{offset} {i}/{len(rows)} (cache {client.hits}h/{client.misses}m)", end="", file=sys.stderr)
        grad = parse_ts(r["grad"])
        try:
            ser = client.get_price_series(r["mint"], grad - H(1), grad + D(HOLD_DAYS + 8))
        except Exception:
            ser = None
        if not (ser and ser.candles):
            continue
        cands = ser.candles
        atg = [c for c in cands if c.ts >= grad]
        win = [c for c in cands if grad <= c.ts <= grad + H(24)]
        after = [c for c in cands if c.ts > grad + H(24)]
        if not atg or len(win) < 3 or not after:
            continue
        ref = atg[0].open or atg[0].close
        if not ref or ref <= 0:
            continue
        X.append(feats(win, ref)); M.append(simulate_exit(ser, after[0].high * FILL_SLIP, after[0].ts, P_MOON))
        T.append(after[0].ts)
    print("", file=sys.stderr)
    return np.array(X), np.array(M), T


def select_oos(Xtr, Mtr, Xte, frac=0.2):
    mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
    w = ridge_fit((Xtr - mu) / sd, np.log(np.clip(Mtr, 0.05, None)), lam=2.0)
    sc = np.hstack([np.ones((len(Xte), 1)), (Xte - mu) / sd]) @ w
    return np.argsort(sc)[::-1][:max(20, int(len(Xte) * frac))]


def single_pass(mults, times, frac=0.05, cap=50.0, liq_cap=None):
    idx = np.argsort(times)
    B = B0
    for j in idx:
        m = min(mults[j], liq_cap) if liq_cap else mults[j]
        stake = min(frac * B, cap, B)
        if B < 10:
            break
        B = B - stake - 2 * GAS_PER_SIDE + stake * m
    return B


def main() -> int:
    dune = DuneClient()
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_surv"))
    Xtr, Mtr, _ = collect(dune, client, "2026-01-01", "2026-04-01", 1500, 0)
    print("=" * 100)
    print("  STAGE 13 — VERIFY selected-survivor edge: stability / drop-top / liquidity cap / single-pass bankroll")
    print("=" * 100)
    print(f"  train survivors usable: {len(Mtr)}")
    print("\n  [1] STABILITY — selected-top20% OOS mean across 4 DISJOINT deterministic samples:")
    means = []
    for s in range(4):
        Xte, Mte, Tte = collect(dune, client, "2026-04-01", "2026-06-01", 700, s * 700)
        if len(Mte) < 50:
            continue
        sel = select_oos(Xtr, Mtr, Xte)
        ms = Mte[sel]
        a = np.sort(ms)[::-1]
        drop1 = float(a[1:].mean()) if len(a) > 1 else float("nan")
        drop3 = float(a[3:].mean()) if len(a) > 3 else float("nan")
        means.append(float(ms.mean()))
        print(f"    sample{s}: n_sel={len(sel)} mean={ms.mean():.2f} median={np.median(ms):.2f} win={np.mean(ms>1)*100:.0f}% "
              f"| drop-top1={drop1:.2f} drop-top3={drop3:.2f} | top3={np.round(a[:3],1)}")
        if s == 0:
            X0, M0, T0, sel0 = Xte, Mte, Tte, sel
    print(f"    -> selected-mean across samples: {np.round(means,2)}  (std {np.std(means):.2f}) "
          f"{'STABLE' if means and np.std(means) < 0.25 and min(means) > 1.1 else 'UNSTABLE / not robustly >1'}")

    print("\n  [2] SINGLE-PASS bankroll on sample0 selected tokens (trade each ONCE, chronological, no resampling):")
    msel = M0[sel0]; tsel = [T0[j] for j in sel0]
    for frac, cap in ((0.05, 50.0), (0.10, 100.0)):
        full = single_pass(msel, tsel, frac, cap)
        capd = single_pass(msel, tsel, frac, cap, liq_cap=10.0)
        # drop the single best token
        worst_idx = int(np.argmax(msel))
        keep = [k for k in range(len(msel)) if k != worst_idx]
        notop = single_pass(msel[keep], [tsel[k] for k in keep], frac, cap)
        print(f"    size={int(frac*100)}%: ${B0:.0f} -> ${full:,.0f} | 10x-liq-capped ${capd:,.0f} | drop-best-token ${notop:,.0f}")
    print("\n" + "=" * 100)
    print("  REAL iff: stable mean robustly >1 across samples AND survives drop-top AND single-pass grows AND")
    print("  survives the liquidity cap AND doesn't collapse when the single best token is removed. Else artifact #3.")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
