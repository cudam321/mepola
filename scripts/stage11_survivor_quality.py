#!/usr/bin/env python3
"""Stage 11 — quality-filter the CLEAN survivor regime (the honest endpoint of "manage risk properly").

grad+24h survivor entry is ~breakeven (mean 1.04, alpha 2.12, artifact-free). Here we add a multi-feature
QUALITY filter on observable first-24h behaviour (momentum/volume/volatility), enter at the 24h-mark with a
REALISTIC next-candle fill, and ask: does selecting the better survivors push breakeven into ROBUST +EV?
Full discipline: power-law-native (alpha + f=2% log-growth), drop-top, 10x liquidity cap, train->OOS.

    set -a && . ./.env && set +a && PYTHONPATH=src python3 scripts/stage11_survivor_quality.py
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
from stage6_channel_powerlaw import hill_alpha, mean_ci  # noqa: E402
from stage7_channel_classifier import ridge_fit  # noqa: E402
from stage9_survivor import grad_sql, parse_ts, WINDOWS, CAP, H, D, HOLD_DAYS  # noqa: E402
from stage3_oos import drop_top_trade, drop_top_token  # noqa: E402

FILL_SLIP = 1.03


def fixed_f_growth(mults, f=0.02):
    a = np.asarray(mults, dtype=float)
    return float(np.mean(np.log(np.maximum(1 + f * (a - 1), 1e-9))))


def build(dune, client, ws, we):
    rows = dune.run_sql(grad_sql(ws, we))["rows"]
    if len(rows) > CAP:
        rng = np.random.default_rng(0)
        rows = [rows[i] for i in rng.choice(len(rows), CAP, replace=False)]
    X, M, mints = [], [], []
    for i, r in enumerate(rows):
        if i % 200 == 0:
            print(f"\r    {ws[:7]} {i}/{len(rows)} (cache {client.hits}h/{client.misses}m)", end="", file=sys.stderr)
        grad = parse_ts(r["grad"])
        try:
            ser = client.get_price_series(r["mint"], grad - H(1), grad + D(HOLD_DAYS + 8))
        except Exception:
            ser = None
        if not (ser and ser.candles):
            continue
        cands = ser.candles
        atgrad = [c for c in cands if c.ts >= grad]
        if not atgrad:
            continue
        ref = atgrad[0].open or atgrad[0].close
        win = [c for c in cands if grad <= c.ts <= grad + H(24)]
        te = grad + H(24)
        after = [c for c in cands if c.ts > te]
        if ref is None or ref <= 0 or len(win) < 3 or not after:
            continue
        closes = np.array([c.close for c in win], dtype=float)
        rets = np.diff(np.log(np.clip(closes, 1e-12, None)))
        feat = [np.log1p(ref), closes[-1] / ref, max(c.high for c in win) / ref, min(c.low for c in win) / ref,
                np.log1p(sum(c.volume for c in win)), float(rets.std() if len(rets) else 0.0), float(len(win)),
                closes[-1] / max(c.high for c in win)]  # last drawdown from 24h high
        fill = after[0].high * FILL_SLIP
        X.append(feat); M.append(simulate_exit(ser, fill, after[0].ts, P_MOON)); mints.append(r["mint"])
    print(f"\r  [{ws[:7]}..] graduated sampled={len(rows)} usable={len(X)}" + " " * 20, file=sys.stderr)
    return np.array(X), np.array(M), mints


def show(tag, rm):
    mults = [m for _, m in rm]
    if len(mults) < 15:
        print(f"  [{tag}] n={len(mults)} too few"); return
    a = np.asarray(mults, dtype=float)
    m, lo, hi = mean_ci(mults)
    g = fixed_f_growth(mults, 0.02)
    gc = fixed_f_growth(np.clip(a, None, 10.0), 0.02)
    dtt = drop_top_trade(mults); dtk, _ = drop_top_token(rm)
    print(f"  [{tag}] n={len(mults)} mean={m:.2f}(CI {lo:.2f}-{hi:.2f}) alpha={hill_alpha(mults):.2f} win={np.mean(a>1)*100:.0f}% "
          f"| f2%logG={g:+.4f}{'COMPOUNDS' if g>0 else 'shrinks'} cap10logG={gc:+.4f} | dropTrade={dtt*100:+.0f}% dropTok={dtk*100:+.0f}%")


def main() -> int:
    dune = DuneClient()
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_surv"))
    Xtr, Mtr, _ = build(dune, client, *WINDOWS["TRAIN"])
    Xte, Mte, mte = build(dune, client, *WINDOWS["OOS"])
    print("=" * 100)
    print("  STAGE 11 — quality-filtered survivors (grad+24h, realistic fill) | does selection cross breakeven?")
    print("=" * 100)
    mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
    w = ridge_fit((Xtr - mu) / sd, np.log(np.clip(Mtr, 0.05, None)), lam=2.0)
    score = np.hstack([np.ones((len(Xte), 1)), (Xte - mu) / sd]) @ w
    fnames = ["log_ref", "ret24", "max24", "min24", "logvol24", "volat", "n24", "dd_from_high"]
    print("  feature weights:", {n: round(float(x), 2) for n, x in sorted(zip(fnames, w[1:]), key=lambda z: -abs(z[1]))[:6]})
    show("OOS-all", list(zip(mte, Mte)))
    order = np.argsort(score)[::-1]
    for frac in (0.33, 0.20, 0.10):
        k = max(20, int(len(Mte) * frac))
        idx = order[:k]
        show(f"OOS-top{int(frac*100)}%", [(mte[j], Mte[j]) for j in idx])
    print("\n" + "=" * 100)
    print("  GO iff a selection has mean CI-lower>1 AND f2% logG>0 AND survives 10x cap AND drop-top stays +.")
    print("  Else: even quality-filtered survivors top out at ~breakeven -> the memecoin ceiling is breakeven.")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
