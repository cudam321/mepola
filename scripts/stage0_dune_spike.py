#!/usr/bin/env python3
"""Stage 0(a) — Dune feasibility spike for the on-chain wallet-discovery pivot.

Verifies the survivorship-free token universe is queryable and AFFORDABLE on Dune Free:
  1. probe pumpswap_solana.pools schema (confirm column names),
  2. count pump.fun graduations in a 1-week sub-window,
  3. pull the graduation rows for that week and measure datapoint (credit) cost,
  4. extrapolate to the full 3-month TRAIN window vs the ~5,000 datapoints/mo budget (2 keys).

    set -a && . ./.env && set +a && PYTHONPATH=src python3 scripts/stage0_dune_spike.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memebot.data.dune import DuneClient  # noqa: E402

WEEK_START = "2026-01-01"
WEEK_END = "2026-01-08"
TRAIN_START = "2026-01-01"
TRAIN_END = "2026-03-31"


def main() -> int:
    if not os.environ.get("DUNE_API_KEY"):
        print("ERROR: DUNE_API_KEY not set (run: set -a && . ./.env && set +a)", file=sys.stderr)
        return 2
    dune = DuneClient()

    print("=" * 88)
    print("  STAGE 0(a) — Dune feasibility spike: pump.fun graduations (survivorship-free)")
    print("=" * 88)

    # 1) schema probe (cheap) — confirm the columns the research claimed.
    print("\n[1] pumpswap_solana.pools schema probe (LIMIT 1):")
    probe = dune.run_sql("SELECT * FROM pumpswap_solana.pools LIMIT 1")
    print("    columns:", probe["columns"])
    if probe["rows"]:
        r0 = probe["rows"][0]
        for k in probe["columns"]:
            print(f"      {k:24} = {str(r0.get(k))[:60]}")
    print(f"    (datapoints so far: {dune.datapoints})")

    # 2) count graduations in the 1-week sub-window.
    print(f"\n[2] graduation COUNT for {WEEK_START}..{WEEK_END}:")
    cnt = dune.run_sql(
        f"SELECT count(*) AS n FROM pumpswap_solana.pools "
        f"WHERE created_at >= TIMESTAMP '{WEEK_START}' AND created_at < TIMESTAMP '{WEEK_END}'"
    )
    week_n = int(cnt["rows"][0]["n"]) if cnt["rows"] else 0
    print(f"    graduations in week: {week_n}    (datapoints so far: {dune.datapoints})")

    # 3) pull the actual rows for the week (this is the real per-token cost driver).
    print(f"\n[3] pull graduation rows for the week (measure datapoint cost):")
    rows = dune.run_sql(
        f"SELECT baseMint AS token_mint, pool AS pumpswap_pool, created_at AS graduated_at, "
        f"quoteMint AS quote_mint, is_valid_pool "
        f"FROM pumpswap_solana.pools "
        f"WHERE created_at >= TIMESTAMP '{WEEK_START}' AND created_at < TIMESTAMP '{WEEK_END}' "
        f"ORDER BY created_at"
    )
    print(f"    rows returned: {rows['row_count']}    datapoints this call: {rows['datapoints']}")
    if rows["rows"]:
        ex = rows["rows"][0]
        print(f"    example: mint={ex.get('token_mint')}  graduated_at={ex.get('graduated_at')}")

    # 4) extrapolate to the full TRAIN window.
    weeks_in_train = 13.0  # ~3 months
    est_rows = rows["row_count"] * weeks_in_train
    est_dp = rows["datapoints"] * weeks_in_train
    print("\n" + "=" * 88)
    print(f"  EXTRAPOLATION to TRAIN window {TRAIN_START}..{TRAIN_END} (~13 weeks):")
    print(f"    est. graduated tokens : ~{est_rows:,.0f}")
    print(f"    est. datapoints (universe pull only): ~{est_dp:,.0f}")
    print(f"    budget: 2,500/mo/key x 2 keys = ~5,000-15,000 datapoints/mo")
    verdict = "AFFORDABLE" if est_dp < 4000 else ("TIGHT" if est_dp < 12000 else "needs chunking/Helius")
    print(f"    universe-enumeration affordability: {verdict}")
    print(f"    NOTE: this is universe pull ONLY; per-token early-buyers + per-wallet histories")
    print(f"    are the larger cost — measure those next once a wallet sample is scoped.")
    print(f"    total datapoints spent this spike: {dune.datapoints}")
    print("=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
