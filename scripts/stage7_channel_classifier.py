#!/usr/bin/env python3
"""Stage 7 — push the channel lead: proper classifier + threshold sweep + liquidity-haircut robustness.

Stage 6 found the OOS feature-selected channel subset is ~breakeven (mean 0.98, broad-based). Here we
fit a ridge model on the FULL feature set (incl. signal-type), sweep the selection fraction, and stress
each selection with a liquidity cap on the moonbag tail (can you really sell the 10-140x?). GO iff a
selection clears mean CI-lower > 1 AND positive optimal-f log-growth AND survives a 10x liquidity cap.
Cache (jupiter_chan30d) is warm -> fast.

    set -a && . ./.env && set +a && PYTHONPATH=src python3 scripts/stage7_channel_classifier.py
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
from memebot.analysis.features import extract_features  # noqa: E402
from memebot.data.cache import CachedPriceClient  # noqa: E402
from memebot.data.jupiter import JupiterChartsClient  # noqa: E402
from memebot.analysis.exit_sim import simulate_exit  # noqa: E402
from stage4_powerlaw import P_MOON  # noqa: E402
from stage5_tokenfeatures import merged_series  # noqa: E402
from stage6_channel_powerlaw import fill_at, opt_f_growth, mean_ci, hill_alpha, LAT_S  # noqa: E402

SIG_TYPES = ["main", "smartmoney", "volume", "buymore", "holding", "profit", "cto", "other"]


def build():
    calls = sorted(first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json"))),
                   key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_chan30d"))
    X, M, st = [], [], []
    for i, s in enumerate(calls):
        if i % 200 == 0:
            print(f"\r  {i}/{len(calls)} (cache {client.hits}h/{client.misses}m)", end="", file=sys.stderr)
        if not s.mint:
            continue
        f = extract_features(s.raw_text)
        t = s.posted_at + timedelta(seconds=LAT_S)
        try:
            ser = merged_series(client, s.mint, s.posted_at)
        except Exception:
            ser = None
        fill = fill_at(ser, t) if (ser and ser.candles) else None
        if fill is None:
            continue
        h1 = [c for c in ser.candles if t <= c.ts <= t + timedelta(hours=1)]
        h1ret = (h1[-1].close / fill) if h1 else 0.0
        h1max = (max(c.high for c in h1) / fill) if h1 else 0.0
        h1min = (min(c.low for c in h1) / fill) if h1 else 0.0
        feat = [np.log1p(f.get("entry_mc") or 0), f.get("lateness_ratio") or 1.0, f.get("time_since_entry_h") or 0.0,
                np.log1p(sum(c.volume for c in h1)), h1ret, h1max, h1min, float(len(h1))]
        feat += [1.0 if (f.get("signal_type") == k) else 0.0 for k in SIG_TYPES]
        X.append(feat); M.append(simulate_exit(ser, fill, t, P_MOON)); st.append(s.posted_at)
    print("\r" + " " * 50 + "\r", end="", file=sys.stderr)
    return np.array(X), np.array(M), st


def ridge_fit(X, y, lam=1.0):
    Xb = np.hstack([np.ones((len(X), 1)), X])
    A = Xb.T @ Xb + lam * np.eye(Xb.shape[1])
    return np.linalg.solve(A, Xb.T @ y)


def evalsel(M, tag, caps=(None, 20.0, 10.0, 5.0)):
    base_m, lo, hi = mean_ci(M)
    f, g = opt_f_growth(M)
    line = (f"  [{tag}] n={len(M)} mean={base_m:.2f} (CI {lo:.2f}-{hi:.2f}) alpha={hill_alpha(M):.2f} "
            f"win={np.mean(M>1)*100:.0f}% | optF={f:.3f} logG={g:+.4f} {'COMPOUNDS' if g>0 else 'shrinks'}")
    caps_s = "  ".join(
        f"cap{int(c) if c else 0}:mean={np.clip(M, None, c).mean():.2f},logG={opt_f_growth(np.clip(M, None, c))[1]:+.4f}"
        for c in caps if c)
    print(line)
    print(f"        liquidity caps -> {caps_s}")
    return base_m, lo, g


def main() -> int:
    X, M, dates = build()
    n = len(M); cut = int(n * 0.7)
    mu, sd = X[:cut].mean(0), X[:cut].std(0) + 1e-9
    Xs = (X - mu) / sd
    y = np.log(np.clip(M, 0.05, None))
    w = ridge_fit(Xs[:cut], y[:cut], lam=2.0)
    score = np.hstack([np.ones((n, 1)), Xs]) @ w
    Mtest, Stest = M[cut:], score[cut:]
    print("=" * 96)
    print(f"  STAGE 7 — channel classifier | train n={cut} test n={n-cut} | ridge on full features")
    print("=" * 96)
    print("  feature weights (standardized):")
    fnames = ["log_entry_mc", "lateness", "tse_h", "log_h1_vol", "h1_ret", "h1_max", "h1_min", "h1_n"] + ["sig_" + s for s in SIG_TYPES]
    for nm, wt in sorted(zip(fnames, w[1:]), key=lambda z: abs(z[1]), reverse=True)[:8]:
        print(f"     {nm:14} {wt:+.3f}")
    print()
    evalsel(Mtest, "TEST-all")
    order = np.argsort(Stest)[::-1]
    for frac in (0.33, 0.20, 0.10):
        k = max(20, int(len(Mtest) * frac))
        evalsel(Mtest[order[:k]], f"TEST-top{int(frac*100)}%")
    print("\n" + "=" * 96)
    print("  GO iff a selection has mean CI-lower>1 AND logG>0 AND still logG>0 under a 10x liquidity cap.")
    print("  Else: the channel lead tops out at ~breakeven; the structural floor caps it.")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
