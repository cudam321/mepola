#!/usr/bin/env python3
"""Stage 2a — server-side in-sample PnL screen (the funnel that narrows candidates).

Combines Stage-1 candidate discovery with an APPROXIMATE realized-PnL computation, all
server-side in Dune, so we only pull per-trade detail (Stage 2b) for a small top cohort.

PnL is the wallet's OWN realized return (Dune amount_usd) on the tokens it qualified on:
  cost_usd  = sum of the wallet's BUY amount_usd of that token over the window
  proceeds  = sum of the wallet's SELL amount_usd of that token over the window
  per-token never-sold -> proceeds 0 (conservative: leftover written off).
Capital-weighted portfolio_mult = sum(proceeds)/sum(cost). This is APPROXIMATE (own fills,
not the copier's executable price) and is used ONLY to rank/narrow — the precise copier PnL
(Stage 2b) and OOS (Stage 3) are the real gates. NOT an edge claim.

    set -a && . ./.env && set +a && PYTHONPATH=src python3 scripts/stage2_screen.py --slice
    set -a && . ./.env && set +a && PYTHONPATH=src python3 scripts/stage2_screen.py --full
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memebot.data.dune import DuneClient  # noqa: E402

SOL = "So11111111111111111111111111111111111111112"
MCAP_CUTOFF = 50_000.0
SNIPER_EXCLUDE_SEC = 30
SUPPLY = 1e9
MAX_BUYS_PER_TOKEN = 3.0
MIN_COST_USD = 500.0  # ignore dust-only wallets (real capital at risk, not lottery spam)


def build_sql(grad_start, grad_end, month_start, month_end, min_tokens, max_tokens, top_n):
    return f"""
WITH grads AS (
  SELECT basemint AS mint
  FROM pumpswap_solana.pools
  WHERE is_valid_pool = true
    AND created_at >= TIMESTAMP '{grad_start}' AND created_at < TIMESTAMP '{grad_end}'
  GROUP BY basemint
),
curve AS (
  SELECT t.trader_id, t.token_bought_mint_address AS mint, t.block_time,
         (t.amount_usd / nullif(t.token_bought_amount, 0)) * {SUPPLY:.0f} AS mcap,
         min(t.block_time) OVER (PARTITION BY t.token_bought_mint_address) AS launch_at
  FROM dex_solana.trades t
  JOIN grads g ON g.mint = t.token_bought_mint_address
  WHERE t.project = 'pumpdotfun' AND t.token_sold_mint_address = '{SOL}'
    AND t.amount_usd > 0 AND t.token_bought_amount > 0
    AND t.block_month >= DATE '{month_start}' AND t.block_month < DATE '{month_end}'
),
early AS (
  SELECT trader_id, mint FROM curve
  WHERE mcap < {MCAP_CUTOFF:.0f} AND block_time >= launch_at + INTERVAL '{SNIPER_EXCLUDE_SEC}' SECOND
),
cand AS (
  SELECT trader_id FROM early
  GROUP BY trader_id
  HAVING count(DISTINCT mint) >= {min_tokens}
     AND count(DISTINCT mint) <= {max_tokens}
     AND (count(*) * 1.0 / count(DISTINCT mint)) <= {MAX_BUYS_PER_TOKEN}
),
qpairs AS (
  SELECT DISTINCT e.trader_id, e.mint FROM early e JOIN cand c ON c.trader_id = e.trader_id
),
buys AS (   -- restrict to UNIVERSE tokens (join grads) so we don't scan candidates' whole history
  SELECT t.trader_id, t.token_bought_mint_address AS mint, sum(t.amount_usd) AS cost
  FROM dex_solana.trades t
  JOIN cand c  ON c.trader_id = t.trader_id
  JOIN grads g ON g.mint = t.token_bought_mint_address
  WHERE t.amount_usd > 0
    AND t.block_month >= DATE '{month_start}' AND t.block_month < DATE '{month_end}'
  GROUP BY 1, 2
),
sells AS (
  SELECT t.trader_id, t.token_sold_mint_address AS mint, sum(t.amount_usd) AS proceeds
  FROM dex_solana.trades t
  JOIN cand c  ON c.trader_id = t.trader_id
  JOIN grads g ON g.mint = t.token_sold_mint_address
  WHERE t.amount_usd > 0
    AND t.block_month >= DATE '{month_start}' AND t.block_month < DATE '{month_end}'
  GROUP BY 1, 2
),
pos AS (
  SELECT q.trader_id, q.mint,
         coalesce(b.cost, 0) AS cost_usd,
         coalesce(s.proceeds, 0) AS proceeds_usd
  FROM qpairs q
  LEFT JOIN buys b ON b.trader_id = q.trader_id AND b.mint = q.mint
  LEFT JOIN sells s ON s.trader_id = q.trader_id AND s.mint = q.mint
)
SELECT trader_id,
       count(*) AS n_tokens,
       sum(cost_usd) AS cost_usd,
       sum(proceeds_usd) AS proceeds_usd,
       sum(proceeds_usd) / nullif(sum(cost_usd), 0) AS portfolio_mult,
       sum(CASE WHEN proceeds_usd > cost_usd THEN 1 ELSE 0 END) * 1.0 / count(*) AS win_rate,
       sum(greatest(proceeds_usd - cost_usd, 0)) /
         nullif(sum(greatest(cost_usd - proceeds_usd, 0)), 0) AS profit_factor
FROM pos
GROUP BY trader_id
HAVING sum(cost_usd) >= {MIN_COST_USD:.0f}
ORDER BY portfolio_mult DESC
LIMIT {top_n}
""".strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slice", action="store_true")
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()
    if not (args.slice or args.full):
        print("pass --slice or --full", file=sys.stderr)
        return 2

    if args.slice:
        grad_start, grad_end, month_start, month_end = "2026-01-01", "2026-01-08", "2025-12-01", "2026-02-01"
        min_tokens, max_tokens, top_n = 3, 50, 1000
        out = ROOT / "runs" / "stage2_screen_slice.json"
        perf = "medium"
    else:
        grad_start, grad_end, month_start, month_end = "2026-01-01", "2026-04-01", "2025-12-01", "2026-04-01"
        min_tokens, max_tokens, top_n = 15, 300, 500
        out = ROOT / "runs" / "stage2_screen_train.json"
        perf = "medium"

    dune = DuneClient()
    u0 = dune.usage()
    sql = build_sql(grad_start, grad_end, month_start, month_end, min_tokens, max_tokens, top_n)
    print(f"screening: grad[{grad_start},{grad_end}) {min_tokens}<=tok<={max_tokens} "
          f"cost>=${MIN_COST_USD:.0f} -> top {top_n} by portfolio_mult ...", file=sys.stderr)
    res = dune.run_sql(sql, performance=perf)
    u1 = dune.usage()

    rows = res["rows"]
    for r in rows:  # coerce numerics
        for k in ("n_tokens", "cost_usd", "proceeds_usd", "portfolio_mult", "win_rate", "profit_factor"):
            r[k] = float(r[k]) if r[k] is not None else None
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"params": {"grad_start": grad_start, "grad_end": grad_end,
                   "min_tokens": min_tokens, "max_tokens": max_tokens, "top_n": top_n}, "cohort": rows}, indent=2))

    mults = [r["portfolio_mult"] for r in rows if r["portfolio_mult"] is not None]
    print("=" * 84)
    print(f"  STAGE 2a in-sample PnL screen ({'slice' if args.slice else 'full train'})")
    print("=" * 84)
    print(f"  ranked wallets returned: {len(rows)} (top {top_n} by capital-weighted return)")
    if mults:
        qs = statistics.quantiles(mults, n=10) if len(mults) > 10 else mults
        print(f"  portfolio_mult: max={max(mults):.2f}x  p90={qs[-1]:.2f}x  median={statistics.median(mults):.2f}x  min(returned)={min(mults):.2f}x")
        print(f"  wallets with portfolio_mult > 1.5x: {sum(1 for m in mults if m > 1.5)}")
        print(f"  wallets with portfolio_mult > 2.0x: {sum(1 for m in mults if m > 2.0)}")
    print("  top 10 by portfolio_mult (n_tokens, cost$, mult, win%, PF):")
    for r in rows[:10]:
        pf = r["profit_factor"]
        print(f"    {r['trader_id']}  n={int(r['n_tokens']):>3}  ${r['cost_usd']:>10,.0f}  "
              f"{r['portfolio_mult']:>6.2f}x  win={r['win_rate']*100:>4.0f}%  PF={pf if pf is None else round(pf,2)}")
    print(f"  result datapoints: {res['datapoints']}   saved -> {out.name}")
    print(f"  CREDIT COST: {u0['credits_used']:.2f} -> {u1['credits_used']:.2f} (delta {u1['credits_used']-u0['credits_used']:.2f}; lags ~1 query)")
    print("=" * 84)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
