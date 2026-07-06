#!/usr/bin/env python3
"""Stage 6 — power-law-NATIVE evaluation of a multi-feature selector on the TG channel's calls.

Answers the user's correct objection ("a power law can't be judged by a single %"): instead of one
arithmetic EV, evaluate with (1) the tail index alpha (Hill) — is the mean even defined? (2) optimal-f
expected LOG-GROWTH max_f E[log(1+f(M-1))] — does the BOOK compound under survival-first sizing? and
(3) the full terminal-wealth distribution via Monte Carlo. Universe = @your_channel first-call tokens
(curated 1263). Multi-feature selection (channel metadata + on-chain early activity) picks the tail-prone
subset; train on early calls, test on later calls (OOS).

    set -a && . ./.env && set +a && PYTHONPATH=src python3 scripts/stage6_channel_powerlaw.py
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
from stage4_powerlaw import P_MOON, P_HOLD, HOLD_DAYS  # noqa: E402
from stage5_tokenfeatures import merged_series  # noqa: E402

LAT_S = 60.0  # channel-follower read-react latency


def fill_at(series, t):
    win = [c for c in series.candles if t <= c.ts <= t + timedelta(seconds=90)]
    if win:
        return max(c.high for c in win) * 1.015
    prior = [c for c in series.candles if c.ts <= t]
    return prior[-1].high * 1.015 if prior else None


# ---- power-law-native metrics -------------------------------------------------
def hill_alpha(mults, tail_frac=0.10):
    a = np.sort(np.asarray([m for m in mults if m > 0], dtype=float))[::-1]
    k = max(5, int(len(a) * tail_frac))
    if len(a) <= k:
        return float("nan")
    top = a[:k]
    return float(1.0 / np.mean(np.log(top / a[k])))  # Hill estimator (alpha)


def opt_f_growth(mults, grid=None):
    a = np.asarray(mults, dtype=float)
    grid = grid if grid is not None else np.linspace(0.005, 0.5, 60)
    best_f, best_g = 0.0, -1e9
    for f in grid:
        g = float(np.mean(np.log(np.maximum(1 + f * (a - 1), 1e-9))))
        if g > best_g:
            best_g, best_f = g, f
    return best_f, best_g


def mean_ci(mults, n=5000, seed=0):
    a = np.asarray(mults, dtype=float)
    if len(a) < 2:
        return float(a.mean()), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    bs = a[rng.integers(0, len(a), size=(n, len(a)))].mean(axis=1)
    return float(a.mean()), float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))


def portfolio_mc(mults, f, n_bets, n_sims=5000, seed=0):
    a = np.asarray(mults, dtype=float)
    rng = np.random.default_rng(seed)
    finals = []
    for _ in range(n_sims):
        draws = a[rng.integers(0, len(a), size=n_bets)]
        finals.append(float(np.prod(1 + f * (draws - 1))))
    fin = np.asarray(finals)
    return dict(median=float(np.median(fin)), mean=float(fin.mean()),
                p_profit=float((fin > 1).mean()), p5=float(np.percentile(fin, 5)), p95=float(np.percentile(fin, 95)))


def report(tag, mults):
    if len(mults) < 10:
        print(f"  [{tag}] n={len(mults)} too few"); return
    m, lo, hi = mean_ci(mults)
    a = hill_alpha(mults)
    f, g = opt_f_growth(mults)
    mc = portfolio_mc(mults, f, n_bets=min(200, len(mults)))
    print(f"  [{tag}] n={len(mults)}  mean_mult={m:.2f} (CI {lo:.2f}-{hi:.2f})  alpha={a:.2f}  win={np.mean([x>1 for x in mults])*100:.0f}%")
    print(f"        optimal-f={f:.3f}  E[log-growth]/bet={g:+.4f}  -> {'BOOK COMPOUNDS' if g>0 else 'book shrinks'}")
    print(f"        portfolio(200 bets @ f): median_x={mc['median']:.2f}  mean_x={mc['mean']:.2f}  P(profit)={mc['p_profit']*100:.0f}%  p5={mc['p5']:.2f}  p95={mc['p95']:.1f}")


def main() -> int:
    calls = first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json")))
    calls = sorted(calls, key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_chan30d"))
    print("=" * 92)
    print(f"  STAGE 6 — channel calls ({len(calls)}) | 30d moonbag | power-law-native eval")
    print("=" * 92)

    rows = []  # (signal, feats, moon, hold, mfe, jup_features)
    for i, s in enumerate(calls):
        if i % 100 == 0:
            print(f"\r  pricing {i}/{len(calls)} (cache {client.hits}h/{client.misses}m)", end="", file=sys.stderr)
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
            rows.append((s, f, 0.0, 0.0, 0.0, {})); continue
        fwd = [c for c in ser.candles if c.ts >= t]
        h1 = [c for c in ser.candles if t <= c.ts <= t + timedelta(hours=1)]
        jf = {"h1_vol": sum(c.volume for c in h1), "h1_ret": (h1[-1].close / fill if h1 else 0.0),
              "h1_max": (max(c.high for c in h1) / fill if h1 else 0.0), "h1_n": len(h1)}
        rows.append((s, f, simulate_exit(ser, fill, t, P_MOON), simulate_exit(ser, fill, t, P_HOLD),
                     (max(c.high for c in fwd) / fill if fwd else 0.0), jf))
    print("\r" + " " * 50 + "\r", end="", file=sys.stderr)

    moon_all = [r[2] for r in rows]
    print("\n  --- ALL CHANNEL CALLS (full population, moonbag 30d) ---")
    report("all-moonbag", moon_all)
    report("all-hold", [r[3] for r in rows])
    mfe = np.array([r[4] for r in rows])
    print(f"  MFE 30d: median={np.median(mfe):.2f}x p90={np.percentile(mfe,90):.1f}x p99={np.percentile(mfe,99):.1f}x max={mfe.max():.0f}x  >=10x:{(mfe>=10).mean()*100:.1f}%")

    # ---- multi-feature selection, train(early)->test(late) ----
    n = len(rows); cut = int(n * 0.7)
    train, test = rows[:cut], rows[cut:]

    def featvec(r):
        s, f, _, _, _, jf = r
        return [np.log1p(f.get("entry_mc") or 0), f.get("lateness_ratio") or 1.0, f.get("time_since_entry_h") or 0.0,
                np.log1p(jf.get("h1_vol", 0)), jf.get("h1_ret", 0.0), jf.get("h1_max", 0.0), float(jf.get("h1_n", 0))]
    names = ["log_entry_mc", "lateness", "tse_h", "log_h1_vol", "h1_ret", "h1_max", "h1_n"]
    Xtr = np.array([featvec(r) for r in train]); ytr = np.log1p(np.array([r[2] for r in train]))
    # rank features by |corr| with log moonbag on TRAIN
    corrs = []
    for j in range(Xtr.shape[1]):
        col = Xtr[:, j]
        c = float(np.corrcoef(col, ytr)[0, 1]) if col.std() > 0 else 0.0
        corrs.append((names[j], c, j))
    corrs.sort(key=lambda x: abs(x[1]), reverse=True)
    print("\n  --- multi-feature: TRAIN corr(feature, log moonbag) ---")
    for nm, c, _ in corrs:
        print(f"     {nm:14} {c:+.3f}")

    # composite score = sum of z-scored top-3 features signed by their train corr; select top tertile of TEST
    top3 = corrs[:3]
    mu = Xtr.mean(0); sd = Xtr.std(0) + 1e-9

    def score(r):
        x = (np.array(featvec(r)) - mu) / sd
        return sum(np.sign(c) * x[j] for _, c, j in top3)
    test_scored = sorted(test, key=score, reverse=True)
    sel = test_scored[: max(20, len(test) // 3)]
    print(f"\n  --- TEST (OOS): all test vs feature-SELECTED top-tertile (n_test={len(test)}) ---")
    report("test-all-moonbag", [r[2] for r in test])
    report("test-SELECTED-moonbag", [r[2] for r in sel])
    print("\n" + "=" * 92)
    print("  Verdict: power-law thesis WORKS iff a selected subset has alpha>1 AND E[log-growth]>0 (book")
    print("  compounds) OOS. If E[log-growth]<=0 even on the selected subset, no sizing harvests this tail.")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
