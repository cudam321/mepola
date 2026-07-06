#!/usr/bin/env python3
"""Stage 1 — survivorship-free candidate-wallet discovery (server-side Dune aggregation).

Universe = every token that GRADUATED (pumpswap pool created) in the window, incl. dead ones.
"Early buyer" (user-locked rule) = bought a universe token on the bonding curve BELOW ~$50k
implied mcap (1B-supply proxy) but NOT within the first SNIPER_EXCLUDE_SEC of that token's first
bonding-curve trade (snipers' first-block edge is not copyable). A candidate wallet = appears as
an early buyer across >= MIN_TOKENS distinct universe tokens (regardless of outcome).

ALL filtering/joining/grouping runs server-side; only the compact candidate list returns.
COST is driven by DATA SCANNED, so dex_solana.trades is partition-pruned on block_month.

    set -a && . ./.env && set +a && PYTHONPATH=src python3 scripts/stage1_candidates.py --slice
    set -a && . ./.env && set +a && PYTHONPATH=src python3 scripts/stage1_candidates.py --full
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memebot.data.dune import DuneClient  # noqa: E402

SOL = "So11111111111111111111111111111111111111112"
MCAP_CUTOFF = 50_000.0
SNIPER_EXCLUDE_SEC = 30
SUPPLY = 1e9  # pump.fun standard total supply
# Outcome-BLIND behavioral anti-bot filters (no survivorship bias): drop wash/MM wallets that
# re-buy the same token many times, and indiscriminate sprayers that buy one of everything.
MAX_BUYS_PER_TOKEN = 3.0   # ratio n_buys/n_tokens; > this = DCA/MM/wash, not a discretionary entry


def build_sql(grad_start: str, grad_end: str, month_start: str, month_end: str,
              min_tokens: int, max_tokens: int) -> str:
    return f"""
WITH grads AS (
  SELECT basemint AS mint
  FROM pumpswap_solana.pools
  WHERE is_valid_pool = true
    AND created_at >= TIMESTAMP '{grad_start}'
    AND created_at <  TIMESTAMP '{grad_end}'
  GROUP BY basemint
),
curve AS (
  SELECT
    t.trader_id,
    t.token_bought_mint_address AS mint,
    t.block_time,
    (t.amount_usd / nullif(t.token_bought_amount, 0)) * {SUPPLY:.0f} AS mcap,
    min(t.block_time) OVER (PARTITION BY t.token_bought_mint_address) AS launch_at
  FROM dex_solana.trades t
  JOIN grads g ON g.mint = t.token_bought_mint_address
  WHERE t.project = 'pumpdotfun'
    AND t.token_sold_mint_address = '{SOL}'
    AND t.amount_usd > 0
    AND t.token_bought_amount > 0
    AND t.block_month >= DATE '{month_start}'
    AND t.block_month <  DATE '{month_end}'
),
early AS (
  SELECT trader_id, mint
  FROM curve
  WHERE mcap < {MCAP_CUTOFF:.0f}
    AND block_time >= launch_at + INTERVAL '{SNIPER_EXCLUDE_SEC}' SECOND
)
SELECT trader_id,
       count(DISTINCT mint) AS n_tokens,
       count(*) AS n_buys
FROM early
GROUP BY trader_id
HAVING count(DISTINCT mint) >= {min_tokens}
   AND count(DISTINCT mint) <= {max_tokens}
   AND (count(*) * 1.0 / count(DISTINCT mint)) <= {MAX_BUYS_PER_TOKEN}
ORDER BY n_tokens DESC
""".strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slice", action="store_true", help="1-week slice, min_tokens=3 (cost calibration)")
    ap.add_argument("--full", action="store_true", help="full train window 2026-01..03, min_tokens=15")
    args = ap.parse_args()
    if not (args.slice or args.full):
        print("pass --slice or --full", file=sys.stderr)
        return 2

    if args.slice:
        grad_start, grad_end = "2026-01-01", "2026-01-08"
        month_start, month_end = "2025-12-01", "2026-02-01"
        min_tokens, max_tokens = 3, 50
        out = ROOT / "runs" / "stage1_candidates_slice.json"
    else:
        grad_start, grad_end = "2026-01-01", "2026-04-01"
        month_start, month_end = "2025-12-01", "2026-04-01"
        min_tokens, max_tokens = 15, 300
        out = ROOT / "runs" / "stage1_candidates_train.json"

    dune = DuneClient()
    u0 = dune.usage()
    sql = build_sql(grad_start, grad_end, month_start, month_end, min_tokens, max_tokens)
    print(f"discovering candidates: graduated in [{grad_start},{grad_end}), "
          f"mcap<${MCAP_CUTOFF:.0f}, exclude first {SNIPER_EXCLUDE_SEC}s, "
          f"{min_tokens}<=tokens<={max_tokens}, buys/token<={MAX_BUYS_PER_TOKEN} ...",
          file=sys.stderr)
    res = dune.run_sql(sql, performance="large" if args.full else "medium")
    u1 = dune.usage()

    rows = res["rows"]
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        "params": {"grad_start": grad_start, "grad_end": grad_end, "mcap_cutoff": MCAP_CUTOFF,
                   "sniper_exclude_sec": SNIPER_EXCLUDE_SEC, "min_tokens": min_tokens, "supply": SUPPLY},
        "candidates": rows,
    }, indent=2))

    print("=" * 80)
    print(f"  STAGE 1 candidates ({'slice' if args.slice else 'full train'})")
    print("=" * 80)
    print(f"  candidate wallets (>= {min_tokens} qualifying tokens): {len(rows)}")
    if rows:
        tops = rows[:10]
        print("  top by n_tokens:")
        for r in tops:
            print(f"    {r['trader_id']}  n_tokens={r['n_tokens']}  n_buys={r['n_buys']}")
    print(f"  result datapoints: {res['datapoints']}   saved -> {out.name}")
    print(f"  CREDIT COST: {u0['credits_used']:.2f} -> {u1['credits_used']:.2f}  "
          f"(delta {u1['credits_used']-u0['credits_used']:.2f} of {u1['credits_included']:.0f})")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
