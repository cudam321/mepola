#!/usr/bin/env python3
"""Stage 4c — FINAL wallet-copy stone: tail-select from the BROADER 500-wallet pool.

Ranks ALL 500 screened wallets (not just top-100) by in-sample 30d tail-hit rate (frac of picks
>= 10x MFE), sampling each wallet's picks to bound the Jupiter pricing, then freezes the top
tail-pickers and tests their OOS picks under an uncapped moonbag/hold. If even the broadest
outlier-exposure selection fails OOS, the wallet-copy power-law variant is conclusively dead.

    set -a && . ./.env && set +a && PYTHONPATH=src python3 scripts/stage4c_broadpool.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
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
from stage3_oos import cohort_buys_sql, parse_ts, boot_lo, pf, ev, drop_top_trade  # noqa: E402
from stage4_powerlaw import entry_fill, mfe, P_MOON, P_HOLD, HOLD_DAYS, LATENCY_S  # noqa: E402

TAIL_X = 10.0
N_PICKS_RANK = 10     # sample up to this many train picks per wallet for ranking (bounds pricing)
MIN_PICKS = 6
TOP_TAIL_WALLETS = 25


def per_wallet_sample(rows, n, seed=0):
    by = defaultdict(list)
    for r in rows:
        by[r["trader_id"]].append(r)
    rng = np.random.default_rng(seed)
    out = []
    for w, lst in by.items():
        if len(lst) <= n:
            out += lst
        else:
            out += [lst[i] for i in rng.choice(len(lst), n, replace=False)]
    return out


def price(rows, client, mode="mid"):
    per = defaultdict(list)
    for i, r in enumerate(rows):
        if i % 200 == 0:
            print(f"\r    {i}/{len(rows)} (cache {client.hits}h/{client.misses}m)", end="", file=sys.stderr)
        mint, ts, w = r["mint"], parse_ts(r["block_time"]), r["trader_id"]
        t_fill = ts + timedelta(seconds=LATENCY_S)
        try:
            s = client.get_price_series(mint, ts - timedelta(hours=1), ts + timedelta(days=HOLD_DAYS))
        except Exception:
            s = None
        fill = entry_fill(s, t_fill, mode) if (s is not None and not s.empty) else None
        if fill is None:
            per[w].append((0.0, 0.0, 0.0)); continue
        per[w].append((mfe(s, t_fill, fill),
                       simulate_exit(s, fill, t_fill, P_HOLD),
                       simulate_exit(s, fill, t_fill, P_MOON)))
    print("\r" + " " * 50 + "\r", end="", file=sys.stderr)
    return per


def main() -> int:
    pool = [r["trader_id"] for r in json.load(open(ROOT / "runs" / "stage2_screen_train.json"))["cohort"]]
    print("=" * 92)
    print(f"  STAGE 4c — FINAL stone: tail-select from BROADER pool ({len(pool)} wallets)")
    print("=" * 92)
    dune = DuneClient()
    train = dune.run_sql(cohort_buys_sql(pool, "2026-01-01", "2026-04-01", "2025-12-01"))["rows"]
    train_s = per_wallet_sample(train, N_PICKS_RANK)
    print(f"  train picks: {len(train)} -> sampled {len(train_s)} ({N_PICKS_RANK}/wallet) for ranking", file=sys.stderr)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_30d"))
    tr = price(train_s, client, "mid")

    scored = []
    for w, lst in tr.items():
        if len(lst) < MIN_PICKS:
            continue
        mfes = [m for m, _, _ in lst]
        scored.append((w, float(np.mean([1.0 if m >= TAIL_X else 0.0 for m in mfes])), len(lst)))
    scored.sort(key=lambda x: x[1], reverse=True)
    tail_wallets = [w for w, _, _ in scored[:TOP_TAIL_WALLETS]]
    print(f"  ranked {len(scored)} wallets (>= {MIN_PICKS} picks); top tail-pickers by in-sample >= {TAIL_X:.0f}x rate:")
    for w, hit, n in scored[:8]:
        print(f"    {w}  tail_hit={hit*100:>4.1f}%  n={n}")

    oos = dune.run_sql(cohort_buys_sql(tail_wallets, "2026-04-01", "2026-06-01", "2026-03-01"))["rows"]
    print(f"\n  tail-selected cohort = top {len(tail_wallets)} | OOS picks: {len(oos)}")
    osub = price(oos, client, "mid")
    hold = [h for lst in osub.values() for _, h, _ in lst]
    moon = [mo for lst in osub.values() for _, _, mo in lst]
    mfes = [m for lst in osub.values() for m, _, _ in lst]
    if mfes:
        a = np.asarray(mfes)
        print(f"  OOS MFE: median={np.median(a):.2f}x p90={np.percentile(a,90):.2f}x p99={np.percentile(a,99):.2f}x "
              f"max={a.max():.1f}x | >=10x:{(a>=10).mean()*100:.1f}%")
    for name, m in (("P_hold_30d", hold), ("P_moonbag_30d", moon)):
        aa = np.asarray(m, dtype=float)
        print(f"  {name:14} EV={ev(m)*100:>+7.1f}%  CIlo={boot_lo(m)*100:>+7.1f}%  PF={pf(m):>5.2f}  "
              f"drop1trade={drop_top_trade(m)*100:>+7.1f}%  win={(aa>1).mean()*100:>3.0f}%  n={len(m)}")
    print("\n" + "=" * 92)
    print("  Broadest outlier-exposure selection. If OOS CIlo<=0 here too, wallet-copy power-law is dead.")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
