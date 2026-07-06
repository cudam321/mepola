#!/usr/bin/env python3
"""Stage 3 — the DECISIVE out-of-sample copier test (the whole pivot lives here).

Freeze the top-N in-sample cohort (from stage2_screen_train.json). For a given window, pull
their qualifying EARLY buys from Dune (the copy signals: sub-$50k mcap, >30s after token launch),
then price what a COPIER would actually realize (buy at signal_time + detection latency, full
cost stack, managed exit) using the existing Jupiter cache + exit engine. Aggregate to EV / PF /
bootstrap-CI-lower / tail-robustness.

Run on BOTH windows: TRAIN (in-sample sanity — should look good, it was selected) and OOS (the
verdict). The edge claim is ONLY the OOS number. GO iff an EXECUTABLE policy clears, on OOS:
CI_low > 0 AND PF >= 1.3 AND survives dropping the single best trade AND best token.

    set -a && . ./.env && set +a && PYTHONPATH=src python3 scripts/stage3_oos.py --cohort 100
"""

from __future__ import annotations

import argparse
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

SOL = "So11111111111111111111111111111111111111112"
MCAP_CUTOFF = 50_000.0
SNIPER_EXCLUDE_SEC = 30
SUPPLY = 1e9
LATENCY_S = 3.0
ENTRY_COST = 0.015
FWD_H = 16
EXECUTABLE = {"P2_principal_out_trail", "P3_ladder_2_3_5", "P5_aggressive_derisk", "P5b_tight_stop_moonbag"}
TRADE_CAP = 1500  # cap priced trades/window to bound Jupiter fetch time (random-sampled, logged)
WINDOWS = {"TRAIN": ("2026-01-01", "2026-04-01"), "OOS": ("2026-04-01", "2026-06-01")}


def cohort_buys_sql(trader_ids, win_start, win_end, launch_month_start):
    values = ",".join(f"('{w}')" for w in trader_ids)
    return f"""
WITH cohort(trader_id) AS (VALUES {values}),
cb AS (
  SELECT t.trader_id, t.token_bought_mint_address AS mint, t.block_time,
         (t.amount_usd / nullif(t.token_bought_amount,0)) * {SUPPLY:.0f} AS mcap
  FROM dex_solana.trades t JOIN cohort c ON c.trader_id = t.trader_id
  WHERE t.project='pumpdotfun' AND t.token_sold_mint_address='{SOL}'
    AND t.amount_usd>0 AND t.token_bought_amount>0
    AND t.block_month >= DATE '{win_start}' AND t.block_month < DATE '{win_end}'
    AND t.block_time >= TIMESTAMP '{win_start}' AND t.block_time < TIMESTAMP '{win_end}'
),
launch AS (
  SELECT t.token_bought_mint_address AS mint, min(t.block_time) AS launch_at
  FROM dex_solana.trades t
  JOIN (SELECT DISTINCT mint FROM cb) m ON m.mint = t.token_bought_mint_address
  WHERE t.project='pumpdotfun'
    AND t.block_month >= DATE '{launch_month_start}' AND t.block_month < DATE '{win_end}'
  GROUP BY 1
)
SELECT b.trader_id, b.mint, b.block_time
FROM cb b JOIN launch l ON l.mint = b.mint
WHERE b.mcap < {MCAP_CUTOFF:.0f} AND b.block_time >= l.launch_at + INTERVAL '{SNIPER_EXCLUDE_SEC}' SECOND
ORDER BY b.block_time
""".strip()


def worst_fill(series, t):
    hi = max((c.high for c in series.candles if t <= c.ts <= t + timedelta(seconds=60)), default=None)
    if hi is None:
        prior = [c for c in series.candles if c.ts <= t]
        hi = prior[-1].high if prior else None
    return hi * (1 + ENTRY_COST) if hi and hi > 0 else None


def mid_fill(series, t):
    """Neutral/generous fill: price AT signal+latency (last close <= t), small slippage only.
    Sensitivity control to rule out that the NO-GO is an artifact of the pessimistic worst_fill."""
    prior = [c for c in series.candles if c.ts <= t]
    px = prior[-1].close if prior else None
    if px is None:  # before first candle -> use first open
        px = series.candles[0].open if series.candles else None
    return px * 1.005 if px and px > 0 else None


FILLS = {"worst": worst_fill, "mid": mid_fill}


def parse_ts(s):
    from datetime import datetime, timezone
    s = s.replace("Z", "").replace(" UTC", "").strip().replace(" ", "T")
    if "." in s:
        s = s.split(".")[0]
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def boot_lo(m, n=5000, seed=0):
    a = np.asarray(m, dtype=float)
    if len(a) < 2:
        return float("nan")
    rng = np.random.default_rng(seed)
    return float(np.percentile(a[rng.integers(0, len(a), size=(n, len(a)))].mean(axis=1) - 1.0, 2.5))


def ev(m):
    a = np.asarray(m, dtype=float)
    return float(a.mean() - 1.0) if len(a) else float("nan")


def pf(m):
    a = np.asarray(m, dtype=float)
    g = np.maximum(a - 1.0, 0).sum()
    l = np.maximum(1.0 - a, 0).sum()
    return float(g / l) if l > 0 else float("inf")


def drop_top_trade(m):
    a = np.sort(np.asarray(m, dtype=float))[::-1]
    return ev(a[1:]) if len(a) > 1 else float("nan")


def drop_top_token(rows_mults):
    # rows_mults: list of (mint, mult). Drop the mint with the largest summed excess, recompute EV.
    by = {}
    for mint, mult in rows_mults:
        by.setdefault(mint, []).append(mult)
    worst = max(by, key=lambda k: sum(max(x - 1, 0) for x in by[k]))
    kept = [mult for mint, mult in rows_mults if mint != worst]
    return ev(kept), worst


def evaluate_all(rows, client, fill_fn):
    """rows: list[(mint, ts)]. Price each signal ONCE, run all policies. Unpriceable = total loss (0).
    Returns ({polname: (mults, [(mint,mult)])}, n_priced, n_loss)."""
    out = {p.name: ([], []) for p in POLICIES}
    n_priced = n_loss = 0
    for i, (mint, ts) in enumerate(rows):
        if i % 100 == 0:
            print(f"\r    pricing {i}/{len(rows)}  (cache {client.hits}h/{client.misses}m)", end="", file=sys.stderr)
        t_fill = ts + timedelta(seconds=LATENCY_S)
        try:
            series = client.get_price_series(mint, ts - timedelta(minutes=15), ts + timedelta(hours=FWD_H))
        except Exception:
            series = None
        wf = fill_fn(series, t_fill) if (series is not None and not series.empty) else None
        if wf is None:  # bought a token with no liquidity/chart -> copier total loss
            for p in POLICIES:
                out[p.name][0].append(0.0); out[p.name][1].append((mint, 0.0))
            n_loss += 1
            continue
        for p in POLICIES:
            m = simulate_exit(series, wf, t_fill, p)
            out[p.name][0].append(m); out[p.name][1].append((mint, m))
        n_priced += 1
    print("\r" + " " * 60 + "\r", end="", file=sys.stderr)
    return out, n_priced, n_loss


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cohort", type=int, default=100, help="top-N wallets to freeze as the cohort")
    ap.add_argument("--count-only", action="store_true", help="just pull copy-signal counts (no pricing)")
    ap.add_argument("--fill", choices=list(FILLS), default="worst", help="copier fill convention")
    args = ap.parse_args()
    fill_fn = FILLS[args.fill]

    cohort = [r["trader_id"] for r in json.load(open(ROOT / "runs" / "stage2_screen_train.json"))["cohort"]][:args.cohort]
    print(f"frozen cohort: top {len(cohort)} in-sample wallets", file=sys.stderr)
    dune = DuneClient()

    sigs = {}
    for wname, (ws, we) in WINDOWS.items():
        lm = "2025-12-01" if wname == "TRAIN" else "2026-03-01"
        rows = dune.run_sql(cohort_buys_sql(cohort, ws, we, lm))["rows"]
        sig = [(r["mint"], parse_ts(r["block_time"])) for r in rows]
        n_active = len({r["trader_id"] for r in rows})
        n_mints = len({r["mint"] for r in rows})
        print(f"  [{wname}] {len(sig)} copy-signals | {n_active}/{len(cohort)} cohort wallets active | {n_mints} unique mints",
              file=sys.stderr)
        sigs[wname] = sig
    if args.count_only:
        return 0

    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_onchain"))
    print("=" * 92)
    print(f"  STAGE 3 — OOS COPIER TEST | frozen cohort = top {len(cohort)} in-sample wallets")
    print("  copier = buy at signal + 3s latency, full costs, managed exit; edge claim = OOS only")
    print("=" * 92)

    results = {}
    for wname in WINDOWS:
        sig, n_total = sigs[wname], len(sigs[wname])
        if n_total > TRADE_CAP:
            rng = np.random.default_rng(0)
            sig = [sig[i] for i in rng.choice(n_total, TRADE_CAP, replace=False)]
            print(f"  [{wname}] sampled {TRADE_CAP}/{n_total} signals for pricing (logged)", file=sys.stderr)
        by_policy, npx, nloss = evaluate_all(sig, client, fill_fn)
        results[wname] = {"n_total": n_total, "by_policy": by_policy, "n_priced": npx, "n_loss": nloss}

    for wname in WINDOWS:
        r = results[wname]
        print(f"\n  [{wname}]  copy-signals={r['n_total']}  priced={r.get('n_priced')}  "
              f"unpriceable(total-loss)={r.get('n_loss')}")
        print(f"  {'policy':24} | {'EV':>8} | {'CIlo':>8} | {'PF':>7} | {'drop1trade':>10} | {'drop1token':>10} | {'win%':>5} | exec")
        print("  " + "-" * 96)
        for pol in POLICIES:
            mults, rm = r["by_policy"][pol.name]
            a = np.asarray(mults, dtype=float)
            d1, _ = drop_top_token(rm)
            print(f"  {pol.name:24} | {ev(mults)*100:>+6.1f}% | {boot_lo(mults)*100:>+6.1f}% | "
                  f"{pf(mults):>7.2f} | {drop_top_trade(mults)*100:>+8.1f}% | {d1*100:>+8.1f}% | "
                  f"{(a>1).mean()*100:>4.0f}% | {'yes' if pol.name in EXECUTABLE else 'NO'}")

    # verdict on OOS executable policies
    oos = results["OOS"]["by_policy"]
    print("\n" + "=" * 92)
    passing = []
    for pol in POLICIES:
        if pol.name not in EXECUTABLE:
            continue
        mults, rm = oos[pol.name]
        d1t, _ = drop_top_token(rm)
        if boot_lo(mults) > 0 and pf(mults) >= 1.3 and drop_top_trade(mults) > 0 and d1t > 0:
            passing.append(pol.name)
    if passing:
        print(f"  VERDICT: OOS edge CLEARS the bar (executable: {', '.join(passing)}). The top in-sample")
        print("  cohort PERSISTS out-of-sample for a copier -> proceed to Stage 4 (latency sweep + decay).")
    else:
        print("  VERDICT: NO executable policy clears the OOS bar (CI_low>0, PF>=1.3, survives top-1 trade+token).")
        print("  The top in-sample cohort does NOT persist for a copier OOS -> third falsification; the")
        print("  on-chain copy edge is in-sample/selection luck, not durable skill. Do NOT build copy infra.")
    print("=" * 92)

    out = ROOT / "runs" / "stage3_oos_result.json"
    out.write_text(json.dumps({w: {"n_total": results[w]["n_total"], "n_priced": results[w].get("n_priced"),
        "n_loss": results[w].get("n_loss"),
        "by_policy": {p: {"ev": ev(results[w]["by_policy"][p][0]), "ci_lo": boot_lo(results[w]["by_policy"][p][0]),
                          "pf": pf(results[w]["by_policy"][p][0]), "n": len(results[w]["by_policy"][p][0])}
                      for p in oos}} for w in WINDOWS}, indent=2))
    print(f"  saved -> {out.name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
