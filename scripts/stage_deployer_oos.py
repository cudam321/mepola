#!/usr/bin/env python3
"""Decisive test of the DEPLOYER-RIDE thesis: freeze the best-track-record serial deployers in
TRAIN, then test whether an OUTSIDER buying their OOS launches (at launch + latency, full costs,
managed exit, survivorship-free) is +EV. Strong-form: uses the REUSE cohort (pinnable by
account_user) with the best in-sample graduation track record — the idea's best shot, since the
rotation cohort is documented to be even more rug-engineered.

Reuses stage3_oos pricing/sim/gate verbatim; only the SIGNAL source changes (launch, not early-buy).

    set -a && . ./.env && set +a && PYTHONPATH=src python3 scripts/stage_deployer_oos.py
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
from memebot.analysis.exit_sim import POLICIES  # noqa: E402
from stage3_oos import (worst_fill, mid_fill, evaluate_all, boot_lo, pf, ev,  # noqa: E402
                        drop_top_trade, drop_top_token, parse_ts, EXECUTABLE)

TRAIN = ("2026-01-01", "2026-04-01")
OOS = ("2026-04-01", "2026-06-01")
TRADE_CAP = 1500


def cohort_sql():
    return f"""
WITH creates AS (
  SELECT account_user AS dep, account_mint AS mint
  FROM pumpdotfun_solana.pump_call_create
  WHERE call_block_date >= DATE '{TRAIN[0]}' AND call_block_date < DATE '{TRAIN[1]}'
    AND account_user IS NOT NULL AND account_mint IS NOT NULL
),
grads AS (SELECT DISTINCT basemint AS mint FROM pumpswap_solana.pools WHERE is_valid_pool = true),
dep AS (
  SELECT c.dep, count(*) AS n_launch, count(g.mint) AS n_grad
  FROM creates c LEFT JOIN grads g ON g.mint = c.mint
  GROUP BY c.dep
)
SELECT dep, n_launch, n_grad FROM dep
WHERE n_launch >= 3 AND n_grad >= 1
ORDER BY n_grad DESC, n_launch DESC
LIMIT 300
""".strip()


def launches_sql(cohort, win):
    values = ",".join(f"('{w}')" for w in cohort)
    return f"""
WITH cohort(dep) AS (VALUES {values})
SELECT c.account_mint AS mint, c.call_block_time AS t
FROM pumpdotfun_solana.pump_call_create c
JOIN cohort ON cohort.dep = c.account_user
WHERE c.call_block_date >= DATE '{win[0]}' AND c.call_block_date < DATE '{win[1]}'
  AND c.account_mint IS NOT NULL
ORDER BY c.call_block_time
""".strip()


def report(name, rows, client, fill_fn, fill_name):
    sig = [(r["mint"], parse_ts(r["t"])) for r in rows]
    n_total = len(sig)
    if n_total > TRADE_CAP:
        rng = np.random.default_rng(0)
        sig = [sig[i] for i in rng.choice(n_total, TRADE_CAP, replace=False)]
    by_policy, npx, nloss = evaluate_all(sig, client, fill_fn)
    print(f"\n  [{name} | fill={fill_name}] launches={n_total} priced={npx} unpriceable(loss)={nloss}")
    print(f"  {'policy':24} | {'EV':>8} | {'CIlo':>8} | {'PF':>7} | {'drop1trade':>10} | {'drop1token':>10} | {'win%':>5} | exec")
    print("  " + "-" * 96)
    passing = []
    for p in POLICIES:
        mults, rm = by_policy[p.name]
        a = np.asarray(mults, dtype=float)
        d1t, _ = drop_top_token(rm)
        cl = boot_lo(mults)
        if p.name in EXECUTABLE and cl > 0 and pf(mults) >= 1.3 and drop_top_trade(mults) > 0 and d1t > 0:
            passing.append(p.name)
        print(f"  {p.name:24} | {ev(mults)*100:>+6.1f}% | {cl*100:>+6.1f}% | {pf(mults):>7.2f} | "
              f"{drop_top_trade(mults)*100:>+8.1f}% | {d1t*100:>+8.1f}% | {(a>1).mean()*100:>4.0f}% | "
              f"{'yes' if p.name in EXECUTABLE else 'NO'}")
    return passing


def main() -> int:
    dune = DuneClient()
    crows = dune.run_sql(cohort_sql())["rows"]
    cohort = [r["dep"] for r in crows]
    print("=" * 92)
    print(f"  DEPLOYER-RIDE OOS TEST | frozen cohort = top {len(cohort)} serial deployers by TRAIN track record")
    print(f"  (>=3 launches & >=1 graduation in train); outsider buys their launches at launch+3s, managed exit")
    print("=" * 92)
    if crows:
        print(f"  cohort track record: best n_grad={crows[0]['n_grad']} (n_launch={crows[0]['n_launch']}); "
              f"median n_grad={int(np.median([r['n_grad'] for r in crows]))}")

    train_rows = dune.run_sql(launches_sql(cohort, TRAIN))["rows"]
    oos_rows = dune.run_sql(launches_sql(cohort, OOS))["rows"]
    print(f"  cohort launches: TRAIN={len(train_rows)}  OOS={len(oos_rows)}")

    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_launches"))
    oos_pass = None
    for fill_fn, fname in ((mid_fill, "mid"), (worst_fill, "worst")):
        report("TRAIN", train_rows, client, fill_fn, fname)
        p = report("OOS", oos_rows, client, fill_fn, fname)
        if fname == "mid":
            oos_pass = p

    print("\n" + "=" * 92)
    if oos_pass:
        print(f"  VERDICT: deployer-ride CLEARS the OOS bar ({', '.join(oos_pass)}) -> investigate further.")
    else:
        print("  VERDICT: NO executable policy clears the OOS bar. Riding even the BEST-track-record serial")
        print("  deployers' launches is -EV for an outsider -> exit liquidity, as the research predicted.")
        print("  The deployer-RIDE thesis is NO-GO; the residual (if any) is the INVERSE rug-avoid/fade filter.")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
