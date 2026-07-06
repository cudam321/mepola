#!/usr/bin/env python3
"""Stage 4 — POWER-LAW re-test of copy-discovered-wallets (the user's correction).

The Stage 3 NO-GO used a 16h window + tail-clipping exits, which TRUNCATES the power-law tail
(the only source of +EV). This re-tests the SAME frozen cohort over a 30-DAY horizon with a
survival-first moonbag exit (ladder principal out at 2x, then ride uncapped with a wide trail +
long time-stop). Reports the MFE (max-multiple) distribution first — does a harvestable fat tail
even exist for copyable signals? — then the EV/CI/PF gate with the full tail included.

    set -a && . ./.env && set +a && PYTHONPATH=src python3 scripts/stage4_powerlaw.py
"""

from __future__ import annotations

import json
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
from memebot.analysis.exit_sim import ExitPolicy, simulate_exit  # noqa: E402
from stage3_oos import cohort_buys_sql, parse_ts, boot_lo, pf, ev, drop_top_trade, drop_top_token  # noqa: E402

HOLD_DAYS = 30
LATENCY_S = 3.0
TRADE_CAP = 1500
WINDOWS = {"TRAIN": ("2026-01-01", "2026-04-01"), "OOS": ("2026-04-01", "2026-06-01")}

# Survival-first power-law moonbag: recover principal at 2x (sell 50%), then ride uncapped with a
# wide 60% trailing give-back armed at 2x and a 14-day time-stop. No hard stop (accept -100% base case).
P_MOON = ExitPolicy("P_moonbag_30d", tp_ladder=[(2.0, 0.5)], stop_mult=0.0,
                    trail_pct=0.60, trail_arm_mult=2.0, time_stop_h=24 * 14)
# Pure uncapped buy-and-hold to the end of the 30d window (the raw tail).
P_HOLD = ExitPolicy("P_hold_30d", tp_ladder=[], stop_mult=0.0, trail_pct=1.0,
                    trail_arm_mult=float("inf"), time_stop_h=1e9)
POLS = [P_MOON, P_HOLD]


def entry_fill(series, t, mode):
    prior = [c for c in series.candles if c.ts <= t]
    c0 = prior[-1] if prior else (series.candles[0] if series.candles else None)
    if c0 is None:
        return None
    px = c0.high * 1.015 if mode == "worst" else c0.close * 1.005
    return px if px and px > 0 else None


def mfe(series, t, fill):
    fwd = [c.high for c in series.candles if c.ts >= t]
    return (max(fwd) / fill) if (fwd and fill) else 0.0


def pct(a, q):
    return float(np.percentile(a, q)) if len(a) else float("nan")


def evaluate(rows, client, mode):
    out = {p.name: ([], []) for p in POLS}
    mfes, n_loss = [], 0
    for i, (mint, ts) in enumerate(rows):
        if i % 100 == 0:
            print(f"\r    {i}/{len(rows)} (cache {client.hits}h/{client.misses}m)", end="", file=sys.stderr)
        t_fill = ts + timedelta(seconds=LATENCY_S)
        try:
            series = client.get_price_series(mint, ts - timedelta(hours=1), ts + timedelta(days=HOLD_DAYS))
        except Exception:
            series = None
        fill = entry_fill(series, t_fill, mode) if (series is not None and not series.empty) else None
        if fill is None:
            for p in POLS:
                out[p.name][0].append(0.0); out[p.name][1].append((mint, 0.0))
            mfes.append(0.0); n_loss += 1
            continue
        mfes.append(mfe(series, t_fill, fill))
        for p in POLS:
            m = simulate_exit(series, fill, t_fill, p)
            out[p.name][0].append(m); out[p.name][1].append((mint, m))
    print("\r" + " " * 50 + "\r", end="", file=sys.stderr)
    return out, mfes, n_loss


def main() -> int:
    cohort = [r["trader_id"] for r in json.load(open(ROOT / "runs" / "stage2_screen_train.json"))["cohort"]][:100]
    dune = DuneClient()
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_30d"))
    print("=" * 96)
    print(f"  STAGE 4 — POWER-LAW re-test | top-100 cohort | {HOLD_DAYS}d horizon | survival-first moonbag")
    print("=" * 96)

    for wname, (ws, we) in WINDOWS.items():
        lm = "2025-12-01" if wname == "TRAIN" else "2026-03-01"
        rows = dune.run_sql(cohort_buys_sql(cohort, ws, we, lm))["rows"]
        sig = [(r["mint"], parse_ts(r["block_time"])) for r in rows]
        n_total = len(sig)
        if n_total > TRADE_CAP:
            rng = np.random.default_rng(0)
            sig = [sig[i] for i in rng.choice(n_total, TRADE_CAP, replace=False)]
        for mode in ("mid", "worst"):
            by_pol, mfes, n_loss = evaluate(sig, client, mode)
            m = np.asarray(mfes, dtype=float)
            print(f"\n  [{wname} | fill={mode}] signals={n_total} priced={len(sig)-n_loss} dead={n_loss}")
            print(f"   MFE (max multiple over {HOLD_DAYS}d): median={pct(m,50):.2f}x  p90={pct(m,90):.2f}x  "
                  f"p99={pct(m,99):.2f}x  max={m.max():.1f}x | frac>=2x:{(m>=2).mean()*100:.0f}% "
                  f">=10x:{(m>=10).mean()*100:.1f}% >=50x:{(m>=50).mean()*100:.2f}%")
            for p in POLS:
                mults, rm = by_pol[p.name]
                a = np.asarray(mults, dtype=float)
                d1t, _ = drop_top_token(rm)
                print(f"   {p.name:16} EV={ev(mults)*100:>+7.1f}%  CIlo={boot_lo(mults)*100:>+7.1f}%  "
                      f"PF={pf(mults):>5.2f}  drop1trade={drop_top_trade(mults)*100:>+7.1f}%  "
                      f"drop1tok={d1t*100:>+7.1f}%  win={(a>1).mean()*100:>3.0f}%")
    print("\n" + "=" * 96)
    print("  Read: if max/p99 MFE shows a real fat tail (>=50x) AND an uncapped policy has CIlo>0 + PF>=1.3")
    print("  surviving drop-top, the power-law thesis is alive. If the tail is thin or EV stays negative even")
    print("  uncapped, the 16h window was NOT the reason for the NO-GO.")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
