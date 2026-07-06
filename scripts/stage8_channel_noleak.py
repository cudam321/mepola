#!/usr/bin/env python3
"""Stage 8 — LOOKAHEAD-FIXED channel strategy: observe first hour, ENTER at the 1-hour mark.

Stage 7's apparent edge used first-hour features while entering at t+60s (lookahead). The executable
version: features over [call, call+1h] (observable), then ENTER at call+1h (you pay the higher price =
the honest late-entry cost), ride a 30d moonbag. Heavier entry slippage (3%). GO iff a selection clears
mean CI-lower>1 AND optimal-f log-growth>0 AND survives a 10x liquidity cap, OOS.

    set -a && . ./.env && set +a && PYTHONPATH=src python3 scripts/stage8_channel_noleak.py
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
from stage6_channel_powerlaw import opt_f_growth, mean_ci, hill_alpha  # noqa: E402
from stage7_channel_classifier import SIG_TYPES, ridge_fit  # noqa: E402

ENTRY_SLIP = 1.03  # conservative microcap entry slippage at the 1h-mark fill


def price_at(series, t):
    prior = [c for c in series.candles if c.ts <= t]
    return prior[-1].close if prior else None


def build():
    calls = sorted(first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json"))),
                   key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_chan30d"))
    X, M, kept = [], [], 0
    for i, s in enumerate(calls):
        if i % 200 == 0:
            print(f"\r  {i}/{len(calls)} (cache {client.hits}h/{client.misses}m)", end="", file=sys.stderr)
        if not s.mint:
            continue
        try:
            ser = merged_series(client, s.mint, s.posted_at)
        except Exception:
            ser = None
        if not (ser and ser.candles):
            continue
        t_call = s.posted_at + timedelta(seconds=60)
        ref = price_at(ser, t_call)                       # call-time reference price (observable)
        t_entry = s.posted_at + timedelta(hours=1)        # ENTER at the 1h-mark (after the feature window)
        h1 = [c for c in ser.candles if t_call <= c.ts <= t_entry]
        if ref is None or ref <= 0 or not h1:
            continue
        # entry fill at the 1h mark (worst of the entry minute + slippage)
        ewin = [c for c in ser.candles if t_entry <= c.ts <= t_entry + timedelta(minutes=2)]
        if ewin:
            fill = max(c.high for c in ewin) * ENTRY_SLIP
        else:
            pr = price_at(ser, t_entry)
            fill = pr * ENTRY_SLIP if pr else None
        if not fill or fill <= 0:
            continue
        f = extract_features(s.raw_text)
        feat = [np.log1p(f.get("entry_mc") or 0), f.get("lateness_ratio") or 1.0, f.get("time_since_entry_h") or 0.0,
                np.log1p(sum(c.volume for c in h1)),
                h1[-1].close / ref, max(c.high for c in h1) / ref, min(c.low for c in h1) / ref, float(len(h1))]
        feat += [1.0 if (f.get("signal_type") == k) else 0.0 for k in SIG_TYPES]
        X.append(feat); M.append(simulate_exit(ser, fill, t_entry, P_MOON)); kept += 1
    print("\r" + " " * 50 + "\r", end="", file=sys.stderr)
    print(f"  usable calls (entered at 1h-mark): {kept}", file=sys.stderr)
    return np.array(X), np.array(M)


def evalsel(M, tag):
    m, lo, hi = mean_ci(M)
    f, g = opt_f_growth(M)
    cap10 = np.clip(M, None, 10.0)
    _, g10 = opt_f_growth(cap10)
    print(f"  [{tag}] n={len(M)} mean={m:.2f} (CI {lo:.2f}-{hi:.2f}) alpha={hill_alpha(M):.2f} win={np.mean(M>1)*100:.0f}% "
          f"| optF={f:.3f} logG={g:+.4f} {'COMPOUNDS' if g > 0 else 'shrinks'} | cap10: mean={cap10.mean():.2f} logG={g10:+.4f}")
    return lo, g, g10


def main() -> int:
    X, M = build()
    n = len(M); cut = int(n * 0.7)
    mu, sd = X[:cut].mean(0), X[:cut].std(0) + 1e-9
    Xs = (X - mu) / sd
    w = ridge_fit(Xs[:cut], np.log(np.clip(M, 0.05, None))[:cut], lam=2.0)
    score = np.hstack([np.ones((n, 1)), Xs]) @ w
    Mtest, Stest = M[cut:], score[cut:]
    print("=" * 96)
    print(f"  STAGE 8 — LOOKAHEAD-FIXED | enter at 1h-mark + 3% slip | train n={cut} test n={n-cut}")
    print("=" * 96)
    evalsel(Mtest, "TEST-all")
    order = np.argsort(Stest)[::-1]
    go = []
    for frac in (0.33, 0.20, 0.10):
        k = max(20, int(len(Mtest) * frac))
        lo, g, g10 = evalsel(Mtest[order[:k]], f"TEST-top{int(frac*100)}%")
        if lo > 1 and g > 0 and g10 > 0:
            go.append(frac)
    print("\n" + "=" * 96)
    if go:
        print(f"  VERDICT: REAL edge survives the lookahead fix (top {[int(f*100) for f in go]}% clear CI-lower>1 +")
        print("  log-growth>0 + 10x liquidity cap). The first executable +EV found -> validate further (walk-forward).")
    else:
        print("  VERDICT: the apparent edge was LOOKAHEAD. Entering at the honest 1h-mark, no selection clears the")
        print("  bar -> the channel lead collapses to the same structural floor. Settled.")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
