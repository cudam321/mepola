#!/usr/bin/env python3
"""Stage 4b — select wallets for OUTLIER EXPOSURE (tail-hit rate), not own-return, then test OOS.

Honors the power-law thesis: rank the cohort wallets by their IN-SAMPLE 30d tail-hit rate (frac of
their picks reaching >=10x MFE), freeze the top tail-pickers, and test whether THEIR out-of-sample
picks still produce a harvestable, robustly +EV tail under an uncapped/moonbag exit. Reuses the warm
jupiter_30d cache from stage4 (fast).

    set -a && . ./.env && set +a && PYTHONPATH=src python3 scripts/stage4b_tailselect.py
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
from stage3_oos import cohort_buys_sql, parse_ts, boot_lo, pf, ev, drop_top_trade, drop_top_token  # noqa: E402
from stage4_powerlaw import entry_fill, mfe, P_MOON, P_HOLD, HOLD_DAYS, LATENCY_S  # noqa: E402

TAIL_X = 10.0       # "tail hit" = 30d MFE >= 10x
MIN_PICKS = 8       # min in-sample picks to rank a wallet
TOP_TAIL_WALLETS = 25


def price_window(rows, client, mode="mid"):
    """Return per-(wallet) list of (mfe, hold_mult, moon_mult) using the warm 30d cache."""
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


def agg(mults):
    a = np.asarray(mults, dtype=float)
    return ev(mults), boot_lo(mults), pf(mults), float((a > 1).mean())


def sample(rows, n=1500, seed=0):
    """Match stage4_powerlaw's seed-0 1500 sample so the 30d cache is all hits."""
    if len(rows) <= n:
        return rows
    idx = np.random.default_rng(seed).choice(len(rows), n, replace=False)
    return [rows[i] for i in idx]


def main() -> int:
    cohort = [r["trader_id"] for r in json.load(open(ROOT / "runs" / "stage2_screen_train.json"))["cohort"]][:100]
    dune = DuneClient()
    train = sample(dune.run_sql(cohort_buys_sql(cohort, "2026-01-01", "2026-04-01", "2025-12-01"))["rows"])
    oos = sample(dune.run_sql(cohort_buys_sql(cohort, "2026-04-01", "2026-06-01", "2026-03-01"))["rows"])
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_30d"))

    print("=" * 92)
    print(f"  STAGE 4b — TAIL-SELECTED cohort (rank by in-sample frac picks >= {TAIL_X:.0f}x MFE over {HOLD_DAYS}d)")
    print("=" * 92)
    tr = price_window(train, client, "mid")
    # rank wallets by in-sample tail-hit rate
    scored = []
    for w, lst in tr.items():
        if len(lst) < MIN_PICKS:
            continue
        mfes = [m for m, _, _ in lst]
        hit = float(np.mean([1.0 if m >= TAIL_X else 0.0 for m in mfes]))
        scored.append((w, hit, len(lst), float(np.median(mfes))))
    scored.sort(key=lambda x: x[1], reverse=True)
    tail_wallets = [w for w, _, _, _ in scored[:TOP_TAIL_WALLETS]]
    print(f"  ranked {len(scored)} wallets (>= {MIN_PICKS} picks); top tail-pickers by in-sample >= {TAIL_X:.0f}x-rate:")
    for w, hit, n, med in scored[:8]:
        print(f"    {w}  tail_hit={hit*100:>4.1f}%  n={n}  medMFE={med:.2f}x")

    # OOS performance of the tail-selected subset
    oos_sub = [r for r in oos if r["trader_id"] in set(tail_wallets)]
    print(f"\n  tail-selected cohort = top {len(tail_wallets)} | OOS picks by them: {len(oos_sub)}")
    osub = price_window(oos_sub, client, "mid")
    hold = [h for lst in osub.values() for _, h, _ in lst]
    moon = [mo for lst in osub.values() for _, _, mo in lst]
    mfes = [m for lst in osub.values() for m, _, _ in lst]
    if mfes:
        a = np.asarray(mfes)
        print(f"  OOS MFE: median={np.median(a):.2f}x p90={np.percentile(a,90):.2f}x p99={np.percentile(a,99):.2f}x "
              f"max={a.max():.1f}x | >=10x:{(a>=10).mean()*100:.1f}% >=50x:{(a>=50).mean()*100:.2f}%")
    for name, mults in (("P_hold_30d", hold), ("P_moonbag_30d", moon)):
        e, cl, p, win = agg(mults)
        dt = drop_top_trade(mults)
        print(f"  {name:14} EV={e*100:>+7.1f}%  CIlo={cl*100:>+7.1f}%  PF={p:>5.2f}  drop1trade={dt*100:>+7.1f}%  win={win*100:>3.0f}%  n={len(mults)}")
    print("\n" + "=" * 92)
    print("  If tail-selected wallets' OOS picks clear CIlo>0 + PF>=1.3 (even without drop-top, given power-law),")
    print("  outlier-exposure selection works. If still negative, the tail does not persist per-wallet -> dead.")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
