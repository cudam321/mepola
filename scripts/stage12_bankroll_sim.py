#!/usr/bin/env python3
"""Stage 12 — full event-driven BANKROLL simulation (not barehands EV): start $500, run the bot.

Answers "if I actually RUN this with $500, what happens?" — size each position, pay real per-trade gas,
compound over a resampled trade sequence, and report the DISTRIBUTION of final bankroll incl. the lucky
upside tail (p90/max), P(profit), P(moonshot 10x), P(bust). Tests the best clean regime (survivor grad+24h
moonbag, realistic fills) and a feature-selected subset, across position sizings. Power-law-honest: keeps
the full tail, sizes for survival.

    set -a && . ./.env && set +a && PYTHONPATH=src python3 scripts/stage12_bankroll_sim.py
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
from stage9_survivor import grad_sql, parse_ts, WINDOWS, CAP, H, D, HOLD_DAYS  # noqa: E402

FILL_SLIP = 1.05
GAS_PER_SIDE = 0.75   # $ Solana priority/network cost per leg (on top of the % costs in simulate_exit)
B0 = 500.0
N_SIM = 4000
N_BETS = 250          # ~ a few months of trades at this strategy's trigger rate


def feats(win, ref):
    closes = np.array([c.close for c in win], dtype=float)
    rets = np.diff(np.log(np.clip(closes, 1e-12, None)))
    return [np.log1p(ref), closes[-1] / ref, max(c.high for c in win) / ref, min(c.low for c in win) / ref,
            np.log1p(sum(c.volume for c in win)), float(rets.std() if len(rets) else 0.0), float(len(win)),
            closes[-1] / max(c.high for c in win)]


def collect(dune, client, ws, we):
    rows = dune.run_sql(grad_sql(ws, we))["rows"]
    if len(rows) > CAP:
        rng = np.random.default_rng(0)
        rows = [rows[i] for i in rng.choice(len(rows), CAP, replace=False)]
    X, M = [], []
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
        atg = [c for c in cands if c.ts >= grad]
        win = [c for c in cands if grad <= c.ts <= grad + H(24)]
        after = [c for c in cands if c.ts > grad + H(24)]
        if not atg or len(win) < 3 or not after:
            continue
        ref = atg[0].open or atg[0].close
        if not ref or ref <= 0:
            continue
        X.append(feats(win, ref))
        M.append(simulate_exit(ser, after[0].high * FILL_SLIP, after[0].ts, P_MOON))
    print("", file=sys.stderr)
    return np.array(X), np.array(M)


def run_bankroll(mults, frac, fixed_cap, n_sim=N_SIM, n_bets=N_BETS, seed=0):
    rng = np.random.default_rng(seed)
    finals, busts = [], 0
    a = np.asarray(mults, dtype=float)
    for _ in range(n_sim):
        B = B0
        draws = a[rng.integers(0, len(a), size=n_bets)]
        for m in draws:
            if B < 10:
                break
            stake = min(frac * B, fixed_cap, B)
            B = B - stake - 2 * GAS_PER_SIDE + stake * m   # deploy, pay round-trip gas, receive stake*mult
        finals.append(max(B, 0.0))
        if B < 100:
            busts += 1
    f = np.asarray(finals)
    return dict(median=float(np.median(f)), mean=float(f.mean()), p10=float(np.percentile(f, 10)),
                p90=float(np.percentile(f, 90)), p99=float(np.percentile(f, 99)), mx=float(f.max()),
                p_profit=float((f > B0).mean()), p_10x=float((f > 10 * B0).mean()), p_bust=busts / n_sim)


def main() -> int:
    dune = DuneClient()
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_surv"))
    Xtr, Mtr = collect(dune, client, *WINDOWS["TRAIN"])
    Xte, Mte = collect(dune, client, *WINDOWS["OOS"])
    # feature-selected top 20% (ridge on train)
    mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
    w = ridge_fit((Xtr - mu) / sd, np.log(np.clip(Mtr, 0.05, None)), lam=2.0)
    sc = np.hstack([np.ones((len(Xte), 1)), (Xte - mu) / sd]) @ w
    sel = Mte[np.argsort(sc)[::-1][:max(20, len(Mte) // 5)]]

    print("=" * 100)
    print(f"  STAGE 12 — BANKROLL SIM | start ${B0:.0f} | {N_BETS} bets | gas ${GAS_PER_SIDE}/side | {N_SIM} Monte-Carlo runs")
    print(f"  strategy E[M]: survivor-all={Mte.mean():.2f}  selected-top20%={sel.mean():.2f}")
    print("=" * 100)
    for name, mults in (("survivor-all", Mte), ("selected-top20%", sel)):
        print(f"\n  [{name}]  (per-trade mean multiple {np.mean(mults):.2f})")
        for frac, cap in ((0.02, 25.0), (0.05, 50.0), (0.10, 100.0)):
            r = run_bankroll(mults, frac, cap)
            print(f"    size={int(frac*100)}%/cap${int(cap):<4} -> final$: median={r['median']:>6.0f} p90={r['p90']:>7.0f} "
                  f"p99={r['p99']:>8.0f} max={r['mx']:>9.0f} | P(profit)={r['p_profit']*100:>4.1f}% "
                  f"P(10x=${int(10*B0)})={r['p_10x']*100:>4.1f}% P(bust<100)={r['p_bust']*100:>4.1f}%")
    print("\n" + "=" * 100)
    print("  Read: 'P(profit)' = chance $500 ends above $500 after running the bot. 'max/p99' = the lucky")
    print("  upside tail. If P(profit) is low AND even p90 < $500, no sizing of this regime makes money.")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
