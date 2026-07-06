#!/usr/bin/env python3
"""Stage 10 — adversarial stress test of the survivor pullback edge (is it real or artifact?).

Stage 9's pullback rules showed huge OOS means but with alpha<1 (undefined mean), 10^16 MC medians
(overfit optimal-f), and bottom-picking entries. Here we kill every artifact: enter on the NEXT candle's
HIGH after the dip + 5% slip (no catching the wick), cap the exit for illiquidity (can't sell a 50x on a
microcap), use REALISTIC fixed sizing (f=2%, not 45%), and check drop-top-trade/token robustness. GO iff
a rule keeps positive fixed-f log-growth AND survives a 10x liquidity cap AND drop-top OOS.

    set -a && . ./.env && set +a && PYTHONPATH=src python3 scripts/stage10_survivor_stress.py
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
from stage9_survivor import grad_sql, parse_ts, entry_times, WINDOWS, CAP, H, D, HOLD_DAYS  # noqa: E402
from stage3_oos import drop_top_trade, drop_top_token  # noqa: E402

RULES = ["pull50_7d", "pull70_14d"]
FILL_SLIP = 1.05  # enter chasing the bounce on the next candle


def fixed_f_growth(mults, f=0.02):
    a = np.asarray(mults, dtype=float)
    return float(np.mean(np.log(np.maximum(1 + f * (a - 1), 1e-9))))


def next_candle_fill(cands, te):
    after = [c for c in cands if c.ts > te]
    return (after[0].high * FILL_SLIP, after[0].ts) if after else (None, None)


def report(tag, rm):
    mults = [m for _, m in rm]
    if len(mults) < 15:
        print(f"  [{tag}] n={len(mults)} too few"); return
    a = np.asarray(mults, dtype=float)
    m, lo, hi = mean_ci(mults)
    g2 = fixed_f_growth(mults, 0.02)
    cap10 = np.clip(a, None, 10.0); cap20 = np.clip(a, None, 20.0)
    g2_c10 = fixed_f_growth(cap10, 0.02)
    dtt = drop_top_trade(mults); dtk, _ = drop_top_token(rm)
    top = np.sort(a)[::-1][:5]
    print(f"  [{tag}] n={len(mults)} mean={m:.2f}(CI {lo:.2f}-{hi:.2f}) alpha={hill_alpha(mults):.2f} win={np.mean(a>1)*100:.0f}%")
    print(f"        f=2% logGrowth={g2:+.4f} {'COMPOUNDS' if g2>0 else 'shrinks'} | cap10x: mean={cap10.mean():.2f} logG={g2_c10:+.4f} | cap20x mean={cap20.mean():.2f}")
    print(f"        robustness: drop-top-trade EV={dtt*100:+.1f}%  drop-top-token EV={dtk*100:+.1f}%  | top5 mults={np.round(top,1)}")


def main() -> int:
    dune = DuneClient()
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_surv"))
    print("=" * 100)
    print("  STAGE 10 — survivor pullback STRESS: next-candle-high fill +5% | cap exits | f=2% | drop-top")
    print("=" * 100)
    for wname, (ws, we) in WINDOWS.items():
        rows = dune.run_sql(grad_sql(ws, we))["rows"]
        if len(rows) > CAP:
            rng = np.random.default_rng(0)
            rows = [rows[i] for i in rng.choice(len(rows), CAP, replace=False)]
        by = {r: [] for r in RULES}
        for i, r in enumerate(rows):
            if i % 200 == 0:
                print(f"\r    {wname} {i}/{len(rows)} (cache {client.hits}h/{client.misses}m)", end="", file=sys.stderr)
            grad = parse_ts(r["grad"])
            try:
                ser = client.get_price_series(r["mint"], grad - H(1), grad + D(HOLD_DAYS + 8))
            except Exception:
                ser = None
            if not (ser and ser.candles):
                for rule in RULES:
                    by[rule].append((r["mint"], 0.0))
                continue
            ets = entry_times(ser.candles, grad)
            for rule in RULES:
                te = ets.get(rule)
                if te is None:
                    continue
                fill, t_in = next_candle_fill(ser.candles, te)  # realistic: chase the next bar's high
                by[rule].append((r["mint"], simulate_exit(ser, fill, t_in, P_MOON) if fill else 0.0))
        print(f"\r  === {wname} (graduated≈{len(rows)} sampled) ===" + " " * 20)
        for rule in RULES:
            report(rule, by[rule])
    print("\n" + "=" * 100)
    print("  GO iff a rule keeps f=2% logGrowth>0 OOS AND survives the 10x liquidity cap AND drop-top stays +.")
    print("  If the edge lived only in uncapped/bottom-caught/single-token outcomes, it was an artifact.")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
