#!/usr/bin/env python3
"""Stage 3 robustness — per-wallet IN-SAMPLE copier EV among the top-100 cohort.

Kills the "wrong selection metric" objection: we ranked wallets by their OWN return; does ANY
wallet have a positive COPIER EV in-sample? If even the best does not, no selection rule (incl.
selecting on copier-return) can produce a +EV cohort -> the strategy, not the picker, is the problem.

Reuses the warm jupiter_onchain cache (same 1500 seed-0 TRAIN sample as stage3_oos).

    set -a && . ./.env && set +a && PYTHONPATH=src python3 scripts/stage3_perwallet.py
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memebot.data.dune import DuneClient  # noqa: E402
from memebot.data.cache import CachedPriceClient  # noqa: E402
from memebot.data.jupiter import JupiterChartsClient  # noqa: E402
from memebot.analysis.exit_sim import POLICIES, simulate_exit  # noqa: E402
sys.path.insert(0, str(ROOT / "scripts"))
from stage3_oos import (cohort_buys_sql, parse_ts, mid_fill, ev, pf, boot_lo,  # noqa: E402
                        drop_top_trade, drop_top_token, evaluate_all, EXECUTABLE, FWD_H, LATENCY_S)

POL = next(p for p in POLICIES if p.name == "P2_principal_out_trail")  # an executable policy
MIN_SIGNALS = 5
SELECT_K = 15  # take the top-K wallets by IN-SAMPLE copier EV, then test THEM out-of-sample


def main() -> int:
    cohort = [r["trader_id"] for r in json.load(open(ROOT / "runs" / "stage2_screen_train.json"))["cohort"]][:100]
    dune = DuneClient()
    rows = dune.run_sql(cohort_buys_sql(cohort, "2026-01-01", "2026-04-01", "2025-12-01"))["rows"]
    rows.sort(key=lambda r: r["block_time"])
    # same seed-0 1500 sample as stage3_oos
    rng = np.random.default_rng(0)
    idx = rng.choice(len(rows), min(1500, len(rows)), replace=False)
    sample = [rows[i] for i in idx]

    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_onchain"))
    per: dict[str, list[float]] = {}
    for j, r in enumerate(sample):
        if j % 200 == 0:
            print(f"\r  {j}/{len(sample)} (cache {client.hits}h/{client.misses}m)", end="", file=sys.stderr)
        mint, ts = r["mint"], parse_ts(r["block_time"])
        t_fill = ts + timedelta(seconds=LATENCY_S)
        try:
            series = client.get_price_series(mint, ts - timedelta(minutes=15), ts + timedelta(hours=FWD_H))
        except Exception:
            series = None
        wf = mid_fill(series, t_fill) if (series is not None and not series.empty) else None
        m = simulate_exit(series, wf, t_fill, POL) if wf else 0.0
        per.setdefault(r["trader_id"], []).append(m)
    print("\r" + " " * 50 + "\r", end="", file=sys.stderr)

    stats = [(w, ev(ms), (np.asarray(ms) > 1).mean(), len(ms)) for w, ms in per.items() if len(ms) >= MIN_SIGNALS]
    stats.sort(key=lambda x: x[1], reverse=True)
    evs = [e for _, e, _, _ in stats]
    print("=" * 80)
    print(f"  PER-WALLET in-sample COPIER EV (policy {POL.name}, neutral fill) | wallets with >= {MIN_SIGNALS} signals: {len(stats)}")
    print("=" * 80)
    print(f"  wallets with copier-EV > 0 : {sum(1 for e in evs if e > 0)}/{len(evs)}")
    print(f"  best wallet copier-EV      : {max(evs)*100:+.1f}%")
    print(f"  median wallet copier-EV    : {np.median(evs)*100:+.1f}%")
    print("  top 8 wallets by in-sample copier-EV (EV, win%, n_signals):")
    for w, e, wr, n in stats[:8]:
        print(f"    {w}  EV={e*100:>+6.1f}%  win={wr*100:>4.0f}%  n={n}")
    print("=" * 80)
    # DECISIVE CLOSE: select the top-K wallets by IN-SAMPLE copier EV, test THEM out-of-sample.
    selected = [w for w, _, _, _ in stats[:SELECT_K]]
    oos_rows = dune.run_sql(cohort_buys_sql(selected, "2026-04-01", "2026-06-01", "2026-03-01"))["rows"]
    oos_sig = [(r["mint"], parse_ts(r["block_time"])) for r in oos_rows]
    n_active = len({r["trader_id"] for r in oos_rows})
    print(f"\n  COPIER-SELECTED cohort = top {SELECT_K} wallets by in-sample copier EV")
    print(f"  OOS: {len(oos_sig)} copy-signals | {n_active}/{SELECT_K} still active")
    by_policy, npx, nloss = evaluate_all(oos_sig, client, mid_fill)
    print(f"  priced={npx} unpriceable(loss)={nloss}")
    print(f"  {'policy':24} | {'OOS EV':>8} | {'CIlo':>8} | {'PF':>7} | {'drop1trade':>10} | {'drop1token':>10} | {'win%':>5} | exec")
    print("  " + "-" * 96)
    passing = []
    for p in POLICIES:
        mults, rm = by_policy[p.name]
        a = np.asarray(mults, dtype=float)
        d1t, _ = drop_top_token(rm)
        cl = boot_lo(mults)
        ok = p.name in EXECUTABLE and cl > 0 and pf(mults) >= 1.3 and drop_top_trade(mults) > 0 and d1t > 0
        if ok:
            passing.append(p.name)
        print(f"  {p.name:24} | {ev(mults)*100:>+6.1f}% | {cl*100:>+6.1f}% | {pf(mults):>7.2f} | "
              f"{drop_top_trade(mults)*100:>+8.1f}% | {d1t*100:>+8.1f}% | {(a>1).mean()*100:>4.0f}% | "
              f"{'yes' if p.name in EXECUTABLE else 'NO'}")
    print("=" * 80)
    if passing:
        print(f"  -> The copier-return-selected cohort PERSISTS OOS ({', '.join(passing)}). A real (if fragile)")
        print("     lead — worth a larger, deflated re-test before any build.")
    else:
        print("  -> Even selecting wallets by IN-SAMPLE COPIER EV, the cohort FAILS out-of-sample. The few")
        print("     in-sample-positive wallets were luck/tail artifacts. NO selection rule survives -> the")
        print("     copyable token population + latency, not the picker, is the problem. NO-GO is FINAL.")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
