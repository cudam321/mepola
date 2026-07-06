#!/usr/bin/env python3
"""Stage 5 — TOKEN-FEATURE tail prediction (the last legit power-law play: pick tokens, not wallets).

Hypothesis (arXiv 2602.14860: early trading intensity is the strongest success predictor): tokens with
high first-5-min intensity (trades / unique buyers / volume) are more likely to tail; buying them at
minute 5 (an information decision, not slot-0) + an uncapped moonbag is +EV.

Survivorship-free: the denominator = ALL tokens passing the intensity filter (incl. those that pump then
die). Entry priced at true minute-5 resolution (the honest "you enter after the early pump" cost). Tests
(1) does intensity predict MFE? (2) EV by intensity quantile + train->OOS gate.

    set -a && . ./.env && set +a && PYTHONPATH=src python3 scripts/stage5_tokenfeatures.py
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from memebot.models import PriceSeries  # noqa: E402
from memebot.data.dune import DuneClient  # noqa: E402
from memebot.data.cache import CachedPriceClient  # noqa: E402
from memebot.data.jupiter import JupiterChartsClient  # noqa: E402
from memebot.analysis.exit_sim import simulate_exit  # noqa: E402
from stage3_oos import parse_ts, boot_lo, pf, ev, drop_top_trade  # noqa: E402
from stage4_powerlaw import P_MOON, P_HOLD, HOLD_DAYS  # noqa: E402

SOL = "So11111111111111111111111111111111111111112"
MIN_TRADES_5M = 30      # intensity filter: a token must show real early activity to be a candidate
ENTRY_MIN = 5           # buy at launch + 5 minutes (after observing the feature)
MIN_FETCH_H = 12        # minute resolution window (must be < 16h for datapi minute candles)
CAP = 1200              # cap tokens priced per window
WINDOWS = {"TRAIN": ("2026-01-01", "2026-04-01", "2025-12-01"), "OOS": ("2026-04-01", "2026-06-01", "2026-03-01")}


def features_sql(ws, we, month_start):
    return f"""
WITH launches AS (
  SELECT account_mint AS mint, min(call_block_time) AS launch
  FROM pumpdotfun_solana.pump_call_create
  WHERE call_block_date >= DATE '{ws}' AND call_block_date < DATE '{we}' AND account_mint IS NOT NULL
  GROUP BY account_mint
),
early AS (
  SELECT l.mint, l.launch,
         count(*) AS n_trades,
         count(DISTINCT t.trader_id) AS n_traders,
         sum(t.amount_usd) AS vol_usd
  FROM launches l
  JOIN dex_solana.trades t
    ON t.token_bought_mint_address = l.mint AND t.project = 'pumpdotfun'
   AND t.token_sold_mint_address = '{SOL}'
   AND t.block_time >= l.launch AND t.block_time < l.launch + INTERVAL '{ENTRY_MIN}' MINUTE
   AND t.block_month >= DATE '{month_start}' AND t.block_month < DATE '{we}'
  GROUP BY l.mint, l.launch
)
SELECT mint, launch, n_trades, n_traders, vol_usd
FROM early WHERE n_trades >= {MIN_TRADES_5M}
ORDER BY n_traders DESC
""".strip()


def merged_series(client, mint, launch):
    mn = client.get_price_series(mint, launch - timedelta(minutes=5), launch + timedelta(hours=MIN_FETCH_H))
    hr = client.get_price_series(mint, launch + timedelta(hours=MIN_FETCH_H), launch + timedelta(days=HOLD_DAYS))
    boundary = mn.candles[-1].ts if mn.candles else launch
    candles = list(mn.candles) + [c for c in hr.candles if c.ts > boundary]
    candles.sort(key=lambda c: c.ts)
    return PriceSeries(mint=mint, pool=None, timeframe="mixed", aggregate=1, candles=candles)


def fill_at(series, t, mode):
    win = [c for c in series.candles if t <= c.ts <= t + timedelta(minutes=2)]
    if win:
        px = max(c.high for c in win) * 1.015 if mode == "worst" else win[0].close * 1.005
    else:
        prior = [c for c in series.candles if c.ts <= t]
        if not prior:
            return None
        px = prior[-1].close * 1.005
    return px if px and px > 0 else None


def price_tokens(rows, client, mode="mid"):
    """Return list of (n_traders, vol_usd, mfe, hold_mult, moon_mult)."""
    out = []
    for i, r in enumerate(rows):
        if i % 100 == 0:
            print(f"\r    {i}/{len(rows)} (cache {client.hits}h/{client.misses}m)", end="", file=sys.stderr)
        launch = parse_ts(r["launch"]); t = launch + timedelta(minutes=ENTRY_MIN)
        try:
            s = merged_series(client, r["mint"], launch)
        except Exception:
            s = None
        fill = fill_at(s, t, mode) if (s and s.candles) else None
        if fill is None:
            out.append((float(r["n_traders"]), float(r["vol_usd"] or 0), 0.0, 0.0, 0.0)); continue
        fwd = [c.high for c in s.candles if c.ts >= t]
        mfe = (max(fwd) / fill) if fwd else 0.0
        out.append((float(r["n_traders"]), float(r["vol_usd"] or 0), mfe,
                    simulate_exit(s, fill, t, P_HOLD), simulate_exit(s, fill, t, P_MOON)))
    print("\r" + " " * 50 + "\r", end="", file=sys.stderr)
    return out


def gate(name, moons):
    a = np.asarray(moons, dtype=float)
    print(f"   {name:16} EV={ev(moons)*100:>+7.1f}%  CIlo={boot_lo(moons)*100:>+7.1f}%  PF={pf(moons):>5.2f}  "
          f"drop1trade={drop_top_trade(moons)*100:>+7.1f}%  win={(a>1).mean()*100:>3.0f}%  n={len(moons)}")


def main() -> int:
    dune = DuneClient()
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_tokfeat"))
    print("=" * 96)
    print(f"  STAGE 5 — TOKEN-FEATURE tails | intensity>= {MIN_TRADES_5M} trades/5min | buy@+{ENTRY_MIN}min | {HOLD_DAYS}d moonbag")
    print("=" * 96)
    for wname, (ws, we, ms) in WINDOWS.items():
        rows = dune.run_sql(features_sql(ws, we, ms))["rows"]
        n_all = len(rows)
        if n_all > CAP:
            rng = np.random.default_rng(0)
            rows = [rows[i] for i in sorted(rng.choice(n_all, CAP, replace=False))]
        data = price_tokens(rows, client, "mid")
        nt = np.array([d[0] for d in data]); mfe = np.array([d[2] for d in data])
        moon = [d[4] for d in data]; hold = [d[3] for d in data]
        print(f"\n  [{wname}] intensity-candidates={n_all} priced={len(data)}")
        # does intensity predict the tail?
        if len(nt) > 10 and nt.std() > 0:
            corr = float(np.corrcoef(np.log1p(nt), np.log1p(mfe))[0, 1])
            print(f"   corr(log n_traders, log MFE) = {corr:+.3f}   MFE: median={np.median(mfe):.2f}x p99={np.percentile(mfe,99):.1f}x max={mfe.max():.0f}x")
        # EV by intensity quintile (moonbag)
        order = np.argsort(nt)
        qs = np.array_split(order, 5)
        print("   moonbag EV by n_traders quintile (low->high intensity):")
        for qi, idx in enumerate(qs):
            mm = [moon[j] for j in idx]
            print(f"     Q{qi+1} (n_traders {nt[idx].min():.0f}-{nt[idx].max():.0f}): EV={ev(mm)*100:>+6.1f}%  win={np.mean([1 for j in idx if moon[j]>1])/max(len(idx),1)*100:>3.0f}%  n={len(mm)}")
        print("   overall:")
        gate("P_moonbag_30d", moon); gate("P_hold_30d", hold)
    print("\n" + "=" * 96)
    print("  Read: if corr>0 AND the top intensity quintile clears CIlo>0 + PF>=1.3, token-feature selection")
    print("  finds the tail where wallet-copy could not. If top quintile is still -EV, the power-law tail is")
    print("  not predictable from launch features either -> the whole power-law-on-memecoins thesis is settled.")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
